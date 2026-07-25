#!/usr/bin/env python3
"""Normalize Bilibili video URLs — expand short links, extract BV/av IDs."""
import re
import json
import sys
import urllib.request
import urllib.error


# --- 只要输入显式携带分 P，就必须验证，而不是把错误请求改成 P1 ---
def extract_requested_page(url: str) -> int:
    page_match = re.search(r'[?&]p=([^&#\s]*)', url, flags=re.IGNORECASE)
    if not page_match:
        return 1
    raw_page = page_match.group(1)
    if not raw_page.isdigit() or int(raw_page) < 1:
        raise ValueError(f"Invalid Bilibili page parameter: p={raw_page or '<empty>'}")
    return int(raw_page)


def normalize(url: str) -> dict:
    result = {
        "source_url": url,
        "canonical_url": None,
        "platform": "bilibili",
        "bvid": None,
        "aid": None,
        "page": 1,
        "start_time_seconds": 0,
        "is_short_url": False,
    }

    url = url.strip()

    # Check for b23.tv short link
    b23_match = re.match(r'https?://b23\.tv/([a-zA-Z0-9]+)', url)
    if b23_match:
        result["is_short_url"] = True
        try:
            req = urllib.request.Request(url, method='HEAD')
            resp = urllib.request.urlopen(req, timeout=10)
            url = resp.geturl()
            result["canonical_url"] = url
        except Exception:
            result["canonical_url"] = url  # Keep short URL if can't expand
            return result

    # Extract BV ID
    bv_match = re.search(r'(BV[a-zA-Z0-9]{10})', url)
    if bv_match:
        result["bvid"] = bv_match.group(1)

    # Extract av number
    av_match = re.search(r'/av(\d+)', url)
    if av_match:
        result["aid"] = int(av_match.group(1))

    # 显式错误的 p= 不能被默认值掩盖；后续 inspect 会继续验证该页是否真实存在。
    result["page"] = extract_requested_page(url)

    # Extract time parameter
    t_match = re.search(r'[?&]t=(\d+)', url)
    if t_match:
        result["start_time_seconds"] = int(t_match.group(1))

    # Build canonical URL
    if result["bvid"]:
        result["canonical_url"] = f"https://www.bilibili.com/video/{result['bvid']}"
        if result["page"] > 1:
            result["canonical_url"] += f"?p={result['page']}"
    elif result["aid"]:
        result["canonical_url"] = f"https://www.bilibili.com/video/av{result['aid']}"
        if result["page"] > 1:
            result["canonical_url"] += f"?p={result['page']}"
    elif not result["canonical_url"]:
        result["canonical_url"] = url

    return result

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python normalize_bilibili_url.py <url>")
        sys.exit(1)

    try:
        result = normalize(sys.argv[1])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        sys.exit(2)
