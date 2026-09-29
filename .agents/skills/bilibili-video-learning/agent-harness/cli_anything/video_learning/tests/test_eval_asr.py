"""
转写评测脚本的离线测试（对应 docs/DECISIONS.md D25）。

守住的是"指标本身必须可信"：CER 的分母与插入口径、幻觉的最短片段、覆盖率的重叠去重、
时间轴的真对齐（而不是段边界差）。指标算错比没有指标更危险——它会把改进误报成退步。
运行示例：python -m pytest cli_anything/video_learning/tests/test_eval_asr.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill。
import json  # 构造临时金标与假设文件。
import sys  # 把 scripts 目录加入模块搜索路径。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

import pytest  # 提供临时目录与断言辅助。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_asr  # noqa: E402 与被测脚本共用同一实例。


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


# --- 造一份最小金标 ---
def write_gold(tmp_path: Path, text: str, duration: float = 60.0, paragraphs=None) -> Path:
    payload = {
        "id": "gold-fixture", "platform": "douyin",
        "media": {"duration_seconds": duration},
        "reference": {"text": text, "paragraphs": paragraphs or [{"start": 0.0, "end": duration, "text": text}]},
    }
    target = tmp_path / "gold.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


# --- 规范化：只留下会被校对的字符 ---
def test_normalize_text_strips_punctuation_and_width():
    assert eval_asr.normalize_text("中国男性，是　最好的！") == "中国男性是最好的"
    assert eval_asr.normalize_text("ＡＢＣ１２３") == "ABC123"


# --- CER：替换/删除/插入三个方向都要计入分子 ---
def test_character_error_rate_counts_every_direction():
    assert eval_asr.character_error_rate("甲乙丙", "甲乙丙")["cer"] == 0.0
    substitution = eval_asr.character_error_rate("甲乙丁", "甲乙丙")
    assert substitution["substitutions"] == 1 and substitution["cer"] == pytest.approx(1 / 3, abs=1e-4)
    deletion = eval_asr.character_error_rate("甲乙", "甲乙丙")
    assert deletion["deletions"] == 1
    insertion = eval_asr.character_error_rate("甲乙丙丁", "甲乙丙")
    assert insertion["insertions"] == 1
    assert insertion["cer"] == pytest.approx(1 / 3, abs=1e-4)  # 多说话也要罚，否则鼓励编造


# --- 幻觉：只有达到最短长度的新增片段才算 ---
def test_hallucination_requires_minimum_run_length():
    report = eval_asr.hallucination_rate("甲乙丙丁戊己", "甲乙", 60.0, min_run=2)
    assert report["hypothesis_chars"] >= 4
    assert report["chars_per_minute"] is not None
    short = eval_asr.hallucination_rate("甲乙丙", "甲乙", 60.0, min_run=2)
    assert short["hypothesis_chars"] == 0  # 单字多出不进幻觉统计，但仍由 CER 罚（口径分工明确）
    assert eval_asr.character_error_rate("甲乙丙", "甲乙")["insertions"] == 1


# --- 覆盖率：重叠区间只能算一次，且要按参考时长截断 ---
def test_timeline_coverage_deduplicates_overlaps():
    segments = [{"start": 0.0, "end": 10.0, "text": "a"}, {"start": 5.0, "end": 20.0, "text": "b"},
                {"start": 20.0, "end": 90.0, "text": "c"}]
    report = eval_asr.timeline_coverage(segments, 60.0)

    assert report["covered_seconds"] == 60.0  # 0-90 并集按 60s 参考时长截断；重叠的 5-20 只算一次
    assert report["coverage"] == 1.0


# --- 时间轴：内容相同但整体后移时，偏移量必须被正确报出 ---
def test_timeline_offset_detects_a_shifted_but_identical_paragraph():
    hypothesis = [{"start": 30.0, "end": 40.0, "text": "中国男性是天底下最好的血包"}]
    reference = [{"start": 28.0, "end": 38.0, "text": "中国男性是天底下最好的血包"}]
    report = eval_asr.timeline_offset(hypothesis, reference)

    assert report["matched"] == 1
    assert report["median_seconds"] == pytest.approx(2.0, abs=0.1)


# --- 时间轴：完全没被转写的内容不应算作"匹配" ---
def test_timeline_offset_skips_untranscribed_reference():
    hypothesis = [{"start": 0.0, "end": 5.0, "text": "完全无关的内容"}]
    reference = [{"start": 50.0, "end": 60.0, "text": "这一段在假设里根本不存在"}]

    assert eval_asr.timeline_offset(hypothesis, reference)["matched"] == 0


# --- 字符时间映射必须单调且与字符数一致 ---
def test_character_timeline_is_monotonic():
    text, times = eval_asr.build_character_timeline([{"start": 0.0, "end": 10.0, "text": "甲乙丙丁"}])

    assert len(text) == len(times) == 4
    assert times == sorted(times)
    assert times[0] == 0.0 and times[-1] == 10.0


# --- 三种历史产出格式都要能读（规范 JSON / ASR JSON / SRT / TXT）---
def test_load_segments_supports_every_produced_shape(tmp_path):
    canonical = tmp_path / "canonical.json"
    canonical.write_text(json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "甲"}]}), encoding="utf-8")
    asr_shaped = tmp_path / "asr.json"
    asr_shaped.write_text(json.dumps({"segments": [{"from": 1.0, "to": 2.0, "content": "乙"}]}), encoding="utf-8")
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,500\n丙\n", encoding="utf-8")
    text = tmp_path / "a.txt"
    text.write_text("丁", encoding="utf-8")

    assert eval_asr.load_segments(canonical)[0]["text"] == "甲"
    assert eval_asr.load_segments(asr_shaped)[0]["text"] == "乙"
    srt_segment = eval_asr.load_segments(srt)[0]
    assert srt_segment["start"] == 1.0 and srt_segment["end"] == 2.5
    assert eval_asr.load_segments(text)[0]["text"] == "丁"


# --- 端到端报告：字段齐全、可渲染、能判定验收线 ---
def test_evaluate_reports_all_metrics_and_renders(tmp_path):
    gold = write_gold(tmp_path, "中国男性是最好的血包", duration=60.0)
    hypothesis = tmp_path / "hyp.json"
    hypothesis.write_text(json.dumps({"segments": [
        {"start": 0.0, "end": 30.0, "text": "中国男性是最好的雪包"},
        {"start": 30.0, "end": 60.0, "text": "多余的编造内容"},
    ]}, ensure_ascii=False), encoding="utf-8")

    report = eval_asr.evaluate(hypothesis, gold, elapsed_seconds=6.0)

    assert report["character_error_rate"]["cer"] > 0
    assert report["coverage"]["coverage"] == 1.0
    assert report["real_time_factor"] == pytest.approx(0.1, abs=1e-3)
    assert report["passed"] is False
    assert "CER" in eval_asr.render_markdown(report)


# --- 批量模式按金标 id 匹配文件 ---
def test_evaluate_directory_matches_by_gold_id(tmp_path):
    gold_dir = tmp_path / "gold"
    gold_dir.mkdir()
    (gold_dir / "douyin-1.json").write_text(json.dumps({
        "id": "douyin-1", "platform": "douyin", "media": {"duration_seconds": 10.0},
        "reference": {"text": "甲乙丙丁", "paragraphs": [{"start": 0.0, "end": 10.0, "text": "甲乙丙丁"}]},
    }, ensure_ascii=False), encoding="utf-8")
    hypothesis_dir = tmp_path / "out"
    hypothesis_dir.mkdir()
    (hypothesis_dir / "douyin-1-asr.json").write_text(json.dumps({
        "segments": [{"start": 0.0, "end": 10.0, "text": "甲乙丙丁"}]}, ensure_ascii=False), encoding="utf-8")

    reports = eval_asr.evaluate_directory(hypothesis_dir, gold_dir)

    assert len(reports) == 1 and reports[0]["character_error_rate"]["cer"] == 0.0


# --- 真实金标必须在仓库里，且只含文本（仓库禁止媒体入库）---
def test_shipped_gold_set_is_text_only():
    gold_files = sorted((SKILL_ROOT / "eval" / "gold").glob("*.json"))

    assert gold_files, "金标集不应为空：没有金标就无法证明任何改进"
    for path in gold_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["reference"]["text"].strip()
        assert payload["media"]["url"].startswith("http")
        assert not list(path.parent.glob("*.wav")) and not list(path.parent.glob("*.mp4"))
