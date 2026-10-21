"""
抖音 yt-dlp 调用的参数边界测试。
守一件事：来源 URL 是**用户输入**，只能是 `--` 之后的位置参数；
否则 `--exec=...` 这类内容会被 yt-dlp 当成选项执行。
运行示例：python -m pytest cli_anything/video_learning/tests/test_douyin_args.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 构造与真实子进程同形状的返回值。

import pytest  # 提供 monkeypatch 与异常断言。


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


# --- 元数据取流：URL 必须在 -- 之后 ---
def test_metadata_command_puts_the_url_after_the_terminator(monkeypatch):
    module = load_script("douyin_extract")
    captured = {}
    monkeypatch.setattr(module, "_find_yt_dlp", lambda: [sys.executable, "-m", "yt_dlp"])

    def fake_run(command, timeout=60, desc=""):
        captured["command"] = list(command)
        return SimpleNamespace(returncode=0, stdout='{"id": "7000000000000000000"}\n', stderr="")

    monkeypatch.setattr(module, "_run", fake_run)

    module.fetch_metadata("https://www.douyin.com/video/7000000000000000000")

    assert captured["command"][-2:] == ["--", "https://www.douyin.com/video/7000000000000000000"]
    assert "--dump-json" in captured["command"]  # 选项仍在 -- 之前被正常解析


# --- 下载：同样必须以 -- 收尾 ---
def test_download_command_puts_the_url_after_the_terminator(monkeypatch, tmp_path):
    module = load_script("douyin_extract")
    captured = {}
    monkeypatch.setattr(module, "_find_yt_dlp", lambda: [sys.executable, "-m", "yt_dlp"])

    def fake_run(command, timeout=180, desc=""):
        captured["command"] = list(command)
        (tmp_path / "douyin_video.mp4").write_bytes(b"fake")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "_run", fake_run)

    module.download_video_ytdlp("https://www.douyin.com/video/7000000000000000000", str(tmp_path))

    assert captured["command"][-2:] == ["--", "https://www.douyin.com/video/7000000000000000000"]


# --- 非 http(s) 输入一律拒绝，连参数都不拼 ---
def test_non_http_sources_are_rejected(monkeypatch):
    module = load_script("douyin_extract")
    monkeypatch.setattr(module, "_find_yt_dlp", lambda: [sys.executable, "-m", "yt_dlp"])
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: pytest.fail("不应该真的调用 yt-dlp"))

    with pytest.raises(ValueError) as raised:
        module.fetch_metadata("--exec=rm -rf /")

    assert "unsupported source URL scheme" in str(raised.value)
