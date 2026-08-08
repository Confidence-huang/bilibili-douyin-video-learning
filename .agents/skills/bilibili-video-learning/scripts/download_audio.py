#!/usr/bin/env python3
"""
Download accessible Bilibili audio for the exact video part requested by the user.
The script resolves BV/page inputs, selects the matching cid from the public view API,
asks Bilibili's player API for an audio stream, converts it to 16 kHz mono WAV, and
optionally feeds that file into faster-whisper.

Usage examples:
    python download_audio.py https://www.bilibili.com/video/BVxxx/?p=10 --model small
    python download_audio.py BVxxx --page 10 --output-dir ./work
"""
import json
import os
import re
import sys
import subprocess
import urllib.request
import urllib.error
from urllib.parse import parse_qs, urlparse

from speech_to_text import transcribe_audio_file                                  # 统一使用 faster-whisper 优先的本机 ASR 入口
from runtime_output import log                                                   # 下载/ASR 进度写 stderr，最终 JSON 留在 stdout。
from media_tools import find_ffmpeg                                              # 所有平台共用同一 FFmpeg 解析规则。


# --- URL and page selection ---

def extract_bvid(source):
    """Read the BV ID from a URL or plain BV string before any API call is made."""
    match = re.search(r"BV[a-zA-Z0-9]{10}", str(source))  # BV is the stable key for Bilibili public APIs.
    return match.group(0) if match else str(source)


def extract_requested_page(source, page=None):
    """Prefer an explicit page argument, otherwise read ?p= or ?page= from the input URL."""
    if page is not None:
        try:
            page_number = int(page)                       # 显式页必须是整数。
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid page value: {page!r}") from exc
        if page_number < 1:
            raise ValueError(f"Invalid page value: {page_number}; page must be >= 1")
        return page_number
    query = parse_qs(urlparse(str(source)).query)          # URL parsing preserves p even when tracking params follow it.
    values = query.get("p") or query.get("page")
    if values:
        try:
            page_number = int(values[0])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid page value: {values[0]!r}") from exc
        if page_number < 1:
            raise ValueError(f"Invalid page value: {page_number}; page must be >= 1")
        return page_number
    return 1


def select_page(pages, requested_page):
    """Choose the exact cid requested by the user; never substitute another part."""
    for page_data in pages or []:
        if page_data.get("page") == requested_page:
            return page_data
    return None


def get_player_data(bvid, cid, page=1):
    """Get public player data with DASH audio URLs for the selected cid."""
    playurl = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=4048&fourk=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://www.bilibili.com/video/{bvid}/?p={page}",
    }
    req = urllib.request.Request(playurl, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception as e:
        return {"code": -1, "message": str(e)}


def get_video_info(bvid):
    """Get video aid from /x/web-interface/view."""
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception as e:
        return {"code": -1, "message": str(e)}


def find_audio_url(player_data):
    """Extract best audio URL from player DASH data."""
    data = player_data.get("data", {})

    # DASH is the normal public playurl shape for separate audio tracks.
    dash = data.get("dash", {})
    if dash:
        audio_tracks = dash.get("audio", [])
        if audio_tracks:
            # Higher bandwidth gives ASR the cleanest accessible audio without changing content.
            audio_tracks.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            best = audio_tracks[0]
            return {
                "url": best.get("baseUrl") or best.get("base_url", ""),
                "codec": best.get("codecs", best.get("codecid", "")),
                "bandwidth": best.get("bandwidth", 0),
                "mime": best.get("mimeType", best.get("mime_type", "")),
            }

    # Some older videos return a combined durl stream instead of DASH audio.
    durl = data.get("durl", [])
    if durl:
        return {
            "url": durl[0].get("url", ""),
            "codec": "",
            "bandwidth": 0,
            "mime": "",
        }

    return None


def download_audio_stream(url, output_path, headers=None):
    """
    Download audio stream directly and convert to WAV via ffmpeg.

    Bilibili audio streams are M4A/AAC. ffmpeg converts to WAV.
    """
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
        }

    # Download the raw audio stream
    temp_file = output_path + ".raw"

    log("[download] Streaming audio from API...")
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total_size = resp.headers.get('Content-Length')
            downloaded = 0
            with open(temp_file, 'wb') as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = downloaded * 100 // int(total_size)
                        if pct % 20 == 0:
                            log(f"\r[download] {pct}% ({downloaded}/{total_size} bytes)", end="")
            log("")  # 结束同一行进度输出。
    except Exception as e:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        raise RuntimeError(f"Audio stream download failed: {e}")

    # Convert to WAV using ffmpeg
    log("[ffmpeg] Converting to WAV...")
    ffmpeg = find_ffmpeg()
    result = subprocess.run([
        ffmpeg, "-y", "-i", temp_file,
        "-ac", "1",           # mono keeps ASR input stable and small
        "-ar", "16000",       # 16kHz is the standard rate expected by Whisper-family ASR
        "-sample_fmt", "s16", # 16-bit signed
        output_path
    ], capture_output=True, text=True, timeout=120)

    # Cleanup temp file
    os.remove(temp_file)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr}")

    return output_path


