"""
原子写的重试与落盘测试。
守两件事：目标被占用（Windows 上杀软/Obsidian 打开文件）时不能直接失败，
以及重试用尽后要抛真实错误、不留半成品文件。
运行示例：python -m pytest cli_anything/video_learning/tests/test_file_output.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import os  # 用真实 os.replace 验证重试成功后的落盘结果。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

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


# --- 目标短暂被占用：退避后应成功 ---
def test_replace_retries_while_the_target_is_busy(monkeypatch, tmp_path):
    module = load_script("file_output")
    attempts = {"count": 0}
    real_replace = os.replace

    def flaky_replace(source, target):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError("target is open in another process")
        return real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", flaky_replace)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)  # 测试不真的等
    target = tmp_path / "note.md"

    module.write_text_atomically(target, "正文")

    assert target.read_text(encoding="utf-8") == "正文"  # 内容完整落盘
    assert attempts["count"] == 3  # 前两次被占用，第三次成功


# --- 重试用尽：抛真实错误，且不留下半成品文件 ---
def test_replace_fails_after_retries_and_cleans_up(monkeypatch, tmp_path):
    module = load_script("file_output")
    monkeypatch.setattr(module.os, "replace", lambda source, target: (_ for _ in ()).throw(PermissionError("still busy")))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    target = tmp_path / "note.md"

    with pytest.raises(PermissionError):
        module.write_text_atomically(target, "正文")

    assert not target.exists()  # 旧文件不该被半成品覆盖
    assert list(tmp_path.iterdir()) == []  # 临时文件必须清理干净
