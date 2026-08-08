"""Doctor command: report the exact live Skill, Python, tools, modules, and note path."""
from __future__ import annotations  # 使用现代类型标注。

import os  # 检查当前配置的 Obsidian 路径。

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime  # 所有真实环境检查集中在 runtime。


# --- 收集当前机器可复核的运行状态 ---
def inspect_runtime(runtime: SkillRuntime | None = None) -> dict:
    active_runtime = runtime or SkillRuntime()
    fetch_module = active_runtime.load_script("fetch_bilibili")                # 默认笔记路径与生产脚本保持同源。
    media_tools = active_runtime.load_script("media_tools")                    # FFmpeg 可由 PATH 或用户级 imageio 缓存提供。
    tools = {
        "yt-dlp": active_runtime.inspect_tool("yt-dlp", ["--version"]),
        "ffmpeg": active_runtime.inspect_executable(media_tools.find_ffmpeg(), ["-version"]),
    }
    modules = active_runtime.inspect_python_modules([
        "requests",
        "yt_dlp",
        "faster_whisper",
        "ctranslate2",
    ])
    obsidian_vault = fetch_module.default_obsidian_vault()
    required_modules_ok = all(modules.values())
    overall_ok = all(tool["ok"] for tool in tools.values()) and required_modules_ok
    return {
        "ok": overall_ok,
        "skill_root": str(active_runtime.skill_root),
        "runtime_python": str(active_runtime.runtime_python),
        "tools": tools,
        "python_modules": modules,
        "obsidian_vault": obsidian_vault,
        "obsidian_vault_exists": os.path.isdir(obsidian_vault),
    }
