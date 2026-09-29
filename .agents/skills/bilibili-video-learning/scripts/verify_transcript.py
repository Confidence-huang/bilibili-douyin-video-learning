#!/usr/bin/env python3
r"""
多源交叉校验：把两份转写文本做字符级对齐，输出可穷尽的差异清单。

为什么需要它：
    转写正确性从来不是"某个模型对不对"，而是"两个独立来源是否互相印证"。
    实测案例：同一段 259.77 秒音频，硬字幕与 ASR 有 14 处差异——
    硬字幕是 `血包`、`断气`、`45岁`、`经济价值`，ASR 是 `雪包`、`断亲`、`25岁`、`经营价值`。
    人工逐句比对做不完这件事，而字符级对齐能一次给出**全部**差异及其时间范围，
    于是"我核对过了"变成一张可以复查的清单（见 docs/DECISIONS.md D21）。

    它也能反过来用：把本工具的输出当成"漏采检测"——如果某个来源少了整段正文，
    这里会报出该来源缺失的片段与时间范围，而不是让人去猜。

输入可以是：JSON 时间轴（规范 `start/end/text` 或 ASR `from/to/content`）、
完整提取结果文档（取其中的 `segments`），或 SRT/VTT 字幕文件。

调用示例：
    python verify_transcript.py --primary captions.json --secondary asr.json
    python verify_transcript.py --primary hard.srt --secondary result.json --json
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注

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


# --- 命令行入口 ---
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cross-check two transcript sources character by character.")
    parser.add_argument("--primary", required=True, help="Authoritative source: JSON timeline, extraction result, or SRT/VTT")
    parser.add_argument("--secondary", required=True, help="Source to verify against: same accepted formats")
    parser.add_argument("--min-span-chars", type=int, default=DEFAULT_MIN_SPAN_CHARS,
                        help="Ignore differences shorter than this many characters (default 1: report every character)")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable report")
    parser.add_argument("-o", "--output", help="Write the report to this file instead of stdout")
    parser.add_argument("--fail-on-difference", action="store_true",
                        help="Return exit code 25 when the two sources disagree")
    args = parser.parse_args(argv)

    try:
        primary = load_timeline(args.primary)
        secondary = load_timeline(args.secondary)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[verify_transcript] ERROR: {exc}", file=sys.stderr)
        return 1

    report = compare_timelines(primary, secondary, min_span_chars=args.min_span_chars)
    text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_report(report)

    if args.output:
        from file_output import write_text_atomically                               # 与全仓一致：临时文件 + 原子替换
        print(f"Saved to: {write_text_atomically(args.output, text)}")
    else:
        print(text)

    differing = bool(report["spans"])
    print(f"[verify_transcript] similarity={report['similarity']} spans={len(report['spans'])}: {report['verdict']}",
          file=sys.stderr)
    if differing and args.fail_on_difference:
        return EXIT_SOURCES_DISAGREE                                                 # 让调用方按退出码决定是否人工复核
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
