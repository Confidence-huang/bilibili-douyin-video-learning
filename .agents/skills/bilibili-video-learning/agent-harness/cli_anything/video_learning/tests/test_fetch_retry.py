"""
B站取流的 SSL 重试测试。
守两件事：`--legacy-server-connect` 必须落在 `yt_dlp` 之后（否则 Python 先吃掉它），
以及重试本身失败时不能静默（否则原始 SSL 线索被覆盖，现象是"重试了但没效果"）。
运行示例：python -m pytest cli_anything/video_learning/tests/test_fetch_retry.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import subprocess  # 构造与真实子进程同形状的返回值。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"  # 测试名直接映射真实脚本文件。
    module_name = f"video_learning_test_{name}"  # 独立模块名避免测试之间污染缓存。
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 重试标志必须紧跟 yt_dlp，URL 仍然在最后 ---
def test_ssl_retry_places_the_flag_after_yt_dlp(monkeypatch):
    fetch = load_script("fetch_bilibili")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="SSL: CERTIFICATE_VERIFY_FAILED")
        return subprocess.CompletedProcess(command, 0, stdout='{"id": "BV1xx411c7mD"}', stderr="")

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)

    result = fetch._run_ytdlp("https://www.bilibili.com/video/BV1xx411c7mD")

    assert len(commands) == 2  # 第一遍失败后必须真的重试
    assert commands[1][3] == "--legacy-server-connect"  # cmd[0:3] 是 [python, -m, yt_dlp]
    assert commands[1][-1].endswith("BV1xx411c7mD")  # URL 仍在最后一位，不被当成选项
    assert result[0]["id"] == "BV1xx411c7mD"  # 重试成功后照常返回解析结果


# --- 重试本身超时/异常：必须报出来，而不是假装没重试 ---
def test_ssl_retry_failure_is_reported(monkeypatch):
    fetch = load_script("fetch_bilibili")

    def fake_run(command, **_kwargs):
        if len(seen) == 0:
            seen.append(command)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="SSL: EOF occurred in violation of protocol")
        raise subprocess.TimeoutExpired(command, 120)

    seen: list[object] = []
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)

    result = fetch._run_ytdlp("https://www.bilibili.com/video/BV1xx411c7mD")

    assert "SSL retry failed" in result["error"]  # 原始 SSL 线索不能因为重试异常而消失
