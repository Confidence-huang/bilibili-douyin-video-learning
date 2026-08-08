"""
定位并调用 live `bilibili-video-learning` Skill。
这个模块集中所有路径发现、脚本 subprocess、动态模块加载和工具探测，让 Click 入口与业务指令都不依赖当前工作目录。
调用示例：runtime = SkillRuntime(); payload = runtime.run_json_script("fetch_bilibili.py", ["BV...", "--json"])
"""
from __future__ import annotations  # 支持 Python 3.10+ 的类型标注。

import importlib.util  # 本地字幕和笔记命令复用 live 脚本函数。
import json  # 解析真实脚本和 doctor 子进程返回的 JSON。
import os  # 读取显式的 Skill/Python 路径覆盖。
import shutil  # 从 PATH 发现 ffmpeg、yt-dlp 等真实工具。
import subprocess  # 所有平台/ASR 副作用都通过可观察的子进程发生。
import sys  # 把经过脱敏的后端诊断转发给调用者。
from pathlib import Path  # 安全、清晰地组合 Windows 与 POSIX 路径。
from types import ModuleType  # 标注动态加载脚本的返回类型。

from cli_anything.video_learning.utils.security import sanitize_diagnostics, sanitize_text  # 所有子进程错误共用一个脱敏出口。


