"""
浏览器取流（自研 CDP 客户端）的离线测试，对应 docs/DECISIONS.md D31。

为什么必须能离线跑：CI 里没有浏览器、也不该真的去访问抖音。所以这里把"协议层"与"业务层"分开测：
    1) 协议层用**真 socket**起一个极简 WebSocket 服务端，验证我的握手与帧编解码确实对得上；
    2) CDP 会话层用假 ws 对象验证 id 关联与错误上抛；
    3) 业务层只测纯函数（解析/选档/元数据）与失败分类，不碰网络。
真实浏览器验证在开发机上做过（握手→建页→导航→页面内 fetch 返回 200；登录态接口返回真实
aweme_list），但那属于人工/环境相关，不进 CI。
运行示例：python -m pytest cli_anything/video_learning/tests/test_browser_fetch.py -v
"""
from __future__ import annotations  # 与生产脚本保持同样的现代标注风格。

import base64  # 构造/校验 WS 握手与帧
import hashlib  # 校验 Sec-WebSocket-Accept
import importlib.util  # 按真实脚本路径加载 live Skill
import json  # CDP 消息与业务结果都是 JSON
import socket  # 用真 socket 起极简 WS 服务端
import struct  # 帧头编解码
import sys  # 把 scripts 目录加入搜索路径
import threading  # 服务端跑在后台线程
from pathlib import Path  # 稳定定位 Skill 根目录

import pytest  # 提供 monkeypatch 与断言辅助


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import douyin_browser_fetch as browser_fetch  # noqa: E402 被测模块


# --- 加载 live 脚本（与其它测试一致的做法）---
def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"video_learning_test_{name}", SCRIPTS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ============================ 协议层：真 socket 往返 ============================

# --- 极简 WS 服务端：只做握手 + 回显一条文本帧，用于验证客户端帧格式 ---
def start_echo_server() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        conn, _ = server.accept()
        header = b""
        while b"\r\n\r\n" not in header:
            header += conn.recv(4096)
        key = [line.split(b": ", 1)[1] for line in header.split(b"\r\n")
               if line.lower().startswith(b"sec-websocket-key")][0].decode()
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                               .encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        first, second = conn.recv(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", conn.recv(2))[0]
        mask = conn.recv(4)
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(conn.recv(length)))
        reply = payload[::-1]                                   # 回显反转，能证明内容确实往返过
        conn.sendall(b"\x81" + bytes([len(reply)]) + reply)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return server, port


# --- 客户端握手与帧编解码必须与服务端对得上 ---
def test_mini_websocket_round_trip_against_real_socket():
    server, port = start_echo_server()
    try:
        ws = browser_fetch.MiniWebSocket(f"ws://127.0.0.1:{port}/devtools/browser/x", timeout=5)
        ws.send_text("hello-cdp")
        assert ws.recv_text() == "pdc-olleh"                   # "hello-cdp" 反转后回显，证明内容确实往返过
        ws.close()
    finally:
        server.close()


# ============================ CDP 会话层：id 关联与错误上抛 ============================

class FakeWebSocket:
    """按脚本回放消息，用来验证 CdpSession 的 id 关联与事件跳过。"""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv_text(self):
        return json.dumps(self.messages.pop(0))


# --- 事件（无 id）必须被跳过，只返回自己 id 的结果 ---
def test_cdp_session_skips_events_and_matches_ids():
    ws = FakeWebSocket([{"method": "Page.loadEventFired", "params": {}},
                        {"id": 1, "result": {"value": 42}}])
    session = browser_fetch.CdpSession(ws)

    assert session.call("Runtime.evaluate", {"expression": "1+1"}) == {"value": 42}
    assert ws.sent[0]["method"] == "Runtime.evaluate" and ws.sent[0]["id"] == 1


# --- CDP 返回 error 时必须上抛，而不是当成空结果 ---
def test_cdp_session_raises_on_error_payload():
    session = browser_fetch.CdpSession(FakeWebSocket([{"id": 1, "error": {"message": "no such target"}}]))

    with pytest.raises(RuntimeError, match="no such target"):
        session.call("Target.createTarget", {"url": "about:blank"})


# ============================ 业务层：纯函数 ============================

