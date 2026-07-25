#!/usr/bin/env python3
"""
Douyin video extraction pipeline.

The default path first tries Douyin's public SSR share page, which uses an
anonymous ttwid, server-rendered public metadata, a ranged GET probe, and the
public play/playwm endpoint. If that public path fails in auto mode, the script
falls back to the existing yt-dlp download path, then continues through ffmpeg
audio extraction and faster-whisper transcription.

Requires: requests, yt-dlp fallback, ffmpeg (in PATH), faster-whisper/CTranslate2

Usage:
    python douyin_extract.py <douyin_url_or_id_or_share_text> --download-method auto
    python douyin_extract.py "https://v.douyin.com/xxxx/" --ratio 720p --watermark -o ./notes
"""

import argparse
import hashlib
import os
import re
import sys
import json
import tempfile
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

import douyin_ssr
from speech_to_text import transcribe_audio_file                                  # 统一使用 faster-whisper 优先的本机 ASR 入口
from runtime_output import log, sanitize_diagnostics, sanitize_text             # 进度和结构化错误共用脱敏边界。
from file_output import write_json_atomically, write_text_atomically             # 缓存和最终 Markdown 只原子发布完整文件。


CACHE_SCHEMA_VERSION = 2                                                         # v2 缓存带参数身份和真实视频 ID 双重校验。


# --- Helpers ---

# --- 统一 Windows 重定向输出编码 ---
def configure_output_encoding() -> None:
    for output_stream in (sys.stdout, sys.stderr):                                # JSON、标题和诊断都可能含中文或 emoji
        if hasattr(output_stream, "reconfigure"):                                 # Python 3 的文本流允许运行时指定编码
            output_stream.reconfigure(encoding="utf-8", errors="replace")         # 避免 PowerShell/GBK 重定向在最后输出阶段失败


def _find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for loc in [
            "C:\\ProgramData\\chocolatey\\bin\\ffmpeg.exe",
            "C:\\ffmpeg\\bin\\ffmpeg.exe",
            os.path.expanduser("~\\scoop\\shims\\ffmpeg.exe"),
        ]:
            if os.path.exists(loc):
                ffmpeg = loc
                break
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install: choco install ffmpeg / winget install ffmpeg")
    return ffmpeg


def _find_yt_dlp():
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        try:
            subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, timeout=5)
            return [sys.executable, "-m", "yt_dlp"]
        except Exception:
            pass
        raise RuntimeError("yt-dlp not found. Install: pip install yt-dlp")
    return [ytdlp]


