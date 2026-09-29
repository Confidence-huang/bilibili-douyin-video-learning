"""
退出码契约的镜像一致性测试（对应 docs/DECISIONS.md D37）。

独立脚本不能硬依赖 harness（否则"没装 CLI"的机器上直接崩），所以 scripts/exit_contract.py
是"先导入规范、缺失则用本地镜像"。本测试守住镜像不会悄悄漂移：
    1) 镜像数值 == harness 规范数值（装了 harness 时逐项比对）；
    2) fetch_bilibili.py 自带的第二份常量 == 镜像（它是历史副本，用测试锁住而不是改它）；
    3) classify_failure 的映射符合契约语义。
运行示例：python -m pytest cli_anything/video_learning/tests/test_exit_contract.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill
import socket  # 超时类异常
import sys  # 把 scripts 目录加入搜索路径
from pathlib import Path  # 稳定定位 Skill 根目录与 harness

import pytest  # 断言辅助


SKILL_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cli_anything.video_learning.utils import exit_codes as canonical  # noqa: E402 规范实现
import exit_contract  # noqa: E402 镜像模块


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 镜像与规范必须逐项一致（这是本模块存在的唯一理由）---
def test_mirror_matches_canonical_values():
    for name, value in exit_contract.MIRROR_VALUES.items():
        assert getattr(canonical, name) == value, f"{name} 与规范不一致"


# --- fetch_bilibili.py 自带的第二份常量也必须与镜像一致（用测试锁住漂移）---
def test_fetch_bilibili_constants_match_mirror():
    fetch = load_script("fetch_bilibili")

    for name, value in exit_contract.MIRROR_VALUES.items():
        if hasattr(fetch, name):
            assert getattr(fetch, name) == value, f"fetch_bilibili.{name} 与镜像不一致"


# --- 镜像语义：超时→22、平台风控→26、无音轨→27、其它→1 ---
def test_mirror_classification_semantics():
    assert exit_contract.mirror_classify_failure(TimeoutError("timed out")) == exit_contract.EXIT_NETWORK_TIMEOUT
    assert exit_contract.mirror_classify_failure(socket.timeout("read timed out")) == \
        exit_contract.EXIT_NETWORK_TIMEOUT
    assert exit_contract.mirror_classify_failure(RuntimeError("该作品无音轨")) == exit_contract.EXIT_NO_AUDIO_TRACK
    assert exit_contract.mirror_classify_failure(ValueError("完全无法分类")) == exit_contract.EXIT_GENERIC_FAILURE


# --- 镜像与规范必须给出同样的分类：只对齐数值是不够的，语义漂移同样有害 ---
def test_mirror_agrees_with_canonical_classification():
    cases = [TimeoutError("timed out"), RuntimeError("命中平台风控验证码"), RuntimeError("该作品无音轨"),
             RuntimeError("画质档位不可用"), ValueError("完全无法分类")]
    mismatches = [(type(case).__name__, str(case), exit_contract.classify_failure(case),
                   exit_contract.mirror_classify_failure(case))
                  for case in cases
                  if exit_contract.classify_failure(case) != exit_contract.mirror_classify_failure(case)]

    assert not mismatches, f"镜像与规范的分类不一致：{mismatches}"


# --- 平台专属异常类型优先于关键字判断（镜像语义）---
def test_platform_error_types_win():
    class PlatformRiskError(RuntimeError):
        pass

    code = exit_contract.mirror_classify_failure(PlatformRiskError("未提及关键字"), (PlatformRiskError,))

    assert code == exit_contract.EXIT_PLATFORM_VERIFICATION_REQUIRED


# --- 独立 CLI 必须真的返回映射后的码，而不是笼统的 1 ---
def test_douyin_ssr_cli_returns_mapped_exit_code(monkeypatch, capsys):
    ssr = load_script("douyin_ssr")

    def explode(*args, **kwargs):
        raise TimeoutError("request timed out")

    monkeypatch.setattr(ssr, "download_public_video", explode)
    code = ssr.main(["https://v.douyin.com/example/", "--output", "/tmp/never.mp4"])

    payload = capsys_capture(capsys)
    assert code == exit_contract.classify_failure(TimeoutError("request timed out"))   # 用的是统一契约
    assert payload.get("exit_code") == code                                            # JSON 与退出码必须一致


# --- 从多个 JSON 片段里取出最后一个可解析对象（CLI 可能先打印其它信息）---
def capsys_capture(capsys) -> dict:
    import json

    out = capsys.readouterr().out
    for chunk in reversed(out.split("\n}")):
        try:
            return json.loads(chunk + "\n}")
        except Exception:
            continue
    return {}
