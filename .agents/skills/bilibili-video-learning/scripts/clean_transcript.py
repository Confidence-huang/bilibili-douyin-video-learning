#!/usr/bin/env python3
r"""
转写清洗：把分段规整成规范形状，并按保真度模式决定是否删词。

两种保真度（这是本脚本最重要的开关）：
    verbatim（默认）：只规整时间轴——合并过短碎片、去掉相邻重复文本，**绝不删词**。
                      逐字稿、字幕、可回溯引用都应该用它。
    cleaned        ：在 verbatim 的基础上，额外丢弃"整段只有填充词"的分段，并报告丢了多少。
                      适合只保留信息量的学习笔记，不适合逐字稿。

历史问题（见 docs/DECISIONS.md D20）：
    1) 只认 `start/end/text`，而 ASR 产出 `from/to/content`，抖音结果喂进来直接 KeyError；
    2) 文档字符串承诺"合并 <0.5s 短段"，代码里算了 duration 却从未使用；
    3) 填充词表会在 verbatim 场景下静默删词。
    现在三条都已修正：入口先过 normalize_transcript 适配层，合并真实生效，删词只在 cleaned 模式发生且必报数。

调用示例：
    python clean_transcript.py transcript.json --fidelity verbatim > cleaned.json
    python clean_transcript.py transcript.json --fidelity cleaned --summary
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注

import argparse                                                                      # 稳定命令行契约：输入、保真度、摘要
import json                                                                          # 输入输出都是同一份 JSON 时间轴
import sys                                                                           # 摘要写 stderr，正文写 stdout

from normalize_transcript import (                                                   # 唯一的分段形状适配层
    SCHEMA_ASR,
    detect_schema,
    normalize_segments,
    summarize_segments,
    validate_segments,
)


FIDELITY_MODES = ("verbatim", "cleaned")                                             # 只有这两种，不允许第三种含糊语义
DEFAULT_MIN_SEGMENT_SECONDS = 0.5                                                    # 短于该值的碎片合并进邻居
DEFAULT_MERGE_GAP_SECONDS = 0.2                                                      # 与邻居间隔小于该值才允许合并
FILLER_ONLY_TEXTS = {                                                                # 仅当整段等于其中之一时才算填充词
    "呃", "嗯", "啊", "那个", "这个", "就是说", "然后呢", "对吧", "是不是",
}


# --- 清洗主流程：先规整，再按模式决定是否删词 ---
def clean_segments(
    segments,
    *,
    fidelity: str = "verbatim",
    min_segment_seconds: float = DEFAULT_MIN_SEGMENT_SECONDS,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
) -> tuple[list[dict], dict]:
    if fidelity not in FIDELITY_MODES:                                               # 模式拼错时必须显式失败，不能默默按默认跑
        raise ValueError(f"unknown fidelity '{fidelity}'; choose one of {FIDELITY_MODES}")

    report = {
        "fidelity": fidelity,
        "input_segments": len(segments or []),
        "input_schema": detect_schema(segments or []),                               # 输入用的是哪种历史形状，便于排查
        "merged_fragments": 0,
        "deduplicated": 0,
        "dropped_fillers": 0,
        "output_segments": 0,
        "problems": [],
    }
    canonical = normalize_segments(segments or [])                                   # 两套历史字段名在这里被统一

    kept: list[dict] = []
    for item in canonical:
        if not item["text"]:                                                         # 空文本段对任何模式都没有价值
            report["dropped_fillers"] += 1 if fidelity == "cleaned" else 0
            continue
        if fidelity == "cleaned" and item["text"] in FILLER_ONLY_TEXTS:              # 只有 cleaned 模式允许删词，且逐条计数
            report["dropped_fillers"] += 1
            continue
        kept.append(dict(item))

    # 先按原始边界去掉相邻重复，再合并短碎片。
    # 顺序不能反：先合并会把重复段与前一碎片粘在一起，导致重复识别失效。
    deduped: list[dict] = []
    for item in kept:
        previous = deduped[-1] if deduped else None
        if previous is not None and previous["text"] == item["text"]:                 # 相邻完全重复：只延长上一段
            previous["end"] = max(previous["end"], item["end"])
            report["deduplicated"] += 1
            continue
        deduped.append(dict(item))

    # 再把"当前段太短且紧邻上一段"的碎片并入上一段，保证顺序不变、文本只做原样拼接。
    merged: list[dict] = []
    for item in deduped:
        previous = merged[-1] if merged else None
        if previous is not None and _is_fragment(item, min_segment_seconds) and _gap_ok(previous, item, merge_gap_seconds):
            previous["end"] = max(previous["end"], item["end"])
            previous["text"] = f"{previous['text']}{item['text']}"                    # 原样拼接，不插入任何字符
            report["merged_fragments"] += 1
            continue
        merged.append(dict(item))
    if len(merged) >= 2 and _is_fragment(merged[0], min_segment_seconds) and _gap_ok(merged[0], merged[1], merge_gap_seconds):
        merged[1]["start"] = merged[0]["start"]                                       # 首段就是碎片时并入下一段
        merged[1]["text"] = f"{merged[0]['text']}{merged[1]['text']}"
        merged.pop(0)
        report["merged_fragments"] += 1

    report["output_segments"] = len(merged)
    report["problems"] = validate_segments(merged)                                   # 交给调用方的自检结论
    report["summary"] = summarize_segments(merged)
    return merged, report


# --- 判断一段是否短到值得合并 ---
def _is_fragment(item: dict, min_segment_seconds: float) -> bool:
    return (item["end"] - item["start"]) < min_segment_seconds


# --- 判断两段之间的间隔是否小到可以安全合并 ---
def _gap_ok(previous: dict, current: dict, merge_gap_seconds: float) -> bool:
    gap = current["start"] - previous["end"]
    return -0.01 <= gap <= merge_gap_seconds


# --- 从命令行读取一批分段（兼容两种历史形状） ---
def load_segments(path: str) -> list:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):                                                    # 也接受完整的提取结果文档
        segments = payload.get("segments") or []
        if segments:
            return segments
        raise ValueError(f"{path} has no non-empty 'segments' list")
    return payload


# --- 命令行入口 ---
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Clean a transcript timeline; verbatim never deletes words.")
    parser.add_argument("transcript", help="Path to a JSON timeline (canonical start/end/text or ASR from/to/content)")
    parser.add_argument("--fidelity", default="verbatim", choices=FIDELITY_MODES,
                        help="verbatim keeps every word; cleaned also drops filler-only segments")
    parser.add_argument("--min-segment-seconds", type=float, default=DEFAULT_MIN_SEGMENT_SECONDS,
                        help="Fragments shorter than this merge into the previous segment")
    parser.add_argument("--summary", action="store_true", help="Print the cleaning report to stderr")
    args = parser.parse_args(argv)

    try:
        segments = load_segments(args.transcript)
        cleaned, report = clean_segments(segments, fidelity=args.fidelity,
                                         min_segment_seconds=args.min_segment_seconds)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[clean_transcript] ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(cleaned, ensure_ascii=False, indent=2))
    if args.summary or report["problems"]:
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)               # 摘要与问题都走 stderr
    if report["input_schema"] == SCHEMA_ASR:
        print("[clean_transcript] 输入是 ASR 形状，已转换为规范形状 start/end/text", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
