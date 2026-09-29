"""
无音轨（图文/纯图片作品）分流的离线测试（对应 docs/DECISIONS.md D28）。

为什么单独成档：抖音图文作品、纯音乐卡片的"取流成功 + 转写为空"最容易被误判成 ASR 故障，
于是调用方会去重试或换下载方式——两者都毫无意义。这里既要证明**检测是真的**（用 ffmpeg 现场生成
有/无音轨的真实文件，不打桩），也要证明退出码把"该改走图片 OCR"这件事说清楚了。
运行示例：python -m pytest cli_anything/video_learning/tests/test_no_audio.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill。
import json  # 解析 CLI 的 JSON 输出。
import shutil  # 在缺少 ffmpeg 时跳过需要真实媒体的用例。
import subprocess  # 现场生成有/无音轨的测试媒体。
import sys  # 把 scripts 目录加入模块搜索路径。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

import pytest  # 提供 monkeypatch、tmp_path 与断言辅助。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import media_tools  # noqa: E402 被测的音轨检测。
from cli_anything.video_learning.utils import exit_codes  # noqa: E402 退出码契约。

FFMPEG = media_tools.find_ffmpeg()
requires_ffmpeg = pytest.mark.skipif(not FFMPEG or not shutil.which(FFMPEG),
                                     reason="需要 ffmpeg 才能现场生成真实媒体")


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"
    module_name = f"video_learning_test_{name}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 用 ffmpeg 现场生成一段真实媒体（有音轨或无音轨）---
def make_video(path: Path, *, with_audio: bool) -> Path:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


# ============================ 检测本身（真实文件） ============================

# --- 真实的无音轨视频必须被识别为没有音轨 ---
@requires_ffmpeg
def test_real_silent_video_is_detected_as_having_no_audio(tmp_path):
    silent = make_video(tmp_path / "silent.mp4", with_audio=False)

    assert media_tools.has_audio_stream(silent) is False
    assert "Audio:" not in media_tools.ffmpeg_input_report(silent)


# --- 真实带音轨的视频必须被识别为有音轨（避免"一律返回 False"的假实现）---
@requires_ffmpeg
def test_real_video_with_audio_is_detected(tmp_path):
    with_audio = make_video(tmp_path / "with_audio.mp4", with_audio=True)

    assert media_tools.has_audio_stream(with_audio) is True


# --- 探测失败要按"没有音轨"处理，由上层给明确错误，而不是抛未捕获异常 ---
def test_detection_failure_is_treated_as_no_audio():
    assert media_tools.has_audio_stream("/nonexistent/path/definitely-missing.mp4") is False


# ============================ 退出码契约 ============================

# --- 无音轨异常必须映射到 27，而不是被当成转写失败 ---
def test_no_audio_error_maps_to_its_own_exit_code():
    error = exit_codes.NoAudioTrackError("图文作品没有音轨")

    assert exit_codes.classify_failure(error) == exit_codes.EXIT_NO_AUDIO_TRACK
    assert exit_codes.EXIT_NO_AUDIO_TRACK != exit_codes.EXIT_TRANSCRIPTION_FAILED
    assert exit_codes.classify_failure(
        error, no_audio_error_types=(exit_codes.NoAudioTrackError,)) == exit_codes.EXIT_NO_AUDIO_TRACK


# --- 所有退出码互不重复（新增 27 后仍要成立）---
def test_exit_codes_remain_distinct():
    codes = [exit_codes.EXIT_SUCCESS, exit_codes.EXIT_GENERIC_FAILURE, exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE,
             exit_codes.EXIT_COOKIE_PERMISSION_REQUIRED, exit_codes.EXIT_NETWORK_TIMEOUT,
             exit_codes.EXIT_RATIO_UNAVAILABLE, exit_codes.EXIT_TRANSCRIPTION_FAILED,
             exit_codes.EXIT_SOURCES_DISAGREE, exit_codes.EXIT_PLATFORM_VERIFICATION_REQUIRED,
             exit_codes.EXIT_NO_AUDIO_TRACK]

    assert len(codes) == len(set(codes))


# ============================ 入口行为 ============================

# --- 抖音抽取音频：无音轨必须在抽音频前就明确失败 ---
@requires_ffmpeg
def test_douyin_extract_audio_raises_no_audio_error(tmp_path):
    extract = load_script("douyin_extract")
    silent = make_video(tmp_path / "silent.mp4", with_audio=False)

    with pytest.raises(exit_codes.NoAudioTrackError) as excinfo:
        extract.extract_audio(str(silent), str(tmp_path / "out.wav"))

    assert "图片 OCR" in str(excinfo.value)          # 报错必须给出下一步，而不是只说"失败"


# --- 抖音抽取音频：有音轨时必须真的产出 16kHz 单声道 WAV ---
@requires_ffmpeg
def test_douyin_extract_audio_produces_16k_wav(tmp_path):
    extract = load_script("douyin_extract")
    with_audio = make_video(tmp_path / "with_audio.mp4", with_audio=True)
    target = tmp_path / "out.wav"

    extract.extract_audio(str(with_audio), str(target))

    assert target.exists() and target.stat().st_size > 1000
    report = media_tools.ffmpeg_input_report(target)
    assert "16000 Hz" in report and "mono" in report


# --- 通用转写入口：无音轨文件返回 27 并输出可解析的 JSON 错误 ---
@requires_ffmpeg
def test_transcribe_audio_cli_returns_no_audio_exit_code(monkeypatch, tmp_path, capsys):
    cli = load_script("transcribe_audio_cli")
    silent = make_video(tmp_path / "silent.mp4", with_audio=False)
    monkeypatch.setattr(sys, "argv", ["transcribe_audio_cli.py", "--audio", str(silent)])

    code = cli.main()

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == exit_codes.EXIT_NO_AUDIO_TRACK
    assert "no audio track" in payload["error"]
