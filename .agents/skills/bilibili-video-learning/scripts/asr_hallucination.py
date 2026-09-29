#!/usr/bin/env python3
r"""
幻觉门：判断"这段文本像不像编出来的"，既用于标注，也用于拦截补转结果。

为什么需要它（当前唯一能"无中生有"的路径）：
    覆盖率兜底会把"空档 ≥2s 且实测响亮"的窗口切出来**关闭两道静音阈值**重新解码
    （`no_speech_threshold=None`、`logprob_threshold=None`），理由见 D16——丢字时宁可多拿字。
    但这样一来，音乐段/噪声段也可能被解出文本，而**结果之前没有任何校验就并入了正文**。
    本模块提供三道互相独立的门，宁可拒绝也不引入新错：

    1. **压缩比异常**：`compression_ratio` 高（>2.4）通常意味着复读或幻觉；
    2. **重复 n-gram**：同一 4 字以上片段重复 ≥3 次，是复读的典型形态；
    3. **时长-字数比异常**：中文口播约 4–6 字/秒；超过 ~8 字/秒（说不完）或少于 ~0.5 字/秒（几乎没内容）
       都说明这段不可信。

边界：
    - 只做"接受/拒绝/标注"，**不改写任何文本**；被拒窗口只记诊断，不进正文。
    - 门是保守的：宁可让可疑内容以 `suspected_hallucinations` 的形式暴露给人工，也不静默并入。

调用示例：
    from asr_hallucination import looks_hallucinated, flag_segments
    if looks_hallucinated(window_segments, window_seconds=4.0): ...
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import re  # 摘出可用于重复检测的字符片段
from typing import Any, Dict, List, NamedTuple, Optional


DEFAULT_COMPRESSION_RATIO = 2.4     # 与 whisper/CT2 默认阈值一致
DEFAULT_MAX_CHARS_PER_SECOND = 8.0  # 中文口播上限，超过说明"说不完"
DEFAULT_MIN_CHARS_PER_SECOND = 0.5  # 低于此值说明窗口里几乎没内容
DEFAULT_REPEAT_NGRAM = 4            # 重复片段最短长度
DEFAULT_REPEAT_TIMES = 3            # 重复达到该次数即视为复读
NON_WORD = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]+")


# --- 一次判定的结果：是否可疑 + 命中了哪些门 ---
class Verdict(NamedTuple):
    suspicious: bool
    reasons: List[str]
    chars_per_second: Optional[float]


# --- 文本里是否存在高频重复片段（复读的典型形态）---
def repeated_ngram_reasons(text: str, *, ngram: int = DEFAULT_REPEAT_NGRAM,
                          times: int = DEFAULT_REPEAT_TIMES) -> List[str]:
    cleaned = NON_WORD.sub("", text or "")
    if len(cleaned) < ngram * times:
        return []
    counts: Dict[str, int] = {}
    for start in range(len(cleaned) - ngram + 1):
        piece = cleaned[start:start + ngram]
        counts[piece] = counts.get(piece, 0) + 1
    repeats = [piece for piece, count in counts.items() if count >= times]
    return [f"repeated_ngram:{repeats[0]}×{counts[repeats[0]]}"] if repeats else []


# --- 判定一组分段是否可疑（用于补转窗口与整段标注）---
def looks_hallucinated(segments: List[Dict[str, Any]], window_seconds: Optional[float] = None,
                       *, compression_ratio: float = DEFAULT_COMPRESSION_RATIO,
                       max_chars_per_second: float = DEFAULT_MAX_CHARS_PER_SECOND,
                       min_chars_per_second: float = DEFAULT_MIN_CHARS_PER_SECOND) -> Verdict:
    reasons: List[str] = []
    text = "".join(str(item.get("content") or item.get("text") or "") for item in segments)
    for item in segments:
        ratio = item.get("compression_ratio")
        if ratio is not None and float(ratio) > compression_ratio:
            reasons.append(f"compression_ratio:{round(float(ratio), 2)}")
            break

    reasons.extend(repeated_ngram_reasons(text))

    chars = len(NON_WORD.sub("", text))
    chars_per_second: Optional[float] = None
    if window_seconds and window_seconds > 0:
        chars_per_second = round(chars / window_seconds, 2)
        if chars_per_second > max_chars_per_second:
            reasons.append(f"too_many_chars_per_second:{chars_per_second}")
        elif chars_per_second < min_chars_per_second:
            reasons.append(f"too_few_chars_per_second:{chars_per_second}")
    return Verdict(suspicious=bool(reasons), reasons=reasons, chars_per_second=chars_per_second)


# --- 给整份结果标注可疑段落（不改文本，只暴露给人工）---
def flag_segments(segments: List[Dict[str, Any]], *, compression_ratio: float = DEFAULT_COMPRESSION_RATIO
                  ) -> List[Dict[str, Any]]:
    flagged = []
    for index, item in enumerate(segments):
        verdict = looks_hallucinated([item], compression_ratio=compression_ratio,
                                     max_chars_per_second=10 ** 6, min_chars_per_second=0)
        if verdict.suspicious:
            flagged.append({"index": index, "start": item.get("from"), "end": item.get("to"),
                            "confidence": item.get("confidence"), "reasons": verdict.reasons,
                            "text": str(item.get("content") or "")[:60]})
    return flagged
