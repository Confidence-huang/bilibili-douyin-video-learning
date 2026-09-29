#!/usr/bin/env python3
r"""
转写分段的唯一规范形状与适配层。

规范形状（canonical）：
    {"start": float, "end": float, "text": str, "confidence": float | None,
     "words": [{"start": float, "end": float, "word": str}, ...]}
历史形状（ASR 引擎输出，speech_to_text 内部使用）：
    {"from": float, "to": float, "content": str, "words": [...]}
`words` 与 `confidence` 都是可选的：有就原样带过去，缺省绝不补一个假值。

为什么需要这一层：
    同一条链路里曾经并存两套字段名——字幕解析产出 `start/end/text`，
    ASR 产出 `from/to/content`，而清洗/切分脚本只认前者。
    结果是抖音的转写结果喂给 `clean_transcript.py` 会直接 KeyError，
    两个脚本因此长期无人调用（见 docs/DECISIONS.md D20）。
    现在所有跨脚本传递都用规范形状，历史形状只在 ASR 边界内出现，由本模块双向转换。

调用示例：
    from normalize_transcript import normalize_segments, resegment_by_words, to_srt
    canonical = normalize_segments(asr_segments)      # from/to/content -> start/end/text
    shards = resegment_by_words(asr_segments)         # 按词边界重切成 ≤28 字的短句，一个词都不丢
    open("out.srt", "w", encoding="utf-8").write(to_srt(shards))
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注


CANONICAL_FIELDS = ("start", "end", "text")                                          # 跨脚本传递的唯一字段名
ASR_FIELDS = ("from", "to", "content")                                               # 仅在 ASR 模块内部出现
SIMPLIFY_MODES = ("auto", "on", "off")                                               # auto = 装了 OpenCC 就归一，没装就跳过并报告
DEFAULT_SENTENCE_GAP_SECONDS = 0.6                                                    # 停顿超过该值视为句末
DEFAULT_MAX_CHARS = 28                                                                # 中文单行上限：一行 28 字以内才挂得住字幕
STRONG_PUNCTUATION = "。！？!?"                                                        # 句末语气：出现在词尾即认定一句话说完
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
        words = segment.get("words")                                                  # 词级时间戳可选：有就必须带下去，重新分段全靠它
        if words is not None:
            item["words"] = _normalize_words(words)
        normalized.append(item)
    return normalized


# --- 词级时间戳的归一化：字段不全的词条不进 words，交给下游走降级路径 ---
def _normalize_words(words) -> list[dict]:
    normalized = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        token = word.get("word")                                                       # 只认 ASR 的词级形状，别的字段名不猜
        start, end = word.get("start"), word.get("end")
        if token is None or start is None or end is None:                             # 缺时间的词条无法定位帧，宁可丢弃也不补 0
            continue
        normalized.append({"start": round(float(start), 3),
                           "end": round(float(end), 3),
                           "word": str(token)})
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
    if len(lines) > max_lines:                                                       # 单行字数上限是硬约束：宁可多给一行，也不把剩余内容塞进末行越界
        max_lines = len(lines)
    return "\n".join(lines[:max_lines])


# --- 词级重新分段：按词边界把 ASR 的粗段落切成可直接挂字幕的短句 ---
# 为什么需要：
#   词级时间戳实测里既有 8.6 秒 / 35 字的长段，也有大量 1-2 秒的无标点短段，
#   成稿因此没有句读、SRT 单行过长，也无法与硬字幕卡片/抽帧对齐。
#   词级时间戳（timing 档位写进 words）给出唯一可靠的切分依据：字数、词间停顿、句末标点。
# 边界：
#   1. 只在词与词之间下刀：没有 words（或词文本与段文本对不上）的分段原样保留，绝不用字符比例猜时间；
#   2. 输出同时带规范字段 start/end/text 与 ASR 字段 from/to/content，沿用本模块"两套形状都能给"的做法；
#   3. 硬不变量：所有输出文本拼接（去空白口径）与输入拼接逐字一致，顺序不变，一个词都不丢；
#   4. max_chars 是"累计到顶就切"而不是"预先截断词"，所以单段最多超出 max_chars 一个词的长度，
#      越界的段数由 resegment_summary 的 cues_over_limit 如实报出来（切词比越界更不可接受）。
def resegment_by_words(segments, *, max_chars: int = DEFAULT_MAX_CHARS,
                       sentence_gap_seconds: float = DEFAULT_SENTENCE_GAP_SECONDS,
                       strong_punctuation: str = STRONG_PUNCTUATION) -> list[dict]:
    resegmented: list[dict] = []
    for segment in normalize_segments(segments):
        resegmented.extend(_resegment_one_segment(segment, max_chars=max_chars,
                                                  sentence_gap_seconds=sentence_gap_seconds,
                                                  strong_punctuation=strong_punctuation))
    return resegmented


# --- 单段切分：能对齐词就按词边界切，对不齐就整段保留（绝不猜时间、绝不改正文）---
def _resegment_one_segment(segment: dict, *, max_chars: int, sentence_gap_seconds: float,
                           strong_punctuation: str) -> list[dict]:
    text, words = segment["text"], segment.get("words") or []
    spans = _locate_words(text, words) if words and _monotone_words(words) else None
    if spans is None:                                                                 # 降级路径：没有词时间戳、词与正文对不上、或词时间轴不单调
        return [_emit_segment(segment, start=segment["start"], end=segment["end"], text=text, words=words)]
    runs = _word_runs(words, max_chars=max_chars, sentence_gap_seconds=sentence_gap_seconds,
                      strong_punctuation=strong_punctuation)
    return [_segment_from_run(segment, text, words, spans, run) for run in runs]


# --- 词时间轴必须单调不减：不单调的词切出来必然重叠，那种段宁可整段保留 ---
def _monotone_words(words: list[dict]) -> bool:
    previous_end = None
    for word in words:
        if word["end"] < word["start"] or (previous_end is not None and word["start"] < previous_end):
            return False
        previous_end = word["end"]
    return True


# --- 一个词区间 -> 一个输出分段：文本取原文切片（切点空白跟前一段走），时间取词的起止 ---
def _segment_from_run(segment: dict, text: str, words: list[dict], spans: list[tuple[int, int]],
                      run: list[int]) -> dict:
    following = run[-1] + 1
    offset = spans[following][0] if following < len(spans) else len(text)              # 与原文逐字对齐的关键：切点之后的空白留在上一段末尾
    return _emit_segment(segment, start=words[run[0]]["start"], end=words[run[-1]]["end"],
                         text=text[spans[run[0]][0]:offset],
                         words=[words[index] for index in run])


# --- 统一出口：规范字段与 ASR 字段同时给，父段元数据继承而不是伪造 ---
def _emit_segment(parent: dict, *, start: float, end: float, text: str, words: list[dict]) -> dict:
    item = {"start": start, "end": end, "text": text,
            "from": start, "to": end, "content": text}
    if parent.get("confidence") is not None:                                          # 子段没有独立置信度，沿用父段测量值
        item["confidence"] = parent["confidence"]
    if words:
        item["words"] = words
    return item


# --- 把词映射到段文本的字符区间；任何一处对不上就返回 None，让调用方整段保留 ---
def _locate_words(text: str, words: list[dict]) -> list[tuple[int, int]] | None:
    spans = []
    cursor = 0
    for word in words:
        token = word["word"].strip()
        if not token:                                                                 # 空白词条没有可见文字，说明词表不可信
            return None
        start = text.find(token, cursor)
        if start < 0 or text[cursor:start].strip():                                   # 找不到，或中间夹着正文，都说明词序与正文不一致
            return None
        spans.append((start, start + len(token)))
        cursor = start + len(token)
    if text[cursor:].strip():                                                         # 正文尾部还有词级时间戳覆盖不到的字，同样不敢切
        return None
    return spans


# --- 按字数/停顿/句末标点把词下标切成若干连续区间，顺序与覆盖范围都不变 ---
def _word_runs(words: list[dict], *, max_chars: int, sentence_gap_seconds: float,
               strong_punctuation: str) -> list[list[int]]:
    endings = tuple(strong_punctuation) if strong_punctuation else ()                  # endswith 必须收元组：传整个字符串会被当成一个后缀
    runs: list[list[int]] = []
    current: list[int] = []
    chars = 0
    for index, word in enumerate(words):
        if current and word["start"] - words[current[-1]]["end"] >= sentence_gap_seconds:
            runs.append(current)                                                      # 词间停顿够长：上一句到此为止
            current, chars = [], 0
        current.append(index)
        chars += len(word["word"].strip())                                            # 字数只算可见文字，空格不占字幕宽度
        if _ends_clause(word["word"], chars, max_chars=max_chars, endings=endings):
            runs.append(current)                                                      # 字数到顶或词尾是句末标点：断句
            current, chars = [], 0
    if current:
        runs.append(current)
    return runs


# --- 该词是不是断句点：累计字数到顶，或词尾就是句末标点 ---
def _ends_clause(word: str, chars: int, *, max_chars: int, endings: tuple[str, ...]) -> bool:
    return (max_chars > 0 and chars >= max_chars) or (bool(endings) and word.rstrip().endswith(endings))


# --- 重新分段的可验证摘要：证明"更细"没有代价（不丢字、越界 cue 受控）---
# 字数按去空白口径统计：与丢字不变量同口径，中文 ASR 的词间空格不占 28 字的预算。
def resegment_summary(before, after, *, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    canonical_before, canonical_after = normalize_segments(before), normalize_segments(after)
    return {
        "segments_before": len(canonical_before),
        "segments_after": len(canonical_after),
        "dropped_chars": _visible_chars(canonical_before) - _visible_chars(canonical_after),  # 正数=真丢了字，本模块的切分恒为 0
        "cues_over_limit": sum(1 for item in canonical_after
                               if len(_visible_text(item["text"])) > max_chars),
        "median_segment_seconds": {"before": _median_seconds(canonical_before),
                                   "after": _median_seconds(canonical_after)},
        "max_chars": max_chars,
    }


# --- 去空白口径：中文 ASR 的词间空格是装饰性的，不参与"有没有丢字"的比较 ---
def _visible_text(text: str) -> str:
    return "".join(text.split())


def _visible_chars(segments) -> int:
    return sum(len(_visible_text(item["text"])) for item in segments)


# --- 中位段长：比均值更能反映"大量 1-2 秒短段"的真实分布 ---
def _median_seconds(segments) -> float:
    if not segments:
        return 0.0
    durations = sorted(item["end"] - item["start"] for item in segments)
    middle = len(durations) // 2
    median = durations[middle] if len(durations) % 2 else (durations[middle - 1] + durations[middle]) / 2
    return round(median, 3)
