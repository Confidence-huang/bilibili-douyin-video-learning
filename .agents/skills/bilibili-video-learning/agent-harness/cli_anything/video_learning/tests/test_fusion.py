"""
多源融合与 B站「字幕优先」入口的离线测试。

这些测试守住三件事：
1. 融合是**逐段决定**的，每一段都必须带 provenance，且主源与次源的并集一个字都不能丢；
2. 字幕优先路径必须真的跳过 ASR（否则"省下整段 GPU 时间"只是文档里的说法）；
3. 既有返回字段与退出码契约不被破坏。

运行示例：python -m pytest cli_anything/video_learning/tests/test_fusion.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 构造与校验时间轴 JSON。
import re  # 并集不变量要按"有效字符"比较，与实现同一口径。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录与真实素材。

import pytest  # 提供 monkeypatch 与 tmp_path。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
ASR_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "asr_vad_dropped_speech.json"  # 真实 180 段 ASR。
# 真实硬字幕卡片（217 条 cue）随仓库分发：文本可以入库，媒体不行；
# 用规范 JSON 而不是 SRT，既避开 .gitignore 的 *.srt 规则，也与仓库的规范形状一致（D25 的同一条原则）。
DOUYIN_CAPTIONS = SKILL_ROOT / "agent-harness" / "cli_anything" / "video_learning" / "tests" / "fixtures" / "douyin_caption_cards.json"
SIGNIFICANT = re.compile(r"[^\W_\s]", re.UNICODE)  # 与 FUSION_SIGNIFICANT_PATTERN 同一口径。

if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"  # 测试名直接映射真实脚本文件。
    module_name = f"video_learning_fusion_test_{name}"  # 独立模块名避免测试之间污染缓存。
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:  # 文件无法加载时给出明确的测试失败原因。
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 把次级来源写成 ASR 形状，验证适配层真的在工作 ---
def asr_shape(segments: list[dict]) -> list[dict]:
    return [{"from": item["start"], "to": item["end"], "content": item["text"]} for item in segments]


# --- 有效字符集合（去掉标点与空白），用于并集不变量 ---
def significant(text: str) -> str:
    return "".join(character for character in text if SIGNIFICANT.fullmatch(character))


# --- 融合稿交付出去的全部有效字符：正文 + 逐段 alternatives ---
def fused_character_set(report: dict) -> set:
    covered = set()
    for segment in report["segments"]:
        covered.update(significant(segment["text"]))
        for value in (segment.get("alternatives") or {}).values():
            covered.update(significant(str(value)))
    return covered


# --- 三句真实口播素材：两份来源的公共部分 ---
def base_timeline() -> list[dict]:
    return [
        {"start": 0.0, "end": 2.0, "text": "中国男性是天底下最好的血包"},
        {"start": 2.0, "end": 4.0, "text": "被人压榨"},
        {"start": 4.0, "end": 6.0, "text": "被人剥削"},
    ]


# --- 完全一致时全部采用主源，且没有需要复核的段 ---
def test_identical_sources_are_all_primary():
    verify = load_script("verify_transcript")

    report = verify.fuse_transcripts(base_timeline(), asr_shape(base_timeline()))

    assert report["similarity"] == 1.0
    assert report["spans"] == []
    assert report["needs_review_count"] == 0
    assert report["provenance_counts"] == {"subtitle": 3, "asr": 0, "mixed": 0}
    assert {segment["provenance"] for segment in report["segments"]} == {"subtitle"}
    assert all(segment["text"] for segment in report["segments"])  # 不产出空段


# --- 次源多出一句（字幕漏掉的短卡片）必须被补上，并标成次源 ---
def test_secondary_only_segment_is_carried_in_as_secondary():
    verify = load_script("verify_transcript")
    secondary = asr_shape(base_timeline())
    secondary.insert(2, {"from": 1.9, "to": 2.0, "content": "更高级"})  # 短卡片只在 ASR 里出现

    report = verify.fuse_transcripts(base_timeline(), secondary)

    carried = [segment for segment in report["segments"] if segment["provenance"] == "asr"]
    assert len(carried) == 1
    assert carried[0]["text"] == "更高级"
    assert carried[0]["start"] == pytest.approx(1.9)
    assert report["covered"]["secondary_only_spans"] == 1
    assert report["provenance_counts"]["asr"] == 1


# --- 只有主源有的一段必须保留，且不能被降级成次源 ---
def test_primary_only_segment_is_kept_as_primary():
    verify = load_script("verify_transcript")
    primary = [
        {"start": 0.0, "end": 2.0, "text": "中国男性是天底下最好的血包"},
        {"start": 2.0, "end": 4.0, "text": "这段只有字幕里有"},
        {"start": 6.0, "end": 8.0, "text": "被人剥削"},
    ]

    report = verify.fuse_transcripts(primary, asr_shape([primary[0], primary[2]]))

    kept = [segment for segment in report["segments"] if segment["text"] == "这段只有字幕里有"]
    assert len(kept) == 1
    assert kept[0]["provenance"] == "subtitle"
    assert kept[0]["start"] == pytest.approx(2.0)
    assert report["needs_review_count"] == 0  # 主源独有不是冲突，不需要人工判断


# --- replace：保留主源文本、记录次源写法、要求复核 ---
def test_replacement_keeps_primary_text_and_records_alternatives():
    verify = load_script("verify_transcript")
    secondary = asr_shape([{**segment, "text": segment["text"].replace("血包", "雪包")}
                           for segment in base_timeline()])

    report = verify.fuse_transcripts(base_timeline(), secondary)

    conflict = next(segment for segment in report["segments"] if segment["provenance"] == "mixed")
    assert conflict["text"] == "中国男性是天底下最好的血包"  # 文本取主源，绝不静默改成次源写法
    assert conflict["alternatives"] == {"asr": "雪"}
    assert conflict["needs_review"] is True
    assert report["needs_review_count"] == 1
    assert report["provenance_counts"]["mixed"] == 1


# --- 硬不变量：融合结果的字符集合必须覆盖两份来源的并集（除标点/空白） ---
def test_fused_text_covers_the_union_of_both_sources():
    verify = load_script("verify_transcript")
    primary = [
        {"start": 0.0, "end": 2.0, "text": "中国男性是天底下最好的血包"},
        {"start": 2.0, "end": 4.0, "text": "只有主源有的正文"},
    ]
    secondary = asr_shape([
        {"start": 0.0, "end": 2.0, "text": "中国男性是天底下最好的雪包"},
        {"start": 4.0, "end": 5.0, "text": "更高级"},
    ])

    report = verify.fuse_transcripts(primary, secondary)

    # 融合稿正文 + 逐段 alternatives 一起看：两份来源里出现过的每个有效字符都必须找得到
    covered = fused_character_set(report)
    for source in (primary, secondary):
        expected = set(significant("".join(item.get("text") or item.get("content") or "" for item in source)))
        assert not (expected - covered), f"融合稿漏掉了来源正文: {sorted(expected - covered)}"
    # 主源独有的整段必须真的落进交付结果；次源那一段的字符要么进正文，要么挂在 alternatives 上
    materials = "".join(segment["text"] for segment in report["segments"])
    for alternatives in (segment.get("alternatives") or {} for segment in report["segments"]):
        materials += "".join(alternatives.values())
    assert "只有主源有的正文" in materials
    assert "更高级" in materials


# --- provenance_counts 与 needs_review_count 的数值必须和逐段结果一致 ---
def test_counts_match_the_fused_segments():
    verify = load_script("verify_transcript")
    primary = base_timeline()
    secondary = asr_shape([
        base_timeline()[0],
        {"start": 1.9, "end": 2.0, "text": "更高级"},  # 次源补上的短卡片
        {"start": 2.0, "end": 4.0, "text": "被人压榨"},  # 一致
        {"start": 4.0, "end": 6.0, "text": "被人剥雪"},  # 一个字的冲突
    ])

    report = verify.fuse_transcripts(primary, secondary)

    counted = {
        "subtitle": sum(1 for segment in report["segments"] if segment["provenance"] == "subtitle"),
        "asr": sum(1 for segment in report["segments"] if segment["provenance"] == "asr"),
        "mixed": sum(1 for segment in report["segments"] if segment["provenance"] == "mixed"),
    }
    assert report["provenance_counts"] == counted
    assert report["needs_review_count"] == sum(1 for segment in report["segments"] if segment.get("needs_review"))
    assert report["needs_review_count"] == 1
    assert sum(counted.values()) == report["segment_count"] == len(report["segments"])


# --- 每一段都必须带 start/end/text/provenance，且按时间排序 ---
def test_every_fused_segment_carries_provenance_and_timing():
    verify = load_script("verify_transcript")

    report = verify.fuse_transcripts(base_timeline(), asr_shape(base_timeline()))
    starts = [segment["start"] for segment in report["segments"]]

    assert starts == sorted(starts)
    for segment in report["segments"]:
        assert {"start", "end", "text", "provenance"} <= set(segment)
        assert segment["end"] >= segment["start"]
        assert segment["text"].strip()


# --- 公开接口要接受两种历史分段形状（规范起点/终点/正文 与 ASR 起点/终点/内容） ---
def test_fusion_accepts_both_segment_schemas():
    verify = load_script("verify_transcript")

    canonical = verify.fuse_transcripts(base_timeline(), asr_shape(base_timeline()))
    asr_first = verify.fuse_transcripts(asr_shape(base_timeline()),
                                        [{"start": s["start"], "end": s["end"], "text": s["text"]}
                                         for s in base_timeline()])

    assert canonical["similarity"] == asr_first["similarity"] == 1.0


# --- --fuse 与既有 --fail-on-difference 并存，报告仍只打一份 JSON ---
def test_cli_fuse_writes_one_json_and_keeps_the_exit_code_contract(tmp_path, capsys):
    verify = load_script("verify_transcript")
    primary = tmp_path / "captions.json"
    secondary = tmp_path / "asr.json"
    output = tmp_path / "fused.json"
    primary.write_text(json.dumps(base_timeline()), encoding="utf-8")
    secondary.write_text(json.dumps(asr_shape([{**s, "text": s["text"].replace("血包", "雪包")}
                                               for s in base_timeline()])), encoding="utf-8")

    exit_code = verify.main(["--primary", str(primary), "--secondary", str(secondary),
                             "--fuse", "--json", "--fail-on-difference"])

    assert exit_code == 25  # 两份来源仍有差异：退出码契约不变
    payload = json.loads(capsys.readouterr().out)  # stdout 必须仍是一份可解析的 JSON
    assert payload["spans"] and payload["segments"]
    assert payload["sources"] == {"primary": "subtitle", "secondary": "asr"}

    assert verify.main(["--primary", str(primary), "--secondary", str(secondary),
                        "--fuse", "-o", str(output)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["needs_review_count"] == 1
    assert written["segments"][0]["provenance"] == "mixed"


# --- 既有对比/报告功能不能被融合改动破坏 ---
def test_existing_compare_and_report_still_work():
    verify = load_script("verify_transcript")

    report = verify.compare_timelines([{"start": 0.0, "end": 1.0, "text": "血包"}],
                                      [{"from": 0.0, "to": 1.0, "content": "雪包"}])

    assert report["summary"]["replacement_spans"] == 1
    assert "| 类型 | 时间范围 | 主源 | 次源 |" in verify.render_report(report)


# --- B站字幕优先：有字幕轨时不下载音频、不调用 ASR ---
def test_bilibili_prefers_subtitles_and_skips_asr(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    calls = {"download": 0, "asr": 0}

    monkeypatch.setattr(transcribe, "_fetch_subtitle_tracks",
                        lambda bvid, cookies: ([{"lan": "zh-CN", "lan_doc": "中文（简体）", "is_ai": False,
                                                "content": asr_shape(base_timeline())}], {"title": "fixture"}))
    monkeypatch.setattr(transcribe, "download_audio",
                        lambda *a, **k: (calls.__setitem__("download", calls["download"] + 1), (None, "should not run"))[1])
    monkeypatch.setattr(transcribe, "transcribe_audio",
                        lambda *a, **k: (calls.__setitem__("asr", calls["asr"] + 1), {"segments": []})[1])

    result = transcribe.bilibili_transcribe("BV1xx411c7mD", output_dir=str(tmp_path))

    assert result["status"] == "ok"
    assert result["source"] == "subtitle"
    assert result["exit_code"] == 0
    assert calls == {"download": 0, "asr": 0}  # 字幕优先的核心收益：整段 ASR 时间都省下来
    assert len(result["segments"]) == 3
    assert {segment["provenance"] for segment in result["segments"]} == {"subtitle"}
    assert any("asr" in item for item in result["skipped"])


# --- B站无字幕轨：回退到既有 ASR 路径，status 仍为 ok ---
def test_bilibili_without_subtitles_falls_back_to_asr(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"RIFFfixture")

    monkeypatch.setattr(transcribe, "_fetch_subtitle_tracks", lambda bvid, cookies: ([], {}))
    monkeypatch.setattr(transcribe, "download_audio", lambda bvid, output_dir, cookies=None: (str(audio), None))
    monkeypatch.setattr(transcribe, "transcribe_audio", lambda *a, **k: {
        "segments": asr_shape(base_timeline()), "engine": "faster-whisper", "device": "cpu",
        "compute_type": "int8", "coverage_before": 0.8, "coverage_after": 0.99, "diagnostics": [],
    })

    result = transcribe.bilibili_transcribe("BV1ntah6TEe9", output_dir=str(tmp_path))

    assert result["status"] == "ok"
    assert result["source"] == "asr"
    assert result["segment_count"] == 3
    assert result["coverage_after"] == 0.99  # 既有覆盖率字段仍在
    assert {segment["provenance"] for segment in result["segments"]} == {"asr"}


# --- B站 --fuse：即使有字幕也跑 ASR，并给出逐段 provenance ---
def test_bilibili_fuse_runs_asr_even_with_subtitles(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"RIFFfixture")
    calls = {"asr": 0}
    secondary = asr_shape([{**s, "text": s["text"].replace("血包", "雪包")} for s in base_timeline()])

    monkeypatch.setattr(transcribe, "_fetch_subtitle_tracks",
                        lambda bvid, cookies: ([{"lan": "zh-CN", "is_ai": False,
                                                "content": asr_shape(base_timeline())}], {}))
    monkeypatch.setattr(transcribe, "download_audio", lambda bvid, output_dir, cookies=None: (str(audio), None))

    def fake_asr(*args, **kwargs):
        calls["asr"] += 1
        return {"segments": secondary, "engine": "faster-whisper", "device": "cpu", "diagnostics": []}

    monkeypatch.setattr(transcribe, "transcribe_audio", fake_asr)

    result = transcribe.bilibili_transcribe("BV1xx411c7mD", output_dir=str(tmp_path), fuse=True)

    assert calls["asr"] == 1
    assert result["status"] == "ok"
    assert result["source"] == "fused"
    assert result["provenance_counts"]["mixed"] == 1
    assert result["needs_review_count"] == 1
    assert any(segment.get("alternatives") == {"asr": "雪"} for segment in result["segments"])


# --- B站 --no-prefer-subtitles：有字幕也走 ASR，行为与旧版一致 ---
def test_bilibili_no_prefer_subtitles_uses_asr_path(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"RIFFfixture")

    monkeypatch.setattr(transcribe, "_fetch_subtitle_tracks",
                        lambda bvid, cookies: ([{"lan": "zh-CN", "is_ai": False,
                                                "content": asr_shape(base_timeline())}], {}))
    monkeypatch.setattr(transcribe, "download_audio", lambda bvid, output_dir, cookies=None: (str(audio), None))
    monkeypatch.setattr(transcribe, "transcribe_audio", lambda *a, **k: {
        "segments": asr_shape(base_timeline()), "engine": "faster-whisper", "device": "cpu", "diagnostics": [],
    })

    result = transcribe.bilibili_transcribe("BV1xx411c7mD", output_dir=str(tmp_path), prefer_subtitles=False)

    assert result["source"] == "asr"
    assert not audio.exists()  # 既有清理行为不变


# --- 真实素材：抖音硬字幕为主源、真实 ASR 为次源融合一次 ---
@pytest.mark.skipif(not (DOUYIN_CAPTIONS.is_file() and ASR_FIXTURE.is_file()),
                    reason="需要真实抖音硬字幕与 ASR fixture（随仓库分发）")
def test_real_douyin_caption_and_asr_fuse_without_losing_content():
    verify = load_script("verify_transcript")
    captions = json.loads(DOUYIN_CAPTIONS.read_text(encoding="utf-8"))["segments"]  # 217 条真实硬字幕卡片
    asr_segments = json.loads(ASR_FIXTURE.read_text(encoding="utf-8"))["segments"]  # 真实 180 段 ASR

    report = verify.fuse_transcripts(captions, asr_segments)

    assert report["segment_count"] > 0
    assert report["similarity"] > 0.9  # 同一段音频的两个独立来源
    assert report["covered"]["secondary_only_spans"] > 0  # 真实素材里确实有字幕漏掉的正文
    covered = fused_character_set(report)
    for source in (captions, asr_segments):
        expected = set(significant("".join(item.get("text") or item.get("content") or ""
                                           for item in source)))
        assert not (expected - covered), f"真实素材融合后漏字: {sorted(expected - covered)}"


# ============================ 冲突裁决表与复核清单（D35） ============================

verify_transcript = load_script("verify_transcript")   # 本文件其余用例是函数内取用；这组用例需要模块级句柄

# --- 逐位字符混淆：等长表项必须能被拆成位对位比较（difflib 的 opcode 是字符级）---
def test_character_confusions_split_equal_length_entries():
    confusions = verify_transcript.character_confusions({"雪包": "血包", "赠养": "赡养", "长短不一": "短"})

    assert confusions["雪"] == "血" and confusions["赠"] == "赡"
    assert "包" not in confusions            # 相同的位不产生"混淆"
    assert "长" not in confusions            # 长度不等的表项不参与逐位比较


# --- 裁决方向：主源写错 → 采信次源；次源写错 → 维持主源；都不命中 → 维持主源 ---
def test_prefer_side_both_directions():
    table = {"赠养": "赡养", "雪包": "血包"}

    chosen, primary_wrong, secondary_wrong = verify_transcript.prefer_side("赠", "赡", table)
    assert chosen == "secondary" and primary_wrong == "赠"

    chosen, primary_wrong, secondary_wrong = verify_transcript.prefer_side("血", "雪", table)
    assert chosen == "primary" and secondary_wrong == "雪"

    chosen, _, _ = verify_transcript.prefer_side("甲", "乙", table)
    assert chosen == "primary"


# --- 冲突规模：replace 数"有几位不同"，长度不同取较长者 ---
def test_conflict_size_counts_differing_positions():
    assert verify_transcript._conflict_size("血包", "雪包") == 1
    assert verify_transcript._conflict_size("从小到大", "创造了") == 4
    assert verify_transcript._conflict_size("", "补上的三个字") == 6   # 长度不同时取较长者


# --- 清单只列实质冲突：多字差异或单字实词；纯虚词差异不算 ---
def test_span_substantive_ignores_function_words():
    assert verify_transcript._span_is_substantive({"primary_text": "血", "secondary_text": "雪"}) is True
    assert verify_transcript._span_is_substantive({"primary_text": "从小到大", "secondary_text": "创造了"}) is True
    assert verify_transcript._span_is_substantive({"primary_text": "的", "secondary_text": "得"}) is False


# --- 融合报告必须给出排序后的短清单，同时保留全量 spans（不隐藏冲突）---
def test_fusion_reports_ranked_short_list():
    primary = [{"start": 0.0, "end": 2.0, "text": "中国男性是最好的血包"},
               {"start": 2.0, "end": 4.0, "text": "他觉得很欣慰"}]
    secondary = [{"start": 0.0, "end": 2.0, "text": "中国男性是最好的雪包"},
                 {"start": 2.0, "end": 4.0, "text": "他觉得很欣蔚"}]

    report = verify_transcript.fuse_transcripts(primary, secondary)

    assert report["needs_review_total"] >= 1
    assert report["substantive_conflicts"] >= 1
    assert len(report["needs_review_top"]) <= 30
    assert report["spans"]                       # 全量差异清单仍然在，短清单只是"先看哪些"
    assert all(item["difference_chars"] >= 1 for item in report["needs_review_top"])


# --- 裁决表命中要留痕：说明为什么改判/为什么维持 ---
def test_preference_hits_are_recorded():
    primary = [{"start": 0.0, "end": 2.0, "text": "赠养家人"}]      # 主源（OCR）把"赡"认成"赠"
    secondary = [{"start": 0.0, "end": 2.0, "text": "赡养家人"}]     # 次源（ASR）是对的

    report = verify_transcript.fuse_transcripts(primary, secondary, preferences={"赠养": "赡养"})


    assert report["preference_hits"] and report["preference_hits"][0]["action"] == "chose_secondary"
    assert report["preference_hits"][0]["wrong_form"] == "赠"


# --- 随机仓库里自带的裁决表必须可解析（否则融合会静默失去第三判据）---
def test_shipped_conflict_preferences_are_usable():
    table = verify_transcript.load_conflict_preferences()

    assert table, "references/conflict-preferences.txt 不应为空"
    confusions = verify_transcript.character_confusions(table)
    assert confusions.get("雪") == "血" and confusions.get("赠") == "赡"