def _run(cmd, timeout=120, desc=""):
    log(f"[douyin] {desc}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
    return result


# --- Add one JSON-friendly diagnostic entry ---
def add_diagnostic(diagnostics: list, step: str, ok: bool, message: str, **data) -> None:
    diagnostics.append({
        "step": step,
        "ok": ok,
        "message": message,
        **data,
    })


# --- 从明确 URL 或裸 ID 提取可提前验证的视频编号 ---
def extract_explicit_video_id(source_text: str) -> str | None:
    source = source_text.strip()
    if source.isdigit() and 10 <= len(source) <= 25:              # 裸 aweme_id 可在联网前确认身份。
        return source
    match = re.search(r"douyin\.com/video/(\d{10,25})", source, flags=re.IGNORECASE)
    return match.group(1) if match else None                       # 短链需依赖缓存内部 ID 自洽校验。


# --- 生成会影响转写结果的完整缓存身份 ---
def build_cache_identity(
    source_text: str,
    model_size: str,
    language: str,
    download_method: str,
    ratio: str,
    watermark: bool,
) -> dict:
    return {
        "schema": CACHE_SCHEMA_VERSION,                         # 旧格式缓存自动失效，不猜测缺失字段。
        "platform": "douyin",
        "source": source_text.strip(),
        "expected_video_id": extract_explicit_video_id(source_text),
        "model_size": model_size,
        "language": language,
        "download_method": download_method,
        "ratio": ratio,
        "watermark": watermark,
    }


# --- 为完整缓存身份生成稳定目录键 ---
def build_cache_key(
    source_text: str,
    model_size: str,
    language: str,
    download_method: str,
    ratio: str,
    watermark: bool,
) -> str:
    identity = build_cache_identity(source_text, model_size, language, download_method, ratio, watermark)
    cache_input = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(cache_input.encode("utf-8")).hexdigest()[:24]


# --- 从结果读取平台确认过的真实视频编号 ---
def result_video_id(result: dict) -> str:
    metadata = result.get("metadata") or {}
    return str(result.get("video_id") or result.get("aweme_id") or metadata.get("video_id") or "")


# --- 只读取身份与结果都严格自洽的缓存 ---
def load_cached_result(cache_path: str, expected_identity: dict) -> dict | None:
    try:
        envelope = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None                                               # 半文件、旧文本或不存在都视为安全 miss。
    cached_identity = envelope.get("cache_identity") if isinstance(envelope, dict) else None
    cached_result = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(cached_identity, dict) or not isinstance(cached_result, dict):
        return None                                               # v1 裸结果没有可验证身份，禁止复用。
    if any(cached_identity.get(key) != value for key, value in expected_identity.items()):
        return None                                               # 任一模型、语言、画质或来源参数变化即 miss。
    cached_video_id = str(cached_identity.get("video_id") or "")
    if not cached_video_id or cached_video_id != result_video_id(cached_result):
        return None                                               # 身份标记与真实结果编号必须完全一致。
    expected_video_id = str(expected_identity.get("expected_video_id") or "")
    if expected_video_id and expected_video_id != cached_video_id:
        return None                                               # 裸 ID/长链还必须匹配用户明确请求的视频。
    return cached_result


# --- 原子保存带身份封套的轻量 JSON 缓存 ---
def save_cached_result(cache_path: str, cache_identity: dict, result: dict) -> bool:
    video_id = result_video_id(result)
    if not video_id:
        return False                                              # 无真实视频 ID 的结果不能成为可复用缓存。
    envelope_identity = {**cache_identity, "video_id": video_id}
    write_json_atomically(cache_path, {"cache_identity": envelope_identity, "result": result})
    return True


# --- Step 1: Fetch metadata via yt-dlp ---

def _add_ytdlp_network_options(
    cmd: list,
    proxy: str = None,
    impersonate: str = None,
    socket_timeout: int = 60,
) -> list:
    """Add network options after the yt-dlp executable/module prefix."""
    insert_at = 1
    if len(cmd) >= 3 and cmd[1:3] == ["-m", "yt_dlp"]:
        insert_at = 3
    extra = []
    if proxy:
        extra += ["--proxy", proxy]
    if impersonate:
        extra += ["--impersonate", impersonate]
    if socket_timeout:
        extra += ["--socket-timeout", str(socket_timeout)]
    cmd[insert_at:insert_at] = extra
    return cmd


def fetch_metadata(
    url: str,
    proxy: str = None,
    impersonate: str = None,
    socket_timeout: int = 60,
) -> dict:
    """
    Use yt-dlp --dump-json to get metadata without downloading.
    Returns dict with: id, title, description, uploader, channel, duration,
                        upload_date, like_count, etc.
    """
    ytdlp = _find_yt_dlp()
    cmd = ytdlp + [
        "--dump-json", "--no-download",
        "--no-playlist", "--ignore-errors", "--no-warnings",
    ]
    _add_ytdlp_network_options(cmd, proxy=proxy, impersonate=impersonate, socket_timeout=socket_timeout)
    cmd.append(url)
    result = _run(cmd, timeout=60, desc="Fetching Douyin metadata")
    # yt-dlp outputs JSON to stdout; stderr may have warnings
    stdout = (result.stdout or "").strip()
    if not stdout:
        raise RuntimeError(f"yt-dlp metadata fetch failed:\nSTDERR: {result.stderr[:500]}")
    data = json.loads(stdout.splitlines()[-1])
    return {
        "video_id": data.get("id", ""),
        "title": data.get("title", ""),
        "fulltitle": data.get("fulltitle", data.get("title", "")),
        "description": data.get("description", ""),
        "uploader": data.get("uploader", data.get("channel", "")),
        "channel": data.get("channel", ""),
        "channel_url": data.get("channel_url", ""),
        "duration": data.get("duration", 0),
        "duration_string": data.get("duration_string", ""),
        "upload_date": data.get("upload_date", ""),
        "webpage_url": data.get("webpage_url", data.get("original_url", url)),
        "view_count": data.get("view_count", 0),
        "like_count": data.get("like_count", 0),
        "comment_count": data.get("comment_count", 0),
        "repost_count": data.get("repost_count", 0),
        "save_count": data.get("save_count", 0),
        "thumbnail": data.get("thumbnail", ""),
        "extractor": data.get("extractor", "Douyin"),
        "_raw": data,
    }


# --- Step 2: Download video via yt-dlp ---

def download_video_ytdlp(
    url: str,
    output_dir: str,
    proxy: str = None,
    impersonate: str = None,
    socket_timeout: int = 60,
) -> str:
    ytdlp = _find_yt_dlp()
    outpath = os.path.join(output_dir, "douyin_video.%(ext)s")
    cmd = ytdlp + [
        "-f", "b",
        "-o", outpath,
        "--no-playlist", "--ignore-errors",
        "--retries", "3",
        "--fragment-retries", "3",
    ]
    _add_ytdlp_network_options(cmd, proxy=proxy, impersonate=impersonate, socket_timeout=socket_timeout)
    cmd.append(url)
    result = _run(cmd, timeout=180, desc="Downloading Douyin video")
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed:\nSTDERR: {result.stderr[:500]}")

    mp4_files = list(Path(output_dir).glob("*.mp4"))
    if not mp4_files:
        # Try flv
        flv_files = list(Path(output_dir).glob("*.flv"))
        if flv_files:
            return str(flv_files[0])
        raise RuntimeError("Video download failed: no mp4/flv found")
    return str(mp4_files[0])


# --- Download through the public SSR share path ---
def download_video_with_ssr(
    url: str,
    work_dir: str,
    ratio: str = "1080p",
    watermark: bool = False,
) -> dict:
    video_path = os.path.join(work_dir, "douyin_video.mp4")
    ssr_result = douyin_ssr.download_public_video(
        url,
        video_path,
        ratio=ratio,
        watermark=watermark,
    )
    metadata = {
        "video_id": ssr_result.get("aweme_id") or ssr_result.get("video_id") or "douyin",
        "title": ssr_result.get("metadata", {}).get("title") or f"douyin_{ssr_result.get('aweme_id', '')}",
        "fulltitle": ssr_result.get("metadata", {}).get("title") or f"douyin_{ssr_result.get('aweme_id', '')}",
        "description": ssr_result.get("metadata", {}).get("description", ""),
        "uploader": "",
        "channel": "",
        "channel_url": "",
        "duration": 0,
        "duration_string": "",
        "upload_date": "",
        "webpage_url": ssr_result.get("canonical_url", url),
        "view_count": 0,
        "like_count": 0,
        "comment_count": 0,
        "repost_count": 0,
        "save_count": 0,
        "thumbnail": "",
        "extractor": "DouyinSSR",
    }
    return {
        "download_method": "ssr",
        "video_path": ssr_result["video_path"],
        "metadata": metadata,
        "canonical_url": ssr_result.get("canonical_url"),
        "aweme_id": ssr_result.get("aweme_id"),
        "video_id": ssr_result.get("video_id"),
        "requested_ratio": ssr_result.get("requested_ratio"),
        "ratio": ssr_result.get("ratio"),
        "diagnostics": ssr_result.get("diagnostics", []),
    }


# --- Download through the existing yt-dlp fallback path ---
def download_video_with_ytdlp(
    url: str,
    work_dir: str,
    proxy: str = None,
    impersonate: str = None,
    socket_timeout: int = 60,
) -> dict:
    diagnostics = []
    metadata = fetch_metadata(url, proxy=proxy, impersonate=impersonate, socket_timeout=socket_timeout)
    add_diagnostic(diagnostics, "ytdlp_metadata", True, "Fetched Douyin metadata through yt-dlp", video_id=metadata.get("video_id"), webpage_url=metadata.get("webpage_url"))

    video_path = download_video_ytdlp(
        url,
        work_dir,
        proxy=proxy,
        impersonate=impersonate,
        socket_timeout=socket_timeout,
    )
    add_diagnostic(diagnostics, "ytdlp_download", True, "Downloaded Douyin video through yt-dlp", video_path=video_path, file_size=os.path.getsize(video_path))

    return {
        "download_method": "ytdlp",
        "video_path": video_path,
        "metadata": metadata,
        "canonical_url": metadata.get("webpage_url", url),
        "aweme_id": metadata.get("video_id", ""),
        "video_id": metadata.get("video_id", ""),
        "diagnostics": diagnostics,
    }


# --- Choose SSR first or force a requested download method ---
def choose_downloaded_video(
    url: str,
    work_dir: str,
    download_method: str = "auto",
    ratio: str = "1080p",
    watermark: bool = False,
    proxy: str = None,
    impersonate: str = None,
    socket_timeout: int = 60,
) -> dict:
    diagnostics = []

    if download_method in ("auto", "ssr"):
        try:
            ssr_result = download_video_with_ssr(url, work_dir, ratio=ratio, watermark=watermark)
            ssr_result["diagnostics"] = diagnostics + ssr_result.get("diagnostics", [])
            return ssr_result
        except Exception as exc:
            diagnostics.extend(getattr(exc, "diagnostics", []))
            add_diagnostic(diagnostics, "ssr_pipeline", False, f"SSR public download failed: {exc}")
            if download_method == "ssr":
                raise RuntimeError(json.dumps({
                    "error": f"SSR public download failed before transcription: {exc}",
                    "download_method": download_method,
                    "diagnostics": diagnostics,
                }, ensure_ascii=False)) from exc

    if download_method in ("auto", "ytdlp"):
        try:
            ytdlp_result = download_video_with_ytdlp(
                url,
                work_dir,
                proxy=proxy,
                impersonate=impersonate,
                socket_timeout=socket_timeout,
            )
            ytdlp_result["diagnostics"] = diagnostics + ytdlp_result.get("diagnostics", [])
            return ytdlp_result
        except Exception as exc:
            add_diagnostic(diagnostics, "ytdlp_pipeline", False, f"yt-dlp fallback failed: {exc}")
            raise RuntimeError(json.dumps({
                "error": "Douyin download failed before transcription",
                "download_method": download_method,
                "diagnostics": diagnostics,
            }, ensure_ascii=False)) from exc

    raise ValueError("download_method must be one of: auto, ssr, ytdlp")


# --- Step 3: Extract audio ---

def extract_audio(video_path: str, wav_path: str) -> str:
    ffmpeg = _find_ffmpeg()
    cmd = [
        ffmpeg,
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        wav_path,
    ]
    result = _run(cmd, timeout=60, desc="Extracting audio")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed:\nSTDERR: {result.stderr[:500]}")
    if not os.path.exists(wav_path):
        raise RuntimeError("Audio extraction failed")
    log(f"[douyin] Audio extracted: {wav_path} ({os.path.getsize(wav_path)} bytes)")
    return wav_path


# --- Step 4: Transcribe ---

def transcribe(wav_path: str, model_size: str = "small", language: str = "zh") -> tuple[list, str, dict]:
    asr_result = transcribe_audio_file(                                           # 首选 faster-whisper + CTranslate2 + cuda/float16
        wav_path,
        model_size=model_size,
        language=language,
        log_prefix="douyin-asr",
    )
    segments = [                                                                  # 保持旧 Douyin Markdown 需要的字段名
        {"start": round(seg["from"], 1), "text": seg["content"]}                  # 旧格式只有开始时间和文本
        for seg in asr_result["segments"]                                         # 共享模块输出标准 from/to/content
    ]
    full_text = asr_result["text"].strip()                                        # 兼容旧输出里的 full_text
    log(
        f"[douyin] Transcription complete with {asr_result['engine']}: "
        f"{len(segments)} segments, {len(full_text)} chars"
    )
    return segments, full_text, asr_result


# --- Main entry ---

def extract_douyin(
    url: str,
    model_size: str = "small",
    language: str = "zh",
    keep_temp: bool = False,
    temp_dir: str = None,
    download_method: str = "auto",
    ratio: str = "1080p",
    watermark: bool = False,
    proxy: str = None,
    impersonate: str = None,
    socket_timeout: int = 60,
    use_cache: bool = True,
) -> dict:
    """
    Main pipeline: choose download method → extract audio → transcribe.

    Returns dict with: metadata, segments, full_text, segment_count, etc.
    """
    if temp_dir is None:
        temp_dir = os.path.join(tempfile.gettempdir(), "opencode", "douyin")
    os.makedirs(temp_dir, exist_ok=True)

    # Step 1: Use a stable non-identifying folder before the real video ID is known.
    cache_identity = build_cache_identity(url, model_size, language, download_method, ratio, watermark)
    cache_key = build_cache_key(url, model_size, language, download_method, ratio, watermark)
    work_dir = os.path.join(temp_dir, cache_key)                  # 分享文本不再直接出现在临时目录名中。
    os.makedirs(work_dir, exist_ok=True)
    cache_path = os.path.join(work_dir, "result.json")
    if use_cache and os.path.exists(cache_path):                 # 命中完整结果时不再重打脆弱的 share 页。
        cached_result = load_cached_result(cache_path, cache_identity)
        if cached_result is not None:
            cached_result.setdefault("diagnostics", []).append({
                "step": "result_cache",
                "ok": True,
                "message": "Reused identity-verified cached transcription; no platform request was made",
                "cache_path": cache_path,
                "video_id": result_video_id(cached_result),
            })
            log(f"[douyin] Using identity-verified cached result: {cache_path}")
            return cached_result
        log(f"[douyin] Ignoring cache with missing or mismatched identity: {cache_path}")

    video_path = None
    downloaded_video = None

    # Step 2: Check for cached audio after download metadata chooses the real ID.
    downloaded_video = choose_downloaded_video(
        url,
        work_dir,
        download_method=download_method,
        ratio=ratio,
        watermark=watermark,
        proxy=proxy,
        impersonate=impersonate,
        socket_timeout=socket_timeout,
    )
    metadata = downloaded_video["metadata"]
    video_id = metadata.get("video_id") or downloaded_video.get("aweme_id") or "douyin"
    wav_path = os.path.join(work_dir, f"{video_id}.wav")
    video_path = downloaded_video["video_path"]                  # 即使音频缓存存在，也要跟踪刚下载的视频用于清理。

    if not os.path.exists(wav_path):
        extract_audio(video_path, wav_path)
    else:
        log(f"[douyin] Using cached audio: {wav_path}")

    # Step 3: Transcribe the downloaded or cached audio.
    segments, full_text, asr_result = transcribe(wav_path, model_size, language)

    # Step 4: Remove heavy media files unless the caller asked to inspect them.
    if not keep_temp:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)

    result = {
        "platform": "douyin",
        "metadata": metadata,
        "download_method": downloaded_video.get("download_method"),
        "canonical_url": downloaded_video.get("canonical_url"),
        "aweme_id": downloaded_video.get("aweme_id"),
        "video_id": downloaded_video.get("video_id"),
        "requested_ratio": downloaded_video.get("requested_ratio"),
        "downloaded_ratio": downloaded_video.get("ratio"),
        "diagnostics": downloaded_video.get("diagnostics", []),
        "model_size": model_size,
        "transcription_engine": asr_result.get("engine"),
        "transcription_device": asr_result.get("device"),
        "transcription_compute_type": asr_result.get("compute_type"),
        "transcription_diagnostics": asr_result.get("diagnostics", []),
        "language": language,
        "segments": segments,
        "segment_count": len(segments),
        "full_text": full_text,
        "retrieval_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "work_dir": work_dir,
    }
    if use_cache:
        save_cached_result(cache_path, cache_identity, result)    # 完整 ASR 与真实视频 ID 齐全后才可复用。
    return result


