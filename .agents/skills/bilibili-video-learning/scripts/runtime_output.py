"""
视频脚本共用的运行诊断输出。
业务结果继续写 stdout，进度、fallback 和工具诊断统一写 stderr，这样 `--json` 调用方永远能直接解析 stdout。
调用示例：from runtime_output import log; log("[fetch] using direct API")
"""
from __future__ import annotations  # 允许在 Python 3.10+ 使用现代类型标注。

import sys  # stderr 是命令诊断与机器可读结果之间的固定边界。
from pathlib import Path  # 从 scripts 目录稳定定位同一 Skill 的 agent-harness。
from typing import Any  # log 接受路径、数字和异常等常见诊断对象。


HARNESS_ROOT = Path(__file__).resolve().parents[1] / "agent-harness"  # 后端与 CLI 共用一份脱敏实现。
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))                            # `.venv-gpu` 无需重复安装 harness 包。

from cli_anything.video_learning.utils.security import sanitize_diagnostics, sanitize_text  # noqa: E402 统一错误安全出口。


# --- 输出一条不会污染 JSON stdout 的诊断 ---
def log(*values: Any, end: str = "\n") -> None:
    message = " ".join(str(value) for value in values)            # 先拼成一条文本，避免分片秘密漏过正则。
    print(sanitize_text(message), end=end, file=sys.stderr, flush=True)  # 长时间 ASR 仍能看到安全进度。
