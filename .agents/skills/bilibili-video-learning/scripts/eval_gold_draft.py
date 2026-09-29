#!/usr/bin/env python3
r"""
半自动金标：把 ASR 初稿变成"只差人工核对几处"的金标草稿（见 docs/DECISIONS.md D33）。

为什么需要它（人工成本不可压缩，但可以缩小）：
    金标的价值最高，却卡在"人工逐句核对"上——一支 5 分钟视频要 15–30 分钟。
    但人工真正需要看的其实只有少数位置：**低置信段**、**高压缩比段**（复读/幻觉嫌疑）、
    以及**两个解码配置给出不同文本的段**。本工具把这三类位置标出来，输出
    「金标草稿 + 核对清单」，人工只需核对被标记的行，其余行抽查即可。

产物（都在仓库内的纯文本）：
    eval/gold/<id>.draft.json   —— 与正式金标同形状，但 reference.kind = "semi-automatic-draft"，
                                   并带 review 块（标记了什么、为什么、还差哪些步骤）
    <worksheet>.md              —— 给人看的核对清单：逐段表格 + 分歧清单 + 勾选步骤

边界：
    - **产物不是真值**：`kind` 明确写着 draft，评测脚本照旧能读它（用于 A/B 比较），
      但只有人工把 `kind` 改成 `human-verified` 之后，它才能当验收基准；
    - 不做任何自动改写：本工具只标记与排版，不替人决定哪个写法对。

调用示例：
    python eval_gold_draft.py --primary lv3.json --secondary small.json \
        --id bilibili-BV1ntah6TEe9 --platform bilibili \
        --media-url "https://www.bilibili.com/video/BV1ntah6TEe9" --duration 30.63 \
        -o ../eval/gold/bilibili-BV1ntah6TEe9.draft.json --worksheet /tmp/worksheet.md
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import argparse  # 稳定命令行契约
import json  # 金标与报告都是 JSON
import sys  # 退出码
from pathlib import Path  # 路径处理
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_asr  # 复用同一套规范化与读取逻辑，避免第二份实现


DEFAULT_LOW_CONFIDENCE = -1.0        # 与 whisper/CT2 默认阈值一致
DEFAULT_COMPRESSION_RATIO = 2.4      # 异常高通常意味着复读或幻觉
FALLBACK_FLAG_FRACTION = 0.1         # 一段都没低于阈值时，标出置信度最低的 10%
DEFAULT_MAX_FLAGS_FRACTION = 0.3     # 清单上限：否则"分歧多"的素材会把所有段落都标出来，等于没有优先级
DEFAULT_MIN_FLAGS = 8
REASON_PRIORITY = {"high_compression_ratio": 0, "low_confidence": 1, "lowest_confidence_decile": 2,
                   "disagreement_substantive": 3, "disagreement_with_secondary": 4}
CHECKLIST = [
    "逐行核对被标记的段落（低置信 / 复读嫌疑 / 两个配置分歧），其余行抽查即可",
    "确认专有名词与术语（人名、机构、产品名），必要时补进 references/asr-lexicon.txt",
    "确认数字：本工具只标不改，数字写法差异已由评测口径归一（见 D33）",
    "补上被漏掉的整句（覆盖率兜底只看时间轴，看不出一句话被吞掉）",
    "改完后把 reference.kind 从 semi-automatic-draft 改成 human-verified，并删掉 review 块",
]


# --- 读取一份转写产出（支持规范形状与 ASR 形状）---
def load_segments(path: str | Path) -> List[Dict[str, Any]]:
    return eval_asr.load_segments(path)


# --- 标记需要人工看的段落：低置信 / 复读嫌疑 / 与另一配置分歧 ---
def flag_paragraphs(paragraphs: List[Dict[str, Any]], *, low_confidence: float = DEFAULT_LOW_CONFIDENCE,
                    compression_ratio: float = DEFAULT_COMPRESSION_RATIO,
                    disagreement_ranges: Optional[List[Dict[str, Any]]] = None,
                    bottom_fraction: float = FALLBACK_FLAG_FRACTION,
                    max_flags: Optional[int] = None,
                    substantive_chars: int = 2) -> List[Dict[str, Any]]:
    ranges = disagreement_ranges or []
    scored = [(index, item.get("confidence")) for index, item in enumerate(paragraphs)
              if item.get("confidence") is not None]
    fallback: set = set()
    if scored and not any(value < low_confidence for _, value in scored):
        ordered = sorted(scored, key=lambda pair: pair[1])
        fallback = {index for index, _ in ordered[:max(1, int(len(ordered) * bottom_fraction))]}

    flags = []
    for index, item in enumerate(paragraphs):
        reasons = []
        confidence = item.get("confidence")
        ratio = item.get("compression_ratio")
        if confidence is not None and confidence < low_confidence:
            reasons.append("low_confidence")
        elif index in fallback:
            reasons.append("lowest_confidence_decile")
        if ratio is not None and ratio > compression_ratio:
            reasons.append("high_compression_ratio")
        overlap_size = _max_overlap_size(item, ranges)
        if overlap_size:
            # 差异只有一两个字 → 多半是虚词/标点噪声；差异更长才值得人工看
            reasons.append("disagreement_substantive" if overlap_size >= substantive_chars
                           else "disagreement_with_secondary")
        if reasons:
            flags.append({"index": index, "start": item.get("start"), "end": item.get("end"),
                          "reasons": reasons, "text": (item.get("text") or "")[:60],
                          "difference_chars": overlap_size})

    # 排序 + 上限：优先"复读嫌疑 > 低置信 > 实质分歧"，同类按差异大小排
    flags.sort(key=lambda flag: (min(REASON_PRIORITY.get(reason, 9) for reason in flag["reasons"]),
                                 -flag.get("difference_chars") or 0))
    limit = max_flags if max_flags is not None else max(DEFAULT_MIN_FLAGS,
                                                        int(len(paragraphs) * DEFAULT_MAX_FLAGS_FRACTION))
    for flag in flags[limit:]:
        flag["trimmed"] = True                                   # 被截断的仍如实保留在产物里，只是不列进清单
    return flags[:limit]


# --- 某段落与分歧区间的最大差异规模（用于排序：实质差异优先）---
def _max_overlap_size(paragraph: Dict[str, Any], ranges: List[Dict[str, Any]]) -> int:
    start, end = paragraph.get("start"), paragraph.get("end")
    if start is None or end is None:
        return 0
    size = 0
    for span in ranges:
        span_start, span_end = span.get("start"), span.get("end")
        if span_start is None or span_end is None:
            continue
        if span_end >= start and span_start <= end:
            primary = str(span.get("primary") or span.get("primary_text") or "")
            secondary = str(span.get("secondary") or span.get("secondary_text") or "")
            size = max(size, len(primary), len(secondary), 1)      # 只有区间没有文本时也算命中，只是不算"实质"
    return size


# --- 段落是否与某条分歧区间重叠 ---
def _overlaps_any(paragraph: Dict[str, Any], ranges: List[Dict[str, Any]]) -> bool:
    start, end = paragraph.get("start"), paragraph.get("end")
    if start is None or end is None:
        return False
    for span in ranges:
        span_start, span_end = span.get("start"), span.get("end")
        if span_start is None or span_end is None:
            continue
        if span_end >= start and span_start <= end:
            return True
    return False


# --- 组装金标草稿（与正式金标同形状，外加 review 块）---
def build_draft(paragraphs: List[Dict[str, Any]], *, gold_id: str, platform: str, media_url: str,
                duration: Optional[float], flags: List[Dict[str, Any]], report: Optional[Dict[str, Any]],
                primary_label: str, secondary_label: Optional[str],
                flagged_total: Optional[int] = None) -> Dict[str, Any]:
    text = "".join(item.get("text", "") for item in paragraphs)
    return {
        "_comment": "半自动金标草稿：初稿来自 ASR，未经人工逐句核对；核对完成前不要当作验收基准。",
        "id": gold_id,
        "platform": platform,
        "media": {"url": media_url, "duration_seconds": duration,
                  "media_ref": "不入库：用上面的链接重新取流后即可复现"},
        "reference": {"kind": "semi-automatic-draft", "char_count": len(text), "paragraphs": paragraphs,
                      "text": text,
                      "provenance": f"初稿 = {primary_label}；分歧清单 = {secondary_label or '未提供'}；**等待人工核对**"},
        "review": {
            "flagged_paragraphs": flags,
            "flagged_count": len(flags),
            "flagged_total": flagged_total if flagged_total is not None else len(flags),
            "paragraph_count": len(paragraphs),
            "similarity_with_secondary": (report or {}).get("similarity"),
            "disagreement_spans": (report or {}).get("spans", []),
            "checklist": CHECKLIST,
        },
        "known_asr_errors": [],
    }


# --- 核对清单（人看的 Markdown）---
def render_worksheet(draft: Dict[str, Any]) -> str:
    review = draft.get("review") or {}
    paragraphs = (draft.get("reference") or {}).get("paragraphs") or []
    flagged = {item["index"]: item["reasons"] for item in review.get("flagged_paragraphs") or []}
    lines = [
        f"# 金标核对清单：{draft.get('id')}",
        "",
        f"- 段落数：**{review.get('paragraph_count')}**；"
        f"可疑段落共 **{review.get('flagged_total')}** 个，本清单按优先级列出前 **{review.get('flagged_count')}** 个"
        f"（其余请在 JSON 的 review.flagged_paragraphs 之外抽查；这样清单不会长到没人看）",
        f"- 与另一配置的相似度：**{review.get('similarity_with_secondary')}**",
        f"- 初稿来源：{(draft.get('reference') or {}).get('provenance')}",
        "",
        "## 逐段（⚠ = 需要你确认）",
        "",
        "| # | 时间 | 标记 | 正文 |",
        "|---|---|---|---|",
    ]
    for index, item in enumerate(paragraphs):
        reasons = flagged.get(index)
        mark = ("⚠ " + ",".join(reasons)) if reasons else ""
        moment = f"{item.get('start')}–{item.get('end')}s"
        text = (item.get("text") or "").replace("|", "\\|")[:80]
        lines.append(f"| {index} | {moment} | {mark} | {text} |")

    spans = review.get("disagreement_spans") or []
    lines += ["", "## 分歧清单（两个配置写出不同内容的地方）", ""]
    if spans:
        lines += ["| 类型 | 时间 | 主配置 | 次配置 |", "|---|---|---|---|"]
        for span in spans[:60]:
            primary = (span.get("primary") or span.get("primary_text") or "").replace("|", "\\|")[:40]
            secondary = (span.get("secondary") or span.get("secondary_text") or "").replace("|", "\\|")[:40]
            lines.append(f"| {span.get('type')} | {span.get('start')}–{span.get('end')}s | {primary} | {secondary} |")
    else:
        lines.append("（无：两个配置完全一致，或未提供第二个配置）")

    lines += ["", "## 完成步骤", ""] + [f"{index + 1}. {step}" for index, step in enumerate(review.get("checklist") or [])]
    return "\n".join(lines) + "\n"


# --- CLI ---
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build a semi-automatic gold draft plus a human review worksheet.")
    parser.add_argument("--primary", required=True, help="初稿来源（建议最好的配置，如 large-v3）")
    parser.add_argument("--secondary", help="第二配置（用于分歧清单，如 small）")
    parser.add_argument("--id", required=True, help="金标 id，例如 bilibili-BV1ntah6TEe9")
    parser.add_argument("--platform", default="bilibili", choices=("bilibili", "douyin"))
    parser.add_argument("--media-url", default="", help="复现入口（媒体本身不入库）")
    parser.add_argument("--duration", type=float, help="音频/视频时长（秒），用于覆盖率与 RTF")
    parser.add_argument("--primary-label", default="primary ASR")
    parser.add_argument("--secondary-label", default="secondary ASR")
    parser.add_argument("-o", "--output", required=True, help="金标草稿输出路径（建议 eval/gold/<id>.draft.json）")
    parser.add_argument("--worksheet", help="核对清单输出路径（Markdown）")
    parser.add_argument("--json", action="store_true", help="把草稿打到 stdout")
    args = parser.parse_args(argv)

    try:
        primary = load_segments(args.primary)
        secondary = load_segments(args.secondary) if args.secondary else []
        report = None
        if secondary:
            import verify_transcript  # 复用既有对比器：分歧清单口径与人工看到的一致

            report = verify_transcript.compare_timelines(primary, secondary)
        all_flags = flag_paragraphs(primary, disagreement_ranges=(report or {}).get("spans"),
                                   max_flags=10 ** 6)                     # 先算全量，再按上限取前 N，并如实报总数
        flags = flag_paragraphs(primary, disagreement_ranges=(report or {}).get("spans"))
        draft = build_draft(primary, gold_id=args.id, platform=args.platform, media_url=args.media_url,
                            duration=args.duration, flags=flags, report=report, flagged_total=len(all_flags),
                            primary_label=args.primary_label,
                            secondary_label=args.secondary_label if secondary else None)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2

    Path(args.output).write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.worksheet:
        Path(args.worksheet).write_text(render_worksheet(draft), encoding="utf-8")
    summary = {"draft": args.output, "worksheet": args.worksheet,
               "paragraphs": draft["review"]["paragraph_count"], "flagged": draft["review"]["flagged_count"],
               "similarity": draft["review"]["similarity_with_secondary"]}
    print(json.dumps(draft if args.json else summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
