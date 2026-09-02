#!/usr/bin/env python3
"""
Fetch Bilibili video metadata and subtitles via yt-dlp (backed by its WBI-signed API calls).
Replaces direct API calls with yt-dlp's battle-tested Bilibili extractor.

yt-dlp handles: WBI signing, rate limiting, geo-bypass, cookie auth, format selection,
multi-page (anthology) detection, subtitle URL extraction, chapter extraction, comments.

Output: JSON with full video info + optional subtitle bodies.

Usage:
    python fetch_bilibili.py <url_or_bvid> [--subtitles] [--transcribe small] [--json] [-o <dir>]
    python fetch_bilibili.py <url_or_bvid> --cookies chrome --comments --json
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from urllib.parse import parse_qs, urlparse
from datetime import datetime

# ── Windows GBK 控制台编码保护 ──────────────────────────────────────
# 当 stdout/stderr 是 GBK 终端时，含非 BMP 字符的 JSON 输出会崩溃。
# 强制重新包装为 utf-8，仅在非 utf-8 环境下生效，不影响已重定向的管道。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from convert_subtitle import parse_bilibili_json, parse_srt, parse_vtt, parse_ytdlp_json  # 统一解析 yt-dlp 可返回的字幕格式。
from file_output import write_text_atomically  # 所有最终 Markdown 先完整写入临时文件再原子发布。
from runtime_output import log, sanitize_diagnostics, sanitize_text  # 进度与 JSON 诊断使用同一脱敏边界。


# --- 解析接收者自己的笔记库位置 ---
def default_obsidian_vault():
    configured_path = os.environ.get("BILIBILI_OBSIDIAN_VAULT")  # 环境变量允许其他机器显式配置。
    if configured_path:
        return os.path.abspath(os.path.expanduser(configured_path))
    return os.path.abspath(os.path.expanduser("~/Notes"))        # 未配置时只使用当前用户目录，避免继承分享者路径。


DEFAULT_OBSIDIAN_VAULT = default_obsidian_vault()
DEFAULT_OBSIDIAN_FOLDER = r"20_沉淀箱/Bilibili"


# ─── yt-dlp invocation ────────────────────────────────────────────

def _run_ytdlp(url, *, cookies=None, extra_args=None):
    """Run yt-dlp --dump-json and return parsed info dict(s)."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--dump-json", "--skip-download", "--flat-playlist", "--no-warnings",
    ]

    if cookies:
        if cookies in ("chrome", "edge", "firefox", "brave", "opera"):
            cmd.extend(["--cookies-from-browser", cookies])
        else:
            cmd.extend(["--cookies", cookies])

    if extra_args:
        cmd.extend(extra_args)

    # Try with legacy-server-connect first (workaround for OpenSSL issues)
    cmd.append(url)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"error": "yt-dlp timed out after 120s"}
    except FileNotFoundError:
        return {"error": "Skill-owned Python runtime is unavailable for yt-dlp"}

    if proc.returncode != 0:
        stderr = proc.stderr.strip().split("\n")[-3:] if proc.stderr else []

        # Retry with --legacy-server-connect for OpenSSL compatibility
        if "SSL" in (proc.stderr or "") or "EOF occurred" in (proc.stderr or ""):
            cmd.insert(1, "--legacy-server-connect")
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                    encoding="utf-8", errors="replace",
                )
            except Exception:
                pass

        if proc.returncode != 0:
            stderr = proc.stderr.strip().split("\n")[-3:] if proc.stderr else []
            return {"error": f"yt-dlp exited with code {proc.returncode}", "stderr": stderr}

    # yt-dlp outputs one JSON line per video (for playlists, multiple lines)
    entries = []
    for line in proc.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        return {"error": "yt-dlp produced no output"}

    return entries


def _summarize_ytdlp_failure(result):
    """Turn raw yt-dlp stderr into operator-facing hints without exposing cookie values."""
    stderr_tail = result.get("stderr") or []          # The last stderr lines usually contain the actionable failure.
    combined = "\n".join(stderr_tail + [result.get("error", "")])
    hints = []
    if "HTTP Error 412" in combined or "HTTP 412" in combined:
        hints.append("Bilibili returned HTTP 412; direct API metadata fallback was attempted.")
    if "could not copy" in combined.lower() and "cookie" in combined.lower():
        hints.append("Browser cookie copy failed; close the browser or export a Netscape cookie file if auth is needed.")
    if "dpapi" in combined.lower():
        hints.append("Windows DPAPI could not decrypt browser cookies for this process.")
    return {
        "message": result.get("error", "yt-dlp failed"),
        "stderr_tail": [_short_text(line) for line in stderr_tail],
        "hints": hints,
    }


# ─── URL normalization ────────────────────────────────────────────

def _short_text(text, limit=360):
    """Keep diagnostics useful without dumping full stderr or account paths."""
    if not text:                                      # Empty subprocess output should stay empty.
        return ""
    compact = re.sub(r"\s+", " ", sanitize_text(str(text))).strip()  # 先脱敏，再压缩多行后端输出。
    return compact[:limit] + ("..." if len(compact) > limit else "")


