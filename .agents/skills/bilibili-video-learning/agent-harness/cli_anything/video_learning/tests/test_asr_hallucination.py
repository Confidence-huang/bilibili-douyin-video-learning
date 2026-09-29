"""
幻觉门与标注的离线测试（对应 docs/DECISIONS.md D34）。

守住的是"唯一能无中生有的路径"：补转窗口关闭了两道静音阈值，结果必须过门才能进正文；
以及"可疑内容只标注、不改写"——人工能看到它，但文本不被工具擅自篡改。
运行示例：python -m pytest cli_anything/video_learning/tests/test_asr_hallucination.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill
import sys  # 把 scripts 目录加入搜索路径
from pathlib import Path  # 稳定定位 Skill 根目录

import pytest  # 断言辅助


SKILL_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import asr_hallucination  # noqa: E402 被测模块
import speech_to_text  # noqa: E402 接线点


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def segment(content, *, ratio=1.1, confidence=-0.2, start=0.0, end=3.0):
    return {"from": start, "to": end, "content": content, "compression_ratio": ratio, "confidence": confidence}


# ============================ 三道门 ============================

# --- 复读：同一 4 字片段重复 ≥3 次必须被抓到；正常文本不能误报 ---
def test_repeated_ngram_is_detected():
    normal = "中国男性是家庭里最被忽视的那个人"

    assert asr_hallucination.repeated_ngram_reasons(normal) == []
    assert asr_hallucination.repeated_ngram_reasons("谢谢观看谢谢观看谢谢观看") != []


# --- 压缩比异常：只影响该段判定，不牵连正常段 ---
def test_high_compression_ratio_is_suspicious():
    verdict = asr_hallucination.looks_hallucinated([segment("正常的一句话", ratio=9.9)], 3.0)

    assert verdict.suspicious is True
    assert verdict.reasons[0].startswith("compression_ratio")
    assert asr_hallucination.looks_hallucinated([segment("正常的一句话")], 3.0).suspicious is False


# --- 时长-字数比：说得太快（说不完）与几乎没内容都要报 ---
def test_characters_per_second_bounds():
    too_fast = asr_hallucination.looks_hallucinated([segment("字" * 60)], 3.0)
    too_slow = asr_hallucination.looks_hallucinated([segment("嗯")], 10.0)

    assert any(reason.startswith("too_many_chars_per_second") for reason in too_fast.reasons)
    assert any(reason.startswith("too_few_chars_per_second") for reason in too_slow.reasons)
    assert too_fast.chars_per_second == pytest.approx(20.0, abs=0.1)


# --- 没有窗口时长时跳过该门，其余门仍然生效 ---
def test_without_window_duration_only_other_gates_apply():
    verdict = asr_hallucination.looks_hallucinated([segment("正常的一句话", ratio=9.9)])

    assert verdict.suspicious is True and verdict.chars_per_second is None


# ============================ 标注（不改文本） ============================

# --- 只标注可疑段，且不改动任何文本 ---
def test_flag_segments_marks_without_rewriting():
    segments = [segment("正常段"), segment("重复重复重复", ratio=5.0)]

    flagged = asr_hallucination.flag_segments(segments)

    assert len(flagged) == 1 and flagged[0]["index"] == 1
    assert segments[1]["content"] == "重复重复重复"          # 输入未被改动


# ============================ 与转写入口的接线 ============================

# --- _finalize 必须把可疑段暴露出来，并在诊断里说明 ---
def test_finalize_exposes_suspected_hallucinations():
    result = {"segments": [segment("正常段"), segment("复读复读复读", ratio=6.0)],
              "text": "", "duration": 0, "diagnostics": []}
    guard = {"segments": result["segments"], "report": {"checked": True, "coverage_before": 1.0,
                                                        "coverage_after": 1.0, "audio_duration": 6.0}}

    finalized = speech_to_text._finalize(result, guard, low_confidence_threshold=-1.0)

    assert len(finalized["suspected_hallucinations"]) == 1
    assert any(item.get("step") == "asr_hallucination_marks" for item in finalized["diagnostics"])


# --- 干净音频不能误报（否则标注会失去意义）---
def test_finalize_clean_audio_has_no_suspicions():
    result = {"segments": [segment("这是一句正常的中文口播")], "text": "", "duration": 0, "diagnostics": []}
    guard = {"segments": result["segments"], "report": {"checked": True}}

    finalized = speech_to_text._finalize(result, guard, low_confidence_threshold=-1.0)

    assert finalized["suspected_hallucinations"] == []
    assert not any(item.get("step") == "asr_hallucination_marks" for item in finalized["diagnostics"])


# --- 门的开关与阈值都要进缓存身份（换设置必须失效旧缓存）---
def test_hallucination_settings_enter_cache_identity():
    base = speech_to_text.TranscriptionSettings()
    off = base._replace(hallucination_gate=False)
    strict = base._replace(hallucination_compression_ratio=2.0)

    assert base.identity() != off.identity() != strict.identity()
    assert base.hallucination_gate is True and base.hallucination_compression_ratio == 2.4
