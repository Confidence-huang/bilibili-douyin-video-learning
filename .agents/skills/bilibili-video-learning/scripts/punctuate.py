#!/usr/bin/env python3
r"""
标点恢复的**可选**接入点：有模型就用模型，没有就退回规则法（见 docs/DECISIONS.md D44）。

为什么做成"可选 + 降级"：
    D42 实测规则法的标点 F1 只有 0.313，天花板来自分段粒度（中文标点约每 10 字一个，
    而 ASR 分段约 1.4 秒/段）。要再上一个台阶需要**标点模型**，但那会引入新的重依赖（torch/onnx）。
    本仓库的既有做法是把它做成可选依赖组 + 优雅降级（同 `gpu-cuda12` / `zh-normalize`），
    于是"要不要上模型"是一行安装配置，而不是重构。

边界：
    - **默认关闭**：不装 `punctuation` extra 时永远走规则法；
    - 无论走哪条路，返回的 `mode` 都会写进产出，绝不假装用了模型。
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import sys  # 导入同目录的规则法实现
from pathlib import Path  # 定位脚本目录
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize_transcript  # 规则法后备：join_with_pause_punctuation

BACKEND_MODULE = "deepmultilingualpunctuation"     # 可选依赖组 `punctuation` 提供的后端
MODEL_IDS = ("oliverguhr/fullstop-punctuation-multilingual-base",)


# --- 后端是否可用（缺失时**不报错**，只是走规则法）---
def available() -> bool:
    try:
        __import__(BACKEND_MODULE)

        return True
    except Exception:
        return False


# --- 恢复标点：返回 (文本, 模式)；模式取值 model / rules ---
def restore(text: str, *, segments: Optional[List[Dict[str, Any]]] = None,
            enabled: bool = False) -> Tuple[str, str]:
    if enabled and available():
        try:
            from deepmultilingualpunctuation import PunctuationModel  # type: ignore

            model = PunctuationModel(model=MODEL_IDS[0])
            return str(model.restore_punctuation(text)), "model"
        except Exception:
            pass                                                          # 模型失败也必须能产出：退回规则法
    if segments:
        return normalize_transcript.join_with_pause_punctuation(segments), "rules"
    return text, "rules"
