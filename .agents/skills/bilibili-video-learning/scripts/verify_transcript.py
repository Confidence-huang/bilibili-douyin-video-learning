#!/usr/bin/env python3
r"""
多源交叉校验与融合：把两份转写文本做字符级对齐，先穷尽差异，再合成一份带出处的逐字稿。

为什么需要它：
    转写正确性从来不是"某个模型对不对"，而是"两个独立来源是否互相印证"。
    实测案例：同一段 259.77 秒音频，硬字幕与 ASR 有 14 处差异——
    硬字幕是 `血包`、`断气`、`45岁`、`经济价值`，ASR 是 `雪包`、`断亲`、`25岁`、`经营价值`。
    人工逐句比对做不完这件事，而字符级对齐能一次给出**全部**差异及其时间范围，
    于是"我核对过了"变成一张可以复查的清单（见 docs/DECISIONS.md D21）。

    它也能反过来用：把本工具的输出当成"漏采检测"——如果某个来源少了整段正文，
    这里会报出该来源缺失的片段与时间范围，而不是让人去猜。

    但它只报告、不产出：真实场景要的是「一份能引用的逐字稿」。B站有平台字幕轨时字幕
    比 ASR 权威，字幕却常常漏掉短卡片；抖音没有字幕轨，硬字幕是文本主源而时间轴来自 ASR。
    于是这里再补一层 `fuse_transcripts`：沿用同一套字符级对齐，逐段决定采用哪一侧的写法，
    并给每一段标上 provenance。硬不变量是**不编造**——融合结果的字符集合必须覆盖两份来源
    的并集（除标点/空白）；某一侧独有的正文只会被搬进来，绝不会被丢弃或被改写。
    两侧都有但写法不同的段保留主源文本，同时把次源写法记进 `alternatives` 并要求人工复核。

    输入可以是：JSON 时间轴（规范 `start/end/text` 或 ASR `from/to/content`）、
    完整提取结果文档（取其中的 `segments`），或 SRT/VTT 字幕文件。

    调用示例：
        python verify_transcript.py --primary captions.json --secondary asr.json
        python verify_transcript.py --primary hard.srt --secondary result.json --json
        python verify_transcript.py --primary captions.json --secondary asr.json --fuse -o fused.json
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注


from collections import Counter  # 并集不变量按字符多重集比对，不能只看集合
import argparse                                                                      # 稳定命令行契约：两份来源、阈值、产出
import difflib                                                                       # 字符级对齐是唯一能穷尽差异的方法
import json                                                                          # 输入输出都是 JSON
import re                                                                            # 判断字符是否为有效正文
import sys                                                                           # 报告写 stdout，诊断写 stderr
from pathlib import Path                                                             # 统一处理 Windows 路径

from convert_subtitle import detect_and_parse                                        # 复用既有字幕解析，不重写一份
from normalize_transcript import normalize_segments                                  # 两种历史字段名统一在这里处理
from runtime_output import EXIT_SOURCES_DISAGREE                                     # 与 CLI 共用同一份退出码契约


SIGNIFICANT_PATTERN = re.compile(r"[\w\u4e00-\u9fff]")                                # 只对齐有效字符，标点差异不算内容差异
DEFAULT_MIN_SPAN_CHARS = 1                                                           # 同音字差异常常正好是一个字（血/雪），默认必须报出
MIN_SHARED_CHARS = 2                                                                 # 逐字符对齐里，次源与某段只重合一个字多半是跨段巧合，不算覆盖
FUSION_SIGNIFICANT_PATTERN = re.compile(r"[^\W_\s]", re.UNICODE)                      # 融合把标点与空白都归一掉，判定口径必须与 _significant_text 完全一致
# 注意：必须用 fullmatch 判定单字符，negative class 的 match 会在位置 0 匹配空串而把每个字符都放行。


# --- 读取任意一种来源并转成规范时间轴 ---
def load_timeline(path: str) -> list[dict]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"timeline not found: {source}")
    suffix = source.suffix.lower()
    if suffix in (".srt", ".vtt", ".json"):
        if suffix in (".srt", ".vtt"):
            return normalize_segments(detect_and_parse(str(source)))                 # 字幕文件走既有解析器
        payload = json.loads(source.read_text(encoding="utf-8"))
        segments = payload.get("segments") if isinstance(payload, dict) else payload  # 兼容完整提取结果文档
        if not segments:
            raise ValueError(f"{source} contains no non-empty 'segments' list")
        return normalize_segments(segments)
    raise ValueError(f"unsupported timeline format: {suffix}")


# --- 把时间轴压成"有效字符 -> 时间"的序列 ---
def build_character_stream(segments: list[dict]) -> list[tuple[str, float, float]]:
    stream: list[tuple[str, float, float]] = []
    for item in segments:
        for character in item["text"]:
            if SIGNIFICANT_PATTERN.match(character):                                 # 标点不参与对齐
                stream.append((character, item["start"], item["end"]))
    return stream


# --- 对齐两份来源，产出差异清单 ---
def compare_timelines(primary: list[dict], secondary: list[dict], *, min_span_chars: int = DEFAULT_MIN_SPAN_CHARS) -> dict:
    primary = normalize_segments(primary)                                            # 公开接口自己过适配层：两种历史形状都能直接传
    secondary = normalize_segments(secondary)
    primary_stream = build_character_stream(primary)
    secondary_stream = build_character_stream(secondary)
    primary_text = "".join(item[0] for item in primary_stream)
    secondary_text = "".join(item[0] for item in secondary_stream)

    matcher = difflib.SequenceMatcher(None, primary_text, secondary_text, autojunk=False)
    spans: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        primary_span = primary_text[i1:i2]
        secondary_span = secondary_text[j1:j2]
        if max(len(primary_span), len(secondary_span)) < min_span_chars:              # 调用方可以显式抬高阈值来过滤噪声
            continue
        spans.append({
            "kind": tag,                                                             # replace / delete / insert
            "side": "primary_only" if tag == "delete" else ("secondary_only" if tag == "insert" else "both"),
            "start": _span_start(primary_stream, secondary_stream, i1, j1),
            "end": _span_end(primary_stream, secondary_stream, i2, j2),
            "primary_text": primary_span,
            "secondary_text": secondary_span,
        })

    summary = {
        "primary_only_spans": sum(1 for span in spans if span["side"] == "primary_only"),
        "secondary_only_spans": sum(1 for span in spans if span["side"] == "secondary_only"),
        "replacement_spans": sum(1 for span in spans if span["side"] == "both"),
        "primary_only_chars": sum(len(span["primary_text"]) for span in spans if span["side"] == "primary_only"),
        "secondary_only_chars": sum(len(span["secondary_text"]) for span in spans if span["side"] == "secondary_only"),
    }
    return {
        "primary": _describe(primary, primary_text),
        "secondary": _describe(secondary, secondary_text),
        "similarity": round(matcher.ratio(), 4),
        "spans": spans,
        "summary": summary,
        "verdict": _verdict(summary),
    }


# --- 差异片段的起始时间：优先用有内容的一侧 ---
def _span_start(primary_stream, secondary_stream, primary_index: int, secondary_index: int) -> float | None:
    if primary_index < len(primary_stream):
        return round(primary_stream[primary_index][1], 2)
    if secondary_index < len(secondary_stream):
        return round(secondary_stream[secondary_index][1], 2)
    return None


# --- 差异片段的结束时间 ---
def _span_end(primary_stream, secondary_stream, primary_index: int, secondary_index: int) -> float | None:
    if primary_index - 1 < len(primary_stream) and primary_index - 1 >= 0:
        return round(primary_stream[primary_index - 1][2], 2)
    if secondary_index - 1 < len(secondary_stream) and secondary_index - 1 >= 0:
        return round(secondary_stream[secondary_index - 1][2], 2)
    return None


# --- 一份来源的摘要 ---
def _describe(segments: list[dict], text: str) -> dict:
    return {
        "segments": len(segments),
        "chars": len(text),
        "start": segments[0]["start"] if segments else None,
        "end": segments[-1]["end"] if segments else None,
    }


# --- 给调用方一句可以直接转述的结论 ---
def _verdict(summary: dict) -> str:
    total = summary["primary_only_spans"] + summary["secondary_only_spans"] + summary["replacement_spans"]
    if total == 0:
        return "两份来源在有效字符上完全一致"
    if summary["primary_only_spans"] and not summary["secondary_only_spans"]:
        return "次源（secondary）缺少正文，需要检查是否漏采或漏识别"
    if summary["secondary_only_spans"] and not summary["primary_only_spans"]:
        return "主源（primary）缺少正文，需要检查是否漏采或漏识别"
    return "两份来源各有出入，逐条见 spans；替换类差异通常是同音字，需按上下文判断"


# --- 报告渲染成 Markdown，便于贴进笔记或 PR ---
def render_report(report: dict) -> str:
    lines = [
        "# 转写交叉校验报告", "",
        f"- 主源：{report['primary']['segments']} 段 / {report['primary']['chars']} 字",
        f"- 次源：{report['secondary']['segments']} 段 / {report['secondary']['chars']} 字",
        f"- 相似度：{report['similarity']}",
        f"- 结论：{report['verdict']}", "",
        "| 类型 | 时间范围 | 主源 | 次源 |", "|---|---|---|---|",
    ]
    for span in report["spans"]:
        window = f"{span['start']}–{span['end']}s" if span["start"] is not None else "—"
        lines.append(f"| {span['kind']} | {window} | {span['primary_text'] or '—'} | {span['secondary_text'] or '—'} |")
    if not report["spans"]:
        lines.append("| — | — | — | — |")
    return "\n".join(lines) + "\n"


# --- 把一份来源的片段按字符下标区间整段搬进来（只截取整个已对齐的片段，绝不切半张卡片） ---
def _copy_segments(segments: list[dict], stream: list[tuple[str, float, float]],
                   first_index: int, last_index: int, *, covered_by: tuple[str, ...]) -> list[dict]:
    if last_index <= first_index:
        return []                                                     # 空区间说明这段没有正文，不产出半截段落
    window = Counter(character for character, _start, _end in stream[first_index:last_index])
    copied: list[dict] = []
    for index, item in enumerate(segments):
        text = _significant_text(item["text"])                        # 标点与空白按任务约定归一：只保留可引用正文
        if not text:
            continue                                                  # 纯标点段没有可引用内容，不得产出空段
        overlap = window & Counter(text)                              # 用字符多重集求交，不依赖时间窗宽度
        if not overlap:
            continue                                                  # 该段没有一个字符落在这段对齐里
        piece = {
            "start": round(float(item["start"]), 3),
            "end": round(float(item["end"]), 3),
            "text": text,
            "segment_index": index,                                   # 同源片段以 (来源, 下标) 为身份，可跨 opcode 合并
            "chars": sum(overlap.values()),                           # 本段贡献的字符数，供诊断使用
            "covered_by": set(covered_by),                            # 内部标记，出口转成来源名列表
        }
        copied.append(piece)
    return copied


# --- 只保留参与对齐的有效字符，与逐字稿归一规则保持同一口径 ---
def _significant_text(text: str) -> str:
    return "".join(character for character in str(text or "") if FUSION_SIGNIFICANT_PATTERN.fullmatch(character))


# --- 把两份来源合成一份带出处的逐字稿（主源更权威，次源只负责补空档） ---
def fuse_transcripts(primary: list[dict], secondary: list[dict], *,
                     primary_source: str = "subtitle", secondary_source: str = "asr") -> dict:
    primary = normalize_segments(primary)                             # 公开接口自己过适配层：两种历史形状都能直接传
    secondary = normalize_segments(secondary)
    sources = {"primary": primary_source, "secondary": secondary_source}
    primary_stream = build_character_stream(primary)
    secondary_stream = build_character_stream(secondary)
    primary_text = "".join(item[0] for item in primary_stream)
    secondary_text = "".join(item[0] for item in secondary_stream)
    matcher = difflib.SequenceMatcher(None, primary_text, secondary_text, autojunk=False)

    spans: list[dict] = []
    pieces: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        primary_span = primary_text[i1:i2]
        secondary_span = secondary_text[j1:j2]
        if tag == "equal":
            if primary_span:
                # 两侧在同一位置有同样的正文 -> 采用主源，并标记两侧都覆盖了这一段
                pieces.extend(_copy_segments(primary, primary_stream, i1, i2,
                                             covered_by=("primary", "secondary")))
            else:
                # 该区间主源为空但次源有内容（与相邻 insert 合并后的 equal）：用次源补上
                pieces.extend(_copy_segments(secondary, secondary_stream, j1, j2, covered_by=("secondary",)))
            continue

        spans.append({
            "kind": tag,                                              # replace / delete / insert，与 compare_timelines 同结构
            "side": "primary_only" if tag == "delete" else ("secondary_only" if tag == "insert" else "both"),
            "start": _span_start(primary_stream, secondary_stream, i1, j1),
            "end": _span_end(primary_stream, secondary_stream, i2, j2),
            "primary_text": primary_span,
            "secondary_text": secondary_span,
        })
        if tag == "insert":
            pieces.extend(_copy_segments(secondary, secondary_stream, j1, j2, covered_by=("secondary",)))
        else:
            # delete：只有主源有，保留；replace：主源文本进稿，同时记录次源在这段里的实际字符
            pieces.extend(_copy_segments(primary, primary_stream, i1, i2, covered_by=("primary",)))
            if primary_span and secondary_span:
                # 次源在同一段给出了别的写法：按主源片段切分，逐段记录，冲突才不会被静默丢掉
                for piece in _copy_segments(primary, primary_stream, i1, i2, covered_by=("secondary",)):
                    piece["alternative_text"] = _secondary_window_text(secondary, secondary_stream, j1, j2)
                    pieces.append(piece)

    fused_segments = _combine_fused_segments(pieces, sources=sources)
    fused_segments = _ensure_source_coverage(fused_segments, secondary, sources)  # 次源段落绝不因对齐歧义消失
    _assert_union_preserved(primary_text, secondary_text, fused_segments, sources)

    secondary_only_spans = sum(1 for span in spans if span["side"] == "secondary_only")
    needs_review_count = sum(1 for item in fused_segments if item.get("needs_review"))
    provenance_counts = {
        primary_source: sum(1 for item in fused_segments if item["provenance"] == primary_source),
        secondary_source: sum(1 for item in fused_segments if item["provenance"] == secondary_source),
        "mixed": sum(1 for item in fused_segments if item["provenance"] == "mixed"),
    }
    return {
        "primary": _describe(primary, primary_text),                  # 与 compare_timelines 保持同一份来源摘要结构
        "secondary": _describe(secondary, secondary_text),
        "similarity": round(matcher.ratio(), 4),
        "spans": spans,
        "segments": fused_segments,
        "segment_count": len(fused_segments),
        "provenance_counts": provenance_counts,
        "needs_review_count": needs_review_count,
        "sources": dict(sources),
        "covered": {
            "secondary_only_spans": secondary_only_spans,             # "补上的段数"就是它：次源有、主源漏
            "secondary_only_chars": sum(len(span["secondary_text"]) for span in spans if span["side"] == "secondary_only"),
        },
        "verdict": _fusion_verdict(provenance_counts, needs_review_count, secondary_only_spans, secondary_source),
    }


# --- 把片段按 (来源, 下标, 时间窗) 归并成逐段带出处的融合稿 ---
def _combine_fused_segments(pieces: list[dict], *, sources: dict) -> list[dict]:
    groups: dict = {}
    for piece in pieces:
        groups.setdefault((piece["segment_index"], piece["start"], piece["end"]), []).append(piece)
    fused: list[dict] = []
    for group in groups.values():
        text_piece = group[0]                                         # 段文本始终取自被采用的那一份来源
        text = text_piece["text"]
        count = len(text) or 1
        primary_chars = sum(piece["chars"] for piece in group if "primary" in piece["covered_by"])
        # 对齐是逐字符的：次源与这一段只重叠一两个字多半是跨段汉字巧合，不构成"次源覆盖了这一段"
        secondary_chars = sum(piece["chars"] for piece in group if "secondary" in piece["covered_by"])
        primary_full = primary_chars >= count
        secondary_count = secondary_chars if secondary_chars >= MIN_SHARED_CHARS else 0
        secondary_full = secondary_count >= count
        alternative = next((piece["alternative_text"] for piece in group if piece.get("alternative_text")), None)
        # 次源只覆盖了一部分，或对这一段给出了别的写法 -> 需要人工按上下文复核
        needs_review = alternative is not None or 0 < secondary_count < count
        if primary_full and secondary_count and needs_review:
            provenance = "mixed"
        elif primary_full:
            provenance = sources["primary"]                           # 主源完整覆盖且次源无异议
        elif secondary_count:
            provenance = sources["secondary"]                         # 主源缺这一段，由次源补上
        else:
            provenance = "mixed"                                      # 理论上不可达；真发生就必须人工看
            needs_review = True
        # 时间轴取"文本来源那一侧"的范围：只有时间真正相交的片段才扩边，避免把邻段的时间也算进来
        relative = [piece for piece in group if piece["text"] == text] or group
        entry = {
            "start": round(min(piece["start"] for piece in relative), 3),
            "end": round(max(piece["end"] for piece in relative), 3),
            "text": text,
            "chars": sum(piece.get("chars", 0) for piece in group),
            "provenance": provenance,
            "covered_by": _source_labels({side for piece in group for side in piece["covered_by"]}, sources),
        }
        if alternative:
            entry["alternatives"] = {sources["secondary"]: alternative}  # 保留次源写法，供人工按上下文判断
            entry["needs_review"] = True
        if needs_review:
            entry["needs_review"] = True
        fused.append(entry)
    fused.sort(key=lambda item: (item["start"], item["end"]))         # 逐段决定后仍必须按时间顺序可阅读
    return fused


# --- 取出一段对齐区间在某个来源里的原始写法（用于 alternatives） ---
def _secondary_window_text(segments: list[dict], stream: list[tuple[str, float, float]],
                           first_index: int, last_index: int) -> str:
    if last_index <= first_index:
        return ""
    window_text = _significant_text("".join(character for character, _start, _end in stream[first_index:last_index]))
    # 只取"这一段正文完全落在该区间里"的片段，再挑最短的一个，避免挂上一整句无关的备选
    inside = [text for text in (_significant_text(item["text"]) for item in segments)
              if text and text in window_text]
    if inside:
        return min(inside, key=len)
    return window_text                                                  # 没有整段落在区间里时退化成区间原文


# --- 完整性兜底：逐字符对齐可能漏掉的次源段落，原样补进融合稿并标成次源 ---
def _ensure_source_coverage(fused_segments: list[dict], secondary: list[dict], sources: dict) -> list[dict]:
    covered = _covered_characters(fused_segments)                      # 已经进稿的有效字符集合
    rescued: list[dict] = []
    for item in secondary:
        text = _significant_text(item["text"])
        if not text:
            continue
        missing = set(text) - covered
        if not missing:
            continue                                                   # 这一段的每个字都已在融合稿里出现
        rescued.append({
            "start": round(float(item["start"]), 3),
            "end": round(float(item["end"]), 3),
            "text": text,
            "chars": len(missing),
            "provenance": sources["secondary"],
            "covered_by": [sources["secondary"]],
            "needs_review": True,                                      # 逐字符对齐没能覆盖它，必须人工确认时间轴
            "recovered_by": "coverage_audit",
        })
        covered.update(text)
    if not rescued:
        return fused_segments
    combined = fused_segments + rescued
    combined.sort(key=lambda item: (item["start"], item["end"]))
    return combined


# --- 融合稿里已经出现过的有效字符（段文本 + alternatives） ---
def _covered_characters(fused_segments: list[dict]) -> set:
    covered: set = set()
    for item in fused_segments:
        covered.update(_significant_text(item["text"]))
        for value in (item.get("alternatives") or {}).values():
            covered.update(_significant_text(str(value)))
    return covered


# --- 内部来源标记 -> 出口用的来源名 ---
def _source_labels(sources_seen: set, sources: dict) -> list[str]:
    labels = []
    if "primary" in sources_seen:
        labels.append(sources["primary"])
    if "secondary" in sources_seen:
        labels.append(sources["secondary"])
    return labels


# --- 硬不变量：两份来源出现过的有效字符一个都不能丢 ---
def _assert_union_preserved(primary_text: str, secondary_text: str, fused_segments, sources: dict) -> None:
    covered = _covered_characters(fused_segments)
    dropped = {
        "primary": _missing_characters(primary_text, covered),
        "secondary": _missing_characters(secondary_text, covered),
    }
    if dropped["primary"] or dropped["secondary"]:
        raise ValueError(f"fusion dropped content from the union: {dropped}")  # 宁可显式失败，也不产出丢字的逐字稿


# --- 某一侧正文里没能进融合稿的字符（稳定排序，报告可复现） ---
def _missing_characters(text: str, covered: set) -> list[str]:
    return sorted(set(text) - covered)


# --- 给融合结果一句可以直接转述的结论 ---
def _fusion_verdict(provenance_counts: dict, needs_review_count: int, secondary_only_spans: int,
                    secondary_source: str = "asr") -> str:
    secondary_count = provenance_counts.get(secondary_source, 0)      # 补上的段落数按调用方给的来源名统计
    if needs_review_count == 0 and secondary_only_spans == 0:
        return "两份来源逐段一致，融合稿没有需要复核的段落"
    parts = []
    if secondary_only_spans:
        parts.append(f"用次源补上 {secondary_only_spans} 段主源漏掉的正文（融合稿中次源段落 {secondary_count} 段）")
    if needs_review_count:
        parts.append(f"{needs_review_count} 处写法冲突保留主源文本，alternatives 已记录，需要人工复核")
    return "；".join(parts) if parts else "融合完成"


# --- 命令行入口 ---
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cross-check two transcript sources character by character.")
    parser.add_argument("--primary", required=True, help="Authoritative source: JSON timeline, extraction result, or SRT/VTT")
    parser.add_argument("--secondary", required=True, help="Source to verify against: same accepted formats")
    parser.add_argument("--min-span-chars", type=int, default=DEFAULT_MIN_SPAN_CHARS,
                        help="Ignore differences shorter than this many characters (default 1: report every character)")
    parser.add_argument("--fuse", action="store_true",
                        help="Emit one fused transcript with per-segment provenance instead of only a difference report")
    parser.add_argument("--primary-source", default="subtitle",
                        help="Label recorded as provenance for content taken from --primary (default subtitle)")
    parser.add_argument("--secondary-source", default="asr",
                        help="Label recorded as provenance for content taken from --secondary (default asr)")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable report")
    parser.add_argument("-o", "--output", help="Write the report to this file instead of stdout")
    parser.add_argument("--fail-on-difference", action="store_true",
                        help="Return exit code 25 when the two sources disagree")
    args = parser.parse_args(argv)

    try:
        primary = load_timeline(args.primary)
        secondary = load_timeline(args.secondary)
        if args.fuse:
            # 融合报告本身就是差异报告：spans / similarity 与 --fuse 之外的调用完全一致
            report = fuse_transcripts(primary, secondary,
                                      primary_source=args.primary_source,
                                      secondary_source=args.secondary_source)
            report["spans"] = [span for span in report["spans"]              # min-span-chars 对融合同样生效
                               if max(len(span["primary_text"]), len(span["secondary_text"])) >= args.min_span_chars]
            differing = bool(report["spans"])
        else:
            report = compare_timelines(primary, secondary, min_span_chars=args.min_span_chars)
            differing = bool(report["spans"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[verify_transcript] ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json or args.fuse:
        text = json.dumps(report, ensure_ascii=False, indent=2)          # 融合结果必须机器可读：它是产出而不是报告
    else:
        text = render_report(report)

    if args.output:
        from file_output import write_text_atomically                               # 与全仓一致：临时文件 + 原子替换
        print(f"Saved to: {write_text_atomically(args.output, text)}")
    else:
        print(text)

    print(f"[verify_transcript] similarity={report['similarity']} spans={len(report['spans'])}: {report['verdict']}",
          file=sys.stderr)
    if differing and args.fail_on_difference:
        return EXIT_SOURCES_DISAGREE                                                 # 让调用方按退出码决定是否人工复核
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
