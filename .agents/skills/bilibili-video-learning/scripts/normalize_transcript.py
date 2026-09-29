#!/usr/bin/env python3
r"""
转写分段的唯一规范形状与适配层。

规范形状（canonical）：
    {"start": float, "end": float, "text": str, "confidence": float | None}
历史形状（ASR 引擎输出，speech_to_text 内部使用）：
    {"from": float, "to": float, "content": str}

为什么需要这一层：
    同一条链路里曾经并存两套字段名——字幕解析产出 `start/end/text`，
    ASR 产出 `from/to/content`，而清洗/切分脚本只认前者。
    结果是抖音的转写结果喂给 `clean_transcript.py` 会直接 KeyError，
    两个脚本因此长期无人调用（见 docs/DECISIONS.md D20）。
    现在所有跨脚本传递都用规范形状，历史形状只在 ASR 边界内出现，由本模块双向转换。

调用示例：
    from normalize_transcript import normalize_segments, to_srt
    canonical = normalize_segments(asr_segments)      # from/to/content -> start/end/text
    open("out.srt", "w", encoding="utf-8").write(to_srt(canonical))
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注


CANONICAL_FIELDS = ("start", "end", "text")                                          # 跨脚本传递的唯一字段名
ASR_FIELDS = ("from", "to", "content")                                               # 仅在 ASR 模块内部出现
SIMPLIFY_MODES = ("auto", "on", "off")                                               # auto = 装了 OpenCC 就归一，没装就跳过并报告
DEFAULT_SENTENCE_GAP_SECONDS = 0.6                                                    # 停顿超过该值视为句末
SCHEMA_CANONICAL = "canonical"
SCHEMA_ASR = "asr"
SCHEMA_EMPTY = "empty"
SCHEMA_UNKNOWN = "unknown"


# --- 分段形状不认识时给出的明确错误 ---
class TranscriptSchemaError(ValueError):
    """分段既不是规范形状也不是 ASR 形状；调用方应显式失败而不是猜。"""


# --- 判断一批分段用的是哪种字段名 ---
def detect_schema(segments) -> str:
    if not segments:                                                                 # 空列表无法判断，单独归类
        return SCHEMA_EMPTY
    first = segments[0]
    if not isinstance(first, dict):                                                  # 非字典元素属于无法识别的输入
        return SCHEMA_UNKNOWN
    if all(field in first for field in CANONICAL_FIELDS):
        return SCHEMA_CANONICAL
    if all(field in first for field in ASR_FIELDS):
        return SCHEMA_ASR
    return SCHEMA_UNKNOWN


# --- 把任意一种已知形状转成规范形状 ---
def normalize_segments(segments, *, origin: str = "auto") -> list[dict]:
    schema = detect_schema(segments)
    if schema == SCHEMA_EMPTY:
        return []
    if schema == SCHEMA_UNKNOWN and origin == "auto":                                # 自动判断失败时，按字段逐个尝试更宽容的推断
        schema = _guess_schema_by_fields(segments)
    if schema == SCHEMA_UNKNOWN:
        raise TranscriptSchemaError(
            f"unrecognized transcript schema (origin={origin}); "
            f"expected {CANONICAL_FIELDS} or {ASR_FIELDS}, got keys={sorted(segments[0].keys())}"
        )

    normalized = []
    for index, segment in enumerate(segments):
        if schema == SCHEMA_ASR:
            start, end, text = segment.get("from"), segment.get("to"), segment.get("content")
        else:
            start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
        if start is None or end is None:                                             # 时间缺失会让下游时间轴失效，必须显式失败
            hint = ""
            if start is not None and end is None and "text" in segment:
                hint = ("；这看起来是 1.6 之前保存的抖音结果（只有 start/text）。"
                        "重新跑一次提取即可，缓存 schema 已升级到 4，旧缓存不会被复用")
            raise TranscriptSchemaError(f"segment #{index} is missing start/end (schema={schema}){hint}")
        item = {
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "text": str(text or "").strip(),
        }
        confidence = segment.get("confidence")                                       # 置信度可选，缺省不伪造
        if confidence is not None:
            item["confidence"] = float(confidence)
        normalized.append(item)
    return normalized


# --- 首元素字段不全时的宽容推断（混合来源结果里常见） ---
def _guess_schema_by_fields(segments) -> str:
    first = segments[0]
    if not isinstance(first, dict):
        return SCHEMA_UNKNOWN
    canonical_hits = sum(1 for field in CANONICAL_FIELDS if field in first)
    asr_hits = sum(1 for field in ASR_FIELDS if field in first)
    if canonical_hits >= 2 and canonical_hits >= asr_hits:
        return SCHEMA_CANONICAL
    if asr_hits >= 2:
        return SCHEMA_ASR
    return SCHEMA_UNKNOWN


# --- 反向转换：给仍然按 ASR 字段名取值的调用方 ---
def to_asr_segments(segments) -> list[dict]:
    return [
        {"from": item["start"], "to": item["end"], "content": item["text"]}
        for item in normalize_segments(segments)
    ]


# --- 规范形状的自检：返回问题清单而不是抛错，便于诊断输出 ---
def validate_segments(segments) -> list[str]:
    problems: list[str] = []
    previous_end = None
    for index, item in enumerate(segments):
        if item["end"] < item["start"]:
            problems.append(f"segment #{index} ends before it starts ({item['start']} -> {item['end']})")
        if previous_end is not None and item["start"] < previous_end - 0.01:
            problems.append(f"segment #{index} overlaps the previous one at {item['start']}s")
        if not item["text"]:
            problems.append(f"segment #{index} has empty text")
        previous_end = max(previous_end or 0.0, item["end"])
    return problems


# --- 摘要：让调用方与诊断都能看到覆盖情况 ---
def summarize_segments(segments) -> dict:
    if not segments:
        return {"count": 0, "start": None, "end": None, "covered_seconds": 0.0, "text_chars": 0}
    return {
        "count": len(segments),
        "start": segments[0]["start"],
        "end": segments[-1]["end"],
        "covered_seconds": round(sum(item["end"] - item["start"] for item in segments), 3),
        "text_chars": len("".join(item["text"] for item in segments)),
    }


# --- 规范形状 -> 纯文本 ---
def to_plain_text(segments, *, separator: str = "", pause_punctuation: bool = False,
                  sentence_gap_seconds: float = DEFAULT_SENTENCE_GAP_SECONDS) -> str:
    if pause_punctuation:
        return join_with_pause_punctuation(segments, sentence_gap_seconds=sentence_gap_seconds)
    return separator.join(item["text"] for item in normalize_segments(segments)).strip()

# --- 中文文本归一化：繁简与停顿标点 ---
# 为什么需要：
#   同一个模型在不同设备/精度下会输出不同字形——实测 large-v3 出简体，
#   large-v3-turbo 在同一段音频上出「存在的價值是給別人創造價值的那和豬眷裡養肥了再殺的豬」。
#   逐字稿里混着繁体与简体，检索与引用都会失效。
#   whisper 的中文标点又很稀疏：28 秒的长段里有逗号，而大量 1-2 秒短段完全没有标点。
# --- 尝试加载 OpenCC 转换器（可选依赖） ---
def load_simplifier():
    try:
        from opencc import OpenCC                                                     # 可选依赖：opencc-python-reimplemented
        return OpenCC("t2s")
    except Exception:                                                                 # 没装不该让整条链路失败
        return None


# --- 繁简归一 ---
def simplify_segments(segments, *, mode: str = "auto", converter=None) -> tuple[list[dict], dict]:
    if mode not in SIMPLIFY_MODES:
        raise ValueError(f"unknown simplify mode '{mode}'; choose one of {SIMPLIFY_MODES}")
    canonical = normalize_segments(segments)
    note = {"mode": mode, "applied": False, "changed_segments": 0, "reason": None}
    if mode == "off":
        note["reason"] = "disabled by caller"
        return canonical, note

    simplifier = converter or load_simplifier()
    if simplifier is None:                                                            # auto 模式下缺依赖只记录，不报错
        note["reason"] = "OpenCC not installed (pip install opencc-python-reimplemented)"
        if mode == "on":
            raise RuntimeError(note["reason"])                                        # 显式要求归一却缺依赖，必须让调用方知道
        return canonical, note

    changed = 0
    for item in canonical:
        original = item["text"]
        item["text"] = simplifier.convert(original)
        if item["text"] != original:
            changed += 1
    note.update({"applied": True, "changed_segments": changed})
    return canonical, note


# --- 停顿标点：只在段与段的边界插入，绝不改动段内文字 ---
def join_with_pause_punctuation(segments, *, sentence_gap_seconds: float = DEFAULT_SENTENCE_GAP_SECONDS) -> str:
    canonical = normalize_segments(segments)
    if not canonical:
        return ""
    pieces = [canonical[0]["text"]]
    for previous, current in zip(canonical, canonical[1:]):
        gap = current["start"] - previous["end"]                                       # 静音长度是唯一可用的句读线索
        if gap >= sentence_gap_seconds:
            pieces.append("。" if not pieces[-1].endswith(("。", "！", "？", "，")) else "")
        elif not pieces[-1].endswith(("。", "！", "？", "，", "、", "：", "；")):
            pieces.append("，")
        pieces.append(current["text"])
    return "".join(pieces).strip()                                                    # 不插入任何字符到段内，保证可回溯



# --- 规范形状 -> SRT 字幕 ---
def to_srt(segments, *, max_line_chars: int = 24) -> str:
    blocks = []
    for index, item in enumerate(normalize_segments(segments), 1):
        text = _wrap_subtitle(item["text"], max_line_chars)
        blocks.append(f"{index}\n{_srt_timestamp(item['start'])} --> {_srt_timestamp(item['end'])}\n{text}\n")
    return "\n".join(blocks)


# --- SRT 时间戳：毫秒，逗号分隔 ---
def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(int(round(float(seconds) * 1000)), 0)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


# --- 过长的字幕按标点折行，避免单行铺满屏幕 ---
def _wrap_subtitle(text: str, max_line_chars: int, max_lines: int = 2) -> str:
    text = text.strip()
    if len(text) <= max_line_chars or max_line_chars <= 0:
        return text
    lines = [text[index:index + max_line_chars] for index in range(0, len(text), max_line_chars)]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = lines[:max_lines - 1]
    head.append("".join(lines[max_lines - 1:]))                                      # 末行吸收剩余内容，保证不丢字
    return "\n".join(head)