# --- Output generators ---

def format_time(seconds):
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def to_markdown(result: dict, *, include_transcript: bool = False) -> str:
    """Generate learning notes; full ASR text requires explicit authorization."""
    meta = result["metadata"]
    lines = []

    # YAML frontmatter
    lines.append("---")
    lines.append(f'title: "{meta.get("title", "")}"')
    lines.append(f'url: "{meta.get("webpage_url", "")}"')
    lines.append(f'platform: douyin')
    lines.append(f'author: "{meta.get("channel", meta.get("uploader", ""))}"')
    lines.append(f'duration: {meta.get("duration", 0)}')
    lines.append(f'upload_date: "{meta.get("upload_date", "")}"')
    lines.append(f'transcription: "{result.get("transcription_engine", "faster-whisper")}-{result.get("model_size", "small")}"')
    lines.append(f'retrieval_time: "{result.get("retrieval_time", "")}"')
    lines.append("---\n")

    # Title
    lines.append(f"# {meta.get('title', meta.get('fulltitle', ''))}\n")

    # Basic info
    lines.append("## 基本信息\n")
    lines.append("| 字段 | 内容 |")
    lines.append("|------|------|")
    lines.append(f'| 平台 | 抖音 Douyin |')
    lines.append(f'| 链接 | {result.get("canonical_url") or meta.get("webpage_url", "")} |')
    lines.append(f'| 作者 | {meta.get("channel", meta.get("uploader", ""))} |')
    lines.append(f'| 时长 | {meta.get("duration_string", "")} |')
    lines.append(f'| 发布时间 | {meta.get("upload_date", "")} |')
    lines.append(f'| 下载方法 | {result.get("download_method", "")} |')
    if result.get("downloaded_ratio"):
        lines.append(f'| 下载画质 | {result.get("downloaded_ratio")}（请求 {result.get("requested_ratio", "")}） |')
    lines.append(f'| 转录方式 | {result.get("transcription_engine", "faster-whisper")} {result.get("model_size", "small")} |')
    lines.append(f'| 转录设备 | {result.get("transcription_device", "")} / {result.get("transcription_compute_type", "")} |')
    if meta.get("like_count"):
        lines.append(f'| 互动 | {meta.get("like_count")}点赞 / {meta.get("comment_count")}评论 / {meta.get("save_count")}收藏 |')
    lines.append("")

    # Description
    desc = meta.get("description", "").strip()
    if desc and desc != meta.get("title", ""):
        lines.append("## 视频简介\n")
        lines.append(desc)
        lines.append("")

    if include_transcript:
        lines.append("## ASR 转录全文\n")
        for seg in result.get("segments", []):
            t = format_time(seg["start"])
            lines.append(f"`{t}` {seg['text']}")
        lines.append("")
    elif result.get("segments"):
        lines.append("## 正文状态\n")
        lines.append("> 已完成 ASR，但默认未写入完整转录。仅在内容自有或明确授权时使用 `--include-transcript`。")
        lines.append("")

    lines.append("## 一句话总结\n\n[待整理]\n")
    lines.append("## 核心要点\n\n- [待整理]\n")
    lines.append("## 详细笔记\n\n[待整理]\n")

    return "\n".join(lines)


