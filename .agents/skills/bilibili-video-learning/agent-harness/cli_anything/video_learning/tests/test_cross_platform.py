"""Cross-platform runtime contracts for the public Video Learning Skill."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime


SKILL_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(name: str):
    """Load the exact backend file shipped in this checkout."""
    script_path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"cross_platform_{name}", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_for(skill_root: Path) -> SkillRuntime:
    """Create only the resolver state; no live installation is required."""
    runtime = object.__new__(SkillRuntime)
    runtime.skill_root = skill_root
    return runtime


@pytest.mark.parametrize(
    "relative_python",
    [
        Path(".venv") / "bin" / "python",
        Path(".venv") / "Scripts" / "python.exe",
        Path(".venv-gpu") / "bin" / "python",
        Path(".venv-gpu") / "Scripts" / "python.exe",
    ],
)
def test_runtime_python_supports_linux_and_windows_layouts(monkeypatch, tmp_path, relative_python):
    monkeypatch.delenv("BILIBILI_VIDEO_LEARNING_PYTHON", raising=False)
    python_path = tmp_path / relative_python
    python_path.parent.mkdir(parents=True)
    python_path.touch()

    assert runtime_for(tmp_path).find_runtime_python() == python_path.resolve()


def test_runtime_python_supports_external_linux_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("BILIBILI_VIDEO_LEARNING_PYTHON", raising=False)
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    python_path = data_home / "bilibili-video-learning" / "runtime" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.touch()

    assert runtime_for(tmp_path / "skill").find_runtime_python() == python_path.absolute()


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="Linux installer contract requires a POSIX host and bash",
)
def test_linux_installer_rejects_runtime_inside_skill(tmp_path):
    repository_root = SKILL_ROOT.parents[2]
    installer = repository_root / "install_linux.sh"
    if not installer.is_file():
        pytest.skip("Repository-level installer is not included in an installed Skill copy")
    destination = tmp_path / "bilibili-video-learning"
    completed = subprocess.run(
        [
            "bash",
            str(installer),
            "--destination-root",
            str(destination),
            "--runtime-root",
            str(destination / ".venv"),
            "--skip-runtime",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "Runtime must be outside the Skill destination" in completed.stderr
    assert not destination.exists()


def test_media_tools_prefers_path_ffmpeg(monkeypatch, tmp_path):
    media_tools = load_script("media_tools")
    ffmpeg_path = tmp_path / "ffmpeg"
    ffmpeg_path.touch()
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: str(ffmpeg_path))

    assert media_tools.find_ffmpeg() == str(ffmpeg_path)


def test_media_tools_falls_back_to_imageio_ffmpeg(monkeypatch, tmp_path):
    media_tools = load_script("media_tools")
    ffmpeg_path = tmp_path / "imageio_ffmpeg"
    ffmpeg_path.touch()
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        media_tools,
        "_imageio_ffmpeg_executable",
        lambda: str(ffmpeg_path),
    )

    assert media_tools.find_ffmpeg() == str(ffmpeg_path)


def test_bilibili_metadata_uses_skill_python_module(monkeypatch):
    fetch = load_script("fetch_bilibili")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "BV1xx411c7mD", "title": "fixture"}),
            stderr="",
        )

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch._run_ytdlp("BV1xx411c7mD")

    assert commands[0][:3] == [sys.executable, "-m", "yt_dlp"]


def test_douyin_uses_skill_python_module(monkeypatch):
    douyin = load_script("douyin_extract")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(douyin.subprocess, "run", fake_run)

    assert douyin._find_yt_dlp() == [sys.executable, "-m", "yt_dlp"]
    assert commands == [[sys.executable, "-m", "yt_dlp", "--version"]]


def test_default_obsidian_vault_is_host_portable(monkeypatch):
    fetch = load_script("fetch_bilibili")
    monkeypatch.delenv("BILIBILI_OBSIDIAN_VAULT", raising=False)

    assert Path(fetch.default_obsidian_vault()) == Path.home() / "Notes"
