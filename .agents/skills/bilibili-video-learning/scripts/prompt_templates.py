#!/usr/bin/env python3
"""Load the versioned note templates shipped in `prompts/`.

The note skeleton used to live inline in each backend (`build_notes.py`,
`fetch_bilibili.py`, `douyin_extract.py`). Keeping it in Markdown files means
the section list and its Chinese headings can be reviewed and changed as text,
instead of hunting string literals across three files.

Templates are opt-in: a missing or unreadable `prompts/` directory must never
break a transcription run, because notes are the last step after the expensive
work is already done. Callers fall back to their built-in skeleton and report
which one they used.
"""
from __future__ import annotations

import re
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"  # scripts/ -> Skill root -> prompts/.
VERSION_PATTERN = re.compile(r"^template-version:\s*(\S+)\s*$", flags=re.MULTILINE)  # First YAML-ish line wins.

# --- 读取模板声明的版本，用于让笔记可追溯 ---
def template_version(text: str) -> str:
    """Return the declared `template-version`, or "unversioned" when absent."""
    match = VERSION_PATTERN.search(text)
    return match.group(1) if match else "unversioned"


# --- 加载一个模板文件；缺失时返回 None 而不是抛错 ---
def load_template(name: str) -> tuple[str, str] | None:
    """Return `(version, body)` for `prompts/<name>.md`, or None when unavailable.

    `name` is a bare stem, never a path: callers pass `"bilibili-standard"`, so a
    typo yields "no template" instead of reading an unexpected file.
    """
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"Template name must be a bare stem, got: {name!r}")
    path = PROMPTS_DIR / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return template_version(raw), raw


# --- 把 `## 标题` 行解析成有序小节 ---
def parse_sections(text: str) -> list[tuple[int, str]]:
    """Parse `## ` headings into `(level, title)` pairs, skipping fenced code.

    Only two- and three-hash headings count, so a `# ` title or a `#### ` note
    cannot masquerade as a section.
    """
    sections: list[tuple[int, str]] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,3})\s+(.*?)\s*$", stripped)
        if match:
            sections.append((len(match.group(1)), match.group(2)))
    return sections


# --- 校验模板文件本身是否完整，供 CI 与测试调用 ---
def validate_template(name: str, required_sections: list[str]) -> list[str]:
    """Return a list of problems; an empty list means the template is usable."""
    loaded = load_template(name)
    if loaded is None:
        return [f"prompts/{name}.md is missing or unreadable"]
    version, body = loaded
    problems: list[str] = []
    if version == "unversioned":
        problems.append(f"prompts/{name}.md declares no template-version")
    titles = {title for _level, title in parse_sections(body)}
    for required in required_sections:
        if required not in titles:
            problems.append(f"prompts/{name}.md is missing section: {required}")
    return problems
