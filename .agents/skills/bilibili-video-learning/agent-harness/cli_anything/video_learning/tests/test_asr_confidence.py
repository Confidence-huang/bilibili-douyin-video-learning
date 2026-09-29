"""
ASR 置信度采集与低置信标记的离线测试。
这些测试守住一件事：模型自己觉得不可信的区间必须被标出来供人工复核，而不是混在正文里当事实。
运行示例：python -m pytest cli_anything/video_learning/tests/test_asr_confidence.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 构造假的分段对象与假引擎模块。

import pytest  # 提供 monkeypatch 与临时目录。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

import asr_coverage  # noqa: E402 与 speech_to_text 共用同一实例，便于注入时长探测。
import normalize_transcript as nt  # noqa: E402 置信度必须能一路带到规范形状。


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"  # 测试名直接映射真实脚本文件。
    module_name = f"video_learning_test_{name}"  # 独立模块名避免测试之间污染缓存。
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:  # 文件无法加载时给出明确的测试失败原因。
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 造一个只返回固定分段的假引擎 ---
def install_fake_engine(monkeypatch, speech_to_text, segments):
    class FakeModel:
        def transcribe(self, audio, **kwargs):
            return iter(segments), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=lambda *a, **k: FakeModel()))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: None)  # 跳过覆盖率校验，只看置信度


# --- 低置信区间必须被标出来 ---
def test_low_confidence_spans_are_reported(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    install_fake_engine(monkeypatch, speech_to_text, [
        SimpleNamespace(text="可信", start=0.0, end=1.0, avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.2),
        SimpleNamespace(text="不可信", start=1.0, end=2.0, avg_logprob=-1.6, no_speech_prob=0.8, compression_ratio=3.1),
    ])
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = speech_to_text.transcribe_audio_file(audio)

    assert result["segments"][0]["confidence"] == pytest.approx(-0.2)  # 置信度被采集
    assert result["segments"][1]["confidence"] == pytest.approx(-1.6)
    assert len(result["low_confidence_spans"]) == 1  # 只有低置信那段被标出
    assert result["low_confidence_spans"][0]["text"] == "不可信"
    assert [d for d in result["diagnostics"] if d.get("step") == "asr_confidence"]


# --- 阈值可调 ---
def test_low_confidence_threshold_is_configurable(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    install_fake_engine(monkeypatch, speech_to_text, [
        SimpleNamespace(text="边界", start=0.0, end=1.0, avg_logprob=-0.8, no_speech_prob=0.1, compression_ratio=1.5),
    ])
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    lenient = speech_to_text.transcribe_audio_file(
        audio, settings=speech_to_text.TranscriptionSettings(low_confidence_logprob=-2.0))
    strict = speech_to_text.transcribe_audio_file(
        audio, settings=speech_to_text.TranscriptionSettings(low_confidence_logprob=-0.5))

    assert lenient["low_confidence_spans"] == []  # 阈值更宽松时不报警
    assert len(strict["low_confidence_spans"]) == 1  # 阈值更严格时报警


# --- 置信度必须一路带到规范形状 ---
def test_confidence_survives_normalization():
    canonical = nt.normalize_segments([{"from": 0.0, "to": 1.0, "content": "正文", "confidence": -0.42}])

    assert canonical[0]["confidence"] == pytest.approx(-0.42)
