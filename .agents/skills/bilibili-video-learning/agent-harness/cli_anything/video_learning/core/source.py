"""Source commands: normalize public inputs and inspect Bilibili without hidden media downloads."""
from __future__ import annotations  # 使用现代类型标注。

import re  # 平台 auto 模式只做轻量输入识别。

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime  # 所有真实脚本调用集中在 runtime。
from cli_anything.video_learning.utils.security import sanitize_text  # 授权提示只能包含脱敏后的失败原因。


AUTHENTICATION_HINTS = (                                  # 仅这些明确认证信号能触发 Cookie 授权状态。
    "age-restricted", "authentication required", "cookie required", "cookies are needed", "cookies required",
    "cookies-from-browser", "fresh cookies", "log in", "login", "members only", "password",
    "premium member", "sign in", "会员", "需要登录", "登录", "认证",
)


# --- 判断失败是否可能由用户明确授权的浏览器 Cookie 解决 ---
def needs_cookie_permission(message: str) -> bool:
    normalized = str(message).casefold()                        # 平台与 yt-dlp 的错误大小写并不稳定。
    return any(hint.casefold() in normalized for hint in AUTHENTICATION_HINTS)


# --- 判断输入平台并执行无媒体规范化 ---
def normalize_source(source_text: str, platform: str = "auto", runtime: SkillRuntime | None = None) -> dict:
    active_runtime = runtime or SkillRuntime()                                   # 默认使用 live Skill 与 GPU Python。
    selected_platform = platform
    if platform == "auto":
        selected_platform = "douyin" if re.search(r"douyin\.com|iesdouyin\.com|^\d{10,25}$", source_text.strip()) else "bilibili"
    if selected_platform == "douyin":
        return active_runtime.run_json_script("douyin_ssr.py", [source_text, "--parse-only"], timeout=30)
    return active_runtime.run_json_script("normalize_bilibili_url.py", [source_text], timeout=30)


# --- 提取 B站元数据、字幕或显式 ASR ---
def inspect_bilibili(
    source_text: str,
    *,
    include_subtitles: bool = False,
    transcribe_model: str | None = None,
    include_comments: bool = False,
    cookies: str | None = None,
    runtime: SkillRuntime | None = None,
) -> dict:
    active_runtime = runtime or SkillRuntime()
    arguments = [source_text, "--json"]                         # JSON 是 harness 与后端之间的固定协议。
    if include_subtitles:
        arguments.append("--subtitles")                         # 只取可访问字幕，不下载媒体。
    if transcribe_model:
        arguments.extend(["--transcribe", transcribe_model])    # 只有显式选项才允许音频与 ASR。
    if include_comments:
        arguments.append("--comments")
    if cookies:
        arguments.extend(["--cookies", cookies])                # Cookie 值不进入结果和诊断文本。
    try:
        return active_runtime.run_json_script("fetch_bilibili.py", arguments, timeout=1800 if transcribe_model else 180)
    except Exception as exc:
        safe_error = sanitize_text(str(exc))                     # 状态结果和日志都不能回显认证材料。
        if not cookies and needs_cookie_permission(safe_error): # 第一次匿名失败后才请求浏览器授权。
            return {
                "ok": False,
                "status": "cookie_permission_required",
                "message": "该视频可能需要登录访问。请明确允许并指定 Edge、Chrome 或 Firefox 后再读取浏览器 Cookie。",
                "error": safe_error,
            }
        raise                                                     # 已提供 Cookie 或非认证错误保持真实失败。
