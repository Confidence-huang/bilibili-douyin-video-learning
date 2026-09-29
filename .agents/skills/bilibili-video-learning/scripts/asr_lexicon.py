#!/usr/bin/env python3
r"""
领域词典：把"这段视频大概会说什么词"告诉 ASR，专治同音专名。

为什么需要它（真实错例，全部来自同一支视频）：
    whisper 在纯声学上无法区分这些词，实测错例有：
    `雪包→血包`、`再丢掉→在丢掉`、`工学→供血`、`断亲→断气`、`25岁→45岁`、`两俩→娘俩`、
    `想福→享福`、`一不避体→衣不蔽体`、`经营价值→经济价值`、`干的皮囊→干瘪的皮囊`。
    这些**不是模型不够大**的问题——large-v3 同样错。它们需要外部知识：术语表。
    faster-whisper 1.2.1 提供 `hotwords` 参数，它把词表作为解码偏置，
    相比 `initial_prompt` 不会把整段提示词"续写"进正文里（实测更安全）。

三层来源，按"越具体越优先"合并：
    1. 内置词表 `references/asr-lexicon.txt`（用户长期维护的领域术语）；
    2. 单次运行传入的 `--lexicon` / `--hotwords`（某支视频的专名）；
    3. 视频元数据里的词（标题/简介/作者/话题标签）——标题往往就是主题词。

边界：
    - 词表只影响解码偏置，**不修改任何输出文本**；因此它不会掩盖错误，只会降低同音错率。
    - 词表会进入缓存身份（见 D17）：换词表会重新转写，避免"同一份正文、两套依据"。
    - 长度有上限：提示词过长会挤占上下文并可能被"续写"，默认 200 字符并优先保留前面的高优先词。

调用示例：
    from asr_lexicon import build_hotwords, load_lexicon
    terms = load_lexicon(["references/asr-lexicon.txt", "我的术语.txt"])
    hotwords = build_hotwords(terms, metadata={"title": "男性是最好的血包"}, extra="供血 经济价值")
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import re  # 从元数据里切出候选词
from pathlib import Path  # 读取词表文件
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_LEXICON_PATH = Path(__file__).resolve().parent.parent / "references" / "asr-lexicon.txt"
HOTWORDS_LIMIT_CHARS = 200          # 提示词上限：太长会挤占上下文并增加被"续写"的风险
MIN_TERM_CHARS = 2                  # 单字词（如"血"）噪声太大，不作为术语
MAX_TERM_CHARS = 12                 # 过长的"术语"通常是整句，不适合做解码偏置
TERM_SPLIT = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]+")  # 按非字母数字汉字切分
TITLE_NOISE = {"视频", "抖音", "哔哩哔哩", "bilibili", "douyin", "分享", "转载", "字幕", "合集", "高清"}


# --- 读取词表文件：一行一个词，# 起注释 ---
def load_lexicon(paths: Optional[Iterable[str | Path]] = None) -> List[str]:
    resolved = [Path(p) for p in (paths or [])]
    if not resolved:
        resolved = [DEFAULT_LEXICON_PATH]
    terms: List[str] = []
    for path in resolved:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                                             # 缺文件不是错误：词表是可选增强
        for line in text.splitlines():
            term = line.split("#", 1)[0].strip()
            if term and MIN_TERM_CHARS <= len(term) <= MAX_TERM_CHARS:
                terms.append(term)
    return dedupe(terms)


# --- 从元数据里抽候选词：标题/简介/作者/标签 ---
def terms_from_metadata(metadata: Optional[Dict[str, Any]], *, limit: int = 40) -> List[str]:
    if not metadata:
        return []
    parts: List[str] = []
    for key in ("title", "fulltitle", "description", "uploader", "channel", "tags", "keywords"):
        value = metadata.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    terms: List[str] = []
    for part in parts:
        for raw in TERM_SPLIT.split(part):
            term = raw.strip()
            if MIN_TERM_CHARS <= len(term) <= MAX_TERM_CHARS and term.casefold() not in TITLE_NOISE:
                terms.append(term)
    return dedupe(terms)[:limit]


# --- 去重且保序：顺序即优先级 ---
def dedupe(terms: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for term in terms:
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            ordered.append(term)
    return ordered


# --- 合成 hotwords 字符串：按优先级拼接并截断，绝不超过上限 ---
def build_hotwords(lexicon: Optional[Iterable[str]] = None, metadata: Optional[Dict[str, Any]] = None,
                   extra: Optional[str] = None, *, limit_chars: int = HOTWORDS_LIMIT_CHARS) -> str:
    explicit = dedupe(term for term in (extra or "").replace(",", " ").replace("，", " ").split() if term)
    ordered = dedupe(list(explicit) + list(lexicon or []) + terms_from_metadata(metadata))
    selected: List[str] = []
    used = 0
    for term in ordered:
        cost = len(term) + (1 if selected else 0)                # 空格只是分隔，用于截断估算
        if used + cost > limit_chars:
            break                                               # 超限就停，保留优先级更高（更靠前）的词
        selected.append(term)
        used += cost
    return " ".join(selected)


# --- 供诊断显示：词表规模与截断情况（不泄露正文） ---
def describe_hotwords(hotwords: str) -> Dict[str, Any]:
    terms = [term for term in hotwords.split() if term]
    return {"terms": len(terms), "chars": len(hotwords), "limit_chars": HOTWORDS_LIMIT_CHARS,
            "truncated": len(hotwords) >= HOTWORDS_LIMIT_CHARS}
