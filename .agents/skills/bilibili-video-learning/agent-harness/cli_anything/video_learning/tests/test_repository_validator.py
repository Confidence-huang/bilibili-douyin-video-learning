"""
校验器自身的回归测试（对应 docs/DECISIONS.md D40）。

为什么必须存在这一个测试：
    我在同一轮工作里**连续三次**把新函数追加到 `if __name__ == "__main__":` 之后，
    最后一次甚至让文件里出现**两个** `__main__` 块，靠前的那个在 `main` 定义前就调用它。
    问题是：校验器坏了的时候，CI 里"校验器报错"和"仓库真有问题"看起来一模一样，
    而且没有任何测试会失败——因为校验器是**跑别人**的那个东西。
    所以：导入它、跑一次 `main()`、断言它返回 0 并打印了 REPOSITORY_OK。
运行示例：python -m pytest cli_anything/video_learning/tests/test_repository_validator.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实路径加载根目录下的 tools/validate_repository.py
import sys  # 把 scripts 目录加入搜索路径，保证校验器内部导入可用
from pathlib import Path  # 稳定定位仓库根目录


SKILL_ROOT = Path(__file__).resolve().parents[4]
REPO_ROOT = SKILL_ROOT.parents[2]                       # skill -> skills -> .agents -> 仓库根
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_validator():
    path = REPO_ROOT / "tools" / "validate_repository.py"
    spec = importlib.util.spec_from_file_location("video_learning_test_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                     # 导入期报错（含 __main__ 乱序）会在这里暴露
    return module


# --- 模块必须可被导入：把"只在命令行下才炸"的问题提前到导入期 ---
def test_validator_module_imports():
    validator = load_validator()

    assert callable(validator.main)


# --- 主入口必须真的跑通并打印成功标记（否则校验器坏了等于没人守门）---
def test_validator_main_runs_and_reports_ok(capsys):
    validator = load_validator()

    code = validator.main()
    output = capsys.readouterr().out

    assert code == 0, output
    assert "REPOSITORY_OK" in output


# --- 新规则函数必须可单独调用且返回列表（便于将来做定向诊断）---
def test_validator_rule_functions_are_callable():
    validator = load_validator()

    assert isinstance(validator.report_unevidenced_reference_entries(), list)
    assert isinstance(validator.report_prompt_template_versions(), list)
    assert isinstance(validator.report_unreferenced_scripts(), list)
