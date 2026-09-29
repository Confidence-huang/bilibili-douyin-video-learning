#!/usr/bin/env python3
r"""
B站逐字稿的单一入口：字幕优先，ASR 补位，必要时融合成一份带出处的稿子。

为什么这么改：
    旧版文档把自己描述成 "Fallback when video has no subtitles available"，
    但平台字幕轨（人工 CC 或 AI 自动字幕）在术语与专有名词上通常比 ASR 权威。
    现实痛点是没有一个入口把"字幕优先 + ASR 补空档"合成一份可引用的逐字稿：
    字幕轨常常漏掉一闪而过的短卡片，而 ASR 又把同音词写错。
    现在默认 --prefer-subtitles：有字幕轨就直接用字幕构建逐字稿并**跳过 ASR**（省下整段 GPU 时间）；
    加 --fuse 时即使有字幕也额外跑一次 ASR，再用 fuse_transcripts 逐段标出处。
    没有字幕轨时行为与旧版一致：下载音频、跑 ASR、source="asr"。

边界：
    本脚本只负责"取到一份逐字稿"，不做笔记渲染；下载的音频一律放临时目录，
    --keep-audio 才保留。失败时 exit_code 字段与进程退出码保持一致，
    Agent 才能区分"取流失败"（可重试）与"本机 ASR 失败"（换机器或降模型）。

调用示例：
    python transcribe_bilibili.py BV1xx411c7mD --model small --json
    python transcribe_bilibili.py BV1xx411c7mD --fuse --json -o fused.json
    python transcribe_bilibili.py BV1ntah6TEe9 --no-prefer-subtitles --json
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注

import argparse
import importlib.util                                                                # 复用 fetch_bilibili 与 verify_transcript，不复制实现
import json
import os
import re
import sys
import subprocess
import tempfile

from normalize_transcript import TranscriptSchemaError, normalize_segments            # 分段形状的唯一适配层
from speech_to_text import transcribe_audio_file                                  # 统一使用 faster-whisper 优先的本机 ASR 入口
from runtime_output import (  # 退出码契约：Agent 要能区分"取流失败"与"本机转写失败"
    EXIT_SHARE_PAGE_UNAVAILABLE,
    EXIT_TRANSCRIPTION_FAILED,
    log,
)                                                   # 进度只写 stderr，保持 --json stdout 纯净。
from media_tools import find_ffmpeg                                              # yt-dlp 显式使用同一跨平台 FFmpeg。


SOURCE_SUBTITLE = "subtitle"                                                         # 结果来源标签：平台字幕轨
SOURCE_ASR = "asr"                                                                   # 结果来源标签：本机 ASR
SOURCE_FUSED = "fused"                                                               # 结果来源标签：字幕 + ASR 融合
DEFAULT_FUSION_GAP_SECONDS = 2.5      # 字幕常见每 2.5 秒一条，超过这个间隔就认为字幕轨真的漏了卡片


# --- 复用同目录下的兄弟脚本（本仓用 importlib 动态加载，不注册 sys.modules） ---
def _load_sibling(name: str):
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"transcribe_bilibili_{name}", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sibling script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 从任何输入里取出 BV 号，供取流与结果标识共用 ---
def extract_bvid(text: str) -> str:
    match = re.search(r"BV[a-zA-Z0-9]{10}", str(text or ""))
    return match.group(0) if match else str(text or "").strip()


# --- 平台字幕轨 -> 规范分段；同一语言只保留一份，避免重复正文 ---
def flatten_subtitle_tracks(tracks: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set = set()
    for track in tracks or []:
        for item in _track_segments(track):
            key = (round(item["start"], 3), round(item["end"], 3), item["text"])
            if key in seen:                                                          # 多语言轨常有重复行，去重但不重排
                continue
            seen.add(key)
            merged.append(item)
    return merged


# --- 单条字幕轨的 content 兼容规范形状与 ASR 形状 ---
def _track_segments(track: dict) -> list[dict]:
    content = track.get("content") if isinstance(track, dict) else track
    if not content:
        return []
    try:
        canonical = normalize_segments(content)                                      # 平台与 ASR 两种字段名都在这里统一
    except TranscriptSchemaError:
        return []
    return [item for item in canonical if item["text"].strip()]


# --- 找出一条真正能用的字幕轨：优先人工 CC，其次 AI 字幕 ---
def select_subtitle_track(tracks: list[dict]) -> dict | None:
    usable = [track for track in tracks or [] if _track_segments(track)]
    if not usable:
        return None
    return min(usable, key=lambda track: (bool(track.get("is_ai")), str(track.get("lan", ""))))


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
        "--ffmpeg-location", find_ffmpeg(),
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
    prefer_subtitles=True,
    fuse=False,
):
    """
    逐字稿主流程：字幕优先 -> 需要时 ASR -> 需要时融合。

    Args:
        bvid: Bilibili BV ID 或视频 URL
        output_dir: 临时目录（自动创建）
        model_size: 'tiny' | 'base' | 'small' | 'medium' | 'large'
        cookies: 浏览器名或 cookie 文件路径
        device: 'cpu' | 'cuda' | None（自动探测）
        keep_audio: 保留下载的音频
        prefer_subtitles: 有字幕轨时只跑字幕并跳过 ASR（默认开启）
        fuse: 即使有字幕也额外跑 ASR，并用 fuse_transcripts 逐段标出处

    Returns:
        dict：既有字段（status/segments/bvid/device/coverage_* 等）全部保留，新增
        source（subtitle|asr|fused）、subtitle_*、provenance_counts、needs_review_count 等。
    """
    bvid = extract_bvid(bvid)

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="bilibili_whisper_")

    result = {"bvid": bvid, "status": "error", "segments": [], "error": None, "exit_code": 0,
              "source": None, "skipped": [], "audio_duration": None, "video_duration": None,
              "coverage_before": None, "coverage_after": None}

    # Step 1: 元数据与字幕轨（复用 fetch_bilibili 的 yt-dlp + direct API 双通道）
    tracks, metadata = _fetch_subtitle_tracks(bvid, cookies)
    result.update(metadata)
    canonical_subtitle = flatten_subtitle_tracks(tracks)
    selected = select_subtitle_track(tracks)
    result["subtitle_tracks"] = len(tracks)
    result["subtitle_language"] = (selected or {}).get("lan_doc") or (selected or {}).get("lan") or None
    result["subtitle_is_ai"] = bool((selected or {}).get("is_ai"))

    log(f"\n{'='*50}")
    use_subtitle_only = bool(canonical_subtitle) and prefer_subtitles and not fuse
    needs_asr = fuse or (not canonical_subtitle) or not prefer_subtitles
    log(f"[pipeline] Subtitle tracks: {len(tracks)}; segments: {len(canonical_subtitle)}")

    # Step 2: 只有在确实需要 ASR 时才下载音频
    asr_segments: list[dict] = []
    audio_path = None
    if needs_asr:
        log(f"[pipeline] Step 2/3: Downloading audio for {bvid}")
        audio_path, error = download_audio(bvid, output_dir, cookies)
        if error:
            if canonical_subtitle:
                log(f"[pipeline] Audio download failed ({error}); keeping the subtitle-only transcript")
                result["skipped"].append("asr: audio download failed")
            else:
                result["error"] = f"Audio download failed: {error}"
                result["exit_code"] = EXIT_SHARE_PAGE_UNAVAILABLE                          # 取流失败：可重试或换来源
                log(f"[pipeline] ERROR: {result['error']}")
                return result
        else:
            log(f"[pipeline] Audio saved to: {audio_path}")
            log(f"[pipeline] Step 3/3: Transcribing with faster-whisper first ({model_size})")
            try:
                asr_payload = transcribe_audio(audio_path, model_size=model_size, device=device)
                asr_segments = normalize_segments(asr_payload.get("segments") or [])  # 统一成规范形状再融合
                for key in ("engine", "model_size", "device", "compute_type", "duration",
                            "audio_duration", "coverage_before", "coverage_after", "diagnostics"):
                    if asr_payload.get(key) is not None:
                        result["asr_diagnostics" if key == "diagnostics" else key] = asr_payload.get(key)
                whisper_model = {"tiny": 39, "base": 74, "small": 244, "medium": 769, "large": 1550}
                result["model_mb"] = whisper_model.get(model_size, 0)
            except Exception as exc:
                if canonical_subtitle:
                    log(f"[pipeline] ASR failed ({exc}); keeping the subtitle-only transcript")
                    result["skipped"].append(f"asr: {exc}")
                else:
                    result["error"] = f"ASR transcription failed: {exc}"
                    result["exit_code"] = EXIT_TRANSCRIPTION_FAILED                         # 取流成功、本机 ASR 失败
                    log(f"[pipeline] ERROR: {result['error']}")
                    return result
    else:
        # 字幕优先的省时路径：完全不碰音频，也就不会加载 ASR 模型
        result["skipped"].append("asr: platform subtitle track available")

    # Step 3: 逐段决定用哪一份来源
    if use_subtitle_only:
        result["source"] = SOURCE_SUBTITLE
        result["segments"] = _stamp(canonical_subtitle, SOURCE_SUBTITLE)
        result["subtitle_segments"] = len(result["segments"])
        log(f"[pipeline] Subtitle-first: {len(result['segments'])} segments, ASR skipped")
    elif fuse and canonical_subtitle and asr_segments:
        fused = _fuse_sources(canonical_subtitle, asr_segments)
        result["source"] = SOURCE_FUSED
        result["segments"] = fused["segments"]
        result["similarity"] = fused["similarity"]
        result["provenance_counts"] = fused["provenance_counts"]
        result["needs_review_count"] = fused["needs_review_count"]
        result["fusion_spans"] = len(fused["spans"])
        result["covered"] = fused["covered"]
        result["sources"] = fused["sources"]
        log(f"[pipeline] Fused {len(result['segments'])} segments "
            f"(similarity={fused['similarity']}, needs_review={fused['needs_review_count']})")
    elif asr_segments:
        result["source"] = SOURCE_ASR
        result["segments"] = _stamp(asr_segments, SOURCE_ASR)
        result["subtitle_segments"] = 0
        log(f"[pipeline] ASR-only: {len(result['segments'])} segments")
    elif canonical_subtitle:
        # --no-prefer-subtitles 但 ASR 不可用时，字幕仍是可用的逐字稿
        result["source"] = SOURCE_SUBTITLE
        result["segments"] = _stamp(canonical_subtitle, SOURCE_SUBTITLE)
        result["skipped"].append("asr: no ASR output, fell back to subtitles")
    else:
        result["error"] = "ASR produced no segments and no subtitle track was available"
        result["exit_code"] = EXIT_TRANSCRIPTION_FAILED
        log(f"[pipeline] ERROR: {result['error']}")
        return result

    result["status"] = "ok"
    result["segment_count"] = len(result["segments"])
    result["transcription_method"] = {"subtitle": "api", "asr": "faster-whisper",
                                      "fused": "api+faster-whisper"}[result["source"]]  # 下游 Markdown 靠它标注来源

    if audio_path and not keep_audio:                                           # CLI 选项现在真实控制清理行为。
        try:
            os.remove(audio_path)
        except OSError:
            pass                                                 # 转写结果已完成，清理失败只保留临时文件。
    elif audio_path:
        result["audio_path"] = audio_path                        # 明确保留位置，方便授权的本地复核。

    return result


# --- 取元数据与字幕轨；失败不致命（后面还有 ASR 路径） ---
def _fetch_subtitle_tracks(bvid, cookies):
    try:
        fetch = _load_sibling("fetch_bilibili")
        payload = fetch.fetch_with_subtitles(bvid, cookies=cookies)
    except Exception as exc:                                                     # 取元数据失败不该挡住 ASR 路径
        log(f"[pipeline] Metadata/subtitle fetch failed: {exc}")
        return [], {"metadata_error": str(exc)}
    if payload.get("error"):
        log(f"[pipeline] Metadata fetch reported: {payload['error']}")
        return payload.get("subtitles") or [], {"metadata_error": payload["error"]}
    metadata = {"title": payload.get("title"), "author": payload.get("author"),
                "video_duration": payload.get("duration"), "selected_page": payload.get("selected_page")}
    return payload.get("subtitles") or [], metadata


# --- 调用融合能力（不复制实现：verify_transcript 是唯一出处） ---
def _fuse_sources(subtitle_segments: list[dict], asr_segments: list[dict]) -> dict:
    verify = _load_sibling("verify_transcript")
    return verify.fuse_transcripts(subtitle_segments, asr_segments,
                                   primary_source=SOURCE_SUBTITLE, secondary_source=SOURCE_ASR)


# --- 给每一段补上来源标签，保证输出每段都有 provenance ---
def _stamp(segments: list[dict], provenance: str) -> list[dict]:
    stamped = []
    for item in segments:
        entry = dict(item)
        entry.setdefault("provenance", provenance)
        stamped.append(entry)
    return stamped


# --- 人类可读摘要：一眼看出这份稿子来自字幕、ASR 还是融合 ---
def render_summary(result: dict) -> str:
    lines = [
        "=== Bilibili Transcript Complete ===",
        f"Source: {result.get('source')} (transcription_method={result.get('transcription_method')})",
    ]
    if result.get("subtitle_language"):
        lines.append(f"Subtitle track: {result['subtitle_language']} (AI={result.get('subtitle_is_ai')})")
    if result.get("engine"):
        lines.append(f"ASR: {result.get('engine')} {result.get('model_size', '')} "
                     f"on {result.get('device', '?')} / {result.get('compute_type', '')}")
    if result.get("skipped"):
        lines.append(f"Skipped: {', '.join(result['skipped'])}")
    if result.get("source") == SOURCE_FUSED:
        lines.append(f"Fusion: similarity={result.get('similarity')} "
                     f"provenance={result.get('provenance_counts')} "
                     f"needs_review={result.get('needs_review_count')}")
    lines.append(f"Segments: {len(result['segments'])}")
    lines.append("")
    lines.append("First 10 segments:")
    for segment in result["segments"][:10]:
        lines.append(f"  [{_fmt_seconds(_segment_start(segment))}] {_segment_text(segment)} "
                     f"({segment.get('provenance', '?')})")
    if len(result["segments"]) > 10:
        lines.append(f"  ... ({len(result['segments'])-10} more)")
    return "\n".join(lines)


# --- 秒 -> mm:ss，规范分段与历史 from/to 形状都要能显示 ---
def _fmt_seconds(seconds) -> str:
    total = int(float(seconds or 0))
    return f"{total // 60:02d}:{total % 60:02d}"


# --- 单段文本：优先规范字段，历史结果仍按 from/content 显示 ---
def _segment_text(segment: dict) -> str:
    return segment.get("text") or segment.get("content") or ""


# --- 单段起点：优先规范字段，历史结果仍按 from 显示 ---
def _segment_start(segment: dict):
    return segment.get("start", segment.get("from", 0))


# --- CLI ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bilibili transcript: platform subtitles first, ASR when needed")
    parser.add_argument("bvid", help="Bilibili BV ID or video URL")
    parser.add_argument("--output-dir", "-o", help="Output directory for temp files")
    parser.add_argument("--model", "-m", default="auto",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="ASR model size (default: small)")
    parser.add_argument("--cookies", "-c", help="Browser name (chrome/edge) or cookie file path")
    parser.add_argument("--prefer-subtitles", dest="prefer_subtitles", action="store_true", default=True,
                        help="Use the platform subtitle track and skip ASR when it exists (default: on)")
    parser.add_argument("--no-prefer-subtitles", dest="prefer_subtitles", action="store_false",
                        help="Always run ASR even when a subtitle track exists")
    parser.add_argument("--fuse", action="store_true",
                        help="Run ASR as well and fuse subtitle + ASR into one transcript with provenance")
    parser.add_argument("--json", "-j", action="store_true", help="Print the machine-readable result on stdout")
    parser.add_argument("--keep-audio", "-k", action="store_true", help="Keep downloaded audio file")
    args = parser.parse_args()

    args.model, model_reason = speech_to_text.resolve_model_size(args.model)   # auto 按设备解析（D43）
    log(f"[bili] model={args.model} ({model_reason})")
    result = bilibili_transcribe(
        args.bvid,
        output_dir=args.output_dir,
        model_size=args.model,
        cookies=args.cookies,
        keep_audio=args.keep_audio,
        prefer_subtitles=args.prefer_subtitles,
        fuse=args.fuse,
    )

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        print(payload)                                                              # stdout 只打这一份 JSON
    elif result["status"] == "ok":
        print(render_summary(result))
    else:
        print(f"\nERROR: {result.get('error', 'Unknown error')}")

    raise SystemExit(result.get("exit_code") or 0)                                # 失败必须让调用方从退出码看得出来
