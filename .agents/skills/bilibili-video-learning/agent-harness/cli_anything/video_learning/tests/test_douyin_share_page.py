"""
公开分享页候选链与风控降级识别的离线测试（对应 docs/DECISIONS.md D25）。

守住的核心不变量：**候选的成功判据是"抽出 token"，不是"页面非空"**。
真实故障正是后者：第一个候选返回了 200 的非空页面（视频数据被平台抹成只剩 status_code），
旧实现据此立刻 return，于是永远不去试还有数据的候选。
运行示例：python -m pytest cli_anything/video_learning/tests/test_douyin_share_page.py -v
"""
from __future__ import annotations  # 让测试与生产脚本使用同样的现代标注风格。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 构造诊断与错误载荷需要 JSON。
import sys  # 把 scripts 目录加入模块搜索路径。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 伪造 requests 的响应对象。

import pytest  # 提供 monkeypatch 与断言辅助。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

import douyin_ssr  # noqa: E402 与被测的生产模块共用同一实例。
from cli_anything.video_learning.utils import exit_codes  # noqa: E402 退出码契约。


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"  # 测试名直接映射真实脚本文件。
    module_name = f"video_learning_test_{name}"  # 独立模块名避免测试之间污染缓存。
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 造一页“有壳无数据”的风控降级页（字段按真实响应还原） ---
def degraded_page_html() -> str:
    payload = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"status_code": 0}}}, "errors": None}
    return ("<html><body>请完成验证 captcha verify</body>"
            f"<script>window._ROUTER_DATA = {json.dumps(payload)}</script></html>")


# --- 造一页含真实 play token 的正常 SSR 页 ---
def healthy_page_html(video_id: str = "v0300fg10000abcdefg") -> str:
    payload = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [
        {"video": {"play_addr": {"uri": video_id}}}]}}}}
    return f"<script>window._ROUTER_DATA = {json.dumps(payload)}</script>"


# --- 一个按 URL 返回不同响应的假会话 ---
class FakeSession:
    def __init__(self, responses: dict):
        self.responses = responses  # {host 关键字: (status, html) 或 异常}
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        for marker, value in self.responses.items():
            if marker in url:
                if isinstance(value, Exception):
                    raise value
                status, html = value
                return SimpleNamespace(status_code=status, text=html, content=html.encode(),
                                       url=url)
        return SimpleNamespace(status_code=404, text="", content=b"", url=url)


# --- 核心回归：第一个候选有页面但无 token 时，必须继续试下一个 ---
def test_a_degraded_first_candidate_does_not_stop_the_chain():
    session = FakeSession({
        "iesdouyin": (200, degraded_page_html()),          # 旧实现会在这里 return，然后抽 token 失败
        "m.douyin": (200, healthy_page_html("v0300fg10000healthy01")),
    })
    diagnostics: list[dict] = []

    result = douyin_ssr.fetch_share_page("7690619057690828986", session, diagnostics)

    assert result["video_id"] == "v0300fg10000healthy01"   # 拿到了第二个候选的 token
    assert result["candidate"] == "m_douyin"
    assert len(session.requested) == 2                     # 确实试了两个候选
    assert any("有页面但无 token" in str(item.get("message")) for item in diagnostics)  # 降级被留痕


# --- 所有候选都降级时，必须是"平台风控"这一档，而不是笼统失败 ---
def test_all_candidates_degraded_raises_platform_verification_error():
    session = FakeSession({
        "iesdouyin": (200, degraded_page_html()),
        "m.douyin": (200, degraded_page_html()),
        "douyin.com/share": (200, degraded_page_html()),
        "douyin.com/video": (200, degraded_page_html()),
    })
    diagnostics: list[dict] = []

    with pytest.raises(douyin_ssr.DouyinPlatformVerificationRequired) as excinfo:
        douyin_ssr.fetch_share_page("7690619057690828986", session, diagnostics)

    assert "风控/验证页" in str(excinfo.value)              # 报错必须说明是平台侧
    assert "videoInfoRes 无视频数据" in str(excinfo.value)  # 并给出可核对的原因
    assert len(session.requested) == len(douyin_ssr.SHARE_PAGE_CANDIDATES)  # 候选链被走完


# --- 页面压根取不到，仍归入"取流不可用"（与风控分开） ---
def test_unreachable_candidates_stay_generic():
    session = FakeSession({
        "iesdouyin": TimeoutError("timed out"),
        "m.douyin": (500, ""),
        "douyin.com/share": (403, ""),
        "douyin.com/video": (403, ""),
    })

    with pytest.raises(RuntimeError) as excinfo:
        douyin_ssr.fetch_share_page("7690619057690828986", session, [])

    assert not isinstance(excinfo.value, douyin_ssr.DouyinPlatformVerificationRequired)


