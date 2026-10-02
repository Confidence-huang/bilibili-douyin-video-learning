"""
Skill 资产盘点与"分歧清单进笔记"的离线测试（对应 docs/DECISIONS.md D39）。

守住两件事：`doctor status` 报出的资产数字必须**来自真实文件**（不能是写死的说法）；
笔记里的分歧小节**没有校验信息时不得出现**（否则每篇笔记都挂一个空的"待确认"）。
运行示例：python -m pytest cli_anything/video_learning/tests/test_skill_assets.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill
import sys  # 把 scripts 目录加入搜索路径
from pathlib import Path  # 稳定定位 Skill 根目录

import pytest  # 断言辅助


SKILL_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REPO_ROOT = SKILL_ROOT.parents[2]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cli_anything.video_learning.core import skill_assets  # noqa: E402 被测模块
import review_section  # noqa: E402 被测模块


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================ 资产盘点 ============================

# --- 条目数必须与真实文件一致（注释行不算条目）---
def test_asset_counts_come_from_real_files():
    assets = skill_assets.inspect_skill_assets(SKILL_ROOT)

    lexicon_lines = [line for line in (SKILL_ROOT / "references/asr-lexicon.txt")
                     .read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
    assert assets["lexicon_entries"] == len(lexicon_lines) > 0
    assert assets["conflict_preference_entries"] > 0


# --- 分块默认值必须从源码读出（写死的说法会过期）---
def test_chunk_length_default_is_read_from_source():
    assets = skill_assets.inspect_skill_assets(SKILL_ROOT)

    assert assets["chunking_available"] is True
    assert assets["chunk_length_default"] == 0.0            # 默认关闭是有意为之（D38）
    assert assets["prompt_templates"]                        # 模板清单来自 prompts/ 目录


# --- 目录不存在时不得抛异常，只如实返回 0/False/None ---
def test_missing_skill_root_is_tolerated(tmp_path):
    assets = skill_assets.inspect_skill_assets(tmp_path)

    assert assets["lexicon_entries"] == 0 and assets["chunking_available"] is False
    assert assets["chunk_length_default"] is None


# ============================ 分歧清单进笔记 ============================

# --- 有报告时渲染成表格，并标明"只呈现、不自动改判" ---
def test_review_section_renders_top_items():
    payload = {"fusion": {"needs_review_total": 21, "needs_review_top": [
        {"start": 12.0, "end": 14.0, "text": "供血的血包", "alternative_text": "工学的雪包",
         "difference_chars": 2}]}}

    section = review_section.render_review_section(payload)

    assert "需要人工确认的差异" in section and "**21**" in section
    assert "供血的血包" in section and "工学的雪包" in section and "| 2 |" in section
    assert "不自动改判" in section


# --- 没有校验信息时返回空串：不许产生空小节 ---
def test_review_section_is_empty_without_report():
    assert review_section.render_review_section({}) == ""
    assert review_section.render_review_section({"fusion": {"checked": True}}) == ""


# --- limit 生效，且改判数量会提示出来 ---
def test_review_section_respects_limit_and_reports_preference_hits():
    items = [{"start": i, "end": i + 1, "text": f"甲{i}", "alternative_text": f"乙{i}", "difference_chars": 2}
             for i in range(5)]
    payload = {"fuse": {"needs_review_total": 5, "needs_review_top": items, "preference_hits": [{"wrong_form": "雪"}]}}

    section = review_section.render_review_section(payload, limit=2)

    assert section.count("| 甲") == 2                       # 只列 2 条
    assert "已由已验证的裁决表改判" in section


# --- 两个 to_markdown 都必须真的接上该小节（否则笔记里看不到）---
def test_note_renderers_are_wired():
    for script in ("douyin_extract", "fetch_bilibili"):
        source = (SCRIPTS_DIR / f"{script}.py").read_text(encoding="utf-8")
        assert "render_review_section" in source, f"{script}.to_markdown 未接入分歧小节"


# ============================ 笔记里的"这份稿子经过了什么处理"（D50） ============================

# --- 有诊断就渲染成表；缺字段的那行不出现（绝不编造）---
def test_processing_section_renders_diagnostics():
    payload = {"diagnostics": [
        {"step": "simplify", "ok": True, "message": "繁简归一已生效（7 段被改写）"},
        {"step": "asr_chunking", "ok": True, "message": "4 chunk(s), 10 segment(s) kept, 0 duplicate(s) dropped"},
        {"step": "asr_engine", "ok": True, "message": "ok"},                      # 引擎行由字段单独呈现
    ], "engine": "faster-whisper", "device": "cuda", "model_size": "large"}

    section = review_section.render_processing_section(payload)

    assert "这份稿子经过了什么处理" in section
    assert "繁简归一" in section and "已生效" in section
    assert "长音频分块" in section and "4 chunk" in section
    assert "faster-whisper / cuda / large" in section
    assert "asr_engine" not in section                                          # 不把内部步骤名直接抛给读者


# --- 没有任何诊断时返回空串：不许出现空的"处理说明"小节 ---
def test_processing_section_is_empty_without_diagnostics():
    assert review_section.render_processing_section({}) == ""
    assert review_section.render_processing_section({"diagnostics": [{"step": "asr_engine"}]}) == ""


# --- 未知步骤不得进入表格（避免把内部实现细节泄漏成"处理说明"）---
def test_processing_section_ignores_unknown_steps():
    section = review_section.render_processing_section({"diagnostics": [{"step": "内部步骤", "ok": True}]})

    assert section == ""


# --- 资产盘点要报出"归一依赖是否可用"和"默认模型"（都来自真实探测/源码，D51）---
def test_assets_report_normalisation_and_default_model():
    assets = skill_assets.inspect_skill_assets(SKILL_ROOT)

    assert assets["opencc_available"] in (True, False)
    assert assets["simplification_mode"] == ("opencc" if assets["opencc_available"] else "fallback")
    assert assets["default_model"] in ("auto", "small", "large")               # 默认必须能被读出来


# --- 历史记录只追加、不改写（每次测量一行，便于日后看趋势）---
def test_benchmark_records_history_jsonl(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("video_learning_test_history", SCRIPTS_DIR / "run_benchmark.py")
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    gold = SKILL_ROOT / "eval/gold/bilibili-BV1ntah6TEe9.json"
    hypothesis = SKILL_ROOT / "agent-harness/cli_anything/video_learning/tests/fixtures/bili1_large.json"
    history = tmp_path / "history.jsonl"

    first = benchmark.main(["--case", f"bili1-large={hypothesis}:{gold}", "--record-history", str(history)])
    second = benchmark.main(["--case", f"bili1-large={hypothesis}:{gold}", "--record-history", str(history)])

    lines = history.read_text(encoding="utf-8").strip().splitlines()
    assert first == 0 and second == 0
    assert len(lines) == 2                                                     # 追加而不是覆盖
    import json as _json
    entry = _json.loads(lines[0])
    assert entry["case"] == "bili1-large" and entry["norm"] and entry["cer"] is not None
    assert entry["timestamp"].endswith("Z")


# --- doctor 的"最近一次基准"：无历史时如实为空 ---
def test_latest_benchmark_is_empty_without_history(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_LEARNING_HISTORY", raising=False)

    assert skill_assets.read_latest_benchmark(tmp_path / "无.jsonl") == {}


# --- 有历史时返回最后一行 ---
def test_latest_benchmark_reads_last_line(tmp_path):
    history = tmp_path / "h.jsonl"
    history.write_text('{"case": "a", "cer": 0.01}\n{"case": "b", "cer": 0.02}\n', encoding="utf-8")

    assert skill_assets.read_latest_benchmark(history)["case"] == "b"
