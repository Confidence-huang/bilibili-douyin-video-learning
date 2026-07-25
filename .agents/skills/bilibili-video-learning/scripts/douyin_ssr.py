#!/usr/bin/env python3
"""
Douyin public SSR download path.

This file handles only public, anonymous Douyin share pages. It accepts a user
provided URL, aweme_id, or app share text, follows the public share redirect,
reads the server-rendered page, extracts the public play token, probes the play
endpoint with tiny ranged GETs, chooses an available public ratio, and downloads
the video file for transcription.

It deliberately does not use login cookies, private APIs, paid/private content
workarounds, or stored credentials.

Example:
    python douyin_ssr.py "https://v.douyin.com/example/" --ratio 720p -o out.mp4
"""
import argparse                         # Parses the standalone diagnostic CLI.
import html                             # Decodes escaped URLs from share text and SSR HTML.
import json                             # Reads Douyin JSON blobs embedded in SSR pages.
import os                               # Creates parent folders and checks downloaded size.
import re                               # Normalizes the many public Douyin URL shapes.
import sys                              # Returns explicit command-line exit codes.
import urllib.parse                     # Decodes URL-encoded RENDER_DATA script payloads.
from typing import Any, Dict, Iterable, List, Optional

import requests                         # Performs the public GET/POST requests used by SSR.


MOBILE_WECHAT_UA = (                    # WeChat mobile UA asks Douyin for the SSR share page.
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.53"
)
RATIOS = ("1080p", "720p", "540p", "360p")  # Public play endpoint accepts these ratio labels.
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
PLAY_REFERER = "https://www.douyin.com/"     # CDN accepts this public referer for ranged GET probes.


# --- Carry SSR diagnostics across module boundaries ---
class DouyinSSRDownloadError(RuntimeError):
    def __init__(self, message: str, diagnostics: List[Dict[str, Any]]):
        super().__init__(message)
        self.diagnostics = diagnostics                         # Caller JSON can show the exact failed SSR step.


# --- Record one visible diagnostic step ---
def add_diagnostic(diagnostics: List[Dict[str, Any]], step: str, ok: bool, message: str, **data: Any) -> None:
    diagnostics.append({                    # Each step stays JSON-serializable for caller output.
        "step": step,                       # Human-readable phase name shown in failure reports.
        "ok": ok,                           # Boolean status lets callers filter failed steps quickly.
        "message": message,                 # Message explains what worked or where the chain broke.
        **data,                             # Optional fields keep URLs/status codes near the message.
    })


# --- Build the public share-page URL for a known aweme ---
def canonical_url_for_aweme(aweme_id: str) -> str:
    return f"https://www.iesdouyin.com/share/video/{aweme_id}/"  # iesdouyin is the stable public SSR host.


# --- Pull the first Douyin URL out of app share text ---
def extract_first_douyin_url(source_text: str) -> Optional[str]:
    decoded_text = html.unescape(source_text).strip()           # Share text often contains escaped URL marks.
    url_matches = re.findall(r"https?://[^\s\"'<>]+", decoded_text)

    for raw_url in url_matches:                                 # App share text may include non-Douyin links too.
        clean_url = raw_url.rstrip("，。,.!！)）]】\"'")           # Chinese app text often sticks punctuation to URLs.
        if "douyin.com" in clean_url or "iesdouyin.com" in clean_url:
            return clean_url

    return None                                                 # The caller reports a parse failure with context.