def download_audio(bvid, output_dir, cookies=None, page=None, return_details=False):
    """
    Download Bilibili video audio using direct API + ffmpeg.
    No yt-dlp required — uses Bilibili's own player API.

    Returns: (wav_path, error_message) or (None, error)
    """
    source_url = str(bvid)                              # Keep the original URL so diagnostics can show ?p= intent.
    requested_page = extract_requested_page(source_url, page)
    bvid = extract_bvid(source_url)
    details = {
        "source_url": source_url,
        "bvid": bvid,
        "requested_page": requested_page,
        "selected_page": None,
        "selected_cid": None,
        "pages_count": 0,
        "audio": {"ok": False, "reason": None},
    }

    # Step 1: Get the page directory so the requested P number maps to the correct cid.
    view = get_video_info(bvid)
    if view.get("code") != 0:
        error = f"View API failed: {view.get('message', 'unknown')}"
        details["audio"]["reason"] = error
        return (None, error, details) if return_details else (None, error)

    pages = view["data"]["pages"]
    details["pages_count"] = len(pages or [])
    if not pages:
        error = "No pages found in video"
        details["audio"]["reason"] = error
        return (None, error, details) if return_details else (None, error)

    selected_page = select_page(pages, requested_page)
    if not selected_page:
        error = f"Could not select a page for requested P{requested_page}"
        details["audio"]["reason"] = error
        return (None, error, details) if return_details else (None, error)

    cid = selected_page["cid"]
    details["selected_page"] = selected_page.get("page")
    details["selected_cid"] = cid
    details["selected_title"] = selected_page.get("part", "")
    pg_count = len(pages)
    if pg_count > 1:
        log(f"  Multi-page video ({pg_count} pages), using P{details['selected_page']} (cid={cid})")

    # Step 2: Get player data for audio stream URL
    player = get_player_data(bvid, cid, page=details["selected_page"] or requested_page)
    if player.get("code") != 0:
        error = f"Player API failed for P{details['selected_page']}: {player.get('message', 'unknown')}"
        details["audio"]["reason"] = error
        return (None, error, details) if return_details else (None, error)

    audio_info = find_audio_url(player)
    if not audio_info or not audio_info.get("url"):
        error = f"No audio URL found in player data for P{details['selected_page']}"
        details["audio"]["reason"] = error
        details["player_data_keys"] = sorted((player.get("data") or {}).keys())
        return (None, error, details) if return_details else (None, error)

    log(f"  Audio: {audio_info.get('codec', '?')} @ {audio_info.get('bandwidth', 0)//1000 if audio_info.get('bandwidth') else '?'}kbps")

    # Step 3: Download and convert to WAV
    output_path = os.path.join(output_dir, f"{bvid}_p{details['selected_page']}_audio.wav")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://www.bilibili.com/video/{bvid}/",
    }

    try:
        download_audio_stream(audio_info["url"], output_path, headers)
        details["audio"] = {"ok": True, "reason": None}
        details["audio_codec"] = audio_info.get("codec", "")
        details["audio_bandwidth"] = audio_info.get("bandwidth", 0)
        return (output_path, None, details) if return_details else (output_path, None)
    except Exception as e:
        details["audio"]["reason"] = str(e)
        return (None, str(e), details) if return_details else (None, str(e))


# --- Re-export transcribe function (same as transcribe_bilibili.py) ---

