#!/usr/bin/env python3
"""
Douyin video extraction pipeline.

The default path first tries Douyin's public SSR share page, which uses an
anonymous ttwid, server-rendered public metadata, a ranged GET probe, and the
public play/playwm endpoint. If that public path fails in auto mode, the script
falls back to the existing yt-dlp download path, then continues through ffmpeg
audio extraction and faster-whisper transcription.

Requires: requests, yt-dlp fallback, shared FFmpeg resolver, faster-whisper/CTranslate2

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
from pathlib import Path
from datetime import datetime

import asr_lexicon                                                             # 领域词表（D26）
import douyin_browser_fetch                                                      # 浏览器版取流（模仿已跑通的思路，D31）
import douyin_ssr
import media_tools                                                             # 音频前端滤镜链（D26）
import speech_to_text                                                          # 档位定义与 settings 构造
from speech_to_text import (                                                     # 统一使用 faster-whisper 优先的本机 ASR 入口
    TranscriptionSettings,
    installed_engine_versions,
    transcribe_audio_file,
)
from runtime_output import (                                                      # 进度和结构化错误共用脱敏边界。
    EXIT_GENERIC_FAILURE,
    NoAudioTrackError,
    TranscriptionFailedError,
    classify_failure,
    log,
    sanitize_diagnostics,
    sanitize_text,
)
from file_output import write_json_atomically, write_text_atomically             # 缓存和最终 Markdown 只原子发布完整文件。
from media_tools import find_ffmpeg                                              # 所有平台共用同一 FFmpeg 解析规则。
from prompt_templates import load_template                                       # 笔记骨架来自可评审的 prompts/*.md。
from clean_transcript import FIDELITY_MODES, clean_segments                      # 清洗是管道的一环，不再是孤儿脚本
from normalize_transcript import (                                               # 唯一的分段形状与文本归一化
    SIMPLIFY_MODES,
    join_with_pause_punctuation,
    simplify_diagnostic,
    simplify_segments,
    to_plain_text,
    to_srt,
)


CACHE_SCHEMA_VERSION = 4                                                         # v4 再纳入保真度与归一模式，避免串用缓存。
EMIT_FORMATS = ("md", "json", "srt", "txt")                                      # --emit 允许的产出格式


# --- 两条公开取流路径都失败 ---
class DouyinDownloadUnavailableError(RuntimeError):
    """取流不可用（携带 JSON 诊断体）：Agent 据此重试或换下载方式，而不是当成普通失败。"""


# --- Helpers ---

# --- 统一 Windows 重定向输出编码 ---
def configure_output_encoding() -> None:
    for output_stream in (sys.stdout, sys.stderr):                                # JSON、标题和诊断都可能含中文或 emoji
        if hasattr(output_stream, "reconfigure"):                                 # Python 3 的文本流允许运行时指定编码
            output_stream.reconfigure(encoding="utf-8", errors="replace")         # 避免 PowerShell/GBK 重定向在最后输出阶段失败


def _find_ffmpeg():
    return find_ffmpeg()


def _find_yt_dlp():
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--version"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Skill-owned yt-dlp module is unavailable")
    return [sys.executable, "-m", "yt_dlp"]


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
    asr_params: dict | None = None,
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
        "asr_params": asr_params or {},                          # VAD/beam/覆盖率开关与引擎版本都会改变识别结果。
    }


# --- 汇总一次转写真正会影响结果的全部参数 ---
def build_asr_identity(settings: TranscriptionSettings, fidelity: str = "verbatim",
                       simplify: str = "auto") -> dict:
    return {
        **settings.identity(),                                  # 参数版本 + 模型 + VAD + 覆盖率与置信度阈值
        "engines": installed_engine_versions(),                  # 引擎补丁升级同样要放弃旧缓存。
        "fidelity": fidelity,                                    # 清洗模式会改变落盘正文，必须一起进身份。
        "simplify": simplify,                                    # 繁简归一同样改变字形，也要进身份。
    }


# --- 为完整缓存身份生成稳定目录键 ---
def build_cache_key(
    source_text: str,
    model_size: str,
    language: str,
    download_method: str,
    ratio: str,
    watermark: bool,
    asr_params: dict | None = None,
) -> str:
    identity = build_cache_identity(source_text, model_size, language, download_method, ratio, watermark, asr_params)
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
    bridge_options: dict = None,
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


# --- Download through a real browser context (own implementation, no external service) ---
def download_video_via_browser(
    url: str,
    work_dir: str,
    bridge_options: dict = None,
    ratio: str = "1080p",
) -> dict:
    options = dict(bridge_options or {})
    video_path = os.path.join(work_dir, "douyin_video.mp4")
    result = douyin_browser_fetch.fetch_video(url, video_path, ratio=ratio, **options)
    return {
        "download_method": "browser",
        "video_path": result["video_path"],
        "metadata": result["metadata"],
        "canonical_url": result.get("canonical_url"),
        "aweme_id": result.get("aweme_id"),
        "video_id": result.get("aweme_id"),
        "requested_ratio": result.get("requested_ratio"),
        "ratio": result.get("ratio"),
        "diagnostics": [{"step": "browser_fetch", "ok": True,
                         "message": f"browser fetch ok ratio={result.get('ratio')}",
                         "available_ratios": result.get("available_ratios")}],
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
# --- 两条路径都失败时，按"是否被平台风控"选择异常类型（决定退出码 20 还是 26） ---
def unavailable_error_class(platform_limited: bool) -> type:
    if platform_limited:                                          # 风控：同 IP 换下载方式无效，必须让 Agent 看出来。
        return douyin_ssr.DouyinPlatformVerificationRequired
    return DouyinDownloadUnavailableError


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
    ssr_platform_limited = False                                 # SSR 撞上风控时必须把 26 传出去，不能被笼统的 20 吞掉（D25）。

    if download_method in ("auto", "browser"):                   # 真浏览器上下文取流：匿名路径被风控挡住时的可靠选择（D31）
        try:
            add_diagnostic(diagnostics, "browser_fetch", True, "走浏览器上下文取流")
            browser_result = download_video_via_browser(url, work_dir, bridge_options=bridge_options, ratio=ratio)
            browser_result["diagnostics"] = diagnostics + browser_result.get("diagnostics", [])
            return browser_result
        except Exception as exc:
            add_diagnostic(diagnostics, "browser_fetch", False, f"浏览器取流失败：{exc}")
            if download_method == "browser":
                raise unavailable_error_class(False)(json.dumps({
                    "error": f"Browser download failed before transcription: {exc}",
                    "download_method": download_method,
                    "diagnostics": diagnostics,
                }, ensure_ascii=False)) from exc

    if download_method in ("auto", "ssr"):
        try:
            ssr_result = download_video_with_ssr(url, work_dir, ratio=ratio, watermark=watermark)
            ssr_result["diagnostics"] = diagnostics + ssr_result.get("diagnostics", [])
            return ssr_result
        except Exception as exc:
            ssr_platform_limited = isinstance(exc, douyin_ssr.DouyinPlatformVerificationRequired)
            diagnostics.extend(getattr(exc, "diagnostics", []))
            add_diagnostic(diagnostics, "ssr_pipeline", False, f"SSR public download failed: {exc}")
            if download_method == "ssr":
                raise unavailable_error_class(ssr_platform_limited)(json.dumps({
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
            raise unavailable_error_class(ssr_platform_limited)(json.dumps({
                "error": "Douyin download failed before transcription",
                "download_method": download_method,
                "diagnostics": diagnostics,
            }, ensure_ascii=False)) from exc

    raise ValueError("download_method must be one of: auto, ssr, ytdlp")


# --- Step 3: Extract audio ---

def extract_audio(video_path: str, wav_path: str, *, normalize: bool = True) -> str:
    if not media_tools.has_audio_stream(video_path):                               # 图文作品没有音轨，越早失败越省时间（D28）
        raise NoAudioTrackError(
            "这个作品没有音轨（图文/纯图片作品）：重试或换下载方式都不会有逐字稿，请改走图片 OCR（hard_subtitle 的图片路径）。"
        )
    ffmpeg = _find_ffmpeg()
    filter_chain = media_tools.build_audio_filter_chain(normalize=normalize)   # 抖音普遍叠 BGM，先做基础净化（D26）
    cmd = [
        ffmpeg,
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(media_tools.SAMPLE_RATE),
        "-ac", "1",
    ]
    if filter_chain:
        cmd += ["-af", filter_chain]
    cmd += ["-y", wav_path]
    result = _run(cmd, timeout=60, desc="Extracting audio")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed:\nSTDERR: {result.stderr[:500]}")
    if not os.path.exists(wav_path):
        raise RuntimeError("Audio extraction failed")
    log(f"[douyin] Audio extracted: {wav_path} ({os.path.getsize(wav_path)} bytes)")
    return wav_path


# --- Step 4: Transcribe ---

def transcribe(
    wav_path: str,
    model_size: str = "small",
    language: str = "zh",
    settings: TranscriptionSettings | None = None,
) -> tuple[list, str, dict]:
    active = settings or TranscriptionSettings(model_size=model_size, language=language)  # 未显式传参时沿用历史默认值
    try:
        asr_result = transcribe_audio_file(                                       # 首选 faster-whisper + CTranslate2 + cuda/float16
            wav_path,
            model_size=active.model_size,
            language=active.language,
            log_prefix="douyin-asr",
            settings=active,                                                      # 覆盖率兜底与 VAD 开关随 settings 一起生效
        )
    except Exception as exc:                                                      # 取流已成功，这里只可能是本地 ASR 问题
        raise TranscriptionFailedError(str(exc)) from exc                          # 让调用方用不同退出码区分“换台机器再试”
    segments = [                                                                  # 规范形状：start/end/text
        {"start": round(seg["from"], 1), "end": round(seg["to"], 1), "text": seg["content"]}
        for seg in asr_result["segments"]                                         # 共享模块输出 from/to/content，这里一次性转换
    ]
    full_text = asr_result["text"].strip()                                        # 此处已是补转后的全文，不再是缺字版本
    log(
        f"[douyin] Transcription complete with {asr_result['engine']}: "
        f"{len(segments)} segments, {len(full_text)} chars"
    )
    return segments, full_text, asr_result


# --- Main entry ---

def extract_douyin(
    url: str = None,
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
    settings: TranscriptionSettings | None = None,
    fidelity: str = "verbatim",
    simplify: str = "auto",
    local_video: str = None,
    bridge_options: dict = None,
) -> dict:
    """
    Main pipeline: choose download method → extract audio → transcribe → clean.

    Returns dict with: metadata, segments, full_text, segment_count, etc.
    """
    if temp_dir is None:
        temp_dir = os.path.join(tempfile.gettempdir(), "opencode", "douyin")
    os.makedirs(temp_dir, exist_ok=True)
    active = settings or TranscriptionSettings(model_size=model_size, language=language)  # 转写参数统一由 settings 决定

    # Step 1: Use a stable non-identifying folder before the real video ID is known.
    asr_identity = build_asr_identity(active, fidelity, simplify)               # 转写参数、引擎版本、清洗与归一模式一起进入身份
    cache_source = url or (f"local:{os.path.abspath(local_video)}:{os.path.getsize(local_video)}:"
                           f"{int(os.path.getmtime(local_video))}")                   # 本地文件用路径+大小+时间做身份（D32）
    cache_identity = build_cache_identity(cache_source, active.model_size, active.language, download_method, ratio,
                                          watermark, asr_identity)
    cache_key = build_cache_key(cache_source, active.model_size, active.language, download_method, ratio, watermark,
                                asr_identity)
    pipeline_diagnostics: list = []                                                   # 管线级诊断（本地文件等前置步骤）
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
    if local_video:                                                # 1b：本地文件直接进管线，跳过一切取流（D32）
        download_method = "local"
        local_path = os.path.abspath(local_video)
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"local video not found: {local_path}")
        add_diagnostic(pipeline_diagnostics, "local_video", True, "使用本地视频文件，跳过取流",
                       file_name=os.path.basename(local_path), size_bytes=os.path.getsize(local_path))
        downloaded_video = {
            "download_method": "local",
            "video_path": local_path,
            "metadata": {"video_id": Path(local_path).stem, "title": Path(local_path).stem,
                         "fulltitle": Path(local_path).stem, "description": "", "uploader": "", "channel": "",
                         "duration": 0, "duration_string": "", "upload_date": "", "webpage_url": "",
                         "view_count": 0, "like_count": 0, "comment_count": 0, "repost_count": 0,
                         "save_count": 0, "thumbnail": "", "extractor": "LocalFile"},
            "canonical_url": local_path,
            "aweme_id": None,
            "video_id": Path(local_path).stem,
        }
    else:
        downloaded_video = choose_downloaded_video(
            url,
            work_dir,
            download_method=download_method,
            ratio=ratio,
            watermark=watermark,
            proxy=proxy,
            impersonate=impersonate,
            socket_timeout=socket_timeout,
            bridge_options=bridge_options,
        )
    metadata = downloaded_video["metadata"]
    video_id = metadata.get("video_id") or downloaded_video.get("aweme_id") or "douyin"
    wav_path = os.path.join(work_dir, f"{video_id}.wav")
    video_path = downloaded_video["video_path"]                  # 即使音频缓存存在，也要跟踪刚下载的视频用于清理。

    if not os.path.exists(wav_path):
        extract_audio(video_path, wav_path, normalize=active.normalize_audio)
    else:
        log(f"[douyin] Using cached audio: {wav_path}")

    # Step 3: Transcribe the downloaded or cached audio.
    raw_segments, _, asr_result = transcribe(wav_path, active.model_size, active.language, settings=active)
    segments, cleaning_report = clean_segments(raw_segments, fidelity=fidelity)  # 清洗是管道的一步，不是可选的外部脚本
    segments, simplification_report = simplify_segments(segments, mode=simplify)  # 繁简归一：缺 OpenCC 时只记录不失败
    if simplification_report.get("mode") != "off":                                       # 产出里写明是否真的转换过（D49）
        add_diagnostic(pipeline_diagnostics, "simplify", simplification_report.get("applied", False),
                       simplify_diagnostic(simplification_report)["message"],
                       mode=simplification_report.get("mode"),
                       changed_segments=simplification_report.get("changed_segments"),
                       reason=simplification_report.get("reason"))
    full_text = to_plain_text(segments)                                         # 逐字全文必须与落盘分段一致
    readable_text = join_with_pause_punctuation(segments)                       # 可读全文按停顿补句读，仅供阅读与检索
    log(f"[douyin] Cleaned with fidelity={fidelity}: {cleaning_report['input_segments']} -> "
        f"{cleaning_report['output_segments']} segments, dropped {cleaning_report['dropped_fillers']}")
    if simplification_report["applied"]:
        log(f"[douyin] Simplified {simplification_report['changed_segments']} segments to Simplified Chinese")

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
        "diagnostics": (downloaded_video.get("diagnostics") or []) + pipeline_diagnostics,
        "model_size": active.model_size,
        "transcription_engine": asr_result.get("engine"),
        "transcription_device": asr_result.get("device"),
        "transcription_compute_type": asr_result.get("compute_type"),
        "transcription_diagnostics": asr_result.get("diagnostics", []),
        "language": active.language,
        "audio_duration": asr_result.get("audio_duration"),       # 音频真实时长，用于复核覆盖率分母
        "coverage_before": asr_result.get("coverage_before"),     # 补转前的覆盖率
        "coverage_after": asr_result.get("coverage_after"),       # 补转后的覆盖率
        "fidelity": fidelity,
        "cleaning": cleaning_report,                              # 清洗做了什么，调用方一眼可见
        "simplification": simplification_report,                  # 繁简归一是否生效、为什么没生效
        "readable_text": readable_text,                           # 按停顿补句读的可读全文（full_text 仍是逐字拼接）
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

    # Section headings come from prompts/douyin-standard.md. Missing/unreadable
    # templates fall back to the literal headings so an already-expensive ASR run
    # still produces a note instead of failing at the last step.
    loaded_template = load_template("douyin-standard")
    sections: dict[str, str] = {}
    if loaded_template is not None:
        for line in loaded_template[1].splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                sections[stripped[3:].strip()] = stripped

    def section(title: str) -> str:
        return sections.get(title, f"## {title}")

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
    lines.append(section("基本信息") + "\n")
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
        lines.append(section("视频简介") + "\n")
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

    lines.append(section("一句话总结") + "\n\n[待整理]\n")
    lines.append(section("核心要点") + "\n\n- [待整理]\n")
    lines.append(section("详细笔记") + "\n\n[待整理]\n")

    try:                                                                         # 可选增强：加载不到也不能让笔记渲染失败（D39）
        import review_section
        processing = review_section.render_processing_section(result)            # 让读笔记的人看到处理链（D50）
        if processing:
            lines.append(processing)
        review = review_section.render_review_section(result)                    # 有校验信息才加小节
    except Exception:
        review = ""
    if review:
        lines.append(review)
    return "\n".join(lines)


# --- 解析 --emit 请求的产出格式 ---
def parse_emit_formats(emit: str | None) -> list[str]:
    if not emit:
        return []
    requested = [item.strip().lower() for item in emit.split(",") if item.strip()]
    unknown = [item for item in requested if item not in EMIT_FORMATS]
    if unknown:
        raise ValueError(f"unsupported --emit value(s) {unknown}; choose from {sorted(EMIT_FORMATS)}")
    return requested


# --- 把结果写成请求的多种格式 ---
def write_emitted_artifacts(result: dict, output_dir: str, formats: list[str], *, include_transcript: bool) -> list[str]:
    title = result.get("metadata", {}).get("title") or "douyin_video"
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", title) or "douyin_video"
    os.makedirs(output_dir, exist_ok=True)
    written = []
    for fmt in formats:
        if fmt == "md":
            payload = to_markdown(result, include_transcript=include_transcript)
        elif fmt == "json":
            payload = json.dumps(result, ensure_ascii=False, indent=2)
        elif fmt == "srt":
            payload = to_srt(result.get("segments", []))                      # 字幕本身就是完整转录，受同一授权边界约束
        else:
            payload = result.get("readable_text") or to_plain_text(result.get("segments", []))  # 可读全文优先
        filepath = os.path.join(output_dir, f"{safe_name}.{fmt}")
        write_text_atomically(filepath, payload if payload.endswith("\n") else payload + "\n")
        written.append(filepath)
    return written


# --- CLI argument parser ---
def parse_cli_arguments(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a public Douyin video through SSR first, then ffmpeg + faster-whisper transcription.",
    )
    parser.add_argument("url", nargs="?", help="Douyin URL, aweme_id, or app share text")
    parser.add_argument("--json", "-j", action="store_true", help="Output raw JSON")
    parser.add_argument("--model", default="auto", choices=("auto", "tiny", "base", "small", "medium", "large", "large-v3"),
                        help="ASR 模型；auto（默认）= 有 CUDA 用 large，否则 small（D43）")
    parser.add_argument("--keep-temp", action="store_true", help="Keep downloaded video and audio files")
    parser.add_argument("--no-cache", action="store_true", help="Ignore and do not write the reusable transcription cache")
    parser.add_argument("--include-transcript", action="store_true", help="Include full ASR text only for authorized local use")
    parser.add_argument("-o", "--output", help="Save Markdown to directory")
    parser.add_argument("--chinese", action="store_true", help="Use zh language for transcription")
    parser.add_argument("--english", action="store_true", help="Use en language for transcription")
    parser.add_argument("--download-method", default="auto", choices=("auto", "browser", "ssr", "ytdlp"),
                        help="Download path: auto tries a real browser context, then public SSR, then yt-dlp")
    parser.add_argument("--video", help="本地视频文件：跳过取流，直接用该文件走转写与产出（抖音取流不可靠时的稳定路径）")
    parser.add_argument("--browser", help="浏览器可执行文件路径（浏览器取流用；默认自动查找 Chrome/Edge）")
    parser.add_argument("--browser-profile", help="浏览器专用登录态目录（默认 ~/.cache/douyin-browser-profile，需在该目录登录一次抖音）")
    parser.add_argument("--browser-port", type=int, default=None, help="CDP 端口（默认 9333；已有实例则复用）")
    parser.add_argument("--ratio", default="1080p", choices=douyin_ssr.RATIOS, help="SSR play ratio")
    parser.add_argument("--watermark", action="store_true", help="Use SSR playwm endpoint instead of watermark-free play")
    parser.add_argument("--list-ratios", action="store_true", help="Probe SSR ratio availability and exit before downloading/transcribing")
    parser.add_argument("--proxy", help="Use HTTP/HTTPS/SOCKS proxy for yt-dlp fallback")
    parser.add_argument("--impersonate", help="yt-dlp curl_cffi client, e.g. chrome-110:windows-10")
    parser.add_argument("--socket-timeout", type=int, default=60, help="yt-dlp network timeout in seconds")
    parser.add_argument("--profile", default="balanced", choices=tuple(speech_to_text.PROFILE_OVERRIDES),
                        help="Transcription profile: fast (batch triage), balanced (default), quality (adds a targeted re-decode)")
    parser.add_argument("--hotwords", help="额外的领域词（空格或逗号分隔），做解码偏置以修正同音专名")
    parser.add_argument("--lexicon", action="append", help="词表文件路径（一行一个词），可重复；默认读取 references/asr-lexicon.txt")
    parser.add_argument("--no-normalize-audio", action="store_true", help="跳过 highpass+loudnorm 音频前端净化")
    parser.add_argument("--no-vad", action="store_true", help="Disable VAD silence filtering; keeps speech that VAD would silently drop")
    parser.add_argument("--no-coverage-retry", action="store_true", help="Skip the automatic re-transcription of suspicious timeline gaps")
    parser.add_argument("--vad-min-silence-ms", type=int, default=2000, help="VAD: silence shorter than this is not cut (faster-whisper default 2000)")
    parser.add_argument("--fidelity", default="verbatim", choices=FIDELITY_MODES,
                        help="verbatim keeps every word; cleaned also drops filler-only segments")
    parser.add_argument("--emit", help="Comma-separated artifacts to write next to the Markdown: md,json,srt,txt (srt/txt need --include-transcript)")
    parser.add_argument("--simplify", default="auto", choices=SIMPLIFY_MODES,
                        help="auto: convert Traditional to Simplified when OpenCC is installed; on: require it; off: keep as-is")
    return parser.parse_args(argv)


# --- Convert exceptions into clear command-line feedback ---
def main(argv: list = None) -> int:
    configure_output_encoding()                                                   # 先修正输出层，再打印任何用户来源文本
    args = parse_cli_arguments(argv if argv is not None else sys.argv[1:])

    if not args.url and not args.video:                                           # 二选一：在线链接 或 本地文件（D32）
        parse_cli_arguments(["--help"])
        return EXIT_GENERIC_FAILURE                                               # 缺参数属于用法错误，不是平台故障

    args.model, model_reason = speech_to_text.resolve_model_size(args.model)         # auto 按设备解析（D43）
    log(f"[douyin] model={args.model} ({model_reason})")
    language = "en" if args.english else "zh"
    settings = TranscriptionSettings(                                             # 转写参数在这里定型，缓存身份与转写共用同一份
        model_size=args.model,
        language=language,
        vad_filter=not args.no_vad,                                               # 显式 --no-vad 时不再让 VAD 判断静音
        vad_min_silence_ms=args.vad_min_silence_ms,                                # 调小会让 VAD 更激进，也更容易吞掉整句话
        coverage_check=not args.no_coverage_retry,                                 # 显式关闭补转时保持纯第一遍结果
        hotwords=asr_lexicon.build_hotwords(                                       # 领域词表：内置 + --lexicon + --hotwords + 视频元数据
            asr_lexicon.load_lexicon(args.lexicon), extra=args.hotwords),
        normalize_audio=not args.no_normalize_audio,                               # 音频前端净化开关（D26）
    )
    settings = speech_to_text.apply_profile(settings, args.profile)                 # 档位最后应用，保证覆盖关系确定

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
            local_video=args.video,                                                   # 本地文件入口（D32）
            bridge_options={"port": args.browser_port or douyin_browser_fetch.DEFAULT_PORT,
                            "browser": args.browser, "profile": args.browser_profile},
            use_cache=not args.no_cache,
            settings=settings,                                                    # VAD 与覆盖率兜底开关随参数进入转写
            fidelity=args.fidelity,                                               # 清洗模式同时决定落盘正文与缓存身份
            simplify=args.simplify,                                               # 繁简归一同样进入缓存身份
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
        exit_code = classify_failure(                                            # 取流/网络/转写分别给不同退出码
            exc,
            (douyin_ssr.DouyinSSRDownloadError, DouyinDownloadUnavailableError),
            platform_error_types=(douyin_ssr.DouyinPlatformVerificationRequired,),  # 风控降级页 → 26，与 20 区分开
            no_audio_error_types=(NoAudioTrackError,),                                # 图文作品 → 27，改走图片 OCR

        )
        error_result["exit_code"] = exit_code                                    # 结构化失败里带上同一份判断，便于 Agent 决策
        if args.json:
            print(json.dumps(error_result, ensure_ascii=False, indent=2))
        else:
            print(f"[douyin] ERROR: {error_result['error']}", file=sys.stderr)
            for diagnostic in error_result.get("diagnostics", []):
                print(f"[douyin] {diagnostic.get('step')}: {diagnostic.get('message')}", file=sys.stderr)
        return exit_code

    if "metadata" in result and "_raw" in result["metadata"]:
        del result["metadata"]["_raw"]
    result = sanitize_diagnostics(result)                                        # fallback 成功仍可能携带失败诊断。

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.output:
        try:
            emit_formats = parse_emit_formats(args.emit) or ["md"]                   # 未指定 --emit 时保持历史上的单 Markdown 行为
        except ValueError as exc:
            print(f"[douyin] ERROR: {sanitize_text(str(exc))}", file=sys.stderr)
            return EXIT_GENERIC_FAILURE
        transcript_formats = {"srt", "txt"} & set(emit_formats)
        if transcript_formats and not args.include_transcript:                       # 完整转录的授权边界必须显式跨过
            print(f"[douyin] ERROR: --emit {','.join(sorted(transcript_formats))} writes the full transcript; "
                  f"add --include-transcript", file=sys.stderr)
            return EXIT_GENERIC_FAILURE
        written = write_emitted_artifacts(result, args.output, emit_formats,
                                          include_transcript=args.include_transcript)
        for filepath in written:
            print(f"Saved to: {filepath}")
    else:
        print(to_markdown(result, include_transcript=args.include_transcript))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
