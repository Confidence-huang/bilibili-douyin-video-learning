"""Dependency-free identity behavior probe for Skill lifecycle gates."""
from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from normalize_bilibili_url import normalize


def main() -> int:
    result = normalize("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
    observed = {"platform": result.get("platform"), "page": result.get("page")}
    if observed != {"platform": "bilibili", "page": 2}:
        raise RuntimeError(f"Unexpected normalization: {observed}")
    print(json.dumps({"status": "PASS", **observed}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