def _new_diagnostics(source_url, canonical_url, url_notes=None):
    """Create the shared diagnostics block carried through yt-dlp, API, subtitle, and audio steps."""
    return {
        "source_url": source_url,                     # The exact user input, useful when a short link was supplied.
        "canonical_url": canonical_url,               # The resolved Bilibili URL used by every downstream extractor.
        "url_resolution": url_notes or [],            # Short-link expansion attempts and outcomes.
        "ytdlp": {"ok": None, "error": None, "stderr_tail": []},
        "direct_api": {"ok": None, "error": None},
        "subtitles": {"ok": None, "reason": None},
        "audio": {"ok": None, "reason": None},
    }


def _merge_diagnostics(existing, updates):
    """Merge nested diagnostics without discarding fields populated by earlier fallback steps."""
    merged = dict(existing or {})                     # Keep caller-provided fields such as URL resolution notes.
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])                # Copy before mutating so caller references are not surprising.
            nested.update({k: v for k, v in value.items() if v is not None})
            merged[key] = nested
        elif value is not None:
            merged[key] = value
    return merged


def _expand_b23_url(url, url_notes=None):
    """Resolve b23.tv short links to the final Bilibili page while preserving query parameters like ?p=10."""
    if not re.search(r"https?://(?:b23\.tv|bilibili\.com/s/)", url or "", re.I):
        return url                                    # Non-short URLs are already canonical enough for extraction.

    curl_cmd = [
        "curl.exe", "-sSIL", "-L", "-o", "NUL",
        "-w", "%{url_effective}", url,
        "--connect-timeout", "10", "--max-time", "20",
    ]
    try:
        proc = subprocess.run(
            curl_cmd, capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0 and proc.stdout.strip():
            final_url = proc.stdout.strip()
            if url_notes is not None:
                url_notes.append({"method": "curl", "ok": True, "final_url": final_url})
            return final_url
        if url_notes is not None:
            url_notes.append({"method": "curl", "ok": False, "error": _short_text(proc.stderr)})
    except Exception as exc:
        if url_notes is not None:
            url_notes.append({"method": "curl", "ok": False, "error": _short_text(exc)})

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            final_url = resp.geturl()
        if url_notes is not None:
            url_notes.append({"method": "urllib", "ok": True, "final_url": final_url})
        return final_url
    except Exception as exc:
        if url_notes is not None:
            url_notes.append({"method": "urllib", "ok": False, "error": _short_text(exc)})
        return url

def extract_bvid(url_or_id):
    """Extract BV ID from canonical URLs, short links, plain BV IDs, or av IDs."""
    canonical_url = _expand_b23_url(str(url_or_id))   # Short links must be expanded before the BV regex can work.
    match = re.search(r"BV[a-zA-Z0-9]{10}", canonical_url)
    if match:
        return match.group(0)
    match = re.search(r"/av(\d+)|^av(\d+)$", canonical_url)
    if match:
        return match.group(0).lstrip("/")
    return None


def extract_requested_page(url_or_id):
    """Read an explicitly valid 1-based part number; only a missing query defaults to P1."""
    parsed = urlparse(str(url_or_id))                 # Query parsing handles parameter ordering and extra campaign args.
    values = parse_qs(parsed.query).get("p") or parse_qs(parsed.query).get("page")
    if values:
        try:
            page_number = int(values[0])              # Bilibili page indexes are always integers.
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid page value: {values[0]!r}") from exc
        if page_number < 1:                           # 零页或负数说明输入有误，不能静默改成 P1。
            raise ValueError(f"Invalid page value: {page_number}; page must be >= 1")
        return page_number
    return 1


def resolve_page(pages_data, url_or_page):
    """Select the exact page record; return None instead of silently substituting P1."""
    page_num = url_or_page if isinstance(url_or_page, int) else extract_requested_page(url_or_page)
    for page_data in pages_data or []:
        if page_data.get("page") == page_num:
            return page_data
    return None


# ─── Subtitle body download ──────────────────────────────────────

_PRIORITY = {
    "zh-CN": 0, "zh-Hans-CN": 0, "ai-zh-CN": 0,
    "zh-TW": 1, "zh-HK": 1, "zh": 1, "ai-zh": 1,
    "en": 10, "en-US": 10, "en-GB": 10, "ai-en": 10,
}


def _sub_score(t):
    lang = t.get("lan", "")
    lang_doc = t.get("lan_doc", "")
    if "中文" in lang_doc:
        return 0
    if lang.startswith("ai-"):
        base = lang[3:]
        return _PRIORITY.get(base, 50) + 0.5
    return _PRIORITY.get(lang, 50)


def sort_subtitle_tracks(tracks):
    return sorted(tracks, key=_sub_score)


_SUBTITLE_FORMAT_PRIORITY = {"json3": 0, "json": 1, "srt": 2, "vtt": 3}


# --- 下载一份字幕原文 ---
def _download_subtitle_text(subtitle_url):
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    try:
        req = urllib.request.Request(subtitle_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")                  # 保留原始格式，下一层再判断 JSON/SRT/VTT。
    except (urllib.error.URLError, OSError):
        proc = subprocess.run(
            ["curl.exe", "-sS", "-L", subtitle_url, "-H", "Referer: https://www.bilibili.com/"],
            capture_output=True,
            text=True,
            timeout=35,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0 and proc.stdout.strip():         # curl 使用 Windows SChannel 规避部分 OpenSSL 错误。
            return proc.stdout
    return ""                                                    # 调用方把空正文写进 diagnostics。


# --- 把任意支持的字幕格式统一为平台时间线 ---
def download_subtitle_content(subtitle_url, subtitle_format="json"):
    raw_text = _download_subtitle_text(subtitle_url)
    if not raw_text:
        return []
    try:
        payload = json.loads(raw_text)                           # URL 扩展名不总可信，先按内容识别 JSON。
        if isinstance(payload, dict) and "body" in payload:
            parsed = parse_bilibili_json(raw_text)
        elif isinstance(payload, dict) and "events" in payload:
            parsed = parse_ytdlp_json(raw_text)
        else:
            parsed = []
    except json.JSONDecodeError:
        parsed = parse_vtt(raw_text) if subtitle_format == "vtt" else parse_srt(raw_text)
    return [
        {"from": item["start"], "to": item["end"], "content": item["text"]}
        for item in parsed
        if item.get("text", "").strip()
    ]


# --- 保留 direct API 旧调用需要的 JSON body 结构 ---
def download_subtitle_body(subtitle_url):
    raw_text = _download_subtitle_text(subtitle_url)
    if not raw_text:
        return {}
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {}


def fmt_time(seconds):
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_duration(seconds):
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


# ─── Transform yt-dlp output → skill format ──────────────────────

def _is_bilibili_url(url):
    return bool(re.search(r"bilibili\.com|b23\.tv|BV\w{10}", url or ""))


def _normalize_url(url_or_bvid, url_notes=None):
    source_text = str(url_or_bvid).strip()
    if source_text.startswith("http"):
        return _expand_b23_url(source_text, url_notes)
    if re.match(r"^[aAbB][vV][a-zA-Z0-9]{10}$", source_text):
        return f"https://www.bilibili.com/video/{source_text}"
    if re.match(r"^av\d+$", source_text):
        return f"https://www.bilibili.com/video/{source_text}"
    return f"https://www.bilibili.com/video/{source_text}"


def _parse_ytdlp_entry(entry, *, source_url="", canonical_url="", requested_page=1):
    """Convert a single yt-dlp info dict entry into skill format."""
    bvid = entry.get("id", "")
    # Handle multi-page entries (yt-dlp creates separate entries for each part)
    page_num = entry.get("playlist_index") or 1
    if isinstance(page_num, str):
        try:
            page_num = int(page_num)
        except ValueError:
            page_num = 1

    upload_date = entry.get("upload_date", "")
    if upload_date and len(upload_date) == 8:
        dt = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        dt = ""

    # Build pages list
    pages = []
    for i, p in enumerate(entry.get("entries") or []):
        if not p:
            continue
        pages.append({
            "page": i + 1,
            "cid": p.get("id", ""),
            "title": p.get("title", ""),
            "duration": p.get("duration", 0) or 0,
        })

    # Extract subtitle tracks from yt-dlp infodict
    raw_subs = entry.get("subtitles") or {}
    subtitle_tracks = []
    for lang, subs in raw_subs.items():
        usable_formats = [                                      # 同一语言只保留最稳定的一份，避免重复正文。
            subtitle for subtitle in subs
            if subtitle.get("url") and subtitle.get("ext") in _SUBTITLE_FORMAT_PRIORITY
        ]
        if not usable_formats:
            continue
        selected_format = min(
            usable_formats,
            key=lambda subtitle: _SUBTITLE_FORMAT_PRIORITY[subtitle.get("ext")],
        )
        subtitle_tracks.append({
            "lan": lang,
            "lan_doc": lang,
            "subtitle_url": selected_format["url"],
            "subtitle_format": selected_format.get("ext", "json"),
            "is_ai": lang.startswith("ai-"),
        })

    # Extract chapters
    raw_chapters = entry.get("chapters") or []
    chapters = [
        {
            "from": ch.get("start_time", 0),
            "to": ch.get("end_time", 0),
            "title": ch.get("title", ""),
        }
        for ch in raw_chapters
    ]

    # Extract comments if available
    raw_comments = entry.get("comments") or []
    comments = []
    for c in raw_comments:
        comments.append({
            "author": c.get("author", ""),
            "author_id": c.get("author_id", ""),
            "text": c.get("text", ""),
            "timestamp": c.get("timestamp", 0),
            "parent": c.get("parent", "root"),
            "like_count": c.get("like_count", 0),
        })

    selected_page = resolve_page(pages, requested_page) or {
        "page": page_num,
        "cid": entry.get("id", ""),
        "title": entry.get("title", ""),
        "duration": entry.get("duration", 0) or 0,
    }

    return {
        "source_url": source_url,
        "canonical_url": canonical_url,
        "bvid": bvid,
        "aid": entry.get("aid"),
        "title": entry.get("title", ""),
        "author": entry.get("uploader", ""),
        "uploader_uid": entry.get("uploader_id", ""),
        "description": entry.get("description", ""),
        "duration": entry.get("duration", 0) or 0,
        "pubdate": entry.get("timestamp", 0) or 0,
        "upload_date": dt,
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "comment_count": entry.get("comment_count"),
        "tags": entry.get("tags") or [],
        "thumbnail": entry.get("thumbnail", ""),
        "webpage_url": entry.get("webpage_url", ""),
        "page_num": page_num,
        "requested_page": requested_page,
        "selected_page": selected_page.get("page") if selected_page else page_num,
        "selected_cid": selected_page.get("cid") if selected_page else "",
        "selected_title": selected_page.get("title") if selected_page else "",
        "pages": pages,
        "subtitles": subtitle_tracks,
        "chapters": chapters,
        "comments": comments,
        "entries_raw": entry.get("entries"),  # for multi-video playlists
        "_type": entry.get("_type"),
    }


# ─── Public API ──────────────────────────────────────────────────

def fetch_direct_api(bvid_or_url, *, with_subtitles=False, with_chapters=False):
    """Fallback: direct Bilibili API calls via curl (bypasses Python OpenSSL issue)."""
    source_url = str(bvid_or_url)                    # Preserve the user's input for the final diagnostic report.
    url_notes = []                                   # Each URL resolution attempt is visible in JSON output.
    canonical_url = _normalize_url(source_url, url_notes)
    requested_page = extract_requested_page(canonical_url)
    bvid = extract_bvid(canonical_url)
    diagnostics = _new_diagnostics(source_url, canonical_url, url_notes)
    if not bvid:
        diagnostics = _merge_diagnostics(diagnostics, {"direct_api": {"ok": False, "error": "Could not extract BV ID"}})
        return {"error": "Could not extract BV ID", "source_url": source_url, "canonical_url": canonical_url, "diagnostics": diagnostics}
    view = _get_json_direct(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if view.get("code") != 0:
        diagnostics = _merge_diagnostics(diagnostics, {"direct_api": {"ok": False, "error": f"API view failed: {view.get('message')}"}})
        return {"error": f"API view failed: {view.get('message')}", "bvid": bvid, "source_url": source_url, "canonical_url": canonical_url, "diagnostics": diagnostics}
    data = view["data"]
    pages = [
        {"page": p.get("page", 1), "cid": p.get("cid", 0), "title": p.get("part", ""), "duration": p.get("duration", 0)}
        for p in (data.get("pages") or [])
    ]
    selected_page = resolve_page(pages, requested_page)
    if pages and selected_page is None:
        available_pages = [page.get("page") for page in pages]  # 错误中给出真实可选范围，方便 Agent 自我修正。
        message = f"Requested page P{requested_page} is unavailable; available pages: {available_pages}"
        diagnostics = _merge_diagnostics(diagnostics, {"direct_api": {"ok": False, "error": message}})
        return {
            "error": message,
            "bvid": bvid,
            "source_url": source_url,
            "canonical_url": canonical_url,
            "requested_page": requested_page,
            "pages": pages,
            "diagnostics": diagnostics,
        }
    result = {
        "source_url": source_url,
        "canonical_url": canonical_url,
        "bvid": bvid,
        "aid": data.get("aid"),
        "title": data.get("title", ""),
        "author": data["owner"]["name"],
        "uploader_uid": data["owner"]["mid"],
        "description": data.get("desc", ""),
        "duration": data.get("duration", 0),
        "pubdate": data.get("pubdate", 0),
        "view_count": data.get("stat", {}).get("view"),
        "like_count": data.get("stat", {}).get("like"),
        "comment_count": data.get("stat", {}).get("reply"),
        "tags": [],
        "thumbnail": data.get("pic", ""),
        "webpage_url": canonical_url,
        "requested_page": requested_page,
        "selected_page": selected_page.get("page") if selected_page else None,
        "selected_cid": selected_page.get("cid") if selected_page else None,
        "selected_title": selected_page.get("title") if selected_page else "",
        "pages": pages,
        "subtitles": [],
        "chapters": [],
        "comments": [],
        "diagnostics": _merge_diagnostics(diagnostics, {"direct_api": {"ok": True, "error": None}}),
    }

    # Fetch player data (subtitle tracks + chapters) for the requested page only.
    if with_subtitles or with_chapters:
        target_pages = [selected_page] if selected_page else []  # 不存在的页已在上方失败，不再遍历所有页。
        for page_info in target_pages:
            cid = page_info["cid"]
            if not cid:
                continue
            player = _get_json_direct(f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}")
            if player.get("code") != 0:
                result["diagnostics"] = _merge_diagnostics(
                    result.get("diagnostics"),
                    {"direct_api": {"ok": False, "error": f"Player API failed for P{page_info.get('page')}: {player.get('message')}"}},
                )
                continue
            pd = player.get("data", {})

            if with_chapters:
                for vp in (pd.get("view_points") or []):
                    result["chapters"].append({
                        "from": vp.get("from", 0),
                        "to": vp.get("to", 0),
                        "title": vp.get("content", vp.get("title", "")),
                    })

            if with_subtitles:
                subtitle_info = pd.get("subtitle", {})
                tracks = subtitle_info.get("subtitles", [])
                tracks = sort_subtitle_tracks(tracks)
                for t in tracks:
                    sub_data = {
                        "id": t.get("id"),
                        "page": page_info.get("page"),
                        "cid": cid,
                        "lan": t.get("lan", ""),
                        "lan_doc": t.get("lan_doc", ""),
                        "subtitle_url": t.get("subtitle_url", ""),
                        "is_ai": t.get("lan", "").startswith("ai-"),
                    }
                    # Download subtitle body
                    if with_subtitles and sub_data["subtitle_url"]:
                        body = download_subtitle_body(sub_data["subtitle_url"])
                        if body and body.get("body"):
                            sub_data["content"] = [
                                {"from": i.get("from", 0), "to": i.get("to", 0), "content": i.get("content", "")}
                                for i in body["body"]
                            ]
                    result["subtitles"].append(sub_data)

    if with_subtitles:
        subtitle_reason = None if result["subtitles"] else f"No subtitle tracks returned for P{result.get('selected_page')}"
        result["diagnostics"] = _merge_diagnostics(
            result.get("diagnostics"),
            {"subtitles": {"ok": bool(result["subtitles"]), "reason": subtitle_reason}},
        )

    return result


def _get_json_via_curl(url):
    """Use curl.exe (Windows SChannel) when Python OpenSSL fails on Bilibili."""
    headers = [
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-H", "Referer: https://www.bilibili.com/",
    ]
    try:
        proc = subprocess.run(
            ["curl.exe", "-s", url, *headers, "--connect-timeout", "15", "--max-time", "30"],
            capture_output=True, text=True, timeout=35, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
    except Exception:
        pass
    return {"code": -1, "message": "curl fallback failed"}


def _get_json_direct(url):
    """Try Python urllib first, fall back to curl on SSL errors."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        if "SSL" in str(e) or "EOF" in str(e):
            return _get_json_via_curl(url)
        return {"code": -1, "message": str(e)}


def fetch(url_or_bvid, *, cookies=None, get_comments=False, with_subtitles=False, with_chapters=False):
    """Fetch video metadata using yt-dlp. Falls back to direct API on failure."""
    source_url = str(url_or_bvid)                    # Keep caller input visible for short-link debugging.
    url_notes = []                                   # Short-link expansion evidence is attached to the result.
    url = _normalize_url(source_url, url_notes)
    requested_page = extract_requested_page(url)
    diagnostics = _new_diagnostics(source_url, url, url_notes)
    extra = []
    if get_comments:
        extra.append("--get-comments")

    result = _run_ytdlp(url, cookies=cookies, extra_args=extra)
    if isinstance(result, dict) and "error" in result:
        ytdlp_failure = _summarize_ytdlp_failure(result)
        ytdlp_error = ytdlp_failure["message"]
        log(f"[fetch] yt-dlp failed ({ytdlp_error}), falling back to direct API...")
        fallback = fetch_direct_api(url, with_subtitles=with_subtitles, with_chapters=with_chapters)
        fallback["source_url"] = source_url
        fallback["canonical_url"] = url
        fallback["_ytdlp_error"] = ytdlp_error
        fallback["_ytdlp_stderr_tail"] = ytdlp_failure["stderr_tail"]
        fallback["diagnostics"] = _merge_diagnostics(
            fallback.get("diagnostics"),
            {
                "source_url": source_url,
                "canonical_url": url,
                "url_resolution": url_notes,
                "ytdlp": {"ok": False, "error": ytdlp_error, "stderr_tail": ytdlp_failure["stderr_tail"], "hints": ytdlp_failure["hints"]},
                "direct_api": {"ok": "error" not in fallback, "error": fallback.get("error")},
            },
        )
        return fallback

    # yt-dlp may return multiple entries for playlists/anthologies
    entries = result if isinstance(result, list) else [result]
    parsed = [_parse_ytdlp_entry(e, source_url=source_url, canonical_url=url, requested_page=requested_page) for e in entries]

    if not parsed:
        diagnostics = _merge_diagnostics(diagnostics, {"ytdlp": {"ok": False, "error": "Could not parse yt-dlp output"}})
        return {"error": "Could not parse yt-dlp output", "bvid": extract_bvid(url), "source_url": source_url, "canonical_url": url, "diagnostics": diagnostics}

    # For single video, return first entry directly
    main = parsed[0]
    main["diagnostics"] = _merge_diagnostics(diagnostics, {"ytdlp": {"ok": True, "error": None}})
    if len(parsed) == 1:
        available_pages = main.get("pages") or [{"page": main.get("page_num", 1)}]
        selected_page = resolve_page(available_pages, requested_page)
        if selected_page is None:
            page_numbers = [page.get("page") for page in available_pages]
            error_message = f"Requested page P{requested_page} is unavailable; available pages: {page_numbers}"
            main["error"] = error_message
            main["diagnostics"] = _merge_diagnostics(
                main.get("diagnostics"),
                {"ytdlp": {"ok": False, "error": error_message}},
            )
        return main

    # For multi-page, merge pages from all entries
    all_pages = []
    for entry in parsed:
        if entry["pages"]:
            all_pages.extend(entry["pages"])
        else:
            all_pages.append({
                "page": entry["page_num"],
                "cid": "",
                "title": entry["title"],
                "duration": entry["duration"],
            })

    main["pages"] = all_pages
    selected_page = resolve_page(all_pages, requested_page)
    if selected_page is None:
        page_numbers = [page.get("page") for page in all_pages]
        error_message = f"Requested page P{requested_page} is unavailable; available pages: {page_numbers}"
        main["error"] = error_message
        main["diagnostics"] = _merge_diagnostics(
            main.get("diagnostics"),
            {"ytdlp": {"ok": False, "error": error_message}},
        )
        return main
    main["selected_page"] = selected_page.get("page") if selected_page else main.get("selected_page")
    main["selected_cid"] = selected_page.get("cid") if selected_page else main.get("selected_cid")
    main["selected_title"] = selected_page.get("title") if selected_page else main.get("selected_title")
    return main


def fetch_with_subtitles(url_or_bvid, *, cookies=None):
    """Fetch metadata + download subtitle bodies."""
    result = fetch(url_or_bvid, cookies=cookies, with_subtitles=True, with_chapters=True)
    if "error" in result:
        return result

    # For each page's subtitle track, download body (already done in fallback path)
    if not result.get("_ytdlp_error") and result.get("subtitles"):
        sorted_subs = sort_subtitle_tracks(result["subtitles"])
        for sub in sorted_subs:
            sub_url = sub.get("subtitle_url", "")
            if not sub_url:
                continue
            items = download_subtitle_content(sub_url, sub.get("subtitle_format", "json"))
            if items:
                sub["content"] = items

    result["subtitles"] = sort_subtitle_tracks(result.get("subtitles", []))
    has_tracks = bool(result.get("subtitles"))
    has_content = any(sub.get("content") for sub in result.get("subtitles", []))
    reason = None if has_content else ("Subtitle tracks exist but no body was downloaded" if has_tracks else f"No subtitle tracks found for P{result.get('selected_page') or extract_requested_page(result.get('canonical_url') or url_or_bvid)}")
    result["diagnostics"] = _merge_diagnostics(
        result.get("diagnostics"),
        {"subtitles": {"ok": has_content, "reason": reason}},
    )
    return result


def fetch_all(url_or_bvid, *, model_size="small", cookies=None, prefer_api=True):
    """
    Fetch everything: metadata + subtitles (API) or ASR fallback.

    Priority:
    1. Bilibili API subtitles (if available and prefer_api=True)
    2. faster-whisper ASR transcription (fallback, openai-whisper only if needed)
    """
    result = fetch_with_subtitles(url_or_bvid, cookies=cookies) if prefer_api else fetch(url_or_bvid, cookies=cookies)

    if "error" in result:
        return result

    bvid = result.get("bvid") or extract_bvid(result.get("canonical_url") or url_or_bvid)
    result["transcription_method"] = "api"

    # Check if any subtitle has actual content
    has_content = any(
        sub.get("content")
        for sub in result.get("subtitles", [])
    )

    if has_content:
        return result

    # Fallback to ASR
    log(f"[fetch_all] No API subtitles found for {bvid}, falling back to faster-whisper first ({model_size})...")

    try:
        import importlib.util
        script_dir = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "download_audio",
            os.path.join(script_dir, "download_audio.py")
        )
        audio_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audio_mod)

        transcribe_result = audio_mod.bilibili_transcribe(
            result.get("canonical_url") or bvid,
            model_size=model_size,
            page=result.get("selected_page") or result.get("requested_page"),
        )

        if transcribe_result.get("status") == "ok":
            asr_engine = transcribe_result.get("engine") or "faster-whisper"
            result["transcription_method"] = f"{asr_engine}-{model_size}"
            result["diagnostics"] = _merge_diagnostics(
                result.get("diagnostics"),
                {"audio": {"ok": True, "reason": None}},
            )
            whisper_sub = {
                "id": 0,
                "lan": "zh",
                "lan_doc": f"{asr_engine} ({model_size}) 自动转写",
                "subtitle_url": "",
                "is_ai": True,
                "content": transcribe_result["segments"],
            }
            result["subtitles"] = [whisper_sub]
        else:
            result["transcription_method"] = "failed"
            result["transcription_error"] = transcribe_result.get("error", "Unknown")
            result["diagnostics"] = _merge_diagnostics(
                result.get("diagnostics"),
                {"audio": {"ok": False, "reason": transcribe_result.get("error", "Unknown")}},
            )

    except Exception as e:
        result["transcription_method"] = "failed"
        result["transcription_error"] = str(e)
        result["diagnostics"] = _merge_diagnostics(
            result.get("diagnostics"),
            {"audio": {"ok": False, "reason": str(e)}},
        )
        import traceback
        traceback.print_exc()

    return result


# ─── Output formatting ──────────────────────────────────────────

def to_markdown(data, *, include_transcript=False):
    """Generate learning Markdown; full subtitle text requires explicit authorization."""
    lines = []
    method = data.get("transcription_method") or "metadata"
    subtitles = data.get("subtitles", [])
    best_subtitle = subtitles[0] if subtitles else {}
    subtitle_lang = best_subtitle.get("lan_doc") or best_subtitle.get("lan", "")
    is_asr_method = method.startswith(("faster-whisper", "openai-whisper", "whisper"))
    source_label = "[来自字幕]" if method == "api" else "[来自视频正文]" if is_asr_method else "[来自标题/简介]"

    lines.append("---")
    lines.append(f'title: "{data.get("title", "")}"')
    lines.append(f'url: "https://www.bilibili.com/video/{data.get("bvid", "")}/"')
    lines.append(f'bvid: "{data.get("bvid", "")}"')
    lines.append(f'author: "{data.get("author", "")}"')
    lines.append(f'duration: {data.get("duration", 0)}')
    pubdate = data.get("pubdate") or 0
    if pubdate:
        dt = datetime.fromtimestamp(pubdate)
        lines.append(f'upload_date: "{dt.strftime("%Y-%m-%d")}"')
    lines.append(f'created: "{datetime.now().strftime("%Y-%m-%d %H:%M")}"')
    lines.append(f'transcript_source: "{method}"')
    if subtitle_lang:
        lines.append(f'subtitle_lang: "{subtitle_lang}"')
    lines.append(f'tags: ["bilibili", "learning"]')
    lines.append("---\n")

    lines.append(f"# {data.get('title', '')}\n")

    lines.append(f"> 来源范围：{source_label}。脚本输出用于学习笔记沉淀；如为 ASR 转写，术语和断句可能存在误差。")
    lines.append("")

    lines.append("## 基本信息\n")
    lines.append("| 字段 | 内容 |")
    lines.append("|------|------|")
    lines.append(f'| 平台 | Bilibili |')
    lines.append(f'| 链接 | {data.get("webpage_url") or "https://www.bilibili.com/video/" + data.get("bvid", "")} |')
    lines.append(f'| UP 主 | {data.get("author", "")} |')
    lines.append(f'| 时长 | {fmt_duration(data.get("duration", 0))} |')
    lines.append(f'| BV号 | {data.get("bvid", "")} |')
    if data.get("view_count") is not None:
        lines.append(f'| 播放 | {data["view_count"]} |')
    if data.get("like_count") is not None:
        lines.append(f'| 点赞 | {data["like_count"]} |')
    if data.get("transcription_method"):
        lines.append(f'| 转录方式 | {data["transcription_method"]} |')
    lines.append("")

    desc = (data.get("description") or "").strip()
    if desc:
        lines.append("## 视频简介\n")
        lines.append(desc)
        lines.append("")

    # Chapters
    chapters = data.get("chapters", [])
    if chapters:
        lines.append("## 章节\n")
        for ch in chapters:
            t = fmt_time(ch.get("from", 0))
            lines.append(f"- `{t}` {ch.get('title', '')}")
        lines.append("")

    # Subtitles
    if subtitles:
        best = subtitles[0]
        lines.append(f"**字幕来源：** {best.get('lan_doc', best.get('lan', ''))}")
        lines.append("")

        content = best.get("content", [])
        if content and not include_transcript:
            lines.append("> 已取得字幕/ASR 正文，但默认未写入完整字幕。仅在用户拥有内容或明确授权本地转换时使用 `--include-transcript`。")
            lines.append("")
        elif content and chapters:
            # Group by chapters
            for ch in chapters:
                ch_from = ch.get("from", 0)
                ch_to = ch.get("to", 0)
                ch_lines = [
                    f for f in content
                    if ch_from <= f.get("from", 0) < ch_to - 0.5
                ]
                if ch_lines:
                    lines.append(f"### {ch.get('title', '')} `{fmt_time(ch_from)}`\n")
                    prev_text = ""
                    for item in ch_lines:
                        text = item.get("content", "").strip()
                        if text == prev_text:
                            continue
                        prev_text = text
                        t = fmt_time(item.get("from", 0))
                        lines.append(f"`{t}` {text}")
                    lines.append("")
        elif content:
            lines.append("## 字幕全文\n")
            prev_text = ""
            for item in content:
                text = item.get("content", "").strip()
                if text == prev_text:
                    continue
                prev_text = text
                t = fmt_time(item.get("from", 0))
                lines.append(f"`{t}` {text}")
            lines.append("")

    # Comments
    comments = data.get("comments", [])
    if comments:
        lines.append("## 热评\n")
        for i, c in enumerate(comments[:20]):
            lines.append(f"**{c.get('author', '匿名')}** (点赞 {c.get('like_count', 0)}):")
            lines.append(f"> {c.get('text', '')}")
            lines.append("")

    return "\n".join(lines)


def to_obsidian_markdown(data, vault_path=None, *, include_transcript=False):
    md = to_markdown(data, include_transcript=include_transcript)
    if vault_path:
        title = data.get("title", "untitled")
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', title)
        filepath = os.path.join(vault_path, f"{safe_name}.md")
        return str(write_text_atomically(filepath, md))
    return md


def save_to_obsidian_folder(
    data,
    *,
    vault_path=DEFAULT_OBSIDIAN_VAULT,
    note_folder=DEFAULT_OBSIDIAN_FOLDER,
    include_transcript=False,
):
    """Save Markdown to the same Obsidian folder used by Bilibili Obsidian Clipper."""
    title = data.get("title") or data.get("bvid") or "untitled"
    bvid = data.get("bvid") or "BV_unknown"
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()[:90]
    safe_bvid = re.sub(r'[\\/:*?"<>|]', '_', bvid)
    output_dir = os.path.join(vault_path, note_folder)
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y-%m-%d')}-{safe_bvid}-{safe_title}.md"
    filepath = os.path.join(output_dir, filename)
    markdown = to_markdown(data, include_transcript=include_transcript)
    return str(write_text_atomically(filepath, markdown))


# ─── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python fetch_bilibili.py <url_or_bvid> [options]")
        print("")
        print("Options:")
        print("  -o, --output-dir <path>   Save Markdown to directory")
        print("  -j, --json                 Output raw JSON instead of Markdown")
        print("  -s, --subtitles            Download subtitle tracks from API")
        print("  -t, --transcribe [model]   Fallback to faster-whisper ASR (tiny/base/small/medium/large)")
        print("  -c, --cookies <src>        Cookie source: chrome/edge/firefox or file path")
        print("  --comments                 Include comment extraction")
        print("  --obsidian                 Save Markdown to Obsidian folder (default vault + 20_沉淀箱/Bilibili)")
        print("  --vault <path>             Obsidian vault path for --obsidian")
        print("  --note-folder <path>       Note folder inside vault for --obsidian")
        print("  --include-transcript       Include full subtitle/ASR text only for authorized local use")
        print("")
        print("Examples:")
        print("  fetch_bilibili.py BV1xx --subtitles --json")
        print("  fetch_bilibili.py BV1xx --subtitles --transcribe small -o ./notes")
        print("  fetch_bilibili.py BV1xx --subtitles --transcribe small -c edge --obsidian")
        print("  fetch_bilibili.py BV1xx --cookies chrome --comments --json")
        sys.exit(1)

    arg = sys.argv[1]
    output_dir = None
    json_mode = False
    with_subs = False
    with_transcribe = False
    transcribe_model = "small"
    cookies = None
    get_comments = False
    save_obsidian = False
    include_transcript = False
    obsidian_vault = DEFAULT_OBSIDIAN_VAULT
    obsidian_folder = DEFAULT_OBSIDIAN_FOLDER

    i = 2
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("--output-dir", "-o"):
            if i + 1 < len(sys.argv):
                output_dir = sys.argv[i + 1]
                i += 1
        elif a in ("--json", "-j"):
            json_mode = True
        elif a in ("--subtitles", "-s"):
            with_subs = True
        elif a in ("--transcribe", "-t"):
            with_transcribe = True
            if i + 1 < len(sys.argv) and sys.argv[i + 1] in ("tiny", "base", "small", "medium", "large"):
                transcribe_model = sys.argv[i + 1]
                i += 1
        elif a in ("--cookies", "-c"):
            if i + 1 < len(sys.argv):
                cookies = sys.argv[i + 1]
                i += 1
        elif a == "--comments":
            get_comments = True
        elif a == "--obsidian":
            save_obsidian = True
        elif a == "--vault":
            if i + 1 < len(sys.argv):
                obsidian_vault = sys.argv[i + 1]
                i += 1
        elif a == "--note-folder":
            if i + 1 < len(sys.argv):
                obsidian_folder = sys.argv[i + 1].strip("/\\")
                i += 1
        elif a == "--include-transcript":
            include_transcript = True
        i += 1

    exit_code = 0
    try:
        if with_transcribe:
            result = fetch_all(arg, model_size=transcribe_model, cookies=cookies, prefer_api=with_subs)
        elif with_subs:
            result = fetch_with_subtitles(arg, cookies=cookies)
        else:
            result = fetch(arg, cookies=cookies, get_comments=get_comments)
    except Exception as exc:
        result = {"error": str(exc), "source_url": arg}         # JSON 调用方仍能得到结构化失败。
        exit_code = 2

    if result.get("error"):
        exit_code = exit_code or 1                               # 后端返回错误时不得伪装成功。

    result = sanitize_diagnostics(result)                         # 直接脚本模式也不输出后端凭据或签名链接。

    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("error"):
        log(f"ERROR: {result['error']}")
    elif save_obsidian:
        filepath = save_to_obsidian_folder(
            result,
            vault_path=obsidian_vault,
            note_folder=obsidian_folder,
            include_transcript=include_transcript,
        )
        print(f"Saved to Obsidian: {filepath}")
    else:
        md = to_markdown(result, include_transcript=include_transcript)
        if output_dir:
            title = result.get("title", arg)
            safe_name = re.sub(r'[\\/:*?"<>|]', '_', title)
            os.makedirs(output_dir, exist_ok=True)
            filepath = os.path.join(output_dir, f"{safe_name}.md")
            write_text_atomically(filepath, md)
            print(f"Saved to: {filepath}")
        else:
            print(md)

    raise SystemExit(exit_code)
