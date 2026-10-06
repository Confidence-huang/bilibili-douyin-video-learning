"""
分段 schema 适配层与清洗保真度的离线测试。
这些测试守住三条底线：两套历史字段名都能吃、逐字模式绝不删词、清洗脚本能接真实 ASR 结果。
运行示例：python -m pytest cli_anything/video_learning/tests/test_normalize_transcript.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 校验产出的 SRT/JSON 内容。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

import pytest  # 提供 monkeypatch、临时目录与异常断言。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

import normalize_transcript as nt  # noqa: E402 规范化模块与生产脚本共用同一实例。


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


# --- 两种历史形状都能被识别 ---
def test_detect_schema_recognizes_both_historical_shapes():
    canonical = [{"start": 0.0, "end": 1.0, "text": "规范"}]
    asr = [{"from": 0.0, "to": 1.0, "content": "ASR"}]

    assert nt.detect_schema(canonical) == nt.SCHEMA_CANONICAL
    assert nt.detect_schema(asr) == nt.SCHEMA_ASR
    assert nt.detect_schema([]) == nt.SCHEMA_EMPTY
    assert nt.detect_schema([{"timestamp": 0, "body": "?"}]) == nt.SCHEMA_UNKNOWN


# --- ASR 形状必须能转成规范形状（这正是抖音结果过去 KeyError 的原因） ---
def test_asr_segments_are_converted_to_canonical():
    asr = [{"from": 0.06, "to": 27.98, "content": "中国男性是天底下最好的血包"}]

    canonical = nt.normalize_segments(asr)

    assert canonical == [{"start": 0.06, "end": 27.98, "text": "中国男性是天底下最好的血包"}]
    assert nt.detect_schema(canonical) == nt.SCHEMA_CANONICAL


# --- 反向转换保持可往返 ---
def test_canonical_and_asr_shapes_round_trip():
    canonical = [{"start": 1.5, "end": 2.5, "text": "往返"}]

    asr = nt.to_asr_segments(canonical)

    assert asr == [{"from": 1.5, "to": 2.5, "content": "往返"}]
    assert nt.normalize_segments(asr) == canonical


# --- 不认识的形状必须显式失败，而不是猜 ---
def test_unknown_schema_fails_loudly():
    with pytest.raises(nt.TranscriptSchemaError, match="unrecognized transcript schema"):
        nt.normalize_segments([{"begin": 0, "finish": 1, "body": "?"}])


# --- 缺时间戳不能静默变成 0 ---
def test_missing_timestamps_fail_loudly():
    with pytest.raises(nt.TranscriptSchemaError, match="missing start/end"):
        nt.normalize_segments([{"start": 0.0, "text": "只有开始时间"}])


# --- 可选置信度会被保留，但不会被伪造 ---
def test_optional_confidence_is_preserved_not_invented():
    with_confidence = nt.normalize_segments([{"start": 0.0, "end": 1.0, "text": "甲", "confidence": -0.3}])
    without_confidence = nt.normalize_segments([{"start": 0.0, "end": 1.0, "text": "乙"}])

    assert with_confidence[0]["confidence"] == pytest.approx(-0.3)
    assert "confidence" not in without_confidence[0]  # 缺省不补一个假的


# --- 时间轴自检能发现重叠、倒置与空文本 ---
def test_validation_reports_timeline_problems():
    problems = nt.validate_segments([
        {"start": 0.0, "end": 2.0, "text": "甲"},
        {"start": 1.0, "end": 3.0, "text": "乙"},  # 与上一段重叠
        {"start": 5.0, "end": 4.0, "text": "丙"},  # 结束早于开始
        {"start": 6.0, "end": 7.0, "text": ""},    # 空文本
    ])

    assert len(problems) == 3
    assert any("overlaps" in item for item in problems)
    assert any("ends before" in item for item in problems)
    assert any("empty text" in item for item in problems)


# --- SRT 时间戳与折行 ---
def test_srt_output_has_valid_timestamps_and_keeps_all_words():
    segments = [{"start": 0.0, "end": 1.5, "text": "短句"},
                {"start": 61.25, "end": 63.0, "text": "这是一句需要折行的很长字幕文本用来验证折行逻辑"}]

    srt = nt.to_srt(segments)
    blocks = [block for block in srt.strip().split("\n\n")]

    assert "00:00:00,000 --> 00:00:01,500" in blocks[0]
    assert "00:01:01,250 --> 00:01:03,000" in blocks[1]
    stripped = srt.replace("\n", "").replace(" ", "")
    for char in "这是一句需要折行的很长字幕文本用来验证折行逻辑":
        assert char in stripped  # 折行不得丢字
    assert all(len(line) <= 24 for line in blocks[1].split("\n")[2:])


# --- 纯文本拼接 ---
def test_plain_text_joins_every_segment():
    segments = [{"start": 0.0, "end": 1.0, "text": "甲"}, {"start": 1.0, "end": 2.0, "text": "乙"}]

    assert nt.to_plain_text(segments) == "甲乙"
    assert nt.to_plain_text(segments, separator="\n") == "甲\n乙"


# --- 摘要用于诊断 ---
def test_summary_reports_coverage():
    summary = nt.summarize_segments([{"start": 0.0, "end": 2.0, "text": "甲乙"},
                                     {"start": 3.0, "end": 4.0, "text": "丙"}])

    assert summary == {"count": 2, "start": 0.0, "end": 4.0, "covered_seconds": 3.0, "text_chars": 3}


# --- 逐字模式绝不删词 ---
def test_verbatim_fidelity_never_deletes_words():
    cleaner = load_script("clean_transcript")
    segments = [
        {"from": 0.0, "to": 1.0, "content": "这句话很重要"},
        {"from": 1.0, "to": 1.4, "content": "嗯"},           # 整段是填充词
        {"from": 1.4, "to": 2.4, "content": "这个社会对于好男人的定义"},  # "这个"是语义成分
        {"from": 2.4, "to": 3.4, "content": "这个社会对于好男人的定义"},  # 相邻重复
        {"from": 3.4, "to": 3.6, "content": "吗"},           # 极短碎片
        {"from": 3.6, "to": 4.6, "content": "结尾"},
    ]

    cleaned, report = cleaner.clean_segments(segments, fidelity="verbatim")
    text = "".join(item["text"] for item in cleaned)

    assert report["dropped_fillers"] == 0  # 逐字模式一个词都不丢
    assert "嗯" in text
    assert "这个社会对于好男人的定义" in text
    assert report["input_schema"] == nt.SCHEMA_ASR  # 真实 ASR 形状被正确识别
    assert report["deduplicated"] == 1  # 相邻重复先在原始边界上被识别
    assert report["merged_fragments"] == 2  # "嗯" 与 "吗" 两个碎片各自并入邻居
    assert text == "这句话很重要嗯这个社会对于好男人的定义吗结尾"  # 顺序不变、只做原样拼接


# --- 清洗模式才允许删词，并且必须报数 ---
def test_cleaned_fidelity_drops_fillers_and_reports_them():
    cleaner = load_script("clean_transcript")
    segments = [{"start": 0.0, "end": 1.0, "text": "正文"},
                {"start": 1.0, "end": 1.3, "text": "嗯"},
                {"start": 1.3, "end": 1.6, "text": "这个"},
                {"start": 1.6, "end": 2.6, "text": "继续"}]

    cleaned, report = cleaner.clean_segments(segments, fidelity="cleaned")

    assert report["dropped_fillers"] == 2
    assert [item["text"] for item in cleaned] == ["正文", "继续"]


# --- 保真度参数拼错必须失败 ---
def test_unknown_fidelity_is_rejected():
    cleaner = load_script("clean_transcript")

    with pytest.raises(ValueError, match="unknown fidelity"):
        cleaner.clean_segments([{"start": 0.0, "end": 1.0, "text": "甲"}], fidelity="polished")


# --- 短碎片合并真实生效（旧代码只算了 duration 没用） ---
def test_short_fragments_are_actually_merged():
    cleaner = load_script("clean_transcript")
    segments = [{"start": 0.0, "end": 0.3, "text": "前半"},
                {"start": 0.3, "end": 2.0, "text": "后半句"}]  # 0.3s 短于阈值，且无间隔

    cleaned, report = cleaner.clean_segments(segments, fidelity="verbatim")

    assert report["merged_fragments"] == 1
    assert [item["text"] for item in cleaned] == ["前半后半句"]
    assert cleaned[0]["end"] == pytest.approx(2.0)

    leading = [{"start": 0.0, "end": 0.3, "text": "碎片"},
               {"start": 0.35, "end": 3.0, "text": "正常句"}]
    merged, leading_report = cleaner.clean_segments(leading, fidelity="verbatim")
    assert leading_report["merged_fragments"] == 1  # 首段是碎片时并入下一段
    assert [item["text"] for item in merged] == ["碎片正常句"]
    assert merged[0]["start"] == pytest.approx(0.0)


# --- 切块脚本必须能吃 ASR 形状（过去 KeyError） ---
def test_chunking_accepts_asr_shaped_segments():
    chunk = load_script("chunk_transcript")
    segments = [{"from": 0.0, "to": 1.0, "content": "甲"},
                {"from": 400.0, "to": 401.0, "content": "乙"}]  # 跨过 300 秒分块阈值

    chunks = chunk.chunk_by_time(segments, 300)

    assert len(chunks) == 2
    assert "甲" in chunks[0]["text"] and "乙" in chunks[1]["text"]


# --- 切块产出能被笔记骨架消费 ---
def test_chunks_feed_the_note_builder():
    chunk = load_script("chunk_transcript")
    notes = load_script("build_notes")
    segments = [{"start": 0.0, "end": 1.0, "text": "甲"}, {"start": 400.0, "end": 401.0, "text": "乙"}]

    chunks = chunk.chunk_by_time(segments, 300)
    markdown = notes.build_notes({"title": "夹具", "platform": "douyin"}, chunks)

    assert "夹具" in markdown
    assert "甲" in markdown and "乙" in markdown


# --- 抖音结果现在带 end，且清洗报告随结果返回 ---
def test_douyin_transcribe_returns_canonical_segments(monkeypatch):
    douyin = load_script("douyin_extract")
    monkeypatch.setattr(
        douyin, "transcribe_audio_file",
        lambda *a, **k: {
            "engine": "faster-whisper", "device": "cpu", "compute_type": "int8",
            "segments": [{"from": 0.0, "to": 2.5, "content": "正文"}],
            "text": "正文", "diagnostics": [],
        },
    )

    segments, full_text, _ = douyin.transcribe("audio.wav")

    assert segments == [{"start": 0.0, "end": 2.5, "text": "正文"}]  # end 不再缺失
    assert full_text == "正文"


# --- --emit 参数校验 ---
def test_emit_formats_are_validated():
    douyin = load_script("douyin_extract")

    assert douyin.parse_emit_formats(None) == []
    assert douyin.parse_emit_formats("md, srt") == ["md", "srt"]
    with pytest.raises(ValueError, match="unsupported --emit"):
        douyin.parse_emit_formats("pdf")


# --- 多格式产出与完整转录的授权边界 ---
def test_emit_writes_requested_artifacts_and_guards_the_full_transcript(monkeypatch, tmp_path, capsys):
    douyin = load_script("douyin_extract")
    result = {
        "platform": "douyin",
        "metadata": {"title": "夹具/标题"},
        "segments": [{"start": 0.0, "end": 1.0, "text": "正文"}],
        "full_text": "正文",
        "diagnostics": [],
    }
    monkeypatch.setattr(douyin, "extract_douyin", lambda *a, **k: dict(result))

    blocked = douyin.main(["--json" if False else "-o", str(tmp_path), "7654321098765432100", "--emit", "srt"])
    assert blocked == douyin.EXIT_GENERIC_FAILURE  # 未授权时不得落完整转录
    assert "include-transcript" in capsys.readouterr().err

    written = douyin.main(["-o", str(tmp_path), "7654321098765432100", "--emit", "md,json,srt,txt",
                           "--include-transcript"])
    assert written == 0
    files = sorted(path.name for path in tmp_path.iterdir())
    assert files == ["夹具_标题.json", "夹具_标题.md", "夹具_标题.srt", "夹具_标题.txt"]  # 标题里的斜杠被消毒
    srt = (tmp_path / "夹具_标题.srt").read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,000" in srt


# --- --emit 与 --json 同时使用时，产物照写，stdout 必须仍是纯 JSON ---
def test_json_stdout_stays_parseable_next_to_emit(monkeypatch, tmp_path, capsys):
    douyin = load_script("douyin_extract")
    result = {
        "platform": "douyin",
        "metadata": {"title": "夹具标题"},
        "segments": [{"start": 0.0, "end": 1.0, "text": "正文"}],
        "full_text": "正文",
        "diagnostics": [],
    }
    monkeypatch.setattr(douyin, "extract_douyin", lambda *a, **k: dict(result))
    output_dir = tmp_path / "out"

    exit_code = douyin.main(["-o", str(output_dir), "7654321098765432100", "--emit", "md,json", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)                        # "Saved to:" 之类的通知一旦混进 stdout，这一行就会失败。
    assert payload["metadata"]["title"] == "夹具标题"
    assert "Saved to:" in captured.err                        # 落盘通知改走 stderr，JSON 契约不受影响。
    assert sorted(path.name for path in output_dir.iterdir()) == ["夹具标题.json", "夹具标题.md"]


# --- 缓存身份必须区分保真度 ---
def test_cache_identity_tracks_fidelity():
    douyin = load_script("douyin_extract")
    speech_to_text = load_script("speech_to_text")
    settings = speech_to_text.TranscriptionSettings()

    verbatim = douyin.build_asr_identity(settings, "verbatim")
    cleaned = douyin.build_asr_identity(settings, "cleaned")
    url = "https://v.douyin.com/example/"

    assert verbatim != cleaned
    assert douyin.build_cache_key(url, "small", "zh", "auto", "1080p", False, verbatim) != \
        douyin.build_cache_key(url, "small", "zh", "auto", "1080p", False, cleaned)


# --- 繁简归一：注入转换器，不依赖外部可选包 ---
def test_simplify_segments_uses_the_injected_converter():
    class FakeConverter:  # 只把繁体字替换成简体字，模拟 OpenCC 的行为
        def convert(self, text):
            return text.replace("價", "价").replace("豬", "猪").replace("裡", "里")

    segments = [{"start": 0.0, "end": 1.0, "text": "存在的價值"},
                {"start": 1.0, "end": 2.0, "text": "豬眷裡"}]

    simplified, report = nt.simplify_segments(segments, mode="on", converter=FakeConverter())

    assert [item["text"] for item in simplified] == ["存在的价值", "猪眷里"]
    assert report["applied"] is True
    assert report["changed_segments"] == 2


# --- 缺 OpenCC 时 auto 只记录不失败，on 必须显式失败 ---
def test_missing_simplifier_degrades_in_auto_mode_but_fails_when_required(monkeypatch):
    segments = [{"start": 0.0, "end": 1.0, "text": "存在的價值"}]
    monkeypatch.setattr(nt, "load_simplifier", lambda: None)  # 模拟没装 OpenCC

    kept, report = nt.simplify_segments(segments, mode="auto")
    assert [item["text"] for item in kept] == ["存在的價值"]  # 原文不动
    assert report["applied"] is False
    assert "OpenCC not installed" in report["reason"]

    with pytest.raises(RuntimeError, match="OpenCC not installed"):
        nt.simplify_segments(segments, mode="on")


# --- 关闭归一化时不做任何转换 ---
def test_simplify_can_be_disabled():
    segments = [{"start": 0.0, "end": 1.0, "text": "存在的價值"}]

    kept, report = nt.simplify_segments(segments, mode="off", converter=object())

    assert kept[0]["text"] == "存在的價值"
    assert report["applied"] is False


# --- 停顿标点只在段边界插入，绝不改动段内文字 ---
def test_pause_punctuation_only_touches_segment_boundaries():
    segments = [
        {"start": 0.0, "end": 2.0, "text": "第一句"},
        {"start": 2.1, "end": 4.0, "text": "紧接着的第二句"},   # 间隔 0.1s -> 逗号
        {"start": 5.0, "end": 7.0, "text": "另起一句"},         # 间隔 1.0s -> 句号
        {"start": 7.1, "end": 9.0, "text": "结束"},
    ]

    text = nt.join_with_pause_punctuation(segments)

    assert text == "第一句，紧接着的第二句。另起一句，结束"
    for item in segments:
        assert item["text"] in text  # 段内文字原样保留，可回溯


# --- 已有标点不重复添加 ---
def test_pause_punctuation_does_not_duplicate_existing_marks():
    segments = [{"start": 0.0, "end": 1.0, "text": "已经有句号了。"},
                {"start": 2.0, "end": 3.0, "text": "下一句"}]

    assert nt.join_with_pause_punctuation(segments) == "已经有句号了。下一句"


# --- 简体与归一模式都必须进缓存身份 ---
def test_cache_identity_tracks_simplification_mode():
    douyin = load_script("douyin_extract")
    speech_to_text = load_script("speech_to_text")
    settings = speech_to_text.TranscriptionSettings()

    assert douyin.build_asr_identity(settings, "verbatim", "off") != \
        douyin.build_asr_identity(settings, "verbatim", "auto")


# --- 归一报告要能变成诊断，且 ok 表示"真的改了字"（D49）---
def test_simplify_diagnostic_reports_real_effect():
    applied = nt.simplify_diagnostic({"mode": "auto", "applied": True, "changed_segments": 7})
    degraded = nt.simplify_diagnostic({"mode": "auto", "applied": False,
                                       "reason": "OpenCC not installed"})
    disabled = nt.simplify_diagnostic({"mode": "off", "applied": False})

    assert applied["ok"] is True and "已生效" in applied["message"] and applied["changed_segments"] == 7
    assert degraded["ok"] is False and "降级" in degraded["message"] and "OpenCC" in degraded["message"]
    assert disabled["ok"] is False and "未启用" in disabled["message"]
    assert applied["step"] == "simplify"


# --- 基线改善可一键落库（显式开关；CI 不应使用）---
def test_benchmark_write_baseline_updates_file(tmp_path, capsys):
    import importlib.util
    from pathlib import Path as _Path

    skill_root = _Path(nt.__file__).parent.parent
    scripts = skill_root / "scripts"
    spec = importlib.util.spec_from_file_location("video_learning_test_bench_write", scripts / "run_benchmark.py")
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)

    gold = skill_root / "eval/gold/bilibili-BV1ntah6TEe9.json"
    hypothesis = skill_root / "agent-harness/cli_anything/video_learning/tests/fixtures/bili1_large.json"
    baseline_path = tmp_path / "baselines.json"
    baseline_path.write_text(json.dumps({"cases": [{"case": "bili1-large", "cer_by_mode": {}, "tolerance": 0.005}]}),
                             encoding="utf-8")

    code = benchmark.main(["--case", f"bili1-large={hypothesis}:{gold}",
                           "--baseline", str(baseline_path), "--write-baseline"])

    written = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert code == 0
    assert written["cases"][0]["cer_by_mode"]                       # 本次结果已落库
    assert "WROTE baseline" in capsys.readouterr().out