# --- CLI argument parser ---
def parse_cli_arguments(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a public Douyin video through SSR first, then ffmpeg + faster-whisper transcription.",
    )
    parser.add_argument("url", nargs="?", help="Douyin URL, aweme_id, or app share text")
    parser.add_argument("--json", "-j", action="store_true", help="Output raw JSON")
    parser.add_argument("--model", default="small", choices=("tiny", "base", "small", "medium", "large"), help="ASR model size")
    parser.add_argument("--keep-temp", action="store_true", help="Keep downloaded video and audio files")
    parser.add_argument("--no-cache", action="store_true", help="Ignore and do not write the reusable transcription cache")
    parser.add_argument("--include-transcript", action="store_true", help="Include full ASR text only for authorized local use")
    parser.add_argument("-o", "--output", help="Save Markdown to directory")
    parser.add_argument("--chinese", action="store_true", help="Use zh language for transcription")
    parser.add_argument("--english", action="store_true", help="Use en language for transcription")
    parser.add_argument("--download-method", default="auto", choices=("auto", "ssr", "ytdlp"), help="Download path: auto tries public SSR before yt-dlp")
    parser.add_argument("--ratio", default="1080p", choices=douyin_ssr.RATIOS, help="SSR play ratio")
    parser.add_argument("--watermark", action="store_true", help="Use SSR playwm endpoint instead of watermark-free play")
    parser.add_argument("--list-ratios", action="store_true", help="Probe SSR ratio availability and exit before downloading/transcribing")
    parser.add_argument("--proxy", help="Use HTTP/HTTPS/SOCKS proxy for yt-dlp fallback")
    parser.add_argument("--impersonate", help="yt-dlp curl_cffi client, e.g. chrome-110:windows-10")
    parser.add_argument("--socket-timeout", type=int, default=60, help="yt-dlp network timeout in seconds")
    return parser.parse_args(argv)


