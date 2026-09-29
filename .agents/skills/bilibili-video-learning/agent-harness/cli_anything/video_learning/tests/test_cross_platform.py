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
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-xdg-data"))
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


# --- ctranslate2 声称有 CUDA 但实际不可用时必须降级到 CPU ---
def test_asr_falls_back_to_cpu_when_cuda_is_unusable(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    attempts: list[tuple[str, str]] = []  # 记录每次真实尝试的设备与精度。

    class FakeModel:  # 只在 cuda/float16 上失败，模拟缺 libcublas.so.12 的机器。
        def __init__(self, model_size, device, compute_type):
            attempts.append((device, compute_type))

        def transcribe(self, audio, **kwargs):
            if attempts[-1][0] == "cuda":  # 真实机器上错误发生在第一次 encode，而不是模型构造时。
                def failing_reading():
                    raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")
                    yield  # pragma: no cover 生成器体只为把异常推迟到迭代时抛出。
                return failing_reading(), SimpleNamespace(language="zh", language_probability=1.0)
            reading = [SimpleNamespace(text="正文", start=0.0, end=1.0)]
            return iter(reading), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cuda", "float16", 1))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = speech_to_text.transcribe_audio_file(audio, settings=speech_to_text.TranscriptionSettings(coverage_check=False))

    assert attempts == [("cuda", "float16"), ("cpu", "int8")]  # 必须先试 CUDA，失败后落到 CPU。
    assert result["device"] == "cpu"  # 结果不能假装仍在用 GPU。
    assert result["compute_type"] == "int8"
    assert "libcublas" in (result["device_fallback"] or "")  # 降级原因必须可见，便于解释速度差异。
    assert result["diagnostics"][0]["device_fallback"]  # 诊断里同样带上原因。
    assert result["segments"][0]["content"] == "正文"  # 降级后仍拿到正文，而不是整体失败。


# --- 模型构造期就失败也要能降级（CUDA 缺失的另一种表现） ---
def test_asr_falls_back_when_model_construction_fails(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    attempts: list[tuple[str, str]] = []

    class FakeModel:
        def __init__(self, model_size, device, compute_type):
            attempts.append((device, compute_type))
            if device == "cuda":
                raise RuntimeError("Could not load library libcudnn_ops.so.9")

        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(text="正文", start=0.0, end=1.0)]), SimpleNamespace()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cuda", "float16", 1))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = speech_to_text.transcribe_audio_file(audio, settings=speech_to_text.TranscriptionSettings(coverage_check=False))

    assert attempts == [("cuda", "float16"), ("cpu", "int8")]  # 构造期失败同样降级一次。
    assert result["device"] == "cpu"
    assert "libcudnn" in (result["device_fallback"] or "")  # 根因保留，不会被兜底错误顶掉。


# --- 用户显式要求 CUDA 时不得静默降级 ---
def test_asr_does_not_silently_downgrade_an_explicit_cuda_request(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")

    class FakeModel:
        def __init__(self, model_size, device, compute_type):
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cuda", "float16", 1))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    with pytest.raises(RuntimeError, match="libcublas"):  # 显式请求的设备失败就是失败，不做意外降级。
        speech_to_text.transcribe_audio_file(audio, device="cuda", settings=speech_to_text.TranscriptionSettings())
