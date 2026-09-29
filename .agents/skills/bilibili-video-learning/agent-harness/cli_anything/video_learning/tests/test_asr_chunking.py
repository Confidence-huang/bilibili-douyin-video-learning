"""
长音频分块的离线测试（对应 docs/DECISIONS.md D38）。

守住两条不变量：**时间轴按块起点平移且单调**、**重叠区不重复**；并守住"默认关闭"——
`chunk_length=0` 时行为与分块功能引入前完全一致。
运行示例：python -m pytest cli_anything/video_learning/tests/test_asr_chunking.py -v
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

import asr_chunking  # noqa: E402 被测模块
import speech_to_text  # noqa: E402 接线点


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================ 分块规划 ============================

# --- 短音频不分块：既有行为不变 ---
def test_short_audio_is_not_chunked():
    assert asr_chunking.plan_chunks(30.0, 600.0, 2.0) == []
    assert asr_chunking.plan_chunks(None, 600.0, 2.0) == []


# --- 30 分钟 → 3 块，覆盖到结尾，块间有重叠且不回跳 ---
def test_long_audio_plan_covers_tail_with_overlap():
    plan = asr_chunking.plan_chunks(1800.0, 600.0, 2.0)

    assert len(plan) == 3
    assert plan[0]["start"] == 0.0
    assert plan[-1]["end"] == pytest.approx(1800.0)                   # 必须覆盖到结尾，不能漏掉尾巴
    for previous, current in zip(plan, plan[1:]):
        assert current["start"] < previous["end"]                     # 有重叠
        assert current["start"] >= previous["start"]                  # 单调不回跳
        assert current["end"] > current["start"]


# --- 极短块长被抬到最小值，重叠不得吞掉整块 ---
def test_degenerate_parameters_are_clamped():
    plan = asr_chunking.plan_chunks(60.0, 1.0, 999.0)

    assert plan and all(item["end"] - item["start"] >= asr_chunking.MIN_CHUNK_SECONDS for item in plan)
    assert all(item["start"] < item["end"] for item in plan)


# ============================ 合并 ============================

# --- 每块结果按块起点平移，时间轴单调 ---
def test_merge_shifts_timestamps_by_chunk_start():
    merged = asr_chunking.merge_chunk_results([
        {"start": 0.0, "segments": [{"from": 0.0, "to": 2.0, "content": "第一句"}]},
        {"start": 598.0, "segments": [{"from": 1.0, "to": 3.0, "content": "第二句"}]},
    ])

    assert [item["from"] for item in merged] == [0.0, 599.0]
    assert merged[1]["content"] == "第二句"


# --- 重叠区被解了两遍时只保留前一块那份，并留下标记（不隐藏）---
def test_merge_drops_duplicates_from_overlap():
    duplicated = {"from": 0.5, "to": 2.0, "content": "重复的话"}
    merged = asr_chunking.merge_chunk_results([
        {"start": 0.0, "segments": [{"from": 0.0, "to": 2.5, "content": "重复的话"}]},
        {"start": 598.0, "segments": [duplicated]},
    ])

    assert len(merged) == 1 and merged[0]["content"] == "重复的话"          # 只留一份


# --- 词级时间戳同样要平移（否则词与段对不上）---
def test_merge_shifts_word_timestamps():
    merged = asr_chunking.merge_chunk_results([
        {"start": 100.0, "segments": [{"from": 1.0, "to": 2.0, "content": "词",
                                       "words": [{"start": 1.2, "end": 1.8, "word": "词"}]}]},
    ])

    assert merged[0]["words"][0]["start"] == pytest.approx(101.2)
    assert merged[0]["words"][0]["end"] == pytest.approx(101.8)


# ============================ 接线 ============================

# --- 默认关闭：设置项为 0，且分块长度进缓存身份（改了必须重新转写）---
def test_chunk_settings_default_off_and_enter_identity():
    settings = speech_to_text.TranscriptionSettings()

    assert settings.chunk_length == 0.0
    assert settings.identity() != settings._replace(chunk_length=600.0).identity()


# --- 分块路径：逐块调用、子调用关闭分块（防递归）、结果按时移合并 ---
def test_chunked_path_prevents_recursion_and_merges(monkeypatch, tmp_path):
    plan = [{"index": 0, "start": 0.0, "end": 600.0}, {"index": 1, "start": 598.0, "end": 1200.0}]
    calls = []

    monkeypatch.setattr(speech_to_text.asr_coverage, "audio_duration_seconds", lambda path: 1200.0)
    monkeypatch.setattr(speech_to_text.asr_chunking, "plan_chunks", lambda *a, **k: plan)
    monkeypatch.setattr(speech_to_text.asr_coverage, "cut_audio_window",
                        lambda audio, start, end, out: Path(out).write_bytes(b"RIFF") or Path(out))

    def fake_transcribe(audio, **kwargs):
        calls.append(kwargs.get("settings"))
        return {"segments": [{"from": 0.0, "to": 1.5, "content": f"块{len(calls)}"}],
                "device": "cuda", "diagnostics": [{"step": "asr_engine", "ok": True, "message": "ok"}]}

    monkeypatch.setattr(speech_to_text, "transcribe_audio_file", fake_transcribe)
    chunked_settings = speech_to_text.TranscriptionSettings()._replace(chunk_length=600.0)
    result = speech_to_text._transcribe_with_chunks(tmp_path / "audio.wav", chunked_settings,
                                                    model_size="small", language="zh", device="cpu",
                                                    log_prefix="asr", allow_openai_fallback=False)

    assert len(calls) == 2
    assert all(call.chunk_length == 0.0 for call in calls)                 # 子调用不再分块
    assert result["chunked"] is True and len(result["segments"]) == 2
    assert result["segments"][1]["from"] == pytest.approx(598.0)           # 第二块按时移平移
    assert result["diagnostics"][0]["step"] == "asr_chunking"
    assert result["device"] == "cuda"                                      # 必须报实际用到的设备（D40）
    assert any(item.get("step") == "asr_engine" for item in result["diagnostics"])   # 子诊断不能被吞掉


# --- 分块不适用时必须退回整段解码，且真的一次都不切块 ---
def test_short_audio_falls_back_to_single_pass(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(speech_to_text.asr_coverage, "audio_duration_seconds", lambda path: 20.0)
    monkeypatch.setattr(speech_to_text, "transcribe_audio_file",
                        lambda audio, **kwargs: captured.update(kwargs) or {"segments": []})

    speech_to_text._transcribe_with_chunks(tmp_path / "audio.wav",
                                           speech_to_text.TranscriptionSettings()._replace(chunk_length=600.0),
                                           model_size="small", language="zh", device="cpu",
                                           log_prefix="asr", allow_openai_fallback=False)

    assert captured["settings"].chunk_length == 0.0                        # 退回整段解码
