"""
半自动金标工具的离线测试（对应 docs/DECISIONS.md D33）。

守住两件事：① 只有**该被人工看的段落**才被标记（否则清单又会变长到没人看）；
② 产物必须**明确标注自己是草稿**，不能被误当成验收基准。
运行示例：python -m pytest cli_anything/video_learning/tests/test_eval_gold_draft.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill
import json  # 造临时输入与解析输出
import sys  # 把 scripts 目录加入搜索路径
from pathlib import Path  # 稳定定位 Skill 根目录

import pytest  # 断言辅助


SKILL_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_gold_draft  # noqa: E402 被测模块


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def paragraph(start, end, text, confidence=-0.2, ratio=1.1):
    return {"start": start, "end": end, "text": text, "confidence": confidence, "compression_ratio": ratio}


# ============================ 标记逻辑 ============================

# --- 三种可疑形态都要被标记，正常段落不能进清单 ---
def test_flag_paragraphs_covers_all_suspicious_shapes():
    paragraphs = [paragraph(0, 5, "正常"),
                  paragraph(5, 10, "低置信", confidence=-1.5),
                  paragraph(10, 15, "复读", ratio=3.0),
                  paragraph(15, 20, "分歧")]

    flags = eval_gold_draft.flag_paragraphs(paragraphs, disagreement_ranges=[{"start": 16, "end": 18}])

    reasons = {item["index"]: item["reasons"] for item in flags}
    assert reasons[1] == ["low_confidence"]
    assert reasons[2] == ["high_compression_ratio"]
    assert reasons[3] == ["disagreement_with_secondary"]
    assert 0 not in reasons                      # 正常段落不进清单，否则清单会失去意义


# --- 全片置信度都高于阈值时，退回"最低的十分位"，而不是一个人都不标 ---
def test_flag_paragraphs_falls_back_to_lowest_decile():
    paragraphs = [paragraph(index * 5, index * 5 + 5, f"段{index}", confidence=-0.1 - index * 0.01)
                  for index in range(10)]

    flags = eval_gold_draft.flag_paragraphs(paragraphs)

    assert flags and all(item["reasons"] == ["lowest_confidence_decile"] for item in flags)


# --- 边界相接也算重叠：分歧区间与段落首尾相接时不能漏标 ---
def test_overlap_counts_touching_ranges():
    assert eval_gold_draft._overlaps_any(paragraph(10, 20, "x"), [{"start": 20, "end": 25}]) is True
    assert eval_gold_draft._overlaps_any(paragraph(10, 20, "x"), [{"start": 21, "end": 25}]) is False


# ============================ 产物形状 ============================

# --- 草稿必须与正式金标同形状，且**明确标注是草稿** ---
def test_build_draft_marks_itself_as_draft():
    draft = eval_gold_draft.build_draft([paragraph(0, 5, "正文")], gold_id="bilibili-BV1", platform="bilibili",
                                        media_url="https://example", duration=5.0, flags=[{"index": 0}],
                                        report={"similarity": 0.9, "spans": [{"type": "replace"}]},
                                        primary_label="large-v3", secondary_label="small")

    assert draft["reference"]["kind"] == "semi-automatic-draft"     # 不能被当成真值
    assert draft["reference"]["char_count"] == 2 and draft["reference"]["text"] == "正文"
    assert draft["media"]["url"] == "https://example" and draft["media"]["duration_seconds"] == 5.0
    assert draft["review"]["flagged_count"] == 1 and draft["review"]["similarity_with_secondary"] == 0.9
    assert "large-v3" in draft["reference"]["provenance"] and "small" in draft["reference"]["provenance"]


# --- 没有第二配置时也要能产出（只是没有分歧清单）---
def test_build_draft_without_secondary_is_tolerated():
    draft = eval_gold_draft.build_draft([paragraph(0, 5, "正文")], gold_id="x", platform="bilibili", media_url="",
                                        duration=None, flags=[], report=None, primary_label="solo",
                                        secondary_label=None)

    assert draft["review"]["disagreement_spans"] == []
    assert draft["review"]["similarity_with_secondary"] is None
    assert "未提供" in draft["reference"]["provenance"]


# ============================ 核对清单渲染 ============================

# --- 清单要含：段落计数、⚠ 标记、分歧表、完成步骤 ---
def test_render_worksheet_contains_flags_spans_and_checklist():
    draft = eval_gold_draft.build_draft(
        [paragraph(0, 5, "正常段"), paragraph(5, 10, "可疑段", confidence=-1.6)],
        gold_id="bilibili-BV1", platform="bilibili", media_url="u", duration=10.0,
        flags=[{"index": 1, "start": 5, "end": 10, "reasons": ["low_confidence"], "text": "可疑段"}],
        report={"similarity": 0.8, "spans": [{"type": "replace", "start": 6, "end": 7,
                                             "primary": "甲", "secondary": "乙"}]},
        primary_label="lv3", secondary_label="small")

    text = eval_gold_draft.render_worksheet(draft)

    assert "本清单按优先级列出前 **1**" in text and "⚠ low_confidence" in text
    assert "| replace | 6–7s | 甲 | 乙 |" in text
    assert "semi-automatic-draft" in text
    assert "human-verified" in text                       # 完成步骤里必须说明怎么"转正"


# ============================ CLI ============================

# --- CLI 落盘草稿与清单，并返回 0 ---
def test_cli_writes_draft_and_worksheet(tmp_path, capsys):
    primary = tmp_path / "primary.json"
    primary.write_text(json.dumps({"segments": [{"from": 0, "to": 5, "content": "正文",
                                                 "confidence": -0.2, "compression_ratio": 1.0}]},
                                  ensure_ascii=False), encoding="utf-8")
    draft_path, worksheet = tmp_path / "draft.json", tmp_path / "ws.md"

    code = eval_gold_draft.main(["--primary", str(primary), "--id", "x", "-o", str(draft_path),
                                 "--worksheet", str(worksheet)])

    assert code == 0
    assert json.loads(draft_path.read_text(encoding="utf-8"))["id"] == "x"
    assert "金标核对清单" in worksheet.read_text(encoding="utf-8")


# --- 输入不存在时给可解析错误与用法码，而不是抛栈 ---
def test_cli_reports_missing_primary(tmp_path, capsys):
    code = eval_gold_draft.main(["--primary", str(tmp_path / "missing.json"), "--id", "x",
                                 "-o", str(tmp_path / "draft.json")])

    assert code == 2
    assert "error" in json.loads(capsys.readouterr().out.strip())


# --- 上限与排序：全片都分歧时只列前 N，并如实报告总数（否则清单会失去优先级）---
def test_flags_are_ranked_and_capped():
    paragraphs = [paragraph(index * 5, index * 5 + 5, f"段{index}") for index in range(20)]
    ranges = [{"start": index * 5, "end": index * 5 + 5, "primary": "甲甲", "secondary": "乙"}
              for index in range(20)]
    paragraphs[7]["compression_ratio"] = 9.0                      # 复读嫌疑应排在最前

    flags = eval_gold_draft.flag_paragraphs(paragraphs, disagreement_ranges=ranges, max_flags=5)

    assert len(flags) == 5
    assert flags[0]["index"] == 7 and "high_compression_ratio" in flags[0]["reasons"]
    assert all("difference_chars" in item for item in flags)


# --- 差异只有单字时不算"实质分歧"，但仍被标记（口径要能区分噪声与真差异）---
def test_single_character_difference_is_not_substantive():
    blind = {"start": 0, "end": 5, "text": "x"}          # 没有置信度信息 → 不触发最低十分位兜底
    small = eval_gold_draft.flag_paragraphs([dict(blind)],
                                            disagreement_ranges=[{"start": 0, "end": 5, "primary": "的",
                                                                  "secondary": "得"}])
    big = eval_gold_draft.flag_paragraphs([dict(blind)],
                                          disagreement_ranges=[{"start": 0, "end": 5, "primary": "供血",
                                                                "secondary": "工学"}])

    assert small[0]["reasons"] == ["disagreement_with_secondary"]
    assert big[0]["reasons"] == ["disagreement_substantive"]
