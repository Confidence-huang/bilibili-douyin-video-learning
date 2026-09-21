"""
Note-template contracts.

The note skeleton lives in `prompts/*.md` so it can be reviewed as text. That
introduces one new failure mode worth guarding: a template that is present but
malformed, or that has been renamed, silently degrades every note to the
built-in fallback headings. These tests pin the contract that the shipped
templates actually parse, and that the degraded path is reported rather than
hidden.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill.
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"
    module_name = f"video_learning_test_{name}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 模板必须真的存在且可解析 ---
def test_shipped_templates_parse():
    templates = load_script("prompt_templates")
    assert templates.validate_template(
        "bilibili-standard",
        ["基本信息", "一句话总结", "3 分钟速读", "详细笔记", "核心概念", "复习题", "待确认"],
    ) == []
    assert templates.validate_template(
        "douyin-standard",
        ["基本信息", "一句话总结", "核心要点", "详细笔记", "待确认"],
    ) == []


def test_template_declares_a_version():
    templates = load_script("prompt_templates")
    version, _body = templates.load_template("bilibili-standard")
    assert version != "unversioned"
    assert version.isdigit()


def test_parse_sections_ignores_fenced_blocks():
    templates = load_script("prompt_templates")
    body = "## Real\n\n```\n## NotASection\n```\n\n### Sub\n"
    assert templates.parse_sections(body) == [(2, "Real"), (3, "Sub")]


# --- 缺失模板时降级而不是崩溃 ---
def test_missing_template_returns_none_instead_of_raising():
    templates = load_script("prompt_templates")
    assert templates.load_template("does-not-exist") is None


def test_template_name_must_be_a_bare_stem():
    templates = load_script("prompt_templates")
    for bad_name in ("../SKILL", "sub/dir", "..", ".hidden"):
        with pytest.raises(ValueError):
            templates.load_template(bad_name)


def test_validate_reports_a_missing_required_section():
    templates = load_script("prompt_templates")
    problems = templates.validate_template("bilibili-standard", ["不存在的章节"])
    assert problems and "不存在的章节" in problems[0]


# --- 三个后端共用同一份骨架，且默认输出不因模板化而改变 ---
def test_build_notes_reports_the_template_version():
    build = load_script("build_notes")
    _sections, version = build._template_sections("standard")
    assert version != "builtin-fallback"


def test_build_notes_keeps_the_original_default_headings():
    build = load_script("build_notes")
    metadata = {"title": "T", "canonical_url": "U", "uploader": "A", "duration_seconds": 125}
    markdown = build.build_notes(metadata, [{"start": 0, "end": 65, "text": "x"}])
    for heading in ("## 基本信息", "## 一句话总结", "## 3 分钟速读", "## 详细笔记", "## 核心概念", "## 复习题", "## 待确认"):
        assert heading in markdown
    assert "### 00:00-01:05" in markdown


def test_douyin_note_uses_the_template_headings():
    douyin = load_script("douyin_extract")
    result = {
        "metadata": {"title": "T", "webpage_url": "U", "channel": "C", "duration": 10},
        "transcription_engine": "faster-whisper",
        "model_size": "small",
    }
    markdown = douyin.to_markdown(result)
    assert "## 一句话总结" in markdown
    assert "## 核心要点" in markdown
    assert "## 详细笔记" in markdown


def test_douyin_note_falls_back_when_template_is_missing(monkeypatch):
    douyin = load_script("douyin_extract")
    monkeypatch.setattr(douyin, "load_template", lambda _name: None)
    result = {
        "metadata": {"title": "T", "webpage_url": "U", "channel": "C", "duration": 10},
        "transcription_engine": "faster-whisper",
        "model_size": "small",
    }
    markdown = douyin.to_markdown(result)  # A degraded template must still produce a note.
    assert "## 一句话总结" in markdown
    assert "## 详细笔记" in markdown