# --- Normalize URL, share text, or bare ID into an aweme candidate ---
def parse_input(source_text: str) -> Dict[str, Optional[str]]:
    cleaned_text = html.unescape(source_text).strip()           # Normalize copied app text before regex matching.

    if re.fullmatch(r"\d{10,25}", cleaned_text):                # Public aweme_id values are long numeric IDs.
        return {
            "input_type": "bare_aweme_id",
            "source_url": None,
            "aweme_id": cleaned_text,
            "canonical_url": canonical_url_for_aweme(cleaned_text),
        }

    first_url = extract_first_douyin_url(cleaned_text) or cleaned_text.rstrip("，。,.!！)）]】\"'")
    video_match = re.search(r"(?:douyin\.com|iesdouyin\.com)/(?:share/)?video/(\d+)", first_url)
    if video_match:                                             # Long video/share URLs already expose aweme_id.
        aweme_id = video_match.group(1)
        return {
            "input_type": "video_url",
            "source_url": first_url,
            "aweme_id": aweme_id,
            "canonical_url": canonical_url_for_aweme(aweme_id),
        }

    short_match = re.search(r"https?://(?:v\.)?douyin\.com/[^\s\"'<>]+", first_url)
    if short_match:                                             # Short links need one public redirect resolution.
        return {
            "input_type": "short_url",
            "source_url": short_match.group(0).rstrip("，。,.!！)）]】\"'"),
            "aweme_id": None,
            "canonical_url": None,
        }

    return {                                                    # The caller keeps this object in diagnostics.
        "input_type": "unknown",
        "source_url": first_url if first_url.startswith("http") else None,
        "aweme_id": None,
        "canonical_url": None,
    }


# --- Create a session with public mobile SSR headers ---
def create_public_session(ttwid_cookie: Optional[str] = None) -> requests.Session:
    session = requests.Session()                                # Cookie jar carries anonymous ttwid across steps.
    session.headers.update({
        "User-Agent": MOBILE_WECHAT_UA,
        "Referer": PLAY_REFERER,
    })
    if ttwid_cookie:                                            # The value is anonymous and generated per run.
        session.headers.update({"Cookie": ttwid_cookie})
    return session


# --- Request an anonymous ttwid cookie ---
def get_ttwid(timeout: int = 10) -> str:
    register_payload = {                                        # This public endpoint issues an anonymous visitor ID.
        "region": "cn",
        "aid": 1128,
        "needFid": False,
        "service": "www.douyin.com",
        "migrate_info": {"ticket": "", "source": "node"},
        "cbUrlProtocol": "https",
        "union": True,
    }
    response = requests.post(TTWID_REGISTER_URL, json=register_payload, timeout=timeout)

    for cookie in response.cookies:                             # requests may parse Set-Cookie for us.
        if cookie.name == "ttwid":
            return f"ttwid={cookie.value}"

    set_cookie = response.headers.get("set-cookie", "")         # Some responses only expose the raw header.
    cookie_match = re.search(r"ttwid=([^;]+)", set_cookie)
    if cookie_match:
        return f"ttwid={cookie_match.group(1)}"

    raise RuntimeError("ttwid request returned no anonymous ttwid cookie")