# --- 降级识别本身：真实响应的字段形状必须能被认出来 ---
def test_describe_degraded_page_matches_the_real_shape():
    reason = douyin_ssr.describe_degraded_page(degraded_page_html())

    assert reason is not None
    assert "videoInfoRes" in reason and "captcha" in reason


# --- 正常页面不能被误判为降级 ---
def test_describe_degraded_page_accepts_a_healthy_page():
    assert douyin_ssr.describe_degraded_page(healthy_page_html()) is None


# --- 候选链必须包含实测可用的两个 host ---
def test_candidate_chain_covers_both_proven_hosts():
    urls = [template.format(aweme_id="123") for _, template in douyin_ssr.SHARE_PAGE_CANDIDATES]

    assert any("iesdouyin.com" in url for url in urls)     # 今天成功的生产路径
    assert any("m.douyin.com" in url for url in urls)      # 人工验证过的移动端 host


# --- 风控异常映射到 26，且与 20 分开 ---
def test_platform_verification_maps_to_its_own_exit_code():
    platform_error = douyin_ssr.DouyinPlatformVerificationRequired("风控")

    assert exit_codes.classify_failure(
        platform_error, platform_error_types=(douyin_ssr.DouyinPlatformVerificationRequired,)
    ) == exit_codes.EXIT_PLATFORM_VERIFICATION_REQUIRED
    assert exit_codes.classify_failure(
        douyin_ssr.DouyinSSRDownloadError("x", []), (douyin_ssr.DouyinSSRDownloadError,)
    ) == exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE


# --- 包装层不能吞掉风控信号：两条路径都失败时退出码仍是 26 ---
def test_download_wrapper_preserves_the_platform_signal(monkeypatch, tmp_path):
    extract = load_script("douyin_extract")
    monkeypatch.setattr(extract, "download_video_with_ssr",
                        lambda *a, **k: (_ for _ in ()).throw(
                            douyin_ssr.DouyinPlatformVerificationRequired(json.dumps({"error": "风控"}))))
    monkeypatch.setattr(extract, "download_video_with_ytdlp",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fresh cookies needed")))

    with pytest.raises(douyin_ssr.DouyinPlatformVerificationRequired) as excinfo:
        extract.choose_downloaded_video("https://v.douyin.com/x/", str(tmp_path))

    payload = json.loads(str(excinfo.value))               # 仍是同一种 JSON 诊断体，CLI 能解析
    assert payload["diagnostics"]
    assert exit_codes.classify_failure(
        excinfo.value, platform_error_types=(douyin_ssr.DouyinPlatformVerificationRequired,)
    ) == exit_codes.EXIT_PLATFORM_VERIFICATION_REQUIRED


# --- 非风控的取流失败，包装层仍应是 20 ---
def test_download_wrapper_keeps_generic_failures_as_unavailable(monkeypatch, tmp_path):
    extract = load_script("douyin_extract")
    monkeypatch.setattr(extract, "download_video_with_ssr",
                        lambda *a, **k: (_ for _ in ()).throw(douyin_ssr.DouyinSSRDownloadError("boom", [])))
    monkeypatch.setattr(extract, "download_video_with_ytdlp",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))

    with pytest.raises(extract.DouyinDownloadUnavailableError):
        extract.choose_downloaded_video("https://v.douyin.com/x/", str(tmp_path))


# --- 关键回归：SSR 链路的统一包装不能吞掉"平台风控"类型（真实实测抓到的漏洞） ---
def test_ssr_wrapper_preserves_the_platform_type(monkeypatch):
    monkeypatch.setattr(douyin_ssr, "resolve_public_input",
                        lambda *a, **k: (_ for _ in ()).throw(
                            douyin_ssr.DouyinPlatformVerificationRequired("平台风控/验证页")))
    monkeypatch.setattr(douyin_ssr, "get_ttwid", lambda: "ttwid-fixture")

    with pytest.raises(douyin_ssr.DouyinPlatformVerificationRequired) as excinfo:
        douyin_ssr.download_public_video("https://v.douyin.com/x/", "/tmp/never-written.mp4")

    assert "风控" in str(excinfo.value)                     # 类型与原因都被保留
    assert excinfo.value.diagnostics                        # 诊断体没有丢


# --- 普通取流失败仍然包成取流不可用（不要把所有失败都升级成风控） ---
def test_ssr_wrapper_keeps_generic_failures_generic(monkeypatch):
    monkeypatch.setattr(douyin_ssr, "resolve_public_input",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("短链解析失败")))
    monkeypatch.setattr(douyin_ssr, "get_ttwid", lambda: "ttwid-fixture")

    with pytest.raises(douyin_ssr.DouyinSSRDownloadError):
        douyin_ssr.download_public_video("https://v.douyin.com/x/", "/tmp/never-written.mp4")
