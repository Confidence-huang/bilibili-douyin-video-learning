"""Skill 资产盘点：让 `doctor status` 能看到词表/裁决表/分块这些隐式状态（见 D39）。

为什么放在 core 而不是直接写在命令里：
    这些是"可被测试直接调用"的纯函数（读文件、数条目），放进命令函数里就只能靠 CLI 端到端测。
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import re  # 从源码里读出分块默认值，而不是硬编码一份可能过期的说法
from pathlib import Path  # 统一处理路径


CHUNK_DEFAULT_PATTERN = re.compile(r"chunk_length:\s*float\s*=\s*([0-9.]+)")
MODEL_DEFAULT_PATTERN = re.compile(r'"--model",\s*(?:default=|"-m",\s*default=)?\s*"([a-z0-9-]+)"')


# --- 数出"非注释、非空"的条目数（注释行是说明与证据，不算条目）---
def count_entries(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
               if line.strip() and not line.lstrip().startswith("#"))


# --- 读取本地基准历史里最近一次测量（没有历史就如实返回 None）---
def read_latest_benchmark(history_path=None) -> dict:
    import json
    import os

    raw = history_path or os.environ.get("VIDEO_LEARNING_HISTORY")
    if not raw:
        return {}
    path = Path(raw)
    if not path.exists():
        return {}
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}


# --- 盘点 Skill 资产：词表、裁决表、分块可用性与默认值、提示词模板 ---
def inspect_skill_assets(skill_root: str | Path) -> dict:
    root = Path(skill_root)
    scripts = root / "scripts"
    settings_source = scripts / "speech_to_text.py"
    default_match = None
    if settings_source.exists():
        default_match = CHUNK_DEFAULT_PATTERN.search(settings_source.read_text(encoding="utf-8", errors="replace"))
    templates = sorted((root / "prompts").glob("*.md")) if (root / "prompts").exists() else []
    default_model = None
    for script in ("douyin_extract.py", "transcribe_bilibili.py", "transcribe_audio_cli.py"):
        source = scripts / script
        if source.exists():
            match = MODEL_DEFAULT_PATTERN.search(source.read_text(encoding="utf-8", errors="replace"))
            if match:
                default_model = match.group(1)
                break
    try:                                                                   # 归一依赖在**CLI 自己的运行环境**里是否可用
        import importlib.util as _importlib_util

        opencc_available = _importlib_util.find_spec("opencc") is not None
    except Exception:
        opencc_available = False
    return {
        "latest_benchmark": read_latest_benchmark(),                           # 本地历史里最近一次测量（D53）
        "lexicon_entries": count_entries(root / "references" / "asr-lexicon.txt"),
        "conflict_preference_entries": count_entries(root / "references" / "conflict-preferences.txt"),
        "chunking_available": (scripts / "asr_chunking.py").exists(),
        "chunk_length_default": float(default_match.group(1)) if default_match else None,
        "prompt_templates": [item.name for item in templates],
        "opencc_available": opencc_available,                                  # False 时归一降级为 fallback（D48）
        "simplification_mode": "opencc" if opencc_available else "fallback",
        "default_model": default_model,                                        # auto = 有 CUDA 用 large（D43）
    }