# --- Resolve a short link or confirm a known aweme_id ---
def resolve_public_input(source_text: str, session: requests.Session, diagnostics: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    parsed_input = parse_input(source_text)
    add_diagnostic(diagnostics, "parse_input", parsed_input["aweme_id"] is not None or parsed_input["input_type"] == "short_url", "Parsed user input", **parsed_input)

    if parsed_input["aweme_id"]:                                # Long URLs and bare IDs already have the needed ID.
        return parsed_input

    if parsed_input["input_type"] != "short_url" or not parsed_input["source_url"]:
        raise RuntimeError("Could not find a Douyin video URL or aweme_id in the input")

    response = session.get(parsed_input["source_url"], allow_redirects=True, timeout=12)
    resolved_url = response.url
    resolved_input = parse_input(resolved_url)
    add_diagnostic(diagnostics, "resolve_short_url", bool(resolved_input["aweme_id"]), "Resolved public short link", source_url=parsed_input["source_url"], resolved_url=resolved_url, status_code=response.status_code)

    if not resolved_input["aweme_id"]:
        raise RuntimeError(f"Short link did not resolve to a public video page: {resolved_url}")

    return resolved_input


# --- Fetch the public SSR share page ---
def fetch_share_page(aweme_id: str, session: requests.Session, diagnostics: List[Dict[str, Any]]) -> Dict[str, str]:
    candidate_urls = [                                          # iesdouyin is preferred, douyin is kept as fallback.
        canonical_url_for_aweme(aweme_id),
        f"https://www.douyin.com/share/video/{aweme_id}/",
        f"https://www.douyin.com/video/{aweme_id}",
    ]
    last_error = None

    for share_url in candidate_urls:
        try:
            response = session.get(share_url, allow_redirects=True, timeout=15)
            page_html = response.text or ""
            has_payload = bool(page_html.strip())
            add_diagnostic(diagnostics, "fetch_share_page", response.status_code < 400 and has_payload, "Fetched public SSR share page", share_url=share_url, final_url=response.url, status_code=response.status_code, html_bytes=len(response.content or b""))
            if response.status_code < 400 and has_payload:
                return {"html": page_html, "canonical_url": response.url or share_url}
        except Exception as exc:
            last_error = exc
            add_diagnostic(diagnostics, "fetch_share_page", False, f"Share page request failed: {exc}", share_url=share_url)

    raise RuntimeError(f"Could not fetch a public Douyin share page for aweme_id={aweme_id}: {last_error}")


# --- Extract JSON objects from common Douyin SSR script forms ---
def iter_embedded_json(page_html: str) -> Iterable[Any]:
    route_match = re.search(r"window\._ROUTER_DATA\s*=\s*", page_html)
    if route_match:                                             # Newer SSR pages expose a JS object assignment.
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(page_html[route_match.end():])
            yield payload
        except Exception:
            pass

    for render_match in re.finditer(r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', page_html, re.S):
        raw_payload = html.unescape(render_match.group(1))
        decoded_payload = urllib.parse.unquote(raw_payload)
        try:
            yield json.loads(decoded_payload)
        except Exception:
            continue


# --- Walk SSR JSON until a public play token appears ---
def find_video_token_in_json(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("play_addr", "playwm_addr", "download_addr"):
            address_data = payload.get(key)
            if isinstance(address_data, dict) and address_data.get("uri"):
                return str(address_data["uri"])                 # play_addr.uri is the token accepted by /play/.
        for value in payload.values():
            token = find_video_token_in_json(value)
            if token:
                return token

    if isinstance(payload, list):
        for item in payload:
            token = find_video_token_in_json(item)
            if token:
                return token

    if isinstance(payload, str):
        token_match = re.search(r"video_id=([A-Za-z0-9_\-=]+)", payload)
        if token_match:
            return token_match.group(1)

    return None


# --- Extract the public play token from SSR HTML ---
def extract_video_token(page_html: str) -> str:
    direct_patterns = [
        r"video_id=([A-Za-z0-9_\-=]+)",
        r"aweme/v1/play[^\"'<> ]*video_id=([A-Za-z0-9_\-=]+)",
        r'"uri"\s*:\s*"([A-Za-z0-9_\-=]{12,})"',
    ]
    for pattern in direct_patterns:
        match = re.search(pattern, page_html)
        if match:
            return html.unescape(match.group(1))

    for payload in iter_embedded_json(page_html):
        token = find_video_token_in_json(payload)
        if token:
            return token

    raise RuntimeError("Public share page did not expose a video_id/play token")


# --- Extract small metadata that does not require yt-dlp ---
def extract_page_metadata(page_html: str) -> Dict[str, str]:
    title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page_html, re.I)
    if not title_match:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S)

    description_match = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', page_html, re.I)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else ""
    description = html.unescape(description_match.group(1).strip()) if description_match else ""

    return {
        "title": title.replace(" - 抖音", "").strip(),
        "description": description,
    }


# --- Build the public play endpoint URL ---
def build_play_url(video_id: str, ratio: str = "1080p", watermark: bool = False) -> str:
    if ratio not in RATIOS:
        raise ValueError(f"Unsupported ratio '{ratio}'. Choose one of: {', '.join(RATIOS)}")

    endpoint = "playwm" if watermark else "play"
    return f"https://aweme.snssdk.com/aweme/v1/{endpoint}/?video_id={video_id}&ratio={ratio}&line=0"


# --- Read the full file size from a ranged GET response ---
def content_range_size(content_range: str) -> Optional[int]:
    size_match = re.search(r"/(\d+)$", content_range or "")       # CDN replies like "bytes 0-1/123456".
    if size_match:                                                # A real size lets us compare ratio variants.
        return int(size_match.group(1))
    return None                                                   # Some CDNs omit Content-Range even when usable.


# --- Probe the CDN with ranged GET instead of fragile HEAD ---
def probe_play_url(play_url: str, session: requests.Session, diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    probe_headers = {"Range": "bytes=0-1", "Referer": PLAY_REFERER}
    response = session.get(play_url, headers=probe_headers, allow_redirects=True, stream=True, timeout=12)
    response.close()

    content_type = response.headers.get("content-type", "")
    content_range = response.headers.get("content-range", "")
    file_size = content_range_size(content_range)
    is_video_like = "video" in content_type or bool(content_range) or response.url.endswith(".mp4")
    is_success = response.status_code < 400 and is_video_like
    add_diagnostic(diagnostics, "probe_play_url", is_success, "Probed play endpoint with Range bytes=0-1", play_url=play_url, final_url=response.url, status_code=response.status_code, content_type=content_type, content_range=content_range, file_size=file_size)

    if not is_success:
        raise RuntimeError(f"Play endpoint probe failed: HTTP {response.status_code}, content-type={content_type or 'unknown'}")

    return {
        "play_url": play_url,
        "final_url": response.url,
        "status_code": response.status_code,
        "content_type": content_type,
        "content_range": content_range,
        "file_size": file_size,
    }


# --- Probe all public ratios so callers can see real availability ---
def probe_play_ratios(video_id: str, session: requests.Session, diagnostics: List[Dict[str, Any]], watermark: bool = False) -> List[Dict[str, Any]]:
    ratio_results: List[Dict[str, Any]] = []                      # Returned list becomes the operator-facing matrix.
    seen_file_keys: Dict[str, str] = {}                            # Same file size/final URL usually means same quality.

    for ratio in RATIOS:
        play_url = build_play_url(video_id, ratio=ratio, watermark=watermark)
        try:
            probe = probe_play_url(play_url, session, diagnostics)
            file_key = str(probe.get("file_size") or probe.get("final_url") or play_url)
            previous_ratio = seen_file_keys.get(file_key)
            seen_file_keys.setdefault(file_key, ratio)
            ratio_results.append({
                "ratio": ratio,                                   # Requested ratio label.
                "ok": True,                                       # This ratio returned a usable video-like response.
                "file_size": probe.get("file_size"),              # Distinct sizes usually mark distinct renditions.
                "content_range": probe.get("content_range", ""),  # Kept for manual CDN diagnostics.
                "final_url": probe.get("final_url", ""),          # Useful when size is missing.
                "is_distinct": previous_ratio is None,            # False means CDN returned same payload as another ratio.
                "same_as": previous_ratio,                        # Shows which earlier ratio matched this response.
            })
        except Exception as exc:
            add_diagnostic(diagnostics, "probe_ratio", False, f"Ratio probe failed: {exc}", ratio=ratio, play_url=play_url)
            ratio_results.append({
                "ratio": ratio,                                   # Failed ratio still appears in the matrix.
                "ok": False,                                      # Caller can decide whether to fall back.
                "error": str(exc),                                # Preserve CDN/HTTP failure reason.
            })

    return ratio_results


# --- Choose the requested ratio or the nearest lower available ratio ---
def choose_play_url(video_id: str, ratio: str, watermark: bool, session: requests.Session, diagnostics: List[Dict[str, Any]]) -> Dict[str, Any]:
    start_index = RATIOS.index(ratio)                              # A lower requested ratio should not silently upgrade.
    last_error = None

    for candidate_ratio in RATIOS[start_index:]:
        play_url = build_play_url(video_id, ratio=candidate_ratio, watermark=watermark)
        try:
            probe = probe_play_url(play_url, session, diagnostics)
            if candidate_ratio != ratio:
                add_diagnostic(diagnostics, "fallback_ratio", True, "Requested ratio failed; using the next available lower ratio", requested_ratio=ratio, selected_ratio=candidate_ratio)
            return {
                "requested_ratio": ratio,                          # What the user/script asked for.
                "ratio": candidate_ratio,                          # What will actually be downloaded.
                "play_url": play_url,                              # Public play endpoint for the selected ratio.
                "probe": probe,                                    # Probe evidence before the full download.
            }
        except Exception as exc:
            last_error = exc
            add_diagnostic(diagnostics, "select_ratio", False, f"Requested ratio candidate failed: {exc}", requested_ratio=ratio, candidate_ratio=candidate_ratio, play_url=play_url)

    raise RuntimeError(f"No public play ratio worked for requested ratio {ratio}: {last_error}")


# --- Download the probed public video file ---
def download_video_file(play_url: str, output_path: str, session: requests.Session, diagnostics: List[Dict[str, Any]]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    response = session.get(play_url, headers={"Referer": PLAY_REFERER}, allow_redirects=True, stream=True, timeout=180)

    if response.status_code >= 400:
        add_diagnostic(diagnostics, "download_video", False, "Video GET returned an HTTP error", status_code=response.status_code, final_url=response.url)
        raise RuntimeError(f"Video download failed: HTTP {response.status_code}")

    with open(output_path, "wb") as video_file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                video_file.write(chunk)                         # Streaming avoids holding large videos in memory.

    file_size = os.path.getsize(output_path)
    is_valid_size = file_size > 1000
    add_diagnostic(diagnostics, "download_video", is_valid_size, "Downloaded public video file", output_path=output_path, final_url=response.url, file_size=file_size)

    if not is_valid_size:
        raise RuntimeError(f"Downloaded file is too small to be a video: {file_size} bytes")

    return output_path


# --- Run the full public SSR chain ---
def download_public_video(source_text: str, output_path: str, ratio: str = "1080p", watermark: bool = False) -> Dict[str, Any]:
    diagnostics: List[Dict[str, Any]] = []

    try:
        ttwid_cookie = get_ttwid()
        add_diagnostic(diagnostics, "get_ttwid", True, "Acquired anonymous ttwid cookie")
        session = create_public_session(ttwid_cookie)

        resolved_input = resolve_public_input(source_text, session, diagnostics)
        aweme_id = resolved_input["aweme_id"]
        if not aweme_id:
            raise RuntimeError("Resolved input does not contain aweme_id")

        share_page = fetch_share_page(aweme_id, session, diagnostics)
        video_id = extract_video_token(share_page["html"])
        page_metadata = extract_page_metadata(share_page["html"])
        add_diagnostic(diagnostics, "extract_video_token", True, "Extracted public play token from SSR page", aweme_id=aweme_id, video_id=video_id)

        selected_play = choose_play_url(video_id, ratio, watermark, session, diagnostics)
        download_video_file(selected_play["play_url"], output_path, session, diagnostics)

        return {
            "download_method": "ssr",
            "video_path": output_path,
            "canonical_url": share_page["canonical_url"],
            "aweme_id": aweme_id,
            "video_id": video_id,
            "requested_ratio": selected_play["requested_ratio"],
            "ratio": selected_play["ratio"],
            "watermark": watermark,
            "metadata": page_metadata,
            "probe": selected_play["probe"],
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        if not diagnostics or diagnostics[-1].get("ok") is True:
            add_diagnostic(diagnostics, "ssr_pipeline", False, f"SSR public chain failed: {exc}")
        raise DouyinSSRDownloadError(str(exc), diagnostics) from exc


# --- Inspect public metadata without downloading the media file ---
def inspect_public_metadata(source_text: str) -> Dict[str, Any]:
    diagnostics: List[Dict[str, Any]] = []                        # 返回值保留每个公开请求步骤。

    try:
        ttwid_cookie = get_ttwid()                                # 匿名访客 Cookie 只存在于当前进程。
        add_diagnostic(diagnostics, "get_ttwid", True, "Acquired anonymous ttwid cookie")
        session = create_public_session(ttwid_cookie)             # 同一会话解析短链并读取 SSR 页面。

        resolved_input = resolve_public_input(source_text, session, diagnostics)
        aweme_id = resolved_input["aweme_id"]
        if not aweme_id:                                          # 无真实 ID 时不能生成可复用来源身份。
            raise RuntimeError("Resolved input does not contain aweme_id")

        share_page = fetch_share_page(aweme_id, session, diagnostics)
        video_id = extract_video_token(share_page["html"])         # 播放 token 证明页面含公开媒体引用。
        page_metadata = extract_page_metadata(share_page["html"])  # 标题与简介只来自 SSR HTML。
        add_diagnostic(
            diagnostics,
            "inspect_metadata",
            True,
            "Extracted public Douyin metadata without downloading media",
            aweme_id=aweme_id,
            video_id=video_id,
        )
        return {
            "platform": "douyin",                                 # 统一 CLI 依靠平台字段渲染反馈。
            "canonical_url": share_page["canonical_url"],         # 保留平台确认过的公开来源。
            "aweme_id": aweme_id,
            "video_id": video_id,
            "metadata": page_metadata,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        if not diagnostics or diagnostics[-1].get("ok") is True:
            add_diagnostic(diagnostics, "metadata_pipeline", False, f"Public metadata inspection failed: {exc}")
        raise DouyinSSRDownloadError(str(exc), diagnostics) from exc


# --- Inspect public ratios without downloading the full media file ---
def inspect_public_ratios(source_text: str, watermark: bool = False) -> Dict[str, Any]:
    diagnostics: List[Dict[str, Any]] = []

    try:
        ttwid_cookie = get_ttwid()
        add_diagnostic(diagnostics, "get_ttwid", True, "Acquired anonymous ttwid cookie")
        session = create_public_session(ttwid_cookie)

        resolved_input = resolve_public_input(source_text, session, diagnostics)
        aweme_id = resolved_input["aweme_id"]
        if not aweme_id:
            raise RuntimeError("Resolved input does not contain aweme_id")

        share_page = fetch_share_page(aweme_id, session, diagnostics)
        video_id = extract_video_token(share_page["html"])
        page_metadata = extract_page_metadata(share_page["html"])  # 画质矩阵同时返回同一页面元数据。
        add_diagnostic(diagnostics, "extract_video_token", True, "Extracted public play token from SSR page", aweme_id=aweme_id, video_id=video_id)

        ratio_results = probe_play_ratios(video_id, session, diagnostics, watermark=watermark)
        return {
            "platform": "douyin",
            "canonical_url": share_page["canonical_url"],
            "aweme_id": aweme_id,
            "video_id": video_id,
            "metadata": page_metadata,
            "watermark": watermark,
            "ratios": ratio_results,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        if not diagnostics or diagnostics[-1].get("ok") is True:
            add_diagnostic(diagnostics, "ratio_probe_pipeline", False, f"Public ratio probe failed: {exc}")
        raise DouyinSSRDownloadError(str(exc), diagnostics) from exc


# --- Standalone diagnostic CLI ---
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download a public Douyin video through the anonymous SSR share path.")
    parser.add_argument("source", nargs="?", help="Douyin URL, aweme_id, or app share text")
    parser.add_argument("-o", "--output", default="douyin_ssr.mp4", help="Output mp4 path")
    parser.add_argument("--ratio", default="1080p", choices=RATIOS, help="Requested public play ratio")
    parser.add_argument("--watermark", action="store_true", help="Use playwm instead of play")
    parser.add_argument("--parse-only", action="store_true", help="Only parse input; do not make network requests")
    parser.add_argument("--inspect", action="store_true", help="Read public SSR metadata without downloading media")
    parser.add_argument("--list-ratios", action="store_true", help="Probe 1080p/720p/540p/360p with ranged GET and print availability")
    args = parser.parse_args(argv)                                 # 测试可传参数；真实命令默认读取 sys.argv。

    if not args.source:
        parser.print_help()
        return 1

    if args.parse_only:
        print(json.dumps(parse_input(args.source), ensure_ascii=False, indent=2))
        return 0

    try:
        if args.list_ratios:
            result = inspect_public_ratios(args.source, watermark=args.watermark)  # Range 探测优先于普通元数据模式。
        elif args.inspect:
            result = inspect_public_metadata(args.source)                         # 不探测画质，也不下载媒体。
        else:
            result = download_public_video(args.source, args.output, ratio=args.ratio, watermark=args.watermark)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({
            "error": str(exc),
            "diagnostics": getattr(exc, "diagnostics", []),
        }, ensure_ascii=False, indent=2))                          # stdout 保持一个 JSON，供 harness 解析失败原因。
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
