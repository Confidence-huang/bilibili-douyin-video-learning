"""
Bilibili video transcription fallback pipeline.
Used when fetch_bilibili.py --transcribe fails (no API audio URL).

Pipeline: you-get download → ffmpeg audio extract → faster-whisper transcription

Requires: you-get, ffmpeg (in PATH), faster-whisper/CTranslate2
"""

import os
import sys
import json
import tempfile
import subprocess
from pathlib import Path

from speech_to_text import transcribe_audio_file                                  # 统一使用 faster-whisper 优先的本机 ASR 入口
from media_tools import find_ffmpeg                                              # 所有媒体流程共用跨平台 FFmpeg 入口。

def _find_ffmpeg():
    """Keep the historical private name while using the shared resolver."""
    return find_ffmpeg()


def download_video(url_or_bvid: str, output_dir: str) -> str:
    """
    Download Bilibili video using you-get.
    Returns path to the downloaded .mp4 file.
    """
    url = url_or_bvid
    if not url.startswith("http"):
        url = f"https://www.bilibili.com/video/{url}"

    # This legacy fallback stays anonymous. Authenticated access belongs to the main CLI's explicit permission flow.
    cmd = [
        sys.executable, "-m", "you_get",
        "--debug",
        "-o", output_dir,
    ]
    cmd.append(url)

    print(f"[fallback] Downloading video: {url}")
    print("[fallback] Cookies: no (anonymous-only legacy fallback)")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    stdout = (result.stdout or "") + (result.stderr or "")

    # Find the output file from you-get output
    import re
    # Pattern: "Merged into <filename>.mp4"
    merge_match = re.search(r"Merged into (.+\.mp4)", stdout)
    if merge_match:
        mp4_path = merge_match.group(1)
        if not os.path.isabs(mp4_path):
            mp4_path = os.path.join(output_dir, os.path.basename(mp4_path))
        if os.path.exists(mp4_path):
            print(f"[fallback] Video downloaded: {mp4_path}")
            return mp4_path

    # Pattern: downloading to output_dir
    mp4_files = list(Path(output_dir).glob("*.mp4"))
    if mp4_files:
        print(f"[fallback] Found video: {mp4_files[0]}")
        return str(mp4_files[0])

    raise RuntimeError(
        f"Video download failed. you-get output:\n{stdout[-1000:]}"
    )


def extract_audio(video_path: str, wav_path: str) -> str:
    """
    Extract mono 16kHz WAV from video using ffmpeg.
    Returns wav path.
    """
    ffmpeg = _find_ffmpeg()
    cmd = [
        ffmpeg,
        "-i", video_path,
        "-vn",                      # no video
        "-acodec", "pcm_s16le",     # 16-bit PCM
        "-ar", "16000",             # 16kHz sample rate
        "-ac", "1",                 # mono
        "-y",                       # overwrite
        wav_path,
    ]
    print(f"[fallback] Extracting audio: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not os.path.exists(wav_path):
        raise RuntimeError(
            f"Audio extraction failed: {result.stderr[-500:]}"
        )
    print(f"[fallback] Audio extracted: {wav_path} ({os.path.getsize(wav_path)} bytes)")
    return wav_path


def transcribe(wav_path: str, model_size: str = "small") -> tuple[list[dict], dict]:
    """
    Transcribe WAV audio using the shared faster-whisper-first ASR path.
    Returns list of segment dicts with 'start' and 'text' keys.
    """
    asr_result = transcribe_audio_file(                                           # 首选 faster-whisper + CTranslate2 + cuda/float16
        wav_path,
        model_size=model_size,
        language="zh",
        log_prefix="fallback-asr",
    )
    segments = [                                                                  # 保持 fallback Markdown 使用的旧字段
        {"start": round(seg["from"], 1), "text": seg["content"]}                  # 旧格式只需要开始时间和文本
        for seg in asr_result["segments"]                                         # 共享模块输出 from/to/content
    ]
    # Validate: transcribed duration should cover >80% of audio
    audio_dur = os.path.getsize(wav_path) / 32000  # 16kHz*16bit*1ch
    transcribed_dur = segments[-1]["start"] if segments else 0
    coverage = transcribed_dur / audio_dur if audio_dur > 0 else 0
    print(f"[fallback] Transcription complete with {asr_result['engine']}: {len(segments)} segments, "
          f"audio={audio_dur:.0f}s, transcribed={transcribed_dur:.0f}s, "
          f"coverage={coverage:.0%}")
    if coverage < 0.8:
        print(f"[fallback] WARNING: Only {coverage:.0%} of audio transcribed! "
              f"WAV may be truncated — check ffmpeg -c:a pcm_s16le")
    return segments, asr_result


def transcribe_bilibili(
    bvid: str,
    model_size: str = "small",
    keep_temp: bool = False,
    temp_dir: str = None,
) -> dict:
    """
    Main entry point: download + extract + transcribe a Bilibili video.

    Args:
        bvid: Bilibili BV ID (e.g., 'BV1frEr6cEZq')
        model_size: ASR model size ('tiny'|'base'|'small'|'medium'|'large')
        keep_temp: keep downloaded video and audio files
        temp_dir: custom temp directory (default: system temp)

    Returns:
        dict with keys: bvid, model_size, segments, segment_count, temp_dir
    """
    if temp_dir is None:
        temp_dir = os.path.join(tempfile.gettempdir(), "opencode", "bilibili", bvid)

    os.makedirs(temp_dir, exist_ok=True)

    wav_path = os.path.join(temp_dir, f"{bvid}.wav")
    video_path = None

    try:
        video_path = download_video(bvid, temp_dir)
    except Exception as e:
        # Try finding existing mp4
        existing = list(Path(temp_dir).glob("*.mp4"))
        if existing:
            video_path = str(existing[0])
            print(f"[fallback] Using cached video: {video_path}")
        else:
            raise RuntimeError(f"Download failed and no cached video: {e}")

    extract_audio(video_path, wav_path)
    segments, asr_result = transcribe(wav_path, model_size)

    if not keep_temp:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)

    return {
        "bvid": bvid,
        "model_size": model_size,
        "engine": asr_result.get("engine"),
        "device": asr_result.get("device"),
        "compute_type": asr_result.get("compute_type"),
        "asr_diagnostics": asr_result.get("diagnostics", []),
        "segments": segments,
        "segment_count": len(segments),
        "temp_dir": temp_dir,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python transcribe_fallback.py <bvid> [model_size]")
        print("Example: python transcribe_fallback.py BV1frEr6cEZq small")
        sys.exit(1)

    bvid = sys.argv[1]
    model_size = sys.argv[2] if len(sys.argv) > 2 else "small"

    result = transcribe_bilibili(bvid, model_size, keep_temp=True)

    print("\n" + "=" * 50)
    print(f"BVID: {result['bvid']}")
    print(f"Model: {result['model_size']}")
    print(f"Segments: {result['segment_count']}")
    print("=" * 50)
    for seg in result["segments"]:
        print(f"{seg['start']:6.1f}s | {seg['text']}")

    # Output JSON for downstream processing
    json_path = os.path.join(result["temp_dir"], f"{bvid}.transcript.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[JSON saved: {json_path}]")
