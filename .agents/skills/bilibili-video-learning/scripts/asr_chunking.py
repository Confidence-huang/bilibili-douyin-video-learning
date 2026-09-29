#!/usr/bin/env python3
r"""
长音频分块：把整段解码切成带重叠的窗口，逐块转写后合并回一条时间轴（见 docs/DECISIONS.md D38）。

为什么需要它：
    此前全仓的 `chunk_length` 从未被使用，实测最长素材只有 259.77 秒，而 B站 课程视频动辄
    30–90 分钟。整段解码在长音频上有三个已知风险：显存/内存随长度增长、单次失败要全部重来、
    以及解码器在超长上下文里更容易漂移（复读/漏段）。分块把风险摊平到每块。

两条不变量（合并时必须守住）：
    1. **时间轴单调且对齐原音频**：每块的结果按块起点平移，绝不出现回跳；
    2. **重叠区不重复**：相邻块故意重叠 `overlap_seconds`，避免切在词中间丢字；
       合并时若后一块的文本已出现在前一块的尾部（同一段话被解了两遍），只保留前一块那份。

边界：
    - 默认关闭（`chunk_length=0`），只有显式传入才启用——这样既有行为完全不变；
    - 只做"规划 / 平移 / 去重"，不碰模型与 ffmpeg（那两件事由调用方负责，便于离线测试）。

调用示例：
    from asr_chunking import plan_chunks, merge_chunk_results
    chunks = plan_chunks(1800.0, 600.0, 2.0)          # 30 分钟音频 → 3 块
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

from typing import Any, Dict, List, Optional


DEFAULT_CHUNK_SECONDS = 600.0     # 10 分钟：显存与重试成本都还在可接受区间
DEFAULT_OVERLAP_SECONDS = 2.0     # 2 秒重叠：足够覆盖切在词中间的边界丢失
MIN_CHUNK_SECONDS = 5.0           # 比这更短的块没有意义（切出来只剩边界）


# --- 规划分块：保证覆盖到结尾，且每块不短于最小长度 ---
def plan_chunks(duration_seconds: Optional[float], chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
                overlap_seconds: float = DEFAULT_OVERLAP_SECONDS) -> List[Dict[str, float]]:
    if not duration_seconds or duration_seconds <= 0 or chunk_seconds <= 0:
        return []                                                     # 未启用分块：明确不分块，而不是按最小值硬切
    chunk = max(float(chunk_seconds), MIN_CHUNK_SECONDS)
    if duration_seconds <= chunk:
        return []                                                     # 不需要分块：整段一次解
    overlap = max(0.0, min(float(overlap_seconds), chunk / 2))         # 重叠不得吞掉整块
    plan: List[Dict[str, float]] = []
    start = 0.0
    index = 0
    while start < duration_seconds - 1e-6:
        end = min(start + chunk, duration_seconds)
        plan.append({"index": index, "start": round(start, 3), "end": round(end, 3)})
        if end >= duration_seconds - 1e-6:                             # 已到结尾
            break
        start = end - overlap                                          # 下一块回退一个重叠窗口
        index += 1

    absorb_below = max(MIN_CHUNK_SECONDS, chunk * 0.25)                 # 尾部不足块长 1/4 时并入前一块
    if len(plan) > 1 and plan[-1]["end"] - plan[-1]["start"] < absorb_below:
        # 尾巴只剩几秒时，单独解一块纯属浪费：把这截并入前一块，不产生碎片块
        plan[-2]["end"] = plan[-1]["end"]
        plan.pop()
    return plan


# --- 合并各块结果：按块起点平移时间，并丢掉重叠区里重复解出的段 ---
def merge_chunk_results(chunk_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    previous_texts: List[str] = []
    for result in chunk_results:
        offset = float(result.get("start") or 0.0)
        for segment in result.get("segments") or []:
            text = str(segment.get("content") or segment.get("text") or "").strip()
            shifted = dict(segment)
            for key in ("from", "to", "start", "end"):
                if shifted.get(key) is not None:
                    shifted[key] = round(float(shifted[key]) + offset, 3)
            if shifted.get("words"):
                shifted["words"] = [{**word,
                                     **{key: round(float(word[key]) + offset, 3)
                                        for key in ("start", "end") if word.get(key) is not None}}
                                    for word in shifted["words"]]
            if text and text in previous_texts:                         # 重叠区被解了两遍：保留前一块那份
                shifted["duplicate_of_previous_chunk"] = True
                continue
            merged.append(shifted)
            if text:
                previous_texts.append(text)
    return merged


# --- 合并后的报告片段（分块信息要能被审计）---
def chunking_diagnostic(plan: List[Dict[str, float]], merged_count: int, dropped: int) -> Dict[str, Any]:
    return {"step": "asr_chunking", "ok": True,
            "message": f"{len(plan)} chunk(s), {merged_count} segment(s) kept, {dropped} duplicate(s) dropped",
            "chunks": plan, "merged_segments": merged_count, "dropped_duplicates": dropped}
