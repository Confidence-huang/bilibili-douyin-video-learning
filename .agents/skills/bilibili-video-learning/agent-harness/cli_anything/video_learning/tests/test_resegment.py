"""
词级重新分段与 SRT 质量的离线测试（Phase 3）。

守住四件事：
    1. 绝不丢词：切分后所有文本拼接（去空白口径）与输入逐字一致，顺序不变；
    2. 三条切分依据各自生效且各有边界：累计字数、词间停顿、词尾强标点；
    3. 优雅降级：没有 words 或词与正文对不上的分段原样保留，时间绝不按字符比例猜；
    4. 重新分段后 SRT 每行仍 ≤ max_line_chars，折行不丢字，时间戳单调不重叠。
运行示例：python -m pytest cli_anything/video_learning/tests/test_resegment.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

import pytest  # 提供近似比较与断言辅助。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态导入脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

import normalize_transcript as nt  # noqa: E402 被测模块与生产脚本共用同一实例。


# --- 造真实形状的词级时间戳：词与词首尾相接（实测 faster-whisper 就是这个形状，词间无空隙）---
def build_words(pieces, *, start: float = 0.0, seconds_per_char: float = 0.2,
                pause_after: dict[int, float] | None = None) -> list[dict]:
    words = []
    cursor = start
    for index, piece in enumerate(pieces):
        end = round(cursor + seconds_per_char * len(piece), 3)
        words.append({"start": round(cursor, 3), "end": end, "word": piece})
        cursor = round(end + (pause_after or {}).get(index, 0.0), 3)  # 只在需要复现"词间停顿"时才加空隙
    return words


# --- 造一个 ASR 形状的带词分段：content 与词文本逐字一致（真实数据里 hard mismatch 为 0）---
def build_segment(pieces, **kwargs) -> dict:
    words = build_words(pieces, **kwargs)
    return {"from": words[0]["start"], "to": words[-1]["end"],
            "content": "".join(pieces), "words": words}


# --- 去空白口径的全文：不变量只在这个口径上成立（中文 ASR 的词间空格是装饰性的）---
def visible(segments) -> str:
    return "".join("".join(item["text"].split()) for item in segments)


TWO_CHAR_WORDS = ["今天", "我们", "来讲", "一讲", "视频", "学习", "笔记", "怎么",
                  "做才", "能既", "快又", "准而", "且不", "丢字", "结尾"]  # 15 个词共 30 字


# --- 长段按 max_chars 切开，且拼接后与原文逐字一致 ---
def test_long_segment_splits_at_max_chars_without_losing_a_word():
    segment = build_segment(TWO_CHAR_WORDS)
    original = segment["content"]

    result = nt.resegment_by_words([segment], max_chars=28)  # 28 字到顶即切，末词 2 字单独成段

    assert len(result) == 2
    assert [item["text"] for item in result] == ["".join(TWO_CHAR_WORDS[:14]), "结尾"]
    assert "".join(item["text"] for item in result) == original  # 硬不变量：逐字一致，一个词都不丢
    assert visible(result) == visible(nt.normalize_segments([segment]))
    assert max(len(item["text"]) for item in result) <= 28
    assert result[0]["end"] == segment["words"][13]["end"]  # 时间取词的起止，不是按字数比例算的
    assert result[1]["start"] == segment["words"][14]["start"]


# --- 词间 0.8s 停顿必须切开；0.3s 的换气不能切 ---
def test_pause_between_words_forces_a_break():
    paused = build_segment(["停顿前", "停顿后"], pause_after={0: 0.8})

    result = nt.resegment_by_words([paused])

    assert [item["text"] for item in result] == ["停顿前", "停顿后"]
    assert [(item["start"], item["end"]) for item in result] == [(0.0, 0.6), (1.4, 2.0)]

    breathing = build_segment(["换气前", "换气后"], pause_after={0: 0.3})
    assert len(nt.resegment_by_words([breathing])) == 1  # 短换气不是句末


# --- 词尾强标点（。！？!?）断句，逗号不断句 ---
def test_strong_punctuation_at_word_tail_breaks_the_cue():
    sentence = build_segment(["今天很好。", "明天再说"])

    result = nt.resegment_by_words([sentence])

    assert [item["text"] for item in result] == ["今天很好。", "明天再说"]

    comma = build_segment(["今天很好，", "明天再说"])
    assert [item["text"] for item in nt.resegment_by_words([comma])] == ["今天很好，明天再说"]


# --- 时间单调不减、段间不重叠、每段起止就是首末词的时间 ---
def test_timeline_is_monotone_and_cues_never_overlap():
    segment = build_segment(TWO_CHAR_WORDS, pause_after={6: 0.9})

    result = nt.resegment_by_words([segment], max_chars=12)

    assert len(result) >= 3
    assert nt.validate_segments(result) == []  # 既没有倒置也没有重叠
    for previous, current in zip(result, result[1:]):
        assert current["start"] >= previous["end"]  # 单调不减且不重叠
    assert result[0]["start"] == segment["words"][0]["start"]
    assert result[-1]["end"] == segment["words"][-1]["end"]
    assert "".join(item["text"] for item in result) == segment["content"]


# --- 没有 words 的分段原样保留：不许用字符比例猜时间 ---
def test_segments_without_words_are_kept_verbatim():
    with_words = build_segment(["有词", "的段落"])
    without_words = {"from": 5.0, "to": 7.5, "content": "没有词级时间戳的十一个字段落"}

    result = nt.resegment_by_words([with_words, without_words], max_chars=2)

    assert len(result) == 3  # 只有带词的那段被切开
    assert result[-1] == {"start": 5.0, "end": 7.5, "text": "没有词级时间戳的十一个字段落",
                          "from": 5.0, "to": 7.5, "content": "没有词级时间戳的十一个字段落"}
    assert "words" not in result[-1]  # 不伪造词级时间戳


# --- 词文本与段文本对不上时整段保留，绝不改变正文 ---
def test_segments_whose_words_do_not_match_the_text_are_kept_whole():
    mismatched = {"from": 0.0, "to": 2.0, "content": "甲乙丙丁",
                  "words": [{"start": 0.0, "end": 1.0, "word": "甲乙"},
                            {"start": 1.0, "end": 2.0, "word": "XY"}]}

    result = nt.resegment_by_words([mismatched], max_chars=2)

    assert len(result) == 1
    assert result[0]["text"] == "甲乙丙丁" and result[0]["start"] == 0.0 and result[0]["end"] == 2.0


# --- 词时间轴不单调时同样不切：切出来必然产出重叠字幕 ---
def test_non_monotone_word_timestamps_are_not_split():
    backwards = {"from": 0.0, "to": 2.0, "content": "甲乙丙丁",
                 "words": [{"start": 1.0, "end": 1.5, "word": "甲乙"},  # 第二个词的时间回退了
                           {"start": 0.5, "end": 1.0, "word": "丙丁"}]}

    result = nt.resegment_by_words([backwards], max_chars=2)

    assert len(result) == 1
    assert nt.validate_segments(result) == []  # 宁可不切，也不产出重叠时间轴


# --- 摘要：dropped_chars 与 cues_over_limit 都要能抓出问题 ---
def test_summary_reports_dropped_chars_and_over_limit_cues():
    long_text = {"start": 0.0, "end": 4.0, "text": "甲" * 30}

    kept = nt.resegment_summary([long_text], [long_text])
    assert kept == {"segments_before": 1, "segments_after": 1, "dropped_chars": 0,
                    "cues_over_limit": 1, "median_segment_seconds": {"before": 4.0, "after": 4.0},
                    "max_chars": 28}

    split = nt.resegment_summary([build_segment(TWO_CHAR_WORDS)],
                                 nt.resegment_by_words([build_segment(TWO_CHAR_WORDS)]))
    assert split["segments_before"] == 1 and split["segments_after"] == 2
    assert split["dropped_chars"] == 0 and split["cues_over_limit"] == 0

    lossy = nt.resegment_summary([{"start": 0.0, "end": 1.0, "text": "甲乙丙"}],
                                 [{"start": 0.0, "end": 1.0, "text": "甲乙"}])
    assert lossy["dropped_chars"] == 1  # 这个指标不是摆设：真丢字就报正数


# --- 重新分段是幂等的：再切一次不会多出段、也不会改字 ---
def test_resegmentation_is_idempotent():
    segment = build_segment(TWO_CHAR_WORDS, pause_after={3: 0.9})

    once = nt.resegment_by_words([segment], max_chars=12)
    twice = nt.resegment_by_words(once, max_chars=12)

    assert twice == once


# --- 重新分段后 SRT 每行 ≤ max_line_chars，去换行后与段文本一致 ---
def test_srt_after_resegmentation_respects_line_limit_and_keeps_text():
    result = nt.resegment_by_words([build_segment(TWO_CHAR_WORDS * 2)], max_chars=28)

    srt = nt.to_srt(result, max_line_chars=24)
    blocks = srt.strip().split("\n\n")

    assert len(blocks) == len(result)
    for block, item in zip(blocks, result):
        lines = block.split("\n")[2:]
        assert all(len(line) <= 24 for line in lines)
        assert "".join(lines) == item["text"]  # 折行不得丢字、不得加字

    degraded = nt.to_srt([{"start": 0.0, "end": 9.0, "text": "字" * 80}], max_line_chars=24)
    lines = degraded.strip().split("\n")[2:]
    assert all(len(line) <= 24 for line in lines)  # 没有词级时间戳的坏输入也不能让单行越界
    assert "".join(lines) == "字" * 80


# --- words 字段经过 normalize_segments 不丢，也不伪造 ---
def test_words_survive_normalization_without_being_invented():
    asr = {"from": 0.0, "to": 1.0, "content": "甲乙",
           "words": [{"start": 0.0, "end": 0.5, "word": "甲"},
                     {"start": 0.5, "end": 1.0, "word": "乙"}]}

    canonical = nt.normalize_segments([asr])

    assert canonical[0]["words"] == asr["words"]
    assert nt.detect_schema(canonical) == nt.SCHEMA_CANONICAL
    assert "words" not in nt.normalize_segments([{"start": 0.0, "end": 1.0, "text": "甲乙"}])[0]

    partial = nt.normalize_segments([{"start": 0.0, "end": 1.0, "text": "甲乙",
                                      "words": [{"start": 0.0, "end": 0.5, "word": "甲"},
                                                {"start": 0.5, "word": "乙"}]}])
    assert partial[0]["words"] == [{"start": 0.0, "end": 0.5, "word": "甲"}]  # 缺时间的词条宁可丢弃也不补 0


# --- 无词可切时不是崩，而是原样返回（空输入、空 words 都要安全）---
def test_empty_inputs_and_empty_words_degrade_gracefully():
    assert nt.resegment_by_words([]) == []
    single = {"from": 0.0, "to": 1.0, "content": "没有词", "words": []}

    assert nt.resegment_by_words([single]) == [{"start": 0.0, "end": 1.0, "text": "没有词",
                                                "from": 0.0, "to": 1.0, "content": "没有词"}]
    with pytest.raises(nt.TranscriptSchemaError):  # 认不出的形状仍然要显式失败
        nt.resegment_by_words([{"begin": 0, "finish": 1, "body": "?"}])
