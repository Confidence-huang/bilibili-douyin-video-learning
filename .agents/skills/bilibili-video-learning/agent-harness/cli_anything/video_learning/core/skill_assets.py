"""Skill 资产盘点：让 `doctor status` 能看到词表/裁决表/分块这些隐式状态（见 D39）。

为什么放在 core 而不是直接写在命令里：
    这些是"可被测试直接调用"的纯函数（读文件、数条目），放进命令函数里就只能靠 CLI 端到端测。
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import re  # 从源码里读出分块默认值，而不是硬编码一份可能过期的说法
from pathlib import Path  # 统一处理路径


CHUNK_DEFAULT_PATTERN = re.compile(r"chunk_length:\s*float\s*=\s*([0-9.]+)")


# --- 数出"非注释、非空"的条目数（注释行是说明与证据，不算条目）---
def count_entries(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
               if line.strip() and not line.lstrip().startswith("#"))


# --- 盘点 Skill 资产：词表、裁决表、分块可用性与默认值、提示词模板 ---
def inspect_skill_assets(skill_root: str | Path) -> dict:
    root = Path(skill_root)
    scripts = root / "scripts"
    settings_source = scripts / "speech_to_text.py"
    default_match = None
    if settings_source.exists():
        default_match = CHUNK_DEFAULT_PATTERN.search(settings_source.read_text(encoding="utf-8", errors="replace"))
    templates = sorted((root / "prompts").glob("*.md")) if (root / "prompts").exists() else []
    return {
        "lexicon_entries": count_entries(root / "references" / "asr-lexicon.txt"),
        "conflict_preference_entries": count_entries(root / "references" / "conflict-preferences.txt"),
        "chunking_available": (scripts / "asr_chunking.py").exists(),
        "chunk_length_default": float(default_match.group(1)) if default_match else None,
        "prompt_templates": [item.name for item in templates],
    }