def bilibili_transcribe(bvid, output_dir=None, model_size="small", cookies=None, device=None, page=None):
    """
    Full pipeline: download audio via API → transcribe via faster-whisper.
    No yt-dlp required.

    Args:
        bvid: Bilibili BV ID or URL
        output_dir: Directory for temp files
        model_size: 'tiny', 'base', 'small', 'medium', 'large'
        cookies: (unused, kept for API compatibility)
        device: 'cpu', 'cuda', or None

    Returns:
        dict with {segments: [...], bvid: ..., status: 'ok'|'error'}
    """
    import tempfile

    source_url = str(bvid)                              # This may include ?p=, so keep it until page extraction is done.
    requested_page = extract_requested_page(source_url, page)
    bvid = extract_bvid(source_url)

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="bilibili_whisper_")

    result = {
        "source_url": source_url,
        "bvid": bvid,
        "requested_page": requested_page,
        "selected_page": None,
        "selected_cid": None,
        "status": "error",
        "segments": [],
        "error": None,
        "diagnostics": {"audio": {"ok": False, "reason": None}},
    }

    # Step 1: Download audio via Bilibili API
    log(f"\n{'='*50}")
    log(f"[pipeline] Step 1/2: Downloading audio for {bvid} P{requested_page} (API method)")
    audio_path, error, audio_details = download_audio(
        source_url,
        output_dir,
        cookies=cookies,
        page=requested_page,
        return_details=True,
    )
    result["selected_page"] = audio_details.get("selected_page")
    result["selected_cid"] = audio_details.get("selected_cid")
    result["selected_title"] = audio_details.get("selected_title", "")
    result["diagnostics"]["audio"] = audio_details.get("audio", result["diagnostics"]["audio"])
    if error:
        result["error"] = f"Audio download failed: {error}"
        log(f"[pipeline] ERROR: {result['error']}")
        return result

    file_size = os.path.getsize(audio_path)
    log(f"[pipeline] Audio saved: {audio_path} ({file_size//1024} KB)")

    # Step 2: Transcribe
    log(f"[pipeline] Step 2/2: Transcribing with faster-whisper first ({model_size})")
    try:
        asr_result = transcribe_audio_file(
            audio_path,
            model_size=model_size,
            language="zh",
            device=device,
            log_prefix="bilibili-asr",
        )

        result["segments"] = asr_result["segments"]
        result["status"] = "ok"
        result["engine"] = asr_result.get("engine")
        result["model_size"] = model_size
        result["device"] = asr_result.get("device")
        result["compute_type"] = asr_result.get("compute_type")
        result["duration"] = asr_result.get("duration", 0)
        result["asr_diagnostics"] = asr_result.get("diagnostics", [])
        result["diagnostics"]["audio"] = {"ok": True, "reason": None}
        log(
            f"[pipeline] Transcribed {len(result['segments'])} segments "
            f"with {result['engine']} on {result.get('device')} "
            f"({result['duration']:.1f}s total)"
        )

    except Exception as e:
        result["error"] = f"ASR failed: {str(e)}"
        result["diagnostics"]["audio"] = {"ok": False, "reason": result["error"]}
        log(f"[pipeline] ERROR: {result['error']}")
        import traceback
        traceback.print_exc()
        return result

    # Cleanup
    try:
        os.remove(audio_path)
    except Exception:
        pass

    return result


# --- CLI ---
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python download_audio.py <bvid_or_url> [--page 10] [--output-dir <path>] [--model small]")
        sys.exit(1)

    bvid = sys.argv[1]
    output_dir = None
    model_size = "small"
    page = None

    for i, a in enumerate(sys.argv):
        if a == "--output-dir" and i + 1 < len(sys.argv):
            output_dir = sys.argv[i + 1]
        if a == "--model" and i + 1 < len(sys.argv):
            model_size = sys.argv[i + 1]
        if a == "--page" and i + 1 < len(sys.argv):
            page = sys.argv[i + 1]

    result = bilibili_transcribe(bvid, output_dir=output_dir, model_size=model_size, page=page)
    print(json.dumps({"status": result["status"], "segments_count": len(result["segments"]),
                      "bvid": result["bvid"], "requested_page": result.get("requested_page"),
                      "selected_page": result.get("selected_page"), "error": result.get("error"),
                      "engine": result.get("engine"), "device": result.get("device"),
                      "compute_type": result.get("compute_type"),
                      "diagnostics": result.get("diagnostics")}, ensure_ascii=False, indent=2))
