#!/usr/bin/env python3
"""Build final Markdown notes from metadata, transcript chunks, and summaries."""
import json
import sys
from datetime import datetime

from prompt_templates import load_template

# Section headings come from `prompts/<name>.md` so the note skeleton is reviewable
# as text rather than as string literals. The fallback below is the original inline
# skeleton: a missing prompts/ directory must not break a run that already spent
# minutes on ASR, so it degrades to the built-in headings and reports which was used.
TEMPLATE_FILES = {"standard": "bilibili-standard", "quick": "bilibili-standard"}
FALLBACK_SECTIONS = [
    "基本信息",
    "视频简介",
    "一句话总结",
    "3 分钟速读",
    "详细笔记",
    "核心概念",
    "复习题",
    "待确认",
]


def _template_sections(template: str) -> tuple[dict, str]:
    """Return (section title -> heading text, template version) for a note template."""
    template_file = TEMPLATE_FILES.get(template, template)
    loaded = load_template(template_file)
    if loaded is None:
        return {title: f"## {title}" for title in FALLBACK_SECTIONS}, "builtin-fallback"
    version, body = loaded
    sections: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            sections[stripped[3:].strip()] = stripped
    return (sections or {title: f"## {title}" for title in FALLBACK_SECTIONS}), version


def build_notes(metadata: dict, chunks: list, template: str = "standard") -> str:
    """Assemble learning notes from components."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections, template_version = _template_sections(template)

    title = metadata.get('title', 'Untitled Video')
    url = metadata.get('canonical_url', metadata.get('source_url', ''))
    uploader = metadata.get('uploader', 'Unknown')
    pub_date = metadata.get('publish_date', 'Unknown')
    duration = metadata.get('duration_seconds', 0)
    duration_str = f"{duration//60}:{duration%60:02d}" if duration else 'Unknown'

    lines = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(sections.get("基本信息", "## 基本信息"))
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
        lines.append(sections.get("视频简介", "## 视频简介"))
        lines.append("")
        lines.append(metadata['description'])
        lines.append("")

    lines.append(sections.get("一句话总结", "## 一句话总结"))
    lines.append("")
    lines.append("...")
    lines.append("")

    lines.append(sections.get("3 分钟速读", "## 3 分钟速读"))
    lines.append("")
    lines.append("- ...")
    lines.append("")

    lines.append(sections.get("详细笔记", "## 详细笔记"))
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

    lines.append(sections.get("核心概念", "## 核心概念"))
    lines.append("")
    lines.append("| 概念 | 解释 | 视频中的例子 |")
    lines.append("|------|------|-------------|")
    lines.append("| ... | ... | ... |")
    lines.append("")

    lines.append(sections.get("复习题", "## 复习题"))
    lines.append("")
    lines.append("1. ...")
    lines.append("2. ...")
    lines.append("")

    lines.append(sections.get("待确认", "## 待确认"))
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
    # stderr keeps the report out of the piped Markdown on stdout.
    print(f"[build_notes] template={_template_sections('standard')[1]}", file=sys.stderr)