class SkillRuntime:
    """Resolve one live Skill root and execute its existing scripts."""

    # --- 初始化真实 Skill 与专用 Python ---
    def __init__(self, skill_root: str | Path | None = None, runtime_python: str | Path | None = None):
        self.skill_root = self.find_skill_root(skill_root)                     # 后端文件必须来自同一完整 Skill。
        self.scripts_dir = self.skill_root / "scripts"                         # 每个命令都从这里解析真实脚本。
        self.runtime_python = self.find_runtime_python(runtime_python)          # ASR 和 yt-dlp 使用 GPU uv 环境。

    # --- 按显式配置、本地源码和标准 Skill 入口寻找根目录 ---
    @staticmethod
    def find_skill_root(explicit_path: str | Path | None = None) -> Path:
        package_skill_root = Path(__file__).resolve().parents[4]                # editable install 时直接命中当前 Skill。
        configured_path = os.environ.get("BILIBILI_VIDEO_LEARNING_ROOT")       # 跨机器安装可显式覆盖。
        candidates = [
            explicit_path,
            configured_path,
            package_skill_root,
            Path.home() / ".agents" / "skills" / "bilibili-video-learning",
            Path.home() / ".codex" / "skills" / "bilibili-video-learning",
            Path.home() / ".claude" / "skills" / "bilibili-video-learning",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            resolved = Path(candidate).expanduser().resolve()
            if (resolved / "SKILL.md").is_file() and (resolved / "scripts" / "fetch_bilibili.py").is_file():
                return resolved
        raise RuntimeError("Could not locate a complete bilibili-video-learning Skill root")

    # --- 发现 Skill 自带的 CPU 或 GPU uv Python ---
    def find_runtime_python(self, explicit_path: str | Path | None = None) -> Path:
        configured_path = os.environ.get("BILIBILI_VIDEO_LEARNING_PYTHON")      # 允许测试或其他机器覆盖解释器。
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        external_runtime = data_home / "bilibili-video-learning" / "runtime"  # Linux 运行时不得污染 Skill 扫描树。
        candidates = [
            explicit_path,
            configured_path,
            external_runtime / "bin" / "python",
            external_runtime / "Scripts" / "python.exe",
            self.skill_root / ".venv" / "Scripts" / "python.exe",
            self.skill_root / ".venv" / "bin" / "python",
            self.skill_root / ".venv-gpu" / "Scripts" / "python.exe",
            self.skill_root / ".venv-gpu" / "bin" / "python",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).expanduser().is_file():
                return Path(candidate).expanduser().absolute()                  # POSIX venv Python is a symlink; resolving it loses the venv.
        raise RuntimeError(f"Video-learning Python environment is missing for Skill: {self.skill_root}")

    # --- 运行会输出一个 JSON 文档的生产脚本 ---
    def run_json_script(self, script_name: str, arguments: list[str], timeout: int = 180) -> dict:
        script_path = self.scripts_dir / script_name                             # 文件名固定映射 live 后端。
        if not script_path.is_file():
            raise RuntimeError(f"Backend script not found: {script_path}")
        command = [str(self.runtime_python), str(script_path), *arguments]        # 不使用 shell，避免参数转义和注入问题。
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if completed.stderr:
            safe_stderr = sanitize_text(completed.stderr)                       # Cookie、token、签名 URL 和临时路径不得外泄。
            sys.stderr.write(safe_stderr)                                       # 诊断保留给人类，stdout 留给 JSON。
            if safe_stderr and not safe_stderr.endswith("\n"):
                sys.stderr.write("\n")                                        # 截断或替换后仍保持终端行边界清楚。
        try:
            payload = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            stdout_tail = sanitize_text((completed.stdout or "")[-500:])
            raise RuntimeError(f"{script_name} returned non-JSON stdout: {stdout_tail}") from exc
        payload = sanitize_diagnostics(payload)                                 # 成功 fallback 也可能携带后端错误诊断。
        if completed.returncode != 0:
            error_text = sanitize_text(payload.get("error") or f"{script_name} exited with {completed.returncode}")
            raise RuntimeError(error_text)
        return payload

    # --- 加载只做本地纯转换的脚本函数 ---
    def load_script(self, script_name: str) -> ModuleType:
        script_path = self.scripts_dir / f"{script_name}.py"
        if not script_path.is_file():
            raise RuntimeError(f"Backend script not found: {script_path}")
        scripts_text = str(self.scripts_dir)
        if scripts_text not in sys.path:
            sys.path.insert(0, scripts_text)                                     # 支持脚本现有的同目录绝对导入。
        module_name = f"cli_anything_video_learning_{script_name}"
        specification = importlib.util.spec_from_file_location(module_name, script_path)
        if specification is None or specification.loader is None:
            raise RuntimeError(f"Could not load backend script: {script_path}")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    # --- 找到实际工具并读取版本，不只检查名称 ---
    def inspect_tool(self, tool_name: str, version_arguments: list[str]) -> dict:
        runtime_scripts = self.runtime_python.parent                            # yt-dlp 通常跟随 GPU Python 安装。
        executable_names = [tool_name, f"{tool_name}.exe"] if os.name == "nt" else [tool_name]
        candidates = [runtime_scripts / name for name in executable_names]
        path_text = next((str(path.resolve()) for path in candidates if path.is_file()), None)
        path_text = path_text or shutil.which(tool_name)
        if not path_text:
            return {"ok": False, "path": None, "version": None, "error": f"{tool_name} not found"}
        return self.inspect_executable(path_text, version_arguments)

    # --- 检查已经由 Skill 解析出的可执行文件 ---
    def inspect_executable(self, executable: str | Path, version_arguments: list[str]) -> dict:
        path_text = str(Path(executable).expanduser().resolve())
        completed = subprocess.run(
            [path_text, *version_arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        version_line = sanitize_text(((completed.stdout or completed.stderr or "").strip().splitlines() or [""])[0])
        return {
            "ok": completed.returncode == 0,
            "path": str(Path(path_text).resolve()),
            "version": version_line,
            "error": None if completed.returncode == 0 else version_line,
        }

    # --- 在真实 GPU Python 中检查直接模块，而不是当前 harness venv ---
    def inspect_python_modules(self, module_names: list[str]) -> dict:
        probe_code = (
            "import importlib.util,json; names=" + repr(module_names) + "; "
            "print(json.dumps({name: bool(importlib.util.find_spec(name)) for name in names}))"
        )
        completed = subprocess.run(
            [str(self.runtime_python), "-c", probe_code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if completed.returncode != 0:
            return {name: False for name in module_names}
        return json.loads(completed.stdout)
