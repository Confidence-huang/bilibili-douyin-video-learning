#!/usr/bin/env python3
"""
Multi-video synthesis: compare and cross-reference multiple sources.
Inspired by karpathy-llm-wiki-vault's syntheses concept.

Usage:
  python vault_synthesize.py <vault_path> <topic_name> [source_ids...]
"""
import os
import sys
import re
import json
import argparse
from datetime import datetime


def slugify(text):
    text = re.sub(r'[\\/:*?"<>|]', '_', text)
    text = re.sub(r'\s+', '_', text)
    return text[:80]


def list_sources(vault_path):
    """List all source pages in the vault."""
    source_dir = os.path.join(vault_path, "wiki", "sources")
    if not os.path.exists(source_dir):
        return []

    sources = []
    for f in os.listdir(source_dir):
        if f.endswith(".md"):
            path = os.path.join(source_dir, f)
            with open(path, "r", encoding="utf-8") as fp:
                content = fp.read(500)
                m = re.search(r'title:\s*"(.*?)"', content)
                title = m.group(1) if m else f
            sources.append({"file": f, "path": path, "title": title})
    return sources


def create_synthesis(vault_path, topic_name, source_titles=None, theme=None):
    """
    Create a synthesis page comparing multiple sources.

    Args:
        vault_path: Path to vault root
        topic_name: Name of the synthesis topic
        source_titles: List of source page titles to compare
        theme: Optional theme/category
    """
    synth_dir = os.path.join(vault_path, "wiki", "syntheses")
    os.makedirs(synth_dir, exist_ok=True)

    slug = slugify(topic_name)
    today = datetime.now().strftime("%Y-%m-%d")

    src_list = "\n".join([
        f"## {t}\n\n_[由 LLM 填充：该视频/文章的核心观点]_"
        for t in (source_titles or ["Source A", "Source B"])
    ]) if source_titles else "[由 LLM 填充：关联的多个来源]"

    content = f"""---
title: "{topic_name}"
type: synthesis
tags: ["synthesis"{', "' + theme + '"' if theme else ''}"]
sources: {json.dumps(source_titles or [])}
last_updated: {today}
---

# {topic_name}

## 综合概述

_[由 LLM 填充：综合多个视频/文章后的总体结论]_

## 各来源观点

{src_list}

## 共同点

- [由 LLM 填充]
- [由 LLM 填充]

## 分歧点

| 议题 | 观点 A | 观点 B | 分析 |
|------|--------|--------|------|
| [议题1] | [观点] | [观点] | [分析] |

## 学习路线

_[由 LLM 填充：推荐的学习顺序]_

## 关联连接

_[由 LLM 填充：双链到概念、实体、来源]_
"""

    filepath = os.path.join(synth_dir, f"{slug}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    # Update index
    index_path = os.path.join(vault_path, "wiki", "index.md")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            idx = f.read()

        entry = f"[[{slug}]] — {topic_name} {{: .synthesis}}"
        marker = "<!-- syntheses -->"
        if marker in idx and entry not in idx:
            idx = idx.replace(marker, f"{marker}\n- {entry}")
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(idx)

    # Log
    log_path = os.path.join(vault_path, "wiki", "log.md")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n## [{today}] synthesize\n- 创建综合分析 [[{slug}]]\n")

    return filepath


def build_comparison_table(vault_path, source_files):
    """Build a comparison table from multiple source pages."""
    rows = []

    for sf in source_files:
        if os.path.exists(sf):
            with open(sf, "r", encoding="utf-8") as f:
                content = f.read()
            m_title = re.search(r'title:\s*"(.*?)"', content)
            m_author = re.search(r'author:\s*"(.*?)"', content)
            m_duration = re.search(r'duration:\s*(\d+)', content)
            m_url = re.search(r'url:\s*"(.*?)"', content)

            rows.append({
                "title": m_title.group(1) if m_title else "?",
                "author": m_author.group(1) if m_author else "?",
                "duration": int(m_duration.group(1)) if m_duration else 0,
                "url": m_url.group(1) if m_url else "",
            })

    return rows


# --- CLI ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-video synthesis tool")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List sources in vault")
    p_list.add_argument("vault", help="Vault path")

    p_create = sub.add_parser("create", help="Create a synthesis page")
    p_create.add_argument("vault", help="Vault path")
    p_create.add_argument("topic", help="Synthesis topic name")
    p_create.add_argument("--theme", "-t", help="Theme/category tag")

    p_compare = sub.add_parser("compare", help="Compare source files")
    p_compare.add_argument("files", nargs="+", help="Source .md files to compare")

    args = parser.parse_args()

    if args.command == "list":
        sources = list_sources(args.vault)
        print(f"\nSources in vault ({len(sources)}):\n")
        for s in sources:
            print(f"  • {s['title']}")
            print(f"    File: {s['file']}")

    elif args.command == "create":
        path = create_synthesis(args.vault, args.topic, theme=args.theme)
        print(f"Synthesis created: {path}")

    elif args.command == "compare":
        rows = build_comparison_table(None, args.files)
        if rows:
            print("\n| 标题 | 作者 | 时长 | URL |")
            print("|------|------|------|-----|")
            for r in rows:
                dur = f"{r['duration']//60}m{r['duration']%60}s" if r['duration'] else "?"
                print(f"| {r['title'][:30]} | {r['author']} | {dur} | {r['url']} |")

    else:
        parser.print_help()
