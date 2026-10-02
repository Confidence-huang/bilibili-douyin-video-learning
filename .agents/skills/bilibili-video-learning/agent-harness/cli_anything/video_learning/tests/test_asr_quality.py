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


# ============================ 规则法标点与标点质量（D42） ============================

def load_normalize():
    import importlib.util
    from pathlib import Path as _Path
    scripts = _Path(__file__).resolve().parents[4] / "scripts"
    spec = importlib.util.spec_from_file_location("video_learning_test_normalize_punct", scripts / "normalize_transcript.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


# --- 每个分段边界都必须插标点（实测：抬高阈值会让标点密度不足，F1 从 0.313 掉到 0.033）---
def test_pause_punctuation_marks_every_boundary():
    normalize = load_normalize()
    text = normalize.join_with_pause_punctuation([seg(0.0, 1.0, "第一句"), seg(2.0, 3.0, "第二句"),
                                                  seg(3.2, 4.0, "半句"), seg(4.1, 5.0, "又是半句")])

    assert text.startswith("第一句。第二句")          # 1 秒停顿 → 句号
    assert "第二句，半句" in text                     # 短停顿 → 逗号（密度必须保住）
    assert "半句，又是半句" in text                   # 边界处仍要插


# --- 句末语气词与连词开头也要断句（纯靠停顿会把"所以"粘在上一句）---
def test_punctuation_uses_particles_and_conjunctions():
    normalize = load_normalize()

    particles = normalize.join_with_pause_punctuation([seg(0.0, 1.0, "这样可以吗"), seg(1.05, 2.0, "可以")])
    conjunctions = normalize.join_with_pause_punctuation([seg(0.0, 1.0, "前面说完了"), seg(1.02, 2.0, "所以接下来")])

    assert "这样可以吗。可以" in particles           # 语气词结尾 → 句号
    assert "前面说完了。所以接下来" in conjunctions   # 连词开头 → 上一句收句号


# --- 已有标点的段不得重复插 ---
def test_punctuation_does_not_double_up():
    normalize = load_normalize()
    text = normalize.join_with_pause_punctuation([seg(0.0, 1.0, "已经结束了。"), seg(2.0, 3.0, "下一句")])

    assert "。。" not in text


# --- 标点质量：窗口内配对算命中；金标无标点时如实返回"不适用"---
def test_punctuation_scores_window_and_no_reference_marks():
    import eval_asr

    hit = eval_asr.punctuation_scores("你好，世界。", "你好，世界。")
    shifted = eval_asr.punctuation_scores("你好世界。", "你好，世界。")
    absent = eval_asr.punctuation_scores("你好，世界。", "你好世界")

    assert hit["f1"] == 1.0
    assert shifted["recall"] == 0.5                  # 逗号漏了，句号命中
    assert absent["f1"] is None and "不适用" in absent["note"]


# ============================ 模型档位按设备自适应（D43） ============================

def load_speech():
    import importlib.util
    from pathlib import Path as _Path
    scripts = _Path(__file__).resolve().parents[4] / "scripts"
    spec = importlib.util.spec_from_file_location("video_learning_test_speech_models", scripts / "speech_to_text.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- auto：有 CUDA 用 large，没有就用 small（CPU 上 large 慢到不可用）---
def test_auto_model_follows_device():
    speech = load_speech()

    assert speech.resolve_model_size("auto", cuda_available=True) == ("large", "auto:cuda")
    assert speech.resolve_model_size("auto", cuda_available=False) == ("small", "auto:cpu")


# --- 显式指定必须原样尊重，且 large-v3 归一为 large ---
def test_explicit_model_wins_and_alias_is_mapped():
    speech = load_speech()

    assert speech.resolve_model_size("small", cuda_available=True)[0] == "small"
    assert speech.resolve_model_size("large-v3", cuda_available=False)[0] == "large"
    assert speech.resolve_model_size("LARGE-V3", cuda_available=False)[1] == "explicit:large"


# --- 空值/None 也按 auto 处理，不能让 None 传进引擎 ---
def test_blank_model_falls_back_to_auto():
    speech = load_speech()

    assert speech.resolve_model_size("", cuda_available=True)[0] == "large"
    assert speech.resolve_model_size(None, cuda_available=False)[0] == "small"


# ============================ 跨金标回归与标点可选后端（D44） ============================

def load_script_module(name):
    import importlib.util
    from pathlib import Path as _Path
    scripts = _Path(__file__).resolve().parents[4] / "scripts"
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", scripts / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 用例说明解析：支持"金标只写文件名"（默认在 eval/gold/ 下找）---
def test_benchmark_case_parsing():
    benchmark = load_script_module("run_benchmark")

    case = benchmark.parse_case("bili=/tmp/x.json:bilibili-BV1ntah6TEe9.json")

    assert case["case"] == "bili" and case["gold"].name.endswith(".json")
    assert case["gold"].parent.name == "gold"          # 相对文件名被解析到 eval/gold/


# --- 用例说明写错时给出明确错误，而不是静默跳过 ---
def test_benchmark_case_parsing_rejects_bad_spec():
    benchmark = load_script_module("run_benchmark")

    import pytest
    with pytest.raises(ValueError, match="名称=产出"):
        benchmark.parse_case("没有等号也没有冒号")


# --- 表格渲染必须把列名与 None 都如实输出 ---
def test_benchmark_table_renders_none_honestly():
    benchmark = load_script_module("run_benchmark")

    table = benchmark.render_table([{"case": "a", "cer": 0.01, "coverage": None}])

    assert "| case | cer |" in table and "None" in table


# --- 取键要防御式：字段名跨版本变过，不能猜死一个 ---
def test_benchmark_first_key_helper():
    benchmark = load_script_module("run_benchmark")

    assert benchmark._first({"rate": 0.0, "characters_per_minute": 1.5}, "characters_per_minute", "rate") == 1.5
    assert benchmark._first({"rate": 0.0}, "characters_per_minute", "rate") == 0.0   # 0 也是有效值
    assert benchmark._first({}, "rate") is None


# --- 标点可选后端：CI 里没装 → 必须优雅降级成规则法，而不是报错 ---
def test_punctuation_optional_backend_degrades():
    punctuate = load_script_module("punctuate")

    assert punctuate.available() is False                     # CI 不装可选依赖
    text, mode = punctuate.restore("你好世界", segments=[{"start": 0.0, "end": 1.0, "text": "你好"},
                                                         {"start": 2.0, "end": 3.0, "text": "世界"}])

    assert mode == "rules" and "。" in text                   # 规则法仍然给出标点


# ============================ 基线记录与劣化闸门（D47） ============================

# --- 超出容差算劣化；容差内不算；基线里没有的用例要提醒（新增用例先记录基线）---
def test_baseline_comparison_flags_regression_and_unknown_case():
    benchmark = load_script_module("run_benchmark")
    baseline = {"cases": [{"case": "a", "cer": 0.01, "tolerance": 0.005}, {"case": "b", "cer": 0.02}]}

    problems = benchmark.compare_baseline(
        [{"case": "a", "cer": 0.012}, {"case": "b", "cer": 0.05}, {"case": "c", "cer": 0.0}], baseline)

    assert not any(item.startswith("a:") for item in problems)        # 容差内
    assert any(item.startswith("b:") and "劣化" in item for item in problems)
    assert any(item.startswith("c:") and "基线里没有" in item for item in problems)


# --- 改善必须被打印出来并提示更新基线（否则基线会永远停在旧值）---
def test_baseline_reports_improvement(capsys):
    benchmark = load_script_module("run_benchmark")

    problems = benchmark.compare_baseline([{"case": "a", "cer": 0.001}],
                                          {"cases": [{"case": "a", "cer": 0.01, "tolerance": 0.005}]})

    assert problems == []
    assert "IMPROVED" in capsys.readouterr().out


# --- 仓库自带的基线记录必须覆盖四个用例，且容差合理 ---
def test_shipped_baseline_is_usable():
    import json
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[4]
    baseline = json.loads((root / "eval/baselines.json").read_text(encoding="utf-8"))

    cases = {item["case"]: item for item in baseline["cases"]}
    assert {"bili1-large", "bili1-small", "bili2-large", "bili2-small"} <= set(cases)
    assert all(0 < item["tolerance"] <= 0.05 for item in baseline["cases"])


# --- 基线按归一模式分别记录：同一用例在不同环境各比各的（D47）---
def test_baseline_matches_by_normalization_mode():
    benchmark = load_script_module("run_benchmark")
    baseline = {"cases": [{"case": "a", "tolerance": 0.01,
                           "cer_by_mode": {"t2s:on+n": 0.11, "t2s:off+n": 0.13}}]}

    on = benchmark.compare_baseline([{"case": "a", "cer": 0.115, "norm": "t2s:on+n"}], baseline)
    off = benchmark.compare_baseline([{"case": "a", "cer": 0.135, "norm": "t2s:off+n"}], baseline)
    unknown = benchmark.compare_baseline([{"case": "a", "cer": 0.12, "norm": "t2s:fallback+n"}], baseline)

    assert on == [] and off == []                       # 两种模式都各自在容差内
    assert unknown and "没有模式" in unknown[0]          # 没记录过的模式必须先记录


# --- 归一模式必须报告"真的生效"还是"降级"，不能是笼统的 True（D48）---
def test_simplification_mode_reports_reality():
    normalize = load_normalize()
    eval_asr = load_script_module("eval_asr")
    from pathlib import Path as _Path

    mode = normalize.simplification_mode()
    expected = "opencc" if normalize.load_simplifier() is not None else "fallback"
    assert mode == expected and mode in ("opencc", "fallback")

    # 评测报告里的字段必须等于实际模式（此前写死 True，跨环境基线因此对不上）
    gold = _Path(normalize.__file__).parent.parent / "eval/gold/bilibili-BV1ntah6TEe9.json"
    assert eval_asr.evaluate(gold, gold)["normalization"]["traditional_to_simplified"] == mode


# --- 用例说明必须能在 Windows 盘符下解析（真 bug：最后一个冒号可能属于盘符，D49）---
def test_benchmark_case_parsing_handles_windows_drive_letters(tmp_path):
    benchmark = load_script_module("run_benchmark")
    hypothesis = tmp_path / "hyp.json"
    gold = tmp_path / "gold.json"
    hypothesis.write_text("{}", encoding="utf-8")
    gold.write_text("{}", encoding="utf-8")

    case = benchmark.parse_case(f"win={hypothesis}:{gold}")

    assert case["hypothesis"] == hypothesis and case["gold"] == gold


# --- 文件不存在时退回"最后一个冒号"，并由调用方报缺失 ---
def test_benchmark_case_parsing_falls_back_to_last_colon():
    benchmark = load_script_module("run_benchmark")

    case = benchmark.parse_case("x=/tmp/不存在.json:bilibili-BV1ntah6TEe9.json")

    assert case["hypothesis"].name == "不存在.json"
    assert case["gold"].name == "bilibili-BV1ntah6TEe9.json"


# ============================ 历史趋势表（D52） ============================

# --- 同一 用例×模式 多次测量后要给出首末值与差值 ---
def test_history_report_shows_trend_per_case_and_mode(capsys):
    benchmark = load_script_module("run_benchmark")
    entries = [
        {"case": "a", "norm": "t2s:opencc+n", "cer": 0.0100, "timestamp": "2026-01-01T00:00:00Z"},
        {"case": "a", "norm": "t2s:opencc+n", "cer": 0.0090, "timestamp": "2026-01-02T00:00:00Z"},
        {"case": "a", "norm": "t2s:fallback+n", "cer": 0.0130, "timestamp": "2026-01-02T00:00:00Z"},
    ]

    table = benchmark.render_history_report(entries)

    assert "| a | t2s:opencc+n | 2 | 0.01 | 0.009 | -0.001" in table     # 首末值与差值都要在
    assert "t2s:fallback+n" in table                                   # 不同模式分开统计
    assert table.count("\n") == 3                                      # 表头 + 分隔 + 两行


# --- 历史为空时给出可执行提示，而不是空表 ---
def test_history_report_handles_empty_history():
    benchmark = load_script_module("run_benchmark")

    assert "历史为空" in benchmark.render_history_report([])


# --- 历史文件不存在时返回用法码，而不是抛栈 ---
def test_history_report_missing_file(tmp_path, capsys):
    benchmark = load_script_module("run_benchmark")

    assert benchmark.main(["--case", "x=/tmp/a.json:/tmp/b.json", "--history-report", str(tmp_path / "无.json")]) == 2
    assert "error" in capsys.readouterr().out


# ============================ 发版汇总与"最近一次基准"（D53） ============================

# --- 汇总取每个 用例×模式 的**最新**值（后出现的覆盖先出现的）---
def test_release_summary_uses_latest_per_case_and_mode():
    benchmark = load_script_module("run_benchmark")
    entries = [
        {"case": "a", "norm": "t2s:opencc+n", "cer": 0.02, "timestamp": "T1"},
        {"case": "a", "norm": "t2s:opencc+n", "cer": 0.01, "timestamp": "T2"},
        {"case": "a", "norm": "t2s:fallback+n", "cer": 0.03, "timestamp": "T2"},
    ]

    rows = benchmark.release_summary_rows(entries, "1.28.0")

    joined = "\n".join(rows)
    assert len(rows) == 2                                                 # 两个模式各一行（不假设行序）
    assert "| 1.28.0 | a | t2s:opencc+n | 0.01 |" in joined               # opencc 取最新 0.01
    assert "| 1.28.0 | a | t2s:fallback+n | 0.03 |" in joined


# --- 没有历史时汇总为空，不得凭空写行 ---
def test_release_summary_is_empty_without_measurements():
    benchmark = load_script_module("run_benchmark")

    assert benchmark.release_summary_rows([], "1.28.0") == []


# ============================ 趋势闸门（D54） ============================

# --- 末次比上次劣化超容差 → 报问题；改善或只有一次测量 → 不报 ---
def test_history_regressions_only_flags_worsening_trends():
    benchmark = load_script_module("run_benchmark")
    entries = [
        {"case": "worse", "norm": "m", "cer": 0.010},
        {"case": "worse", "norm": "m", "cer": 0.020},            # 劣化 +0.01 > 容差
        {"case": "better", "norm": "m", "cer": 0.020},
        {"case": "better", "norm": "m", "cer": 0.010},           # 改善
        {"case": "single", "norm": "m", "cer": 0.030},           # 只有一次
    ]

    problems = benchmark.history_regressions(entries)

    assert len(problems) == 1 and problems[0].startswith("worse [m]")
    assert "劣化" in problems[0]


# --- 容差内不算劣化（避免把噪声当回归）---
def test_history_regressions_respects_tolerance():
    benchmark = load_script_module("run_benchmark")
    entries = [{"case": "a", "norm": "m", "cer": 0.010}, {"case": "a", "norm": "m", "cer": 0.012}]

    assert benchmark.history_regressions(entries, tolerance=0.005) == []
    assert benchmark.history_regressions(entries, tolerance=0.001)      # 收紧容差才报


# --- 不同模式分开看趋势：一个模式劣化不影响另一个 ---
def test_history_regressions_are_per_case_and_mode():
    benchmark = load_script_module("run_benchmark")
    entries = [
        {"case": "a", "norm": "opencc", "cer": 0.01}, {"case": "a", "norm": "opencc", "cer": 0.02},
        {"case": "a", "norm": "fallback", "cer": 0.03}, {"case": "a", "norm": "fallback", "cer": 0.03},
    ]

    problems = benchmark.history_regressions(entries)

    assert len(problems) == 1 and "opencc" in problems[0]


# ============================ 发版汇总的幂等与版本校验（D55） ============================

# --- 版本号必须形如 1.29.0，否则拒绝（否则趋势表无法排序比较）---
def test_release_summary_rejects_non_version(tmp_path, capsys):
    benchmark = load_script_module("run_benchmark")
    history = tmp_path / "h.jsonl"
    history.write_text('{"case": "a", "norm": "m", "cer": 0.01, "timestamp": "T"}\n', encoding="utf-8")

    code = benchmark.main(["--append-release-summary", str(history), "--release-version", "latest"])

    assert code == 2 and "1.29.0" in capsys.readouterr().out


# --- 同 版本×用例×模式 重复执行不写第二行（幂等）---
def test_release_summary_is_idempotent(tmp_path, capsys):
    benchmark = load_script_module("run_benchmark")
    history = tmp_path / "h.jsonl"
    history.write_text('{"case": "a", "norm": "m", "cer": 0.01, "timestamp": "T"}\n', encoding="utf-8")
    target = tmp_path / "target.jsonl"

    first = benchmark.existing_summary_keys(target)
    rows = benchmark.release_summary_rows([{"case": "a", "norm": "m", "cer": 0.01, "timestamp": "T"}], "1.29.0")

    assert len(rows) == 1
    assert first == set()                                                # 首次没有已存在的键
    target.write_text("| 版本 | 用例 | 归一模式 | CER | 覆盖率 | 记录时间 |\n|---|---|---|---|---|---|\n" + rows[0] + "\n",
                      encoding="utf-8")
    assert ("1.29.0", "a", "m") in benchmark.existing_summary_keys(target)     # 第二次能识别出来（v 前缀可省）
