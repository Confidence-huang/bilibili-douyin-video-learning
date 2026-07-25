#!/usr/bin/env python3
"""
Knowledge vault pipeline: raw → wiki.
Inspired by karpathy-llm-wiki-vault architecture.

Usage:
  python vault_ingest.py init [--dir <vault_path>]              # Initialize vault
  python vault_ingest.py add <source_file> [--dir <vault_path>] # Ingest a source
  python vault_ingest.py index [--dir <vault_path>]             # Update index
  python vault_ingest.py log <message> [--dir <vault_path>]     # Append to log
"""
import os
import re
import sys
import json
import shutil
import argparse
from datetime import datetime


# --- Vault structure ---

VAULT_DIRS = [
    "raw/transcripts",
    "raw/metadata",
    "raw/archive",
    "wiki/concepts",
    "wiki/entities",
    "wiki/sources",
    "wiki/syntheses",
]


def init_vault(path):
    """Initialize vault directory structure."""
    for d in VAULT_DIRS:
        os.makedirs(os.path.join(path, d), exist_ok=True)

    # Create index.md if not exists
    index_path = os.path.join(path, "wiki", "index.md")
    if not os.path.exists(index_path):
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("# 知识库索引\n\n")
            f.write(f"创建时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write("## 来源 (Sources)\n\n<!-- sources -->\n\n")
            f.write("## 实体 (Entities)\n\n<!-- entities -->\n\n")
            f.write("## 概念 (Concepts)\n\n<!-- concepts -->\n\n")
            f.write("## 综合分析 (Syntheses)\n\n<!-- syntheses -->\n")

    # Create log.md if not exists
    log_path = os.path.join(path, "wiki", "log.md")
    if not os.path.exists(log_path):
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("# 操作日志\n\n")

    return path


def slugify(text):
    """Convert text to safe filename slug."""
    # Remove special chars, keep Chinese/alphanumeric/underscore
    text = re.sub(r'[\\/:*?"<>|]', '_', text)
    text = re.sub(r'\s+', '_', text)
    return text[:80]


def get_title_from_frontmatter(content):
    """Extract title from YAML frontmatter."""
    match = re.search(r'^title:\s*"(.*?)"', content, re.MULTILINE)
    if match:
        return match.group(1)
    # Try first H1
    match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    if match:
        return match.group(1)
    return "Untitled"


def add_source(vault_path, source_file, source_type="bilibili"):
    """
    Add a source to the vault.

    Args:
        vault_path: Path to vault root
        source_file: Path to Markdown or JSON source file
        source_type: 'bilibili', 'article', 'paper', etc.

    Returns:
        dict with paths of created files
    """
    vault_path = os.path.abspath(vault_path)

    # Ensure vault exists
    init_vault(vault_path)

    # Read source
    with open(source_file, "r", encoding="utf-8") as f:
        content = f.read()

    title = get_title_from_frontmatter(content)
    slug = slugify(title)
    today = datetime.now().strftime("%Y-%m-%d")

    result = {"vault": vault_path, "files": []}

    # 1. Copy raw file
    if source_type == "bilibili":
        dest_dir = os.path.join(vault_path, "raw", "transcripts")
    else:
        dest_dir = os.path.join(vault_path, "raw", "metadata")

    raw_name = f"{today}-{slug}.md"
    raw_path = os.path.join(dest_dir, raw_name)
    shutil.copy2(source_file, raw_path)
    result["raw_path"] = raw_path
    result["files"].append(raw_path)

    # 2. Create source summary page
    source_page = f"""---
title: "{title}"
type: source
tags: ["{source_type}"]
sources: ["raw/{'transcripts' if source_type == 'bilibili' else 'metadata'}/{raw_name}"]
last_updated: {today}
---

# {title}

## 核心主旨

_[由 LLM 填充：3-5 句话概括视频/文章的核心内容和价值]_

## 关联实体

_[由 LLM 填充：提及的人物、工具、公司等实体]_

## 关联概念

_[由 LLM 填充：涉及的概念、框架、方法论]_

## 关联连接

_[由 LLM 填充：双链 [[Entity]] / [[Concept]]]_
"""

    # If the content already has an abstract, extract it
    abstract_match = re.search(r'##\s+简介\s*\n+(.+?)(?=\n##|\Z)', content, re.DOTALL)
    if abstract_match and "[由 LLM 填充" not in abstract_match.group(1):
        abstract = abstract_match.group(1).strip()
        source_page = source_page.replace(
            "[由 LLM 填充：3-5 句话概括视频/文章的核心内容和价值]",
            abstract
        )

    source_path = os.path.join(vault_path, "wiki", "sources", f"摘要-{slug}.md")
    with open(source_path, "w", encoding="utf-8") as f:
        f.write(source_page)
    result["source_path"] = source_path
    result["files"].append(source_path)

    # 3. Update index
    update_index_entry(vault_path, "sources", f"[[摘要-{slug}]] — {title} {{: .source}}")
    result["files"].append(os.path.join(vault_path, "wiki", "index.md"))

    # 4. Log
    append_log(vault_path, "ingest", f"新增来源 [[摘要-{slug}]] ({source_type})")
    result["files"].append(os.path.join(vault_path, "wiki", "log.md"))

    return result


def create_entity(vault_path, name, description="", sources=None):
    """Create an entity page in the vault."""
    init_vault(vault_path)
    slug = slugify(name)
    today = datetime.now().strftime("%Y-%m-%d")

    page_path = os.path.join(vault_path, "wiki", "entities", f"{slug}.md")

    if os.path.exists(page_path):
        print(f"  Entity '{name}' already exists, skipping.")
        return page_path

    src_list = "\n".join([f"- {s}" for s in (sources or [])])

    content = f"""---
title: "{name}"
type: entity
tags: ["entity"]
sources: {json.dumps(sources or [])}
last_updated: {today}
---

# {name}

{description or "[由 LLM 填充]"}

## 关联连接

{src_list}

_[由 LLM 填充：双链 [[Concept]] 和其他实体]_
"""

    with open(page_path, "w", encoding="utf-8") as f:
        f.write(content)

    update_index_entry(vault_path, "entities", f"[[{slug}]] — {name} {{: .entity}}")
    append_log(vault_path, "ingest", f"新增实体 [[{slug}]]")

    return page_path


def create_concept(vault_path, name, definition="", sources=None):
    """Create a concept page in the vault."""
    init_vault(vault_path)
    slug = slugify(name)
    today = datetime.now().strftime("%Y-%m-%d")

    page_path = os.path.join(vault_path, "wiki", "concepts", f"{slug}.md")

    if os.path.exists(page_path):
        print(f"  Concept '{name}' already exists, skipping.")
        return page_path

    src_list = "\n".join([f"- {s}" for s in (sources or [])])

    content = f"""---
title: "{name}"
type: concept
tags: ["concept"]
sources: {json.dumps(sources or [])}
last_updated: {today}
---

# {name}

## 定义

{definition or "[由 LLM 填充]"}

## 关联连接

{src_list}

_[由 LLM 填充：相关实体、其他概念、来源的双链]_
"""

    with open(page_path, "w", encoding="utf-8") as f:
        f.write(content)

    update_index_entry(vault_path, "concepts", f"[[{slug}]] — {name} {{: .concept}}")
    append_log(vault_path, "ingest", f"新增概念 [[{slug}]]")

    return page_path


# --- Index management ---

def update_index_entry(vault_path, section, entry):
    """Add an entry to the vault index.md."""
    index_path = os.path.join(vault_path, "wiki", "index.md")

    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()

    marker = f"<!-- {section} -->"
    if marker not in content:
        content += f"\n## {section}\n\n{marker}\n"

    # Check if entry already exists
    entry_name = entry.split(" — ")[0] if " — " in entry else entry
    if entry_name in content:
        return  # Already registered

    # Insert after marker
    content = content.replace(marker, f"{marker}\n- {entry}")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(content)


def append_log(vault_path, operation, message):
    """Append to vault log."""
    log_path = os.path.join(vault_path, "wiki", "log.md")
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = f"\n## [{today}] {operation}\n- {message}\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def vault_status(vault_path):
    """Show vault status summary."""
    if not os.path.exists(vault_path):
        return {"error": f"Vault not found at {vault_path}"}

    status = {"path": vault_path}

    for d in VAULT_DIRS:
        full = os.path.join(vault_path, d)
        if os.path.exists(full):
            count = len([f for f in os.listdir(full) if f.endswith(".md")])
            status[d] = count
        else:
            status[d] = 0

    return status


# --- CLI ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Knowledge vault ingestion pipeline")
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Initialize a new vault")
    p_init.add_argument("--dir", "-d", default=".", help="Vault directory")

    # add
    p_add = sub.add_parser("add", help="Add a source to vault")
    p_add.add_argument("source", help="Path to source Markdown/JSON file")
    p_add.add_argument("--dir", "-d", default=".", help="Vault directory")
    p_add.add_argument("--type", "-t", default="bilibili", help="Source type")

    # new-entity
    p_ent = sub.add_parser("new-entity", help="Create an entity page")
    p_ent.add_argument("name", help="Entity name")
    p_ent.add_argument("--dir", "-d", default=".", help="Vault directory")
    p_ent.add_argument("--desc", default="", help="Description")

    # new-concept
    p_con = sub.add_parser("new-concept", help="Create a concept page")
    p_con.add_argument("name", help="Concept name")
    p_con.add_argument("--dir", "-d", default=".", help="Vault directory")
    p_con.add_argument("--desc", default="", help="Definition")

    # status
    p_stat = sub.add_parser("status", help="Show vault status")
    p_stat.add_argument("--dir", "-d", default=".", help="Vault directory")

    args = parser.parse_args()

    if args.command == "init":
        path = init_vault(args.dir)
        print(f"Vault initialized at: {os.path.abspath(path)}")
        for d in VAULT_DIRS:
            print(f"  {d}/")

    elif args.command == "add":
        result = add_source(args.dir, args.source, args.type)
        print(f"Source ingested: {result['source_path']}")
        for f in result.get("files", []):
            print(f"  → {f}")

    elif args.command == "new-entity":
        path = create_entity(args.dir, args.name, args.desc)
        print(f"Entity created: {path}")

    elif args.command == "new-concept":
        path = create_concept(args.dir, args.name, args.desc)
        print(f"Concept created: {path}")

    elif args.command == "status":
        status = vault_status(args.dir)
        print(f"Vault: {os.path.abspath(args.dir)}")
        for k, v in sorted(status.items()):
            if k != "path":
                print(f"  {k}: {v} files")

    else:
        parser.print_help()
