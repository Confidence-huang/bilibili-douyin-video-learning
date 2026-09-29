#!/usr/bin/env python3
r"""
把"分歧清单"渲染进学习笔记：让读笔记的人直接看见可疑处（见 docs/DECISIONS.md D39）。

为什么放在笔记里：
    `verify_transcript` 早就产出 `needs_review_top`（按差异规模排序的短清单），但它只存在于 JSON 里，
    读笔记的人看不到。笔记是最终交付物，把"需要人工确认的差异"附在末尾，才算真的把校验结果用起来。

边界：
    - **只呈现、不判定**：不挑边、不改写正文，只列出"本稿这么写 / 另一来源那么写 / 差异几个字"；
    - 没有校验信息时**不产生空小节**（返回空串），避免每篇笔记都挂一个"无"字段落。

调用示例：
    from review_section import render_review_section
    section = render_review_section(extraction_payload, limit=30)
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

from typing import Any, Dict, Optional


REPORT_KEYS = ("fusion", "fuse", "verification", "fuse_report")     # 兼容各调用方对同一报告的命名


# --- 从产出里取出校验报告（取第一个像报告的字典）---
def find_review_report(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in REPORT_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, dict) and ("needs_review_top" in candidate or "needs_review_total" in candidate):
            return candidate
    return None


# --- 渲染"需要人工确认的差异"小节；没有可呈现内容时返回空串 ---
def render_review_section(payload: Dict[str, Any], *, limit: int = 30) -> str:
    report = find_review_report(payload or {})
    if not report:
        return ""
    items = report.get("needs_review_top") or []
    total = report.get("needs_review_total")
    if not items and not total:
        return ""
    lines = ["", "## 需要人工确认的差异", "",
             f"多源交叉校验共标出 **{total if total is not None else len(items)}** 处差异，"
             f"下面按差异大小列出前 **{min(len(items), limit)}** 处（只呈现，不自动改判）：", ""]
    if items:
        lines += ["| 时间 | 本稿 | 另一来源 | 差异字数 |", "|---|---|---|---|"]
        for item in items[:limit]:
            moment = f"{item.get('start')}–{item.get('end')}s"
            own = str(item.get("text") or "").replace("|", "\\|")[:40]
            other = str(item.get("alternative_text") or "").replace("|", "\\|")[:40] or "—"
            lines.append(f"| {moment} | {own} | {other} | {item.get('difference_chars')} |")
    preference_hits = report.get("preference_hits") or []
    if preference_hits:
        lines += ["", f"其中 **{len(preference_hits)}** 处已由已验证的裁决表改判"
                      "（`references/conflict-preferences.txt`），可优先复核这几处。"]
    return "\n".join(lines)


# --- 诊断步骤 → 一行人话（缺哪条就少哪行，绝不编造）---
PROCESSING_LABELS = {
    "simplify": ("繁简归一", lambda item: item.get("message")),
    "asr_chunking": ("长音频分块", lambda item: item.get("message")),
    "asr_coverage": ("覆盖率兜底", lambda item: item.get("message")),
    "asr_hallucination_gate": ("幻觉门（拒收窗口）", lambda item: item.get("message")),
    "asr_hallucination_marks": ("幻觉标注", lambda item: item.get("message")),
    "local_video": ("本地文件入口", lambda item: item.get("message")),
    "browser_fetch": ("浏览器取流", lambda item: item.get("message")),
}


# --- 渲染"处理说明"：读产出里的诊断，让读笔记的人知道这份稿子经过了什么 ---
def render_processing_section(payload: Dict[str, Any]) -> str:
    diagnostics = payload.get("diagnostics") or []
    rows = []
    for item in diagnostics:
        step = str(item.get("step") or "")
        if step not in PROCESSING_LABELS or step == "asr_engine":
            continue
        label, extract = PROCESSING_LABELS[step]
        message = extract(item) or ("已生效" if item.get("ok") else "未生效")
        rows.append(f"| {label} | {message} |")
    engine = payload.get("engine") or payload.get("transcription_engine")
    device = payload.get("device") or payload.get("transcription_device")
    model = payload.get("model_size") or payload.get("transcription_model")
    if engine or device or model:
        rows.append(f"| 转写引擎 | {engine or '—'} / {device or '—'} / {model or '—'} |")
    if not rows:
        return ""
    return "\n".join(["", "## 这份稿子经过了什么处理", "", "| 环节 | 结果 |", "|---|---|"] + rows)
