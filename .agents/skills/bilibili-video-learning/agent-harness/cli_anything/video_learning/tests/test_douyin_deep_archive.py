"""
douyin_deep_archive.py 纯函数的离线测试：场景分解析、自适应峰值、对齐与图文对照节构建、笔记更新幂等。
不触网、不调桥接、不下载模型；引擎脚本按相对路径从 Skill 树的 scripts/ 目录加载。
"""
from __future__ import annotations  # 保持测试类型标注兼容 Python 3.10+。

import importlib.util  # scripts/ 不是包，用 spec 直接按文件路径加载。
from pathlib import Path  # 定位 Skill 根目录下的引擎脚本。

import pytest  # 参数化与 tmp_path 夹具。

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "douyin_deep_archive.py"
_spec = importlib.util.spec_from_file_location("douyin_deep_archive_under_test", _SCRIPT)
dda = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dda)


def test_parse_scene_scores_pairs_pts_with_score():
    text = "frame:0 pts_time:0.5\nlavfi.scene_score=0.030000\ndummy\nframe:1 pts_time:2.0\nlavfi.scene_score=0.280000"
    assert dda.parse_scene_scores(text) == [(0.5, 0.03), (2.0, 0.28)]


def test_adaptive_peaks_drop_subpeak_and_floor():
    scores = [(0.5, 0.03), (2.0, 0.28), (3.5, 0.26)]
    assert dda.adaptive_peaks(scores, max_frames=24) == [2.0]  # 3.5 低于前峰，非局部极大值；0.5 被 floor 过滤。


def test_adaptive_peaks_respect_min_gap_by_score_desc_greedy():
    scores = [(1.0, 0.5), (1.5, 0.3), (2.0, 0.45), (10.0, 0.6)]
    assert dda.adaptive_peaks(scores, max_frames=24) == [1.0, 10.0]


def test_adaptive_peaks_cap_is_max_frames_minus_one():
    scores = [(i * 1.0, 0.5 if i % 2 == 0 else 0.1) for i in range(0, 20)]
    assert dda.adaptive_peaks(scores, max_frames=5) == [0.0, 2.0, 4.0, 6.0]


def test_align_segments_spanning_sentence_hits_both_frames():
    frames = [{"idx": 1, "t1": 0.0, "t2": 3.2}, {"idx": 2, "t1": 3.2, "t2": 4.5}]
    segments = [{"from": 0, "to": 2, "content": "a"}, {"from": 3.0, "to": 4, "content": "b"}, {"from": 10, "to": 11, "content": "c"}]
    hits, rest = dda.align_segments(segments, frames)
    assert [h["content"] for h in hits[0]] == ["a", "b"]  # 跨越切换点的句子按插件同款判定式落入两帧。
    assert [h["content"] for h in hits[1]] == ["b"]
    assert [r["content"] for r in rest] == ["c"]


def test_build_section_embeds_frames_and_lists_uncovered_tail():
    frames = [
        {"idx": 1, "t1": 0.0, "t2": 3.2, "vault_path": "附件/douyin-media/frames/x/图01.jpg"},
        {"idx": 2, "t1": 3.2, "t2": 4.5, "vault_path": "附件/douyin-media/frames/x/图02.jpg"},
        {"idx": 3, "t1": 4.5, "t2": 10.0, "vault_path": "附件/douyin-media/frames/x/图03.jpg"},
    ]
    segments = [{"from": 0, "to": 2, "content": "a"}, {"from": 3.5, "to": 4, "content": "b"}, {"from": 10.5, "to": 11, "content": "c"}]
    section = dda.build_section(frames, segments, 11.0)
    assert "### 图 01 ｜ 00:00–00:03" in section
    assert "![[附件/douyin-media/frames/x/图01.jpg]]" in section
    assert "`00:00` a" in section and "`00:03` b" in section
    assert "（该帧区间内无人声解说）" in section
    assert "### 时间轴补遗" in section and "`00:10` c" in section


def test_update_note_is_idempotent_and_sets_fields(tmp_path: Path):
    note = tmp_path / "2020-01-01 示例 [123456].md"
    note.write_text(
        "---\ntitle: \"示例\"\ndouyin_id: \"123456\"\ntranscript_status: not_requested\ntags:\n  - 抖音\n---\n\n# 示例\n\n正文。\n\n---\n\n*同步于 X*\n",
        encoding="utf-8",
    )
    section = dda.build_section(
        [{"idx": 1, "t1": 0.0, "t2": 3.2, "vault_path": "附件/douyin-media/frames/x/图01.jpg"}],
        [{"from": 0, "to": 2, "content": "a"}],
        4.0,
    )
    dda.update_note(note, section, 1, "faster-whisper:small")
    first = note.read_text(encoding="utf-8")
    assert first.count("## 图文对照") == 1
    assert 'transcript_provider: "faster-whisper:small"' in first
    assert "transcript_status: success" in first and "not_requested" not in first
    assert "frames_extracted: 1" in first and "deep_archived_at:" in first
    assert first.rstrip().endswith("*")  # 文末同步页脚保持原位。
    dda.update_note(note, section, 1, "faster-whisper:small")
    assert note.read_text(encoding="utf-8").count("## 图文对照") == 1  # 重跑整节替换，不重复堆节。


def test_find_note_matches_by_id_substring_not_glob(tmp_path: Path):
    (tmp_path / "2020-01-01 示例 [123456].md").write_text("x", encoding="utf-8")
    assert dda.find_note(tmp_path, "123456") is not None
    assert dda.find_note(tmp_path, "999999") is None
