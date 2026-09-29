#!/usr/bin/env python3
r"""
把转写时间轴切成固定时长的块，供长视频分段阅读与笔记分节使用。

为什么要先过适配层（见 docs/DECISIONS.md D20）：
    本脚本以前只认 `start/end/text`，而 ASR 产出 `from/to/content`，
    抖音的转写结果直接喂进来会 KeyError，等于对 ASR 结果不可用。
    现在入口统一调用 `normalize_transcript`，两种历史形状都能吃。

调用示例：
    python chunk_transcript.py transcript.json 300 > chunks.md
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注

import argparse                                                                      # 稳定命令行契约：输入与分块时长
import json                                                                          # 输入是同一份 JSON 时间轴
import sys                                                                           # 摘要写 stderr

from normalize_transcript import normalize_segments, validate_segments               # 唯一的分段形状适配层


DEFAULT_INTERVAL_SECONDS = 300                                                        # 默认 5 分钟一块，适合长课程


# --- 按固定时长切块 ---
def chunk_by_time(segments, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> list[dict]:
    canonical = normalize_segments(segments)                                         # 两种历史形状在这里被统一
    if not canonical:
        return []

    chunks: list[dict] = []
    chunk_text: list[str] = []
    chunk_start = canonical[0]["start"]
    for item in canonical:
        if item["start"] - chunk_start > interval_seconds and chunk_text:            # 超过块长且已有内容时才收口
            chunks.append({"start": chunk_start, "end": item["start"], "text": "\n".join(chunk_text)})
            chunk_start = item["start"]
            chunk_text = []
        chunk_text.append(f"[{item['start']:.1f}s] {item['text']}")                  # 保留段内时间戳，便于回看
    if chunk_text:                                                                   # 最后一块
        chunks.append({"start": chunk_start, "end": canonical[-1]["end"], "text": "\n".join(chunk_text)})
    return chunks


# --- 把块渲染成 Markdown 小节 ---
def format_chunks_markdown(chunks: list[dict]) -> str:
    output: list[str] = []
    for chunk in chunks:
        start_minutes, start_seconds = divmod(int(chunk["start"]), 60)
        end_minutes, end_seconds = divmod(int(chunk["end"]), 60)
        output.append(f"### {start_minutes:02d}:{start_seconds:02d}-{end_minutes:02d}:{end_seconds:02d}")
        output.append(chunk["text"])
        output.append("")                                                            # 小节之间留空行
    return "\n".join(output)


# --- 从命令行读取一份时间轴（兼容两种历史形状） ---
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
    parser = argparse.ArgumentParser(description="Chunk a transcript timeline into fixed-duration sections.")
    parser.add_argument("transcript", help="Path to a JSON timeline (canonical or ASR field names)")
    parser.add_argument("interval", nargs="?", type=int, default=DEFAULT_INTERVAL_SECONDS,
                        help=f"Chunk length in seconds (default {DEFAULT_INTERVAL_SECONDS})")
    args = parser.parse_args(argv)

    try:
        segments = load_segments(args.transcript)
        canonical = normalize_segments(segments)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[chunk_transcript] ERROR: {exc}", file=sys.stderr)
        return 1

    chunks = chunk_by_time(canonical, args.interval)
    print(format_chunks_markdown(chunks))
    problems = validate_segments(canonical)
    print(f"[chunk_transcript] {len(chunks)} chunks at {args.interval}s from {len(canonical)} segments"
          + (f"; {len(problems)} timeline problems" if problems else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
