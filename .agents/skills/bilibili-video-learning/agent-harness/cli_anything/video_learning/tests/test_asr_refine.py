"""
定向二次解码的离线测试（对应 docs/DECISIONS.md D27）。

核心不变量：**宁可拒绝，不可引入新错**。所以这里既要测"该替换时替换了"，
也要测"四种不该替换的情形都被拒绝、且理由被记录"——尤其是"换来一段不相关内容"这种幻觉形态。
运行示例：python -m pytest cli_anything/video_learning/tests/test_asr_refine.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill。
import json  # 构造诊断断言。
import sys  # 把 scripts 目录加入模块搜索路径。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 伪造 faster-whisper 的分段对象。

import pytest  # 提供 monkeypatch 与断言辅助。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import asr_refine  # noqa: E402 被测模块。
import speech_to_text  # noqa: E402 接入点。


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"
    module_name = f"video_learning_test_{name}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 造一个带置信度/压缩比的 ASR 分段 ---
def segment(start: float, end: float, content: str, confidence: float = -0.2,
            compression_ratio: float = 1.2) -> dict:
    return {"from": start, "to": end, "content": content, "confidence": confidence,
            "compression_ratio": compression_ratio, "no_speech_probability": 0.01}


# ============================ 可疑区间识别 ============================

# --- 三种可疑形态都要被抓到，正常段落不能被抓 ---
def test_suspicious_spans_cover_all_three_shapes():
    segments = [
        segment(0.0, 5.0, "正常段落", confidence=-0.2),
        segment(5.0, 10.0, "低置信段落", confidence=-1.5),
        segment(10.0, 15.0, "复读段落", confidence=-0.3, compression_ratio=3.1),
        segment(15.0, 25.0, "超长且没有分数的段落", confidence=None, compression_ratio=None),
        segment(25.0, 25.5, "太短", confidence=-2.0),
    ]

    reasons = {tuple(span["reasons"]) for span in asr_refine.suspicious_spans(segments)}

    assert ("low_logprob",) in reasons
    assert ("high_compression_ratio",) in reasons
    assert ("long_unscored_segment",) in reasons
    assert all(span["start"] != 25.0 for span in asr_refine.suspicious_spans(segments)), "过短区间不重解"
    assert all(span["start"] != 0.0 for span in asr_refine.suspicious_spans(segments)), "正常段落不重解"


# --- 重解预算必须被尊重，且复读/幻觉优先于低置信 ---
def test_suspicious_spans_respect_budget_and_priority():
    segments = [segment(i * 5.0, i * 5.0 + 5.0, "低置信", confidence=-1.6) for i in range(4)]
    segments.append(segment(20.0, 25.0, "复读", confidence=-0.3, compression_ratio=4.0))

    spans = asr_refine.suspicious_spans(segments, asr_refine.RefineSettings(max_spans=2))

    assert len(spans) == 2
    assert spans[0]["reasons"] == ["high_compression_ratio"], "复读/幻觉优先重解"


# ============================ 判据 ============================

# --- 置信度必须是字符加权：长段落更有代表性 ---
def test_weighted_confidence_is_character_weighted():
    segments = [segment(0.0, 5.0, "短", confidence=-1.0), segment(5.0, 10.0, "很长的一段正文", confidence=-0.1)]

    assert asr_refine.weighted_confidence(segments) == pytest.approx((-1.0 * 1 + -0.1 * 7) / 8, abs=1e-3)
    assert asr_refine.weighted_confidence([segment(0.0, 1.0, "无分数", confidence=None)]) is None


# --- 字符重合率：完全无关的文本必须低分（这是幻觉防线）---
def test_char_overlap_ratio_separates_related_from_unrelated():
    assert asr_refine.char_overlap_ratio("中国男性是最好的血包", "中国男性是最好的血包") == 1.0
    assert asr_refine.char_overlap_ratio("中国男性是最好的血包", "今天天气不错适合出门") < 0.2


# --- 四条判据：接受一种、拒绝四种，且理由都要具体 ---
def test_judge_replacement_accepts_and_rejects_explicitly():
    settings = asr_refine.RefineSettings()
    span = {"start": 0.0, "end": 5.0, "reasons": ["low_logprob"]}
    before = [segment(0.0, 5.0, "中国男性是最好的雪包", confidence=-1.4)]

    accepted = asr_refine.judge_replacement(before, [segment(0.0, 5.0, "中国男性是最好的血包", confidence=-0.2)],
                                            span, settings)
    assert accepted["accepted"] is True and accepted["reason"] == "confidence_gain"

    assert asr_refine.judge_replacement(before, [], span, settings)["reason"] == "empty_result"
    assert asr_refine.judge_replacement(
        before, [segment(0.0, 1.0, "中国男性是最好的血包", confidence=-0.1)], span, settings
    )["reason"] == "coverage_shrank"
    assert asr_refine.judge_replacement(
        before, [segment(0.0, 5.0, "今天天气不错适合出门散步", confidence=-0.1)], span, settings
    )["reason"] == "text_diverged"
    assert asr_refine.judge_replacement(
        before, [segment(0.0, 5.0, "中国男性是最好的雪包", confidence=-1.4)], span, settings
    )["reason"] == "confidence_not_improved"


# ============================ 主流程 ============================

# --- 被接受的区间要真的被替换，并留下可复核的理由 ---
def test_apply_refinement_replaces_accepted_span():
    segments = [segment(0.0, 5.0, "中国男性是最好的雪包", confidence=-1.5),
                segment(5.0, 10.0, "正常段落", confidence=-0.2)]
    diagnostics: list = []

    def refine_window(start, end):
        return [segment(start, end, "中国男性是最好的血包", confidence=-0.1)]

    report = asr_refine.apply_refinement(segments, refine_window, asr_refine.RefineSettings(), diagnostics)

    assert [item["content"] for item in report["segments"]] == ["中国男性是最好的血包", "正常段落"]
    assert report["report"]["accepted"] == 1 and report["report"]["rejected"] == 0
    decision = report["report"]["decisions"][0]
    assert decision["reason"] == "confidence_gain" and decision["before"] and decision["after"]
    assert diagnostics[0]["step"] == "asr_refine" and diagnostics[0]["report"]["accepted"] == 1


# --- 被拒绝的区间必须逐字保持原样（宁可保留错误，也不引入新错）---
def test_apply_refinement_keeps_rejected_span_untouched():
    segments = [segment(0.0, 5.0, "中国男性是最好的雪包", confidence=-1.5)]

    def refine_window(start, end):
        return [segment(start, end, "完全不相干的内容在这里", confidence=-0.1)]

    report = asr_refine.apply_refinement(segments, refine_window, asr_refine.RefineSettings())

    assert [item["content"] for item in report["segments"]] == ["中国男性是最好的雪包"]
    assert report["report"]["accepted"] == 0
    assert report["report"]["decisions"][0]["reason"] == "text_diverged"


# --- 单个窗口失败不能拖垮其它窗口 ---
def test_apply_refinement_survives_window_failure():
    segments = [segment(0.0, 5.0, "第一个可疑段落", confidence=-1.6),
                segment(5.0, 10.0, "第二个可疑段落", confidence=-1.6)]
    calls = {"count": 0}

    def refine_window(start, end):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("ffmpeg 切片失败")
        return [segment(start, end, "第二个可疑段落", confidence=-0.1)]

    report = asr_refine.apply_refinement(segments, refine_window, asr_refine.RefineSettings())

    reasons = [decision["reason"] for decision in report["report"]["decisions"]]
    assert any(reason.startswith("window_failed") for reason in reasons)
    assert len(report["segments"]) >= 1  # 仍然返回可用结果


# --- 没有可疑区间时不做任何改动，但仍要报告"检查过" ---
def test_apply_refinement_without_suspicious_spans_is_a_noop():
    segments = [segment(0.0, 5.0, "正常段落", confidence=-0.1)]

    report = asr_refine.apply_refinement(segments, lambda start, end: [], asr_refine.RefineSettings())

    assert report["segments"] == segments
    assert report["report"]["checked"] is True and report["report"]["suspicious"] == 0


# ============================ 与转写入口的接入 ============================

# --- quality 档位必须真的触发重解，并把报告写进结果 ---
def test_quality_profile_triggers_refinement_end_to_end(monkeypatch, tmp_path):
    suspicious = SimpleNamespace(text="中国男性是最好的雪包", start=0.0, end=5.0, avg_logprob=-1.6,
                                 no_speech_prob=0.02, compression_ratio=1.1, words=[])
    improved = SimpleNamespace(text="中国男性是最好的血包", start=0.0, end=5.0, avg_logprob=-0.1,
                               no_speech_prob=0.02, compression_ratio=1.1, words=[])
    calls = {"count": 0}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls["count"] += 1
            segment = suspicious if calls["count"] == 1 else improved   # 第一遍可疑，重解后更好
            return iter([segment]), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(speech_to_text.cuda_runtime, "prepare_cuda_libraries",
                        lambda *a, **k: {"found": {}, "preloaded": [], "guidance": None, "searched_directories": [],
                                         "errors": [], "path_updated": False, "platform": "posix"})
    monkeypatch.setattr(speech_to_text.asr_coverage, "cut_audio_window",
                        lambda audio, start, end, out: Path(out).write_bytes(b"RIFF") or Path(out))
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    settings = speech_to_text.apply_profile(
        speech_to_text.TranscriptionSettings(coverage_check=False), "quality")

    result = speech_to_text.transcribe_audio_file(audio, settings=settings)

    assert result["segments"][0]["content"] == "中国男性是最好的血包"      # 被替换成重解结果
    assert result["refine_report"]["accepted"] == 1
    # 注意：引擎诊断用的是历史键 `engine`，只有 step 类诊断才带 `step`，这里按存在性取值
    assert any(item.get("step") == "asr_refine" for item in result["diagnostics"])
    assert json.dumps(result["refine_report"], ensure_ascii=False)          # 报告可序列化（能落进 JSON 产出）


# --- balanced 档位不应触发重解（默认必须保持最快路径）---
def test_balanced_profile_does_not_refine(monkeypatch, tmp_path):
    calls = {"count": 0}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls["count"] += 1
            segment = SimpleNamespace(text="中国男性是最好的雪包", start=0.0, end=5.0, avg_logprob=-1.6,
                                      no_speech_prob=0.02, compression_ratio=1.1, words=[])
            return iter([segment]), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(speech_to_text.cuda_runtime, "prepare_cuda_libraries",
                        lambda *a, **k: {"found": {}, "preloaded": [], "guidance": None, "searched_directories": [],
                                         "errors": [], "path_updated": False, "platform": "posix"})
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    result = speech_to_text.transcribe_audio_file(
        audio, settings=speech_to_text.apply_profile(speech_to_text.TranscriptionSettings(coverage_check=False), "balanced"))

    assert calls["count"] == 1                          # 只解码一次，没有重解
    assert "refine_report" not in result
