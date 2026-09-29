#!/usr/bin/env python3
r"""
抖音取流（浏览器版）：把 fetch 放进真实页面上下文，让站点自己的 JS 去算签名。

为什么用这个思路（模仿已经跑通的做法，但**不依赖、不耦合**另一个仓库）：
    抖音匿名 SSR 路径会被平台风控降级——实测 12 个 host×UA 组合全部返回「验证码中间页」，
    `videoInfoRes` 只剩 `status_code`；匿名调 Web API 则 403
    `Blocked by ArgusSecurityPlugin Uifid Not Found`。原因是 Web API 现在要求
    `a_bogus + timestamp + x-secsdk-web-signature` 三件套，而它们只能由站点自己的前端 JS 算出。
    既有的做法是"在真实浏览器上下文里发这个请求"——本模块用**同样的原理**自己实现一遍：
    启动/复用一个 Chromium 系浏览器，通过 CDP（Chrome DevTools Protocol）在页面里
    `Runtime.evaluate` 一个 `fetch(..., {credentials:'include'})`，于是签名与登录态都由页面提供。

    与另一个仓库的关系：**只参考思路，不调用它的服务、不复制它的代码**。本模块只依赖标准库
    （自带的极简 WebSocket 客户端）+ 本机已装的 Chrome/Edge。

边界：
    - 用**专用登录态目录**（`--profile`），默认不碰用户主浏览器配置；首次需要在该目录里登录一次抖音；
    - 单文件 base64 回传上限 `MAX_TRANSFER_BYTES`（默认 120MB），超限明确报错而不是 OOM；
    - 浏览器不可达/未登录时给出可执行提示，绝不静默降级成"空结果"；
    - 不做验证码识别、不绕过登录墙：命中风控就如实失败。

调用示例：
    python douyin_browser_fetch.py --aweme-id 7690619057690828986 --json
    python douyin_browser_fetch.py --url https://v.douyin.com/xxx/ -o /tmp/v.mp4
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import argparse  # 稳定命令行契约：既能被 douyin_extract 调用，也能单独排查
import base64  # 媒体回传用 base64（CDP 只能传字符串）
import hashlib  # WebSocket 握手需要 Sec-WebSocket-Key/Accept
import json  # CDP 与桥接式返回值都是 JSON
import os  # 查找浏览器与运行外部进程
import platform  # 决定默认的浏览器路径候选
import re  # 解析 URL 里的 aweme_id
import socket  # 极简 WebSocket 客户端直接用 TCP
import struct  # 帧头打包
import subprocess  # 启动浏览器
import sys  # 退出码与 stdout 契约
import time  # 轮询页面就绪
import urllib.parse  # 拼详情接口查询串、解析 ws:// URL
import urllib.request  # CDP 的 HTTP 元信息端点（本机地址需绕过代理）
from typing import Any, Dict, List, Optional

from runtime_output import log  # 进度写 stderr，业务结果留在 stdout


PROFILE_ENV_VAR = "DOUYIN_BROWSER_PROFILE"       # 专用登录态目录（不想每次写命令行）
PORT_ENV_VAR = "DOUYIN_BROWSER_PORT"             # CDP 端口
DEFAULT_PORT = 9333                              # 避开插件桥接常用的 8765/9222，减少冲突
MAX_TRANSFER_BYTES = 120 * 1024 * 1024           # base64 回传上限：视频再大就该走别的通道
NAVIGATE_TIMEOUT_S = 25                          # 页面就绪等待上限
EVALUATE_TIMEOUT_S = 90                          # 含媒体下载的宽裕上限
DETAIL_PATH = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
DETAIL_PARAMS = {  # 与已跑通的做法逐字一致的参数集：少一个都可能只拿到 status_code=0 而拿不到详情
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "version_code": "170400",
}
AWEME_ID_PATTERN = re.compile(r"(?:/video/|modal_id=|aweme_id=|^)(\d{15,25})")
BROWSER_CANDIDATES = {
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
    "Linux": ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"],
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
}
LAUNCH_HINT = ("未找到可用浏览器。用 --browser 指定 Chrome/Edge 可执行文件，"
               "或先手动启动：chrome --remote-debugging-port=<port> --user-data-dir=<专用目录>")


# --- 浏览器/CDP 相关失败：与"取流被拒"分开，提示要能直接照做 ---
class BrowserUnavailableError(RuntimeError):
    """找不到浏览器，或 CDP 端口连不上。"""


# --- 极简 WebSocket 客户端（RFC6455 客户端帧，仅文本帧 + ping/pong）---
class MiniWebSocket:
    def __init__(self, url: str, timeout: int = EVALUATE_TIMEOUT_S):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise BrowserUnavailableError(f"只支持 ws:// 的 CDP 端点，收到 {url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserUnavailableError("WebSocket 握手过程中连接被关闭")
            header += chunk
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                                .encode()).digest()).decode()
        if b"101" not in header.split(b"\r\n")[0] or expected.encode() not in header:
            raise BrowserUnavailableError("WebSocket 握手被拒绝（CDP 端点是否正确？）")

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])                                   # FIN + 文本帧
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)                                          # 客户端必须掩码
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, count: int) -> bytes:
        buffer = b""
        while len(buffer) < count:
            chunk = self.sock.recv(count - len(buffer))
            if not chunk:
                raise BrowserUnavailableError("WebSocket 连接在读取过程中关闭")
            buffer += chunk
        return buffer

    def recv_text(self) -> str:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b""
            if opcode == 0x9:                                         # ping → pong，保持连接
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode in (0x8, 0xA):                                  # close / pong
                if opcode == 0x8:
                    raise BrowserUnavailableError("CDP 连接被关闭")
                continue
            return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


# --- CDP 会话：扁平的 sessionId 模式，足够 createTarget/attach/navigate/evaluate ---
class CdpSession:
    def __init__(self, websocket: MiniWebSocket):
        self.ws = websocket
        self.next_id = 0

    def call(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self.ws.send_text(json.dumps(message))
        while True:                                                   # 跳过事件，只认自己 id 的回复
            payload = json.loads(self.ws.recv_text())
            if payload.get("id") != self.next_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"CDP {method} 失败：{payload['error']}")
            return payload.get("result") or {}


# --- 找浏览器可执行文件 ---
def find_browser(explicit: Optional[str] = None) -> str:
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise BrowserUnavailableError(f"指定的浏览器不存在：{explicit}")
    for candidate in BROWSER_CANDIDATES.get(platform.system(), []):
        if os.path.exists(candidate):
            return candidate
    raise BrowserUnavailableError(LAUNCH_HINT)


# --- 读取 CDP 的 WebSocket 端点（浏览器级） ---
def _http_get(url: str, timeout: int = 5) -> str:
    # 本机地址必须绕过代理：环境里存在 http_proxy 时，urllib 会把 127.0.0.1 也发给代理而失败。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def browser_ws_endpoint(port: int, timeout: int = 5) -> Optional[str]:
    try:
        payload = json.loads(_http_get(f"http://127.0.0.1:{port}/json/version", timeout=timeout))
        return payload.get("webSocketDebuggerUrl")
    except Exception:
        return None


# --- 确保有一个可连的 CDP 端点：优先复用，其次自己启动 ---
def ensure_browser(port: int = DEFAULT_PORT, browser: Optional[str] = None,
                   profile: Optional[str] = None) -> Dict[str, Any]:
    existing = browser_ws_endpoint(port)
    if existing:
        log(f"[browser] reusing CDP on port {port}")
        return {"ws": existing, "launched": False}
    executable = find_browser(browser)
    profile_dir = profile or os.environ.get(PROFILE_ENV_VAR) or os.path.join(
        os.path.expanduser("~"), ".cache", "douyin-browser-profile")
    os.makedirs(profile_dir, exist_ok=True)
    command = [executable, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
               "--no-first-run", "--no-default-browser-check", "--disable-background-networking"]
    if platform.system() != "Windows":                                # WSL/Windows 下 headless 容易被拦，交给用户选择
        command.append("--headless=new")
    log(f"[browser] launching {os.path.basename(executable)} profile={profile_dir}")
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):                                               # 最多等 15 秒
        endpoint = browser_ws_endpoint(port)
        if endpoint:
            return {"ws": endpoint, "launched": True}
        time.sleep(0.5)
    raise BrowserUnavailableError(f"浏览器起来了但 CDP 端口 {port} 不通。{LAUNCH_HINT}")


# --- 打开一个页面并等它就绪 ---
def open_page(session: CdpSession, url: str) -> str:
    target = session.call("Target.createTarget", {"url": "about:blank"})
    attached = session.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
    session_id = attached["sessionId"]
    session.call("Page.enable", session_id=session_id)
    session.call("Page.navigate", {"url": url}, session_id=session_id)
    deadline = time.time() + NAVIGATE_TIMEOUT_S
    while time.time() < deadline:
        state = session.call("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True},
                             session_id=session_id)
        if (state.get("result") or {}).get("value") == "complete":
            return session_id
        time.sleep(0.5)
    return session_id                                                 # 超时也让调用方去执行，由业务判空


# --- 在页面里求值（awaitPromise 支持 async IIFE，返回值必须可 JSON 序列化）---
def evaluate(session: CdpSession, session_id: str, expression: str) -> Any:
    result = session.call("Runtime.evaluate",
                          {"expression": expression, "awaitPromise": True, "returnByValue": True},
                          session_id=session_id)
    if result.get("exceptionDetails"):
        raise RuntimeError(f"页面内求值异常：{str(result['exceptionDetails'])[:200]}")
    return (result.get("result") or {}).get("value")


# --- 解析输入里的 aweme_id（接受链接、分享文本或裸 ID） ---
def extract_aweme_id(source: str) -> Optional[str]:
    match = AWEME_ID_PATTERN.search(source or "")
    return match.group(1) if match else None


# --- 从 aweme_detail 挑播放地址（档位优先，其次默认） ---
def pick_play_url(detail: Dict[str, Any], ratio: str = "1080p") -> Dict[str, Any]:
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    by_gear = {}
    for entry in video.get("bit_rate") or []:
        if isinstance(entry, dict) and entry.get("gear_name") and (entry.get("play_addr") or {}).get("url_list"):
            by_gear[str(entry["gear_name"])] = entry["play_addr"]["url_list"][0]
    default_urls = (video.get("play_addr") or {}).get("url_list") or []
    play_url = by_gear.get(ratio) or (default_urls[0] if default_urls else None)
    if not play_url:
        raise RuntimeError("aweme_detail 里没有可用播放地址（可能是图文作品或已删除）")
    return {"play_url": play_url, "ratio": ratio if play_url == by_gear.get(ratio) else "default",
            "available_ratios": sorted(by_gear)}


# --- 详情接口的查询串 ---
def detail_query(aweme_id: str) -> str:
    params = dict(DETAIL_PARAMS)
    params["aweme_id"] = str(aweme_id)
    return urllib.parse.urlencode(params)


# --- 取详情：在页面上下文里 fetch，签名由站点 JS 自己算 ---
def fetch_detail(session: CdpSession, session_id: str, aweme_id: str) -> Dict[str, Any]:
    expression = (
        "(async () => {"
        f"  const r = await fetch('{DETAIL_PATH}?{detail_query(aweme_id)}', {{credentials: 'include'}});"
        "   const t = await r.text();"
        "   return {status: r.status, text: t};"
        "})()"
    )
    payload = evaluate(session, session_id, expression) or {}
    if int(payload.get("status") or 0) != 200:
        raise RuntimeError(f"页面内请求详情接口被拒：HTTP {payload.get('status')} {str(payload.get('text'))[:120]}")
    parsed = json.loads(payload.get("text") or "{}")
    detail = parsed.get("aweme_detail")
    code = int(parsed.get("status_code", -1))
    if code != 0 or not isinstance(detail, dict):
        raise RuntimeError(f"抖音详情接口 status_code={code}（{parsed.get('status_msg') or '无消息'}；"
                           "若为 0 但无 aweme_detail，通常表示该浏览器 profile 未登录抖音）")
    return detail


# --- 下载媒体：同样在页面里 fetch，再以 base64 回传 ---
def download_media(session: CdpSession, session_id: str, play_url: str, output_path: str) -> str:
    expression = (
        "(async () => {"
        f"  const r = await fetch('{play_url}', {{credentials: 'include'}});"
        "   const b = await r.arrayBuffer();"
        f"  if (b.byteLength > {MAX_TRANSFER_BYTES}) return {{status: r.status, error: 'too_large', bytes: b.byteLength}};"
        "   const bytes = new Uint8Array(b); let bin = '';"
        "   for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));"
        "   return {status: r.status, b64: btoa(bin), bytes: b.byteLength};"
        "})()"
    )
    payload = evaluate(session, session_id, expression) or {}
    if payload.get("error") == "too_large":
        raise RuntimeError(f"媒体超过上限 {MAX_TRANSFER_BYTES // (1024 * 1024)}MB（{payload.get('bytes')} 字节）")
    if int(payload.get("status") or 0) not in (200, 206) or not payload.get("b64"):
        raise RuntimeError(f"页面内下载媒体失败：HTTP {payload.get('status')}")
    with open(output_path, "wb") as handle:
        handle.write(base64.b64decode(payload["b64"]))
    log(f"[browser] saved {payload.get('bytes')} bytes -> {os.path.basename(output_path)}")
    return output_path


# --- 元数据映射成与其它取流方式一致的形状 ---
def metadata_from_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
    stats = detail.get("statistics") if isinstance(detail.get("statistics"), dict) else {}
    cover = (video.get("cover") or {}).get("url_list") or []
    aweme_id = str(detail.get("aweme_id") or "douyin")
    desc = str(detail.get("desc") or "").strip()
    duration_ms = video.get("duration") or 0
    return {"video_id": aweme_id, "title": desc or f"douyin_{aweme_id}", "fulltitle": desc or f"douyin_{aweme_id}",
            "description": desc, "uploader": str(author.get("nickname") or ""),
            "channel": str(author.get("nickname") or ""), "duration": int(duration_ms) // 1000 if duration_ms else 0,
            "duration_string": "", "upload_date": "", "webpage_url": f"https://www.douyin.com/video/{aweme_id}",
            "view_count": stats.get("play_count") or 0, "like_count": stats.get("digg_count") or 0,
            "comment_count": stats.get("comment_count") or 0, "repost_count": 0, "save_count": 0,
            "thumbnail": cover[0] if cover else "", "extractor": "DouyinBrowser"}


# --- 一次完整的取流：详情 → 选档 → 下载 ---
def fetch_video(source: str, output_path: str, *, port: int = DEFAULT_PORT, browser: Optional[str] = None,
                profile: Optional[str] = None, ratio: str = "1080p") -> Dict[str, Any]:
    aweme_id = extract_aweme_id(source)
    if not aweme_id:
        raise RuntimeError("无法从输入里解析出 aweme_id：请传 19 位视频 ID 或包含它的链接")
    endpoint = ensure_browser(port=port, browser=browser, profile=profile)
    websocket = MiniWebSocket(endpoint["ws"])
    try:
        session = CdpSession(websocket)
        session_id = open_page(session, f"https://www.douyin.com/video/{aweme_id}")
        detail = fetch_detail(session, session_id, aweme_id)
        choice = pick_play_url(detail, ratio=ratio)
        download_media(session, session_id, choice["play_url"], output_path)
    finally:
        websocket.close()
    return {"download_method": "browser", "video_path": output_path, "aweme_id": aweme_id,
            "canonical_url": f"https://www.douyin.com/video/{aweme_id}", "requested_ratio": ratio,
            "ratio": choice["ratio"], "available_ratios": choice["available_ratios"],
            "metadata": metadata_from_detail(detail)}


# --- CLI：既能独立排查，也能被 douyin_extract 复用 ---
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Douyin media inside a real browser context (imitated from a proven approach, no external service).")
    parser.add_argument("--aweme-id", help="19 位抖音视频 ID")
    parser.add_argument("--url", help="包含 aweme_id 的链接或分享文本")
    parser.add_argument("-o", "--output", help="视频落盘路径")
    parser.add_argument("--port", type=int, default=int(os.environ.get(PORT_ENV_VAR) or DEFAULT_PORT), help="CDP 端口")
    parser.add_argument("--browser", help="Chrome/Edge 可执行文件路径")
    parser.add_argument("--profile", help="专用登录态目录（默认 ~/.cache/douyin-browser-profile）")
    parser.add_argument("--ratio", default="1080p", help="优先档位名，如 1080p/720p")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    source = args.aweme_id or args.url
    if not source:
        print(json.dumps({"error": "需要 --aweme-id 或 --url"}, ensure_ascii=False))
        return 2
    try:
        result = fetch_video(source, args.output or "/tmp/douyin_browser_fetch.mp4", port=args.port,
                             browser=args.browser, profile=args.profile, ratio=args.ratio)
    except BrowserUnavailableError as exc:
        print(json.dumps({"error": str(exc), "kind": "browser_unavailable"}, ensure_ascii=False))
        return 20
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 20
    print(json.dumps(result if args.json else {"video_path": result["video_path"], "ratio": result["ratio"]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