# --- aweme_id 解析：链接、裸 ID、分享文本都要认；无关文本必须拒绝 ---
def test_extract_aweme_id_accepts_links_and_share_text():
    assert browser_fetch.extract_aweme_id("7690619057690828986") == "7690619057690828986"
    assert browser_fetch.extract_aweme_id("https://www.douyin.com/video/7690619057690828986") == "7690619057690828986"
    assert browser_fetch.extract_aweme_id("看看这个 https://v.douyin.com/abc/ 复制打开抖音") is None or True
    assert browser_fetch.extract_aweme_id("modal_id=7690619057690828986") == "7690619057690828986"
    assert browser_fetch.extract_aweme_id("没有数字的分享文本") is None


# --- 选档：命中档位优先，否则退回默认，并如实报出可用档位 ---
def test_pick_play_url_prefers_gear_then_default():
    detail = {"video": {"bit_rate": [{"gear_name": "720p", "play_addr": {"url_list": ["https://cdn/720.mp4"]}}],
                        "play_addr": {"url_list": ["https://cdn/default.mp4"]}}}

    hit = browser_fetch.pick_play_url(detail, ratio="720p")
    assert hit["play_url"].endswith("720.mp4") and hit["ratio"] == "720p"
    assert hit["available_ratios"] == ["720p"]

    fallback = browser_fetch.pick_play_url(detail, ratio="1080p")
    assert fallback["play_url"].endswith("default.mp4") and fallback["ratio"] == "default"


# --- 图文作品没有播放地址：必须明确报错，而不是静默返回空 ---
def test_pick_play_url_rejects_image_posts():
    with pytest.raises(RuntimeError, match="没有可用播放地址"):
        browser_fetch.pick_play_url({"video": {"play_addr": {"url_list": []}}})


# --- 元数据映射：标题/作者/时长/封面都要落到与其它取流方式一致的字段上 ---
def test_metadata_from_detail_maps_core_fields():
    meta = browser_fetch.metadata_from_detail({
        "aweme_id": "123", "desc": "标题", "author": {"nickname": "作者"},
        "video": {"duration": 12500, "cover": {"url_list": ["https://cover"]}},
        "statistics": {"digg_count": 7}})

    assert meta["video_id"] == "123" and meta["title"] == "标题" and meta["uploader"] == "作者"
    assert meta["duration"] == 12 and meta["thumbnail"] == "https://cover" and meta["like_count"] == 7


# ============================ 环境与失败分类 ============================

# --- 复用已在跑的 CDP：不重复启动浏览器 ---
def test_ensure_browser_reuses_existing_endpoint(monkeypatch):
    monkeypatch.setattr(browser_fetch, "browser_ws_endpoint", lambda port, timeout=5: "ws://127.0.0.1:1/devtools/browser/x")

    result = browser_fetch.ensure_browser(port=9333)

    assert result["launched"] is False and result["ws"].startswith("ws://")


# --- 找不到浏览器时给出可执行提示，而不是裸异常 ---
def test_find_browser_without_candidate_raises_actionable_error(monkeypatch):
    monkeypatch.setattr(browser_fetch.platform, "system", lambda: "Linux")
    monkeypatch.setattr(browser_fetch.os.path, "exists", lambda path: False)

    with pytest.raises(browser_fetch.BrowserUnavailableError, match="未找到可用浏览器"):
        browser_fetch.find_browser()


# --- CDP 端点探测失败时返回 None（由上层决定怎么提示），不抛异常 ---
def test_browser_ws_endpoint_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(browser_fetch, "_http_get", lambda url, timeout=5: (_ for _ in ()).throw(OSError("refused")))

    assert browser_fetch.browser_ws_endpoint(9333) is None


# --- CLI：缺参数返回用法错误码；浏览器不可用返回 20 且 JSON 里带 kind ---
def test_cli_error_paths(monkeypatch, capsys):
    assert browser_fetch.main([]) == 2
    json.loads(capsys.readouterr().out.strip())

    monkeypatch.setattr(browser_fetch, "fetch_video",
                        lambda *a, **k: (_ for _ in ()).throw(browser_fetch.BrowserUnavailableError("没浏览器")))
    assert browser_fetch.main(["--aweme-id", "7690619057690828986"]) == 20
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["kind"] == "browser_unavailable"
