"""Dependency-free runtime probe for Skill lifecycle installation gates."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1]
    required = [
        skill_root / "SKILL.md",
        skill_root / "scripts" / "normalize_bilibili_url.py",
        skill_root / "scripts" / "media_tools.py",
    ]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Missing runtime source: {missing}")
    if sys.version_info < (3, 12):
        raise RuntimeError(f"Python 3.12+ required, got {sys.version.split()[0]}")
    print(json.dumps({"status": "PASS", "runtime": "portable-python"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
