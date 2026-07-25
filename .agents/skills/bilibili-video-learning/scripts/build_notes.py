#!/usr/bin/env python3
"""Build final Markdown notes from metadata, transcript chunks, and summaries."""
import json
import sys
from datetime import datetime

def build_notes(metadata: dict, chunks: list, template: str = "standard") -> str:
    """Assemble learning notes from components."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    title = metadata.get('title', 'Untitled Video')
    url = metadata.get('canonical_url', metadata.get('source_url', ''))
    uploader = metadata.get('uploader', 'Unknown')
    pub_date = metadata.get('publish_date', 'Unknown')
    duration = metadata.get('duration_seconds', 0)
    duration_str = f"{duration//60}:{duration%60:02d}" if duration else 'Unknown'

    lines = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append("## 基本信息")
    lines.append("")
    lines.append(f"| 字段 | 内容 |")
    lines.append(f"|------|------|")
    lines.append(f"| 平台 | Bilibili |")
    lines.append(f"| 链接 | {url} |")
    lines.append(f"| UP 主 | {uploader} |")
    lines.append(f"| 发布时间 | {pub_date} |")
    lines.append(f"| 时长 | {duration_str} |")
    lines.append(f"| 获取时间 | {now} |")
    lines.append("")

    if metadata.get('description'):
        lines.append("## 视频简介")
        lines.append("")
        lines.append(metadata['description'])
        lines.append("")

    lines.append("## 一句话总结")
    lines.append("")
    lines.append("...")
    lines.append("")

    lines.append("## 3 分钟速读")
    lines.append("")
    lines.append("- ...")
    lines.append("")

    lines.append("## 详细笔记")
    lines.append("")

    for chunk in chunks:
        start_m = int(chunk['start'] // 60)
        start_s = int(chunk['start'] % 60)
        end_m = int(chunk['end'] // 60)
        end_s = int(chunk['end'] % 60)
        lines.append(f"### {start_m:02d}:{start_s:02d}-{end_m:02d}:{end_s:02d}")
        lines.append("")
        lines.append(chunk.get('text', chunk.get('summary', '')))
        lines.append("")

    lines.append("## 核心概念")
    lines.append("")
    lines.append("| 概念 | 解释 | 视频中的例子 |")
    lines.append("|------|------|-------------|")
    lines.append("| ... | ... | ... |")
    lines.append("")

    lines.append("## 复习题")
    lines.append("")
    lines.append("1. ...")
    lines.append("2. ...")
    lines.append("")

    lines.append("## 待确认")
    lines.append("")
    lines.append("- [原文不明确] ...")
    lines.append("")

    return '\n'.join(lines)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python build_notes.py <metadata.json> [chunks.json]")
        sys.exit(1)

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        metadata = json.load(f)

    chunks = []
    if len(sys.argv) > 2:
        with open(sys.argv[2], 'r', encoding='utf-8') as f:
            chunks = json.load(f)

    output = build_notes(metadata, chunks)
    print(output)
