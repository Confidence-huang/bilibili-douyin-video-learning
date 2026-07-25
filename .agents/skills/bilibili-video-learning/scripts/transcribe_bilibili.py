#!/usr/bin/env python3
"""
Download Bilibili video audio and transcribe via faster-whisper.
Fallback when video has no subtitles available.
"""
import json
import os
import re
import sys
import subprocess
import tempfile
import shutil
import argparse

from speech_to_text import transcribe_audio_file                                  # 统一使用 faster-whisper 优先的本机 ASR 入口
from runtime_output import log                                                   # 进度只写 stderr，保持 --json stdout 纯净。


def find_ffmpeg():
    """Locate ffmpeg executable."""
    # Check common locations
    candidates = [
        "ffmpeg",
        "ffmpeg.exe",
        shutil.which("ffmpeg"),
        shutil.which("ffmpeg.exe"),
    ]
    # Also check SteelSeries location
    import glob
    for path in glob.glob("C:\\Program Files\\SteelSeries\\GG\\apps\\moments\\*\\ffmpeg.exe"):
        candidates.append(path)
    for c in candidates:
        if c and os.path.exists(c):
            return c
        if c:
            try:
                result = subprocess.run([c, "-version"], capture_output=True, timeout=5)
                if result.returncode == 0:
                    return c
            except Exception:
                continue
    return "ffmpeg"


def download_audio(bvid, output_dir, cookies=None):
    """Download Bilibili video audio using yt-dlp."""
    url = f"https://www.bilibili.com/video/{bvid}"
    output_template = os.path.join(output_dir, f"{bvid}.%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--extract-audio",
        "--audio-format", "wav",
        "--audio-quality", "0",
        "--output", output_template,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--retries", "3",
        "--fragment-retries", "3",
        url
    ]

    if cookies:
        # cookies can be a file path or a browser name (e.g. 'chrome', 'edge')
        if os.path.exists(cookies):
            cmd.extend(["--cookies", cookies])
        elif cookies in ("chrome", "edge", "firefox", "brave", "opera"):
            cmd.extend(["--cookies-from-browser", cookies])

    log(f"[download] Fetching audio for {bvid}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env={
        **os.environ,
        "PATH": os.environ["PATH"]
    })

    if result.returncode != 0:
        return None, result.stderr

    # Find the output file
    for f in os.listdir(output_dir):
        if f.startswith(bvid) and f.endswith(".wav"):
            return os.path.join(output_dir, f), None

    return None, "No audio file found after download"


def transcribe_audio(audio_path, model_size="small", language="zh", device=None):
    """Transcribe audio through the shared faster-whisper-first ASR path."""
    asr_result = transcribe_audio_file(
        audio_path,
        model_size=model_size,
        language=language,
        device=device,
        log_prefix="bilibili-asr",
    )
    return asr_result


def bilibili_transcribe(
    bvid,
    output_dir=None,
    model_size="small",
    cookies=None,
    device=None,
    keep_audio=False,
):
    """
    Full pipeline: download audio → transcribe → return segments.

    Args:
        bvid: Bilibili BV ID
        output_dir: Directory for temp files (auto-created if None)
        model_size: 'tiny', 'base', 'small', 'medium', 'large'
        cookies: Browser name or cookie file path
        device: 'cpu', 'cuda', or None (auto-detect)

    Returns:
        dict with {segments: [...], bvid: ..., status: 'ok'|'error', error: ...}
    """
    bvid = bvid.strip()
    # Extract BV ID from URL if given
    match = re.search(r'BV[a-zA-Z0-9]{10}', bvid)
    if match:
        bvid = match.group(0)

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="bilibili_whisper_")

    result = {"bvid": bvid, "status": "error", "segments": [], "error": None}

    # Step 1: Download audio
    log(f"\n{'='*50}")
    log(f"[pipeline] Step 1/2: Downloading audio for {bvid}")
    audio_path, error = download_audio(bvid, output_dir, cookies)
    if error:
        result["error"] = f"Audio download failed: {error}"
        log(f"[pipeline] ERROR: {result['error']}")
        return result

    log(f"[pipeline] Audio saved to: {audio_path}")

    # Step 2: Transcribe
    log(f"[pipeline] Step 2/2: Transcribing with faster-whisper first ({model_size})")
    try:
        asr_result = transcribe_audio(audio_path, model_size=model_size, device=device)
        result["segments"] = asr_result["segments"]
        result["status"] = "ok"
        result["engine"] = asr_result.get("engine")
        result["model_size"] = model_size
        result["device"] = asr_result.get("device")
        result["compute_type"] = asr_result.get("compute_type")
        result["asr_diagnostics"] = asr_result.get("diagnostics", [])
        whisper_model = {"tiny": 39, "base": 74, "small": 244, "medium": 769, "large": 1550}
        result["model_mb"] = whisper_model.get(model_size, 0)
        log(
            f"[pipeline] Transcribed {len(result['segments'])} segments "
            f"with {result['engine']} on {result.get('device')}"
        )
    except Exception as e:
        result["error"] = f"ASR transcription failed: {str(e)}"
        log(f"[pipeline] ERROR: {result['error']}")
        return result

    if not keep_audio:                                           # CLI 选项现在真实控制清理行为。
        try:
            os.remove(audio_path)
        except OSError:
            pass                                                 # 转写结果已完成，清理失败只保留临时文件。
    else:
        result["audio_path"] = audio_path                        # 明确保留位置，方便授权的本地复核。

    return result


# --- CLI ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bilibili video audio download + faster-whisper transcription")
    parser.add_argument("bvid", help="Bilibili BV ID or video URL")
    parser.add_argument("--output-dir", "-o", help="Output directory for temp files")
    parser.add_argument("--model", "-m", default="small",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="ASR model size (default: small)")
    parser.add_argument("--cookies", "-c", help="Browser name (chrome/edge) or cookie file path")
    parser.add_argument("--json", "-j", action="store_true", help="Output raw JSON")
    parser.add_argument("--keep-audio", "-k", action="store_true", help="Keep downloaded audio file")
    args = parser.parse_args()

    result = bilibili_transcribe(
        args.bvid,
        output_dir=args.output_dir,
        model_size=args.model,
        cookies=args.cookies,
        keep_audio=args.keep_audio,
    )

    if args.json:
        # Remove segments from display if too many
        output = dict(result)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if result["status"] == "ok":
            print(f"\n=== Transcription Complete ===")
            print(f"Model: {result.get('model_size', '?')} ({result.get('model_mb', '?')} MB)")
            print(f"Engine: {result.get('engine', '?')} on {result.get('device', '?')} / {result.get('compute_type', '')}")
            print(f"Segments: {len(result['segments'])}")
            print(f"\nFirst 10 segments:")
            for seg in result["segments"][:10]:
                t = f"{int(seg['from'])//60:02d}:{int(seg['from'])%60:02d}"
                print(f"  [{t}] {seg['content']}")
            if len(result["segments"]) > 10:
                print(f"  ... ({len(result['segments'])-10} more)")
        else:
            print(f"\nERROR: {result.get('error', 'Unknown error')}")
