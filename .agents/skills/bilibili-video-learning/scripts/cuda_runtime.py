#!/usr/bin/env python3
r"""
让本机 GPU 真正可用：发现并预加载 CUDA 运行时库。

为什么需要这个模块（真实事故，见 docs/DECISIONS.md D23）：
    在 WSL2 上 GPU 直通是好的——`/dev/dxg` 存在、`/usr/lib/wsl/lib/libcuda.so.1` 在加载器路径里、
    `ctranslate2.get_cuda_device_count()` 返回 1。但 WSL 驱动只提供 `libcuda.so`，
    **不含 cuBLAS 与 cuDNN**。于是链路变成：设备枚举通过 → 选中 cuda/float16 →
    模型构造也通过 → **第一次 `encode()` 才抛** `Library libcublas.so.12 is not found`。
    排查这件事花了很久，本模块的目标就是"下次不用再排查"。

两个必须踩对的细节：
    1. **改 `os.environ["LD_LIBRARY_PATH"]` 对当前进程已经启动的动态加载器无效**——
       glibc 只在进程启动时读一次。真正管用的是用 `ctypes.CDLL(绝对路径, RTLD_GLOBAL)` 预加载，
       之后 `dlopen("libcublas.so.12")` 会解析到已加载的那一份。子进程仍然受益于环境变量，所以两者都做。
    2. **加载顺序有依赖**：cublasLt 依赖 cublas，cudnn 依赖 cudart/cublas。
       顺序错了会得到 "cannot open shared object file"，看起来像"没装"，实际是顺序问题。

调用示例：
    from cuda_runtime import prepare_cuda_libraries
    report = prepare_cuda_libraries()      # 在 import ctranslate2 / faster_whisper 之前调用
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注

import ctypes                                                                        # 预加载是唯一对当前进程有效的办法
import os                                                                            # Windows 需要 add_dll_directory，Linux 需要 LD_LIBRARY_PATH
import site                                                                          # 只扫描 site-packages 里的 nvidia/* 目录
import sys                                                                           # 需要知道当前解释器与平台


PRELOAD_ORDER = ("cudart", "cublasLt", "cublas", "cudnn")                            # 依赖在前，被依赖者先加载
LIBRARY_NAME_PATTERNS = {                                                            # 不同 CUDA 主版本的文件名差异
    "cudart": ("libcudart.so.12", "libcudart.so.13", "cudart64_12.dll"),
    "cublasLt": ("libcublasLt.so.12", "libcublasLt.so.13", "cublasLt64_12.dll"),
    "cublas": ("libcublas.so.12", "libcublas.so.13", "cublas64_12.dll"),
    "cudnn": ("libcudnn.so.9", "libcudnn.so.8", "cudnn64_9.dll"),
}
PIP_EXTRA_HINT = 'pip install -e ".[gpu-cuda12]"'                                    # 缺库时给用户的具体命令


# --- 列出可能存放 nvidia 运行时库的目录 ---
def candidate_library_dirs() -> list[str]:
    roots: list[str] = []
    for base in {sys.prefix, sys.base_prefix, getattr(site, "getusersitepackages", lambda: "")()}:  # venv 与用户级都要看
        if not base:
            continue
        packages = os.path.join(base, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")
        roots.extend([
            os.path.join(packages, "nvidia"),
            os.path.join(base, "Lib", "site-packages", "nvidia"),                     # Windows 布局
        ])
        roots.append(os.path.join(base, "nvidia"))
    directories: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for package in sorted(os.listdir(root)):                                      # nvidia/{cublas,cudnn,cuda_runtime,...}
            for leaf in ("lib", "bin"):
                directory = os.path.join(root, package, leaf)
                if os.path.isdir(directory) and directory not in directories:
                    directories.append(directory)
    return directories


# --- 在候选目录里找出一份可用库文件 ---
def discover_libraries(directories: list[str] | None = None) -> dict[str, str]:
    directories = directories if directories is not None else candidate_library_dirs()
    found: dict[str, str] = {}
    for directory in directories:
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for key, patterns in LIBRARY_NAME_PATTERNS.items():
            if key in found:
                continue
            for pattern in patterns:
                if pattern in entries:
                    found[key] = os.path.join(directory, pattern)
                    break
    return found


# --- 预加载 CUDA 运行时库，并让子进程也能找到它们 ---
def prepare_cuda_libraries(directories: list[str] | None = None, *, preload: bool = True) -> dict:
    directories = directories if directories is not None else candidate_library_dirs()
    report = {
        "platform": "windows" if os.name == "nt" else "posix",
        "searched_directories": directories,
        "found": {},
        "preloaded": [],
        "errors": [],
        "path_updated": False,
    }
    libraries = discover_libraries(directories)
    report["found"] = libraries
    if not libraries:
        report["guidance"] = f"未找到 nvidia 运行时库；需要 GPU 时请安装：{PIP_EXTRA_HINT}"
        return report

    if preload:
        for key in PRELOAD_ORDER:                                                     # 依赖顺序：先 cudart 再 cudnn
            path = libraries.get(key)
            if path is None:
                continue
            try:
                ctypes.CDLL(path, mode=getattr(ctypes, "RTLD_GLOBAL", 0))              # RTLD_GLOBAL 让后续 dlopen 复用
                report["preloaded"].append(path)
            except OSError as exc:                                                    # 单个库失败不阻断其余库
                report["errors"].append(f"{os.path.basename(path)}: {exc}")

    if os.name == "nt":                                                              # Windows 用 DLL 目录，比 PATH 更可靠
        for directory in directories:
            try:
                os.add_dll_directory(directory)                                       # 需要保留返回的 cookie，交给进程生命周期
                report["path_updated"] = True
            except (AttributeError, OSError) as exc:
                report["errors"].append(f"add_dll_directory({directory}): {exc}")
    else:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        parts = [part for part in existing.split(os.pathsep) if part]
        missing = [directory for directory in directories if directory not in parts]
        if missing:
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(missing + parts)           # 只对子进程有效，仍需上面的预加载
            report["path_updated"] = True
    if report["errors"] and not report["preloaded"]:
        report["guidance"] = f"找到库但无法加载，安装可能不完整；可重装：{PIP_EXTRA_HINT}"
    return report


# --- 判断 GPU 是否真的可用（可见性 + 运行时库） ---
def describe_gpu_usability() -> dict:
    try:
        import ctranslate2                                                            # 与 ASR 路线同一后端，结论可直接对上
        device_count = int(ctranslate2.get_cuda_device_count())
        probe_error = None
    except Exception as exc:                                                          # 缺 DLL、驱动异常都不该让 doctor 整体失败
        device_count, probe_error = 0, str(exc)

    preparation = prepare_cuda_libraries()
    loaded = preparation["found"]
    usable = bool(device_count > 0 and "cublas" in loaded and "cudnn" in loaded)
    if usable:
        guidance = None
    elif device_count == 0:
        guidance = "未检测到 CUDA 设备；转写会使用 cpu/int8"
    else:
        guidance = f"设备可见但缺少 CUDA 运行时库（cuBLAS/cuDNN）；安装后即可用 GPU：{PIP_EXTRA_HINT}"
    return {
        "device_count": device_count,
        "runtime_libraries": sorted(loaded.keys()),
        "preloaded": preparation["preloaded"],
        "usable": usable,
        "guidance": guidance,
        "error": probe_error,
    }


if __name__ == "__main__":                                                            # 允许单独运行以排查环境
    import json
    print(json.dumps(describe_gpu_usability(), ensure_ascii=False, indent=2))
