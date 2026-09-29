"""Doctor command: report the exact live Skill, Python, tools, modules, and note path."""
from __future__ import annotations  # 使用现代类型标注。

import os  # 检查当前配置的 Obsidian 路径。

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime  # 所有真实环境检查集中在 runtime。


# --- 探测 GPU 是否真的可用（可见性 + 运行时库，不参与 ok 判定） ---
def _inspect_gpu(runtime: SkillRuntime | None = None) -> dict:
    if runtime is None:                                                          # 允许测试直接调用而不准备 runtime。
        try:
            import ctranslate2                                                   # 与 ASR 路线同一后端，结论可直接对上运行日志。

            devices = ctranslate2.get_cuda_device_count()
            return {"available": devices > 0, "devices": devices, "error": None}
        except Exception as exc:                                                 # DLL 缺失、驱动异常都不该让 doctor 整体失败。
            return {"available": False, "devices": 0, "error": str(exc)}

    cuda_runtime = runtime.load_script("cuda_runtime")                            # 与 ASR 入口共用同一份判断，避免两处结论不一致。
    usability = cuda_runtime.describe_gpu_usability()
    return {
        "available": usability["device_count"] > 0,                               # 可见性：设备枚举成功
        "devices": usability["device_count"],
        "usable": usability["usable"],                                            # 可用性：可见且 cuBLAS/cuDNN 都能加载
        "runtime_libraries": usability["runtime_libraries"],
        "guidance": usability["guidance"],                                        # 不可用时给出确切的安装命令
        "error": usability["error"],
    }


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
        "gpu": _inspect_gpu(active_runtime),
        "obsidian_vault": obsidian_vault,
        "obsidian_vault_exists": os.path.isdir(obsidian_vault),
    }
