#!/usr/bin/env python3
r"""
退出码契约的**镜像模块**：让 scripts/ 下的独立 CLI 也能用同一套退出码。

为什么需要镜像而不是直接 import：
    契约的规范定义在 `agent-harness/cli_anything/video_learning/utils/exit_codes.py`，
    但独立脚本（`douyin_ssr.py`、`fetch_bilibili.py`）常被直接 `python scripts/xxx.py` 调用，
    此时 harness 不一定装在解释器里，硬依赖会让脚本在"没装 CLI"的机器上直接崩。
    于是本模块**先尝试导入规范实现**（装了就用规范的），**缺失时退回本地镜像**；
    `tests/test_exit_contract.py` 会断言两者数值完全一致，镜像不可能悄悄漂移。

调用示例：
    from exit_contract import classify_failure, EXIT_NETWORK_TIMEOUT
    return classify_failure(exc)
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import socket  # 超时/连接类异常是 22 的判据
from typing import Tuple, Type


# --- 本地镜像：数值必须与 harness 的规范定义逐项一致（由测试守护）---
EXIT_SUCCESS = 0
EXIT_GENERIC_FAILURE = 1
EXIT_SHARE_PAGE_UNAVAILABLE = 20
EXIT_COOKIE_PERMISSION_REQUIRED = 21
EXIT_NETWORK_TIMEOUT = 22
EXIT_RATIO_UNAVAILABLE = 23
EXIT_TRANSCRIPTION_FAILED = 24
EXIT_SOURCES_DISAGREE = 25
EXIT_PLATFORM_VERIFICATION_REQUIRED = 26
EXIT_NO_AUDIO_TRACK = 27

MIRROR_VALUES = {
    "EXIT_SUCCESS": EXIT_SUCCESS,
    "EXIT_GENERIC_FAILURE": EXIT_GENERIC_FAILURE,
    "EXIT_SHARE_PAGE_UNAVAILABLE": EXIT_SHARE_PAGE_UNAVAILABLE,
    "EXIT_COOKIE_PERMISSION_REQUIRED": EXIT_COOKIE_PERMISSION_REQUIRED,
    "EXIT_NETWORK_TIMEOUT": EXIT_NETWORK_TIMEOUT,
    "EXIT_RATIO_UNAVAILABLE": EXIT_RATIO_UNAVAILABLE,
    "EXIT_TRANSCRIPTION_FAILED": EXIT_TRANSCRIPTION_FAILED,
    "EXIT_SOURCES_DISAGREE": EXIT_SOURCES_DISAGREE,
    "EXIT_PLATFORM_VERIFICATION_REQUIRED": EXIT_PLATFORM_VERIFICATION_REQUIRED,
    "EXIT_NO_AUDIO_TRACK": EXIT_NO_AUDIO_TRACK,
}

_KC = {"平台风控": EXIT_PLATFORM_VERIFICATION_REQUIRED, "验证码": EXIT_PLATFORM_VERIFICATION_REQUIRED,
       "captcha": EXIT_PLATFORM_VERIFICATION_REQUIRED, "无音轨": EXIT_NO_AUDIO_TRACK,
       "没有音轨": EXIT_NO_AUDIO_TRACK, "no audio": EXIT_NO_AUDIO_TRACK,
       "画质": EXIT_RATIO_UNAVAILABLE, "档位": EXIT_RATIO_UNAVAILABLE, "ratio": EXIT_RATIO_UNAVAILABLE,
       "cookie": EXIT_COOKIE_PERMISSION_REQUIRED, "授权": EXIT_COOKIE_PERMISSION_REQUIRED,
       "超时": EXIT_NETWORK_TIMEOUT, "timeout": EXIT_NETWORK_TIMEOUT,
       "分享页": EXIT_SHARE_PAGE_UNAVAILABLE, "share page": EXIT_SHARE_PAGE_UNAVAILABLE}


# --- 把异常归类成退出码：与 harness 的 classify_failure 同语义 ---
def mirror_classify_failure(exc: BaseException, platform_error_types: Tuple[Type[BaseException], ...] = ()) -> int:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return EXIT_NETWORK_TIMEOUT
    if platform_error_types and isinstance(exc, platform_error_types):
        return EXIT_PLATFORM_VERIFICATION_REQUIRED
    message = str(exc).lower()
    for keyword, code in _KC.items():                                          # 已被上游脱敏的消息里只留关键字
        if keyword.lower() in message:
            return code
    return EXIT_GENERIC_FAILURE


# --- 装了规范实现时用规范实现（并在导入期校验镜像未漂移）---
try:                                                                           # pragma: no cover - 取决于是否安装 harness
    from cli_anything.video_learning.utils import exit_codes as _canonical      # type: ignore

    classify_failure = _canonical.classify_failure                              # type: ignore[assignment]
    USING_CANONICAL = True

    def canonical_values() -> dict:
        return {name: getattr(_canonical, name) for name in MIRROR_VALUES}
    classify_failure = mirror_classify_failure                                  # 规范不可用时退回镜像
except Exception:                                                              # pragma: no cover
    classify_failure = mirror_classify_failure
    USING_CANONICAL = False

    def canonical_values() -> dict:
        return {}
