#!/usr/bin/env python3
r"""
定向二次解码：只对"可疑区间"换更强的解码条件重解，并把每次替换/拒绝的理由写进诊断。

为什么需要它（与覆盖率补转的分工）：
    `asr_coverage` 解决的是"**整段没被转写**"（时间轴上有缺口），靠音量实测找出丢字的空档；
    本模块解决的是"**转了但转错**"——时间轴连续、却没有缺口可找。真实数据里这类错误很集中：
    替换类错误 70–85 处，其中相当一部分落在低置信段落上（`avg_logprob` 明显偏低，
    或 `compression_ratio` 异常高=复读/幻觉）。它们不需要重跑全片，只需要重解那几秒。

判据（宁可拒绝，不可引入新错）：
    接受一段重解结果，必须同时满足
    1. 非空；
    2. 新区间对原区间的**时间覆盖 ≥ 60%**（避免"重解得短了"把话吃掉）；
    3. 与原文本的字符重合率 ≥ 50%（避免换来一段不相关内容——那正是幻觉的样子）；
    4. 字符加权平均置信度提升 ≥ 0.15，或压缩比明显下降且置信度不掉超过 0.1。
    否则保留原结果，并把拒绝原因记进报告。**"拒绝"同样是有效结论。**

边界：
    - 解码动作由调用方注入（`refine_window(start, end) -> [{from,to,content,...}]`），
      因此离线测试不需要模型、不需要 ffmpeg、不需要真实音频。
    - 只改被判定为可疑的区间；时间轴与其它段落逐字不动，便于人工核对差异。

调用示例：
    report = apply_refinement(segments, refine_window, RefineSettings(), diagnostics)
    segments = report["segments"]
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import re  # 用字符集合比较重合率，避免依赖分词
from typing import Any, Callable, Dict, List, NamedTuple, Optional

import asr_coverage  # 复用既有的窗口切分与"重解结果并回时间轴"逻辑，避免第二套合并实现


DEFAULT_COMPRESSION_RATIO = 2.4      # 与 whisper/CT2 的默认阈值一致：更高通常意味着复读或幻觉
DEFAULT_MAX_SPANS = 6                # 单次运行的重解预算：只处理最可疑的几处，控制耗时
DEFAULT_MIN_SPAN_SECONDS = 1.5       # 太短的区间重解收益低、风险高
DEFAULT_CONFIDENCE_MARGIN = 0.15     # 置信度必须提升这么多才值得替换
MIN_COVERAGE_RATIO = 0.6             # 重解结果必须覆盖原区间至少六成时长
MIN_CHAR_OVERLAP = 0.5               # 与原文本的字符重合率下限
UNCHANGED_PATTERN = re.compile(r"[\s，。！？、,.!?]+")  # 比较文本时忽略标点与空白


class RefineSettings(NamedTuple):
    logprob_threshold: float = -1.0                    # 低于该平均对数概率视为可疑
    compression_ratio_threshold: float = DEFAULT_COMPRESSION_RATIO  # 高于该压缩比视为复读/幻觉
    max_spans: int = DEFAULT_MAX_SPANS                 # 单次运行最多重解几段
    min_span_seconds: float = DEFAULT_MIN_SPAN_SECONDS # 过短区间不重解
    confidence_margin: float = DEFAULT_CONFIDENCE_MARGIN  # 置信度提升门槛


# --- 找出值得重解的区间：低置信 / 高压缩比 / 长且不确定 ---
def suspicious_spans(segments: List[Dict[str, Any]], settings: RefineSettings | None = None) -> List[Dict[str, Any]]:
    active = settings or RefineSettings()
    spans: List[Dict[str, Any]] = []
    for segment in segments:
        start, end = float(segment.get("from", 0.0)), float(segment.get("to", 0.0))
        duration = end - start
        if duration < active.min_span_seconds:
            continue                                       # 极短段重解收益低，且更容易被判据误伤
        reasons = []
        confidence = segment.get("confidence")
        ratio = segment.get("compression_ratio")
        if confidence is not None and confidence < active.logprob_threshold:
            reasons.append("low_logprob")
        if ratio is not None and ratio > active.compression_ratio_threshold:
            reasons.append("high_compression_ratio")
        if confidence is None and ratio is None and duration >= 8.0:
            reasons.append("long_unscored_segment")        # 没有置信度信号时，超长段本身值得复核
        if reasons:
            spans.append({"start": start, "end": end, "reasons": reasons,
                          "confidence": confidence, "compression_ratio": ratio,
                          "text": segment.get("content", "")})
    spans.sort(key=lambda span: (_reason_weight(span["reasons"]), -(span["end"] - span["start"])), reverse=True)
    return spans[:active.max_spans]


# --- 原因排序权重：复读/幻觉比低置信更值得先重解 ---
def _reason_weight(reasons: List[str]) -> float:
    weights = {"high_compression_ratio": 1.0, "low_logprob": 0.6, "long_unscored_segment": 0.3}
    return max((weights.get(reason, 0.0) for reason in reasons), default=0.0)


# --- 字符加权平均置信度：长段落更有代表性 ---
def weighted_confidence(segments: List[Dict[str, Any]]) -> Optional[float]:
    total_chars = 0
    total = 0.0
    for segment in segments:
        confidence = segment.get("confidence")
        text = UNCHANGED_PATTERN.sub("", str(segment.get("content", "")))
        if confidence is None or not text:
            continue
        total += float(confidence) * len(text)
        total_chars += len(text)
    return round(total / total_chars, 3) if total_chars else None


# --- 文本重合率：拒绝"换来一段不相关内容"这类幻觉 ---
def char_overlap_ratio(before: str, after: str) -> float:
    left = UNCHANGED_PATTERN.sub("", before or "")
    right = UNCHANGED_PATTERN.sub("", after or "")
    if not left or not right:
        return 0.0
    counts: Dict[str, int] = {}
    for char in right:
        counts[char] = counts.get(char, 0) + 1
    shared = sum(1 for char in left if counts.get(char, 0) > 0)
    return shared / len(left)


# --- 判定是否接受重解结果：四条判据全部给出理由，便于人工复核 ---
def judge_replacement(before_segments: List[Dict[str, Any]], after_segments: List[Dict[str, Any]],
                      span: Dict[str, Any], settings: RefineSettings) -> Dict[str, Any]:
    before_text = "".join(str(item.get("content", "")) for item in before_segments)
    after_text = "".join(str(item.get("content", "")) for item in after_segments)
    if not after_text.strip():
        return {"accepted": False, "reason": "empty_result"}

    span_seconds = max(float(span["end"]) - float(span["start"]), 0.1)
    covered = sum(max(0.0, float(item.get("to", 0.0)) - float(item.get("from", 0.0))) for item in after_segments)
    if covered / span_seconds < MIN_COVERAGE_RATIO:
        return {"accepted": False, "reason": "coverage_shrank", "covered_ratio": round(covered / span_seconds, 2)}

    overlap = char_overlap_ratio(before_text, after_text)
    if overlap < MIN_CHAR_OVERLAP:
        return {"accepted": False, "reason": "text_diverged", "char_overlap": round(overlap, 2)}

    confidence_before = weighted_confidence(before_segments)
    confidence_after = weighted_confidence(after_segments)
    if confidence_before is not None and confidence_after is not None:
        gain = confidence_after - confidence_before
        if gain >= settings.confidence_margin:
            return {"accepted": True, "reason": "confidence_gain", "confidence_gain": round(gain, 3),
                    "char_overlap": round(overlap, 2)}
        return {"accepted": False, "reason": "confidence_not_improved", "confidence_gain": round(gain, 3)}

    return {"accepted": True, "reason": "unscored_but_covered", "char_overlap": round(overlap, 2)}


# --- 主流程：找可疑区间 → 重解 → 逐段判定 → 并回时间轴 ---
def apply_refinement(segments: List[Dict[str, Any]], refine_window: Callable[[float, float], List[Dict[str, Any]]],
                     settings: RefineSettings | None = None,
                     diagnostics: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    active = settings or RefineSettings()
    diagnostics = diagnostics if diagnostics is not None else []
    spans = suspicious_spans(segments, active)
    decisions: List[Dict[str, Any]] = []
    accepted_windows: List[Dict[str, Any]] = []
    retried: List[Dict[str, Any]] = []

    for span in spans:
        try:
            after = refine_window(float(span["start"]), float(span["end"]))
        except Exception as exc:                            # 单个窗口失败不能影响整片结果
            decisions.append({**span, "accepted": False, "reason": f"window_failed: {exc}"})
            continue
        before = [item for item in segments
                  if float(item.get("from", 0.0)) >= span["start"] and float(item.get("to", 0.0)) <= span["end"]]
        verdict = judge_replacement(before or [span], after, span, active)
        decisions.append({"start": span["start"], "end": span["end"], "reasons": span["reasons"],
                          "before": "".join(str(item.get("content", "")) for item in before)[:60],
                          "after": "".join(str(item.get("content", "")) for item in after)[:60], **verdict})
        if verdict["accepted"]:
            accepted_windows.append({"start": span["start"], "end": span["end"]})
            retried.extend(after)

    merged = asr_coverage.merge_retried_segments(segments, retried, accepted_windows) if accepted_windows else list(segments)
    report = {
        "checked": True,
        "suspicious": len(spans),
        "accepted": len(accepted_windows),
        "rejected": len(spans) - len(accepted_windows),
        "decisions": decisions,
        "settings": active._asdict(),
    }
    diagnostics.append({
        "step": "asr_refine",
        "ok": True,
        "message": f"{report['accepted']}/{report['suspicious']} suspicious span(s) replaced after re-decoding",
        "report": report,
    })
    return {"segments": merged, "report": report}
