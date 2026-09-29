"""
模块级 CLI 的静态冒烟测试（对应 docs/DECISIONS.md D46）。

为什么需要它：v1.19.0 让 `transcribe_bilibili.py` 在**运行时**崩了——文件只 from-import 了函数，
却调用模块级 `speech_to_text.resolve_model_size`，于是 NameError；而 343 个测试全绿，
因为那个脚本的 argparse 在模块级、没有可调用入口，**没有任何测试碰过它的主路径**。

本测试用 AST 找出"模块级执行体里用到的 模块.属性"，导入该模块后逐个断言属性存在：
    ① 覆盖"导入缺失"这一类错误（就是上面那个 NameError 的形态）；
    ② 不执行主路径（无网络、无模型、无 ffmpeg），因此可以在 CI 里跑；
    ③ 顺带断言"模块级执行体必须有 __main__ 守卫"，否则导入即执行。
运行示例：python -m pytest cli_anything/video_learning/tests/test_cli_smoke.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import ast  # 静态找出模块级用到的属性
import builtins  # 区分"未导入的模块"与内置名（str/len/int 不是模块）
import importlib.util  # 按真实路径加载 live Skill 的脚本
import sys  # 把 scripts 目录加入搜索路径
from pathlib import Path  # 稳定定位 Skill 根目录

import pytest  # 断言辅助


SKILL_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SCRIPTS = sorted(path for path in SCRIPTS_DIR.glob("*.py") if path.name != "__init__.py")


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"video_learning_smoke_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                                     # 导入期错误会在这里暴露
    return module


def module_level_bodies(tree: ast.Module):
    """模块级执行体：跳过函数/类定义，只留真正会被 import 执行的语句。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node


def module_level_attribute_names(tree: ast.Module) -> set:
    """模块级执行体里出现的 `名字.属性` 中的**名字**（跳过 __main__ 守卫内的语句）。"""
    names = set()

    def is_main_guard(node) -> bool:
        return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__")

    for node in module_level_bodies(tree):
        if is_main_guard(node):
            continue                                                    # 守卫内的代码不在 import 时执行
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                names.add(child.value.id)
    return names


# --- 核心守卫：模块级用到的每个"模块名"都必须是真实存在的导入（v1.19.0 的形态）---
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_module_level_attributes_are_imported(script: Path):
    tree = ast.parse(script.read_text(encoding="utf-8"))
    expected = module_level_attribute_names(tree)
    if not expected:
        return                                                          # 该脚本没有模块级执行体
    module = load_module(script)
    missing = sorted(name for name in expected
                     if name not in dir(builtins) and not hasattr(module, name))

    assert not missing, f"{script.name} 模块级用到但未导入：{missing}（这正是 v1.19.0 的 NameError 形态）"


# --- 模块级执行体必须被 __main__ 守卫包住，否则"导入即执行" ---
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_module_level_execution_is_guarded(script: Path):
    tree = ast.parse(script.read_text(encoding="utf-8"))
    # 只盯"会让导入即执行主路径"的调用；模块级 Path(...)/re.compile(...) 这类纯调用是有意为之
    dangerous = {"main", "cli_main", "run"}
    risky = []
    for node in module_level_bodies(tree):
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Name) and func.id in dangerous:
                risky.append(child)
            elif isinstance(func, ast.Attribute) and func.attr == "parse_args":
                risky.append(child)

    guarded = any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                  and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
                  for node in tree.body)
    if risky and not guarded:
        assert False, f"{script.name} 有模块级函数调用但没有 if __name__ == \"__main__\" 守卫"
