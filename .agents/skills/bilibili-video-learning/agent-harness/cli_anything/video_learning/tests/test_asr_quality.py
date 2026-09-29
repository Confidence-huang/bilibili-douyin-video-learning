"""
ASR 质量层的离线测试：领域词表、音频前端、转写档位、词级时间戳（对应 D26 与 D28）。

守住四件事：
    1. 词表只做"解码偏置"，且优先级与长度上限明确（越具体越优先，超限按优先级截断）；
    2. 音频前端开关真的改变 ffmpeg 滤镜链，关掉时必须与历史行为一致；
    3. 档位把速度/精度取舍打包，未知档位必须报错而不是静默沿用默认值；
    4. `hotwords` / `word_timestamps` 真的传到了引擎，而不是"设了没用"。
运行示例：python -m pytest cli_anything/video_learning/tests/test_asr_quality.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill。
import json  # 造临时词表与假设文件。
import sys  # 把 scripts 目录加入模块搜索路径。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 伪造 faster-whisper 的分段对象。

import pytest  # 提供 monkeypatch 与断言辅助。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import asr_lexicon  # noqa: E402 被测的词表模块。
import media_tools  # noqa: E402 被测的音频前端模块。
import speech_to_text  # noqa: E402 被测的 ASR 入口。


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


# ============================ 词表 ============================

# --- 仓库内置词表必须存在且可解析（它是默认偏置的来源）---
def test_shipped_lexicon_is_readable_and_clean():
    terms = asr_lexicon.load_lexicon()

    assert len(terms) >= 10, "内置词表不应为空：没有词表就等于没有词表偏置"
    assert all(term and "#" not in term for term in terms)
    assert all(asr_lexicon.MIN_TERM_CHARS <= len(term) <= asr_lexicon.MAX_TERM_CHARS for term in terms)
    assert len(terms) == len({term.casefold() for term in terms}), "词表必须去重"


# --- 词表文件：注释、空行、越界词都要被正确忽略；缺文件不是错误 ---
def test_load_lexicon_ignores_comments_and_out_of_range_terms(tmp_path):
    path = tmp_path / "lex.txt"
    path.write_text("血包\n# 注释\n\n单\ntoolongtermtoolongterm\n供血 # 尾注释\n", encoding="utf-8")

    terms = asr_lexicon.load_lexicon([path])

    assert terms == ["血包", "供血"]
    assert asr_lexicon.load_lexicon([tmp_path / "missing.txt"]) == []


# --- 元数据抽词：标题/简介/标签都要用上，噪声词要丢掉 ---
def test_terms_from_metadata_extracts_and_filters():
    metadata = {"title": "男性是最好的血包 #成长#认知", "uploader": "冤种小徐",
                "tags": ["字幕", "抖音", "情绪价值"], "description": "精神内耗"}

    terms = asr_lexicon.terms_from_metadata(metadata)

    # 标题按分隔符切分：不带分隔的长标题会整体成为一个短语（这是刻意的，不做不可靠的子串猜测）
    assert "男性是最好的血包" in terms
    assert "认知" in terms and "成长" in terms and "情绪价值" in terms and "冤种小徐" in terms
    assert "字幕" not in terms and "抖音" not in terms and "bilibili" not in terms


# --- hotwords 合成：显式词最优先，其次词表，最后元数据；超限截断 ---
def test_build_hotwords_priority_and_limit():
    hotwords = asr_lexicon.build_hotwords(["词表甲", "词表乙"], metadata={"title": "元数据词"}, extra="显式词")

    assert hotwords.split()[:3] == ["显式词", "词表甲", "词表乙"]
    assert "元数据词" in hotwords.split()

    capped = asr_lexicon.build_hotwords([f"术语{i:03d}" for i in range(60)], limit_chars=30)
    assert len(capped) <= 30
    assert capped.split()[0] == "术语000", "截断必须保留优先级更高的词"


# --- 词表诊断只报规模，不泄露内容规模之外的信息 ---
def test_describe_hotwords_reports_truncation():
    described = asr_lexicon.describe_hotwords(asr_lexicon.build_hotwords(["甲" * 12] * 30, limit_chars=50))

    assert described["terms"] >= 1
    assert described["limit_chars"] == asr_lexicon.HOTWORDS_LIMIT_CHARS
    assert set(described) == {"terms", "chars", "limit_chars", "truncated"}


# ============================ 音频前端 ============================

# --- 前端滤镜链：默认做高通+响度归一，关闭时必须是空串（与历史行为一致）---
def test_audio_filter_chain_default_and_disabled():
    chain = media_tools.build_audio_filter_chain()

    assert "highpass" in chain and "loudnorm" in chain
    assert media_tools.build_audio_filter_chain(normalize=False) == ""


# --- 降噪默认关闭（会引入金属感），显式打开才加 ---
def test_audio_filter_chain_denoise_is_opt_in():
    assert "afftdn" not in media_tools.build_audio_filter_chain()
    assert "afftdn" in media_tools.build_audio_filter_chain(denoise=True)


# --- 采样率常量必须与 Whisper 期望一致 ---
def test_sample_rate_is_whisper_default():
    assert media_tools.SAMPLE_RATE == 16000


# ============================ 档位 ============================

# --- 三个档位要给出不同的"速度/精度"取舍，且都保留安全网 ---
def test_profiles_encode_measured_tradeoffs():
    base = speech_to_text.TranscriptionSettings()

    balanced = speech_to_text.apply_profile(base, "balanced")
    timing = speech_to_text.apply_profile(base, "timing")
    quality = speech_to_text.apply_profile(base, "quality")

    # 档位按实测定义：balanced 追最低 CER，timing 换词级时间轴，quality 追覆盖率并开启定向重解
    assert balanced.beam_size == 1 and balanced.word_timestamps is False
    assert timing.word_timestamps is True and timing.beam_size == 1
    assert quality.beam_size == 5 and quality.refine_low_confidence is True
    assert all(profile.coverage_check for profile in (balanced, timing, quality)), "任何档位都不能关掉覆盖率安全网"
    assert all(profile.normalize_audio for profile in (balanced, timing, quality)), "音频前端是实测收益，默认不关"
    assert (balanced.profile, timing.profile, quality.profile) == ("balanced", "timing", "quality")


# --- 未知档位必须报错：静默沿用默认值会让用户以为参数生效了 ---
def test_unknown_profile_raises():
    with pytest.raises(ValueError):
        speech_to_text.apply_profile(speech_to_text.TranscriptionSettings(), "turbo-max")


# --- 词表与词级时间戳都要进缓存身份（否则同一正文会有两套依据）---
def test_cache_identity_tracks_quality_settings():
    base = speech_to_text.TranscriptionSettings()
    with_hotwords = base._replace(hotwords="血包 供血")
    with_words = base._replace(word_timestamps=True)
    with_profile = base._replace(profile="quality")

    identities = {json.dumps(settings.identity(), sort_keys=True, ensure_ascii=False)
                  for settings in (base, with_hotwords, with_words, with_profile)}

    assert len(identities) == 4, "参数变化必须改变缓存身份"
    assert base.identity()["params_version"] == speech_to_text.ASR_PARAMS_VERSION


# ============================ 真的传到了引擎 ============================

# --- 伪造引擎：记录 transcribe 的 kwargs，并返回一段带词级时间戳的结果 ---
def install_fake_engine(monkeypatch, captured: dict):
    segment = SimpleNamespace(text=" 中国男性是最好的血包 ", start=0.0, end=2.0, avg_logprob=-0.2,
                              no_speech_prob=0.01, compression_ratio=1.2,
                              words=[SimpleNamespace(word="血包", start=1.0, end=1.5)])

    class FakeModel:
        def __init__(self, *args, **kwargs):
            captured["model_args"] = (args, kwargs)

        def transcribe(self, audio, **kwargs):
            captured.setdefault("calls", []).append(kwargs)
            return iter([segment]), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(speech_to_text.cuda_runtime, "prepare_cuda_libraries",
                        lambda *a, **k: {"found": {}, "preloaded": [], "guidance": None,
                                         "searched_directories": [], "errors": [], "path_updated": False,
                                         "platform": "posix"})
    return captured


# --- hotwords 与 word_timestamps 必须出现在真实解码调用里 ---
def test_engine_receives_hotwords_and_word_timestamps(monkeypatch, tmp_path):
    captured = install_fake_engine(monkeypatch, {})
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    settings = speech_to_text.apply_profile(
        speech_to_text.TranscriptionSettings(hotwords="血包 供血", coverage_check=False), "timing")

    result = speech_to_text.transcribe_audio_file(audio, settings=settings)

    first_call = captured["calls"][0]
    assert first_call["hotwords"] == "血包 供血"          # 词表必须真的传到引擎
    assert first_call["word_timestamps"] is True         # timing 档位开启词级时间戳
    assert first_call["beam_size"] == 1
    assert result["segments"][0]["words"] == [{"start": 1.0, "end": 1.5, "word": "血包"}]  # 词级时间戳被保留
    assert result["profile"] == "timing"
    assert result["hotwords"]["terms"] == 2 and "血包" not in json.dumps(result["hotwords"])


# --- 关闭词级时间戳时不应把 words 字段塞进产出（避免无谓膨胀）---
def test_words_are_empty_when_word_timestamps_disabled(monkeypatch, tmp_path):
    captured = install_fake_engine(monkeypatch, {})
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    result = speech_to_text.transcribe_audio_file(
        audio, settings=speech_to_text.apply_profile(speech_to_text.TranscriptionSettings(coverage_check=False), "balanced"))

    assert captured["calls"][0]["word_timestamps"] is False
    assert result["segments"][0]["words"] == []


# --- 空词表要传 None 而不是空字符串：空提示词会污染解码 ---
def test_empty_hotwords_are_passed_as_none(monkeypatch, tmp_path):
    captured = install_fake_engine(monkeypatch, {})
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    speech_to_text.transcribe_audio_file(audio, settings=speech_to_text.TranscriptionSettings(coverage_check=False))

    assert captured["calls"][0]["hotwords"] is None