# --- Convert exceptions into clear command-line feedback ---
def main(argv: list = None) -> int:
    configure_output_encoding()                                                   # 先修正输出层，再打印任何用户来源文本
    args = parse_cli_arguments(argv if argv is not None else sys.argv[1:])

    if not args.url:
        parse_cli_arguments(["--help"])
        return 1

    language = "en" if args.english else "zh"

    try:
        if args.list_ratios:
            ratio_result = douyin_ssr.inspect_public_ratios(args.url, watermark=args.watermark)
            print(json.dumps(ratio_result, ensure_ascii=False, indent=2))
            return 0

        result = extract_douyin(
            args.url,
            model_size=args.model,
            language=language,
            keep_temp=args.keep_temp,
            download_method=args.download_method,
            ratio=args.ratio,
            watermark=args.watermark,
            proxy=args.proxy,
            impersonate=args.impersonate,
            socket_timeout=args.socket_timeout,
            use_cache=not args.no_cache,
        )
    except Exception as exc:
        error_result = {
            "platform": "douyin",
            "download_method": args.download_method,
            "canonical_url": None,
            "aweme_id": None,
            "video_id": None,
            "error": sanitize_text(str(exc)),
            "diagnostics": [],
        }
        try:
            parsed_error = json.loads(str(exc))
            error_result.update(parsed_error)
        except Exception:
            error_result["diagnostics"].append({
                "step": "extract_douyin",
                "ok": False,
                "message": sanitize_text(str(exc)),
            })

        error_result = sanitize_diagnostics(error_result)                        # 解析出的嵌套诊断也必须脱敏。
        if args.json:
            print(json.dumps(error_result, ensure_ascii=False, indent=2))
        else:
            print(f"[douyin] ERROR: {error_result['error']}", file=sys.stderr)
            for diagnostic in error_result.get("diagnostics", []):
                print(f"[douyin] {diagnostic.get('step')}: {diagnostic.get('message')}", file=sys.stderr)
        return 1

    if "metadata" in result and "_raw" in result["metadata"]:
        del result["metadata"]["_raw"]
    result = sanitize_diagnostics(result)                                        # fallback 成功仍可能携带失败诊断。

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.output:
        md = to_markdown(result, include_transcript=args.include_transcript)
        title = result["metadata"].get("title", "douyin_video")
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', title) or "douyin_video"
        filepath = os.path.join(args.output, f"{safe_name}.md")
        write_text_atomically(filepath, md)                       # 中断时保留已有完整笔记。
        print(f"Saved to: {filepath}")
    else:
        print(to_markdown(result, include_transcript=args.include_transcript))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
