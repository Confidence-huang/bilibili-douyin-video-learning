"""
安装态 Video Learning CLI 的真实文件与 subprocess 测试。
测试只使用本地 fixture 和真实本机工具探测；平台联网、媒体下载和 GPU ASR 不在默认套件中隐式发生。
"""
from __future__ import annotations  # 保持测试类型标注兼容 Python 3.10+。

import json  # 验证每个 --json 命令只返回一个可解析文档。
import os  # 读取强制安装态开关并构造 subprocess 环境。
import shutil  # 从 PATH 解析真正安装的 console script。
import subprocess  # 以用户/Agent 的方式执行 CLI。
import sys  # 开发态回退到当前 Python 模块。
from pathlib import Path  # 构造真实临时字幕和结果文件。

import pytest  # 参数化验证所有非法分 P 都从安装态入口失败。


# --- 解析安装命令，发布验收可禁止源码回退 ---
def _resolve_cli(name: str) -> list[str]:
    force_installed = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    installed_path = shutil.which(name)  # PATH 命中证明 console_scripts 安装完成。
    if installed_path:
        print(f"[_resolve_cli] Using installed command: {installed_path}")
        return [installed_path]
    if force_installed:  # 发布验收不允许悄悄使用源码模块。
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m cli_anything.video_learning")
    return [sys.executable, "-m", "cli_anything.video_learning"]


class TestCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-video-learning")  # 整个类复用同一真实命令入口。

    def _run(self, arguments: list[str], check: bool = True):
        return subprocess.run(  # 不设置 cwd，验证安装命令能从任意位置工作。
            self.CLI_BASE + arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def test_help(self):
        result = self._run(["--help"])
        assert "source" in result.stdout
        assert "doctor" in result.stdout

    def test_version(self):
        from cli_anything.video_learning import __version__  # 断言跟随包内唯一版本源，避免发版忘记改测试。

        result = self._run(["--version"])
        assert __version__ in result.stdout

    def test_normalize_bilibili_json(self):
        result = self._run(["--json", "source", "normalize", "https://www.bilibili.com/video/BV1xx411c7mD?p=2"])
        payload = json.loads(result.stdout)
        assert payload["platform"] == "bilibili"
        assert payload["bvid"] == "BV1xx411c7mD"
        assert payload["page"] == 2

    @pytest.mark.parametrize("page_value", ["0", "abc", ""])
    def test_normalize_rejects_invalid_bilibili_page(self, page_value):
        result = self._run([
            "--json",
            "source",
            "normalize",
            f"https://www.bilibili.com/video/BV1xx411c7mD?p={page_value}",
        ], check=False)
        payload = json.loads(result.stdout)
        assert result.returncode != 0
        assert payload["ok"] is False
        assert "page" in payload["error"].lower()

    def test_normalize_douyin_json(self):
        result = self._run(["--json", "source", "normalize", "1234567890123456789", "--platform", "douyin"])
        payload = json.loads(result.stdout)
        assert payload["input_type"] == "bare_aweme_id"
        assert payload["aweme_id"] == "1234567890123456789"

    def test_convert_real_vtt_file(self, tmp_path):
        source_path = tmp_path / "fixture.vtt"
        output_path = tmp_path / "timeline.json"
        source_path.write_text("WEBVTT\n\n00:01.000 --> 00:02.500\n本地字幕\n", encoding="utf-8")

        result = self._run(["--json", "subtitle", "convert", str(source_path), "--output", str(output_path)])
        payload = json.loads(result.stdout)
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["segment_count"] == 1
        assert saved[0]["text"] == "本地字幕"

    def test_note_render_defaults_to_no_transcript(self, tmp_path):
        source_path = tmp_path / "result.json"
        output_path = tmp_path / "note.md"
        source_path.write_text(json.dumps({
            "bvid": "BV1xx411c7mD",
            "title": "fixture",
            "author": "tester",
            "duration": 10,
            "transcription_method": "api",
            "subtitles": [{"lan": "zh-CN", "content": [{"from": 0, "to": 1, "content": "不应默认复制"}]}],
        }, ensure_ascii=False), encoding="utf-8")

        result = self._run(["--json", "note", "render", str(source_path), "--output", str(output_path)])
        payload = json.loads(result.stdout)
        markdown = output_path.read_text(encoding="utf-8")
        assert Path(payload["output"]) == output_path
        assert "不应默认复制" not in markdown
        assert payload["source_identity"]["video_id"] == "BV1xx411c7mD"
        assert payload["source_identity"]["page"] == 1
        assert "<!-- video-learning-source:" in markdown
        assert not list(tmp_path.glob(".note-*.tmp"))                          # 原子发布后不残留半文件。

    def test_note_render_can_include_authorized_transcript(self, tmp_path):
        source_path = tmp_path / "result.json"
        output_path = tmp_path / "authorized.md"
        source_path.write_text(json.dumps({
            "bvid": "BV1xx411c7mD",
            "title": "fixture",
            "author": "tester",
            "duration": 10,
            "transcription_method": "api",
            "subtitles": [{"lan": "zh-CN", "content": [{"from": 0, "to": 1, "content": "已授权正文"}]}],
        }, ensure_ascii=False), encoding="utf-8")

        self._run(["--json", "note", "render", str(source_path), "--output", str(output_path), "--include-transcript"])
        assert "已授权正文" in output_path.read_text(encoding="utf-8")

    def test_doctor_uses_real_local_backends(self):
        if os.environ.get("VIDEO_LEARNING_SKIP_INSTALLED_RUNTIME_TESTS") == "1":
            pytest.skip("CI source suite intentionally omits the multi-GB installed GPU runtime")
        result = self._run(["--json", "doctor", "status"])
        payload = json.loads(result.stdout)
        assert Path(payload["skill_root"]).is_dir()
        assert Path(payload["runtime_python"]).is_file()
        assert payload["tools"]["yt-dlp"]["ok"] is True
        assert payload["tools"]["ffmpeg"]["ok"] is True
