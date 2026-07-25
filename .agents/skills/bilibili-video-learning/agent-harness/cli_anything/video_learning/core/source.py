"""
Video source commands: normalize Bilibili/Douyin inputs and dispatch inspection by platform.
Metadata inspection never downloads full media; only an explicit transcribe model may enter an
ASR pipeline. Call inspect_source("...", platform="auto") from the Click entry.
"""
from __future__ import annotations  # 使用现代类型标注。

import re  # 平台 auto 模式只做轻量输入识别。

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime  # 所有真实脚本调用集中在 runtime。
from cli_anything.video_learning.utils.security import sanitize_text  # 授权提示只能包含脱敏后的失败原因。


AUTHENTICATION_HINTS = (                                  # 仅这些明确认证信号能触发 Cookie 授权状态。
    "age-restricted", "authentication required", "cookie required", "cookies are needed", "cookies required",
    "cookies-from-browser", "fresh cookies", "log in", "login", "members only", "password",
    "premium member", "sign in", "会员", "需要登录", "登录", "认证",
)


# --- 只根据输入形态判断平台 ---
def detect_source_platform(source_text: str, platform: str = "auto") -> str:
    if platform != "auto":                                                        # 显式平台优先于启发式判断。
        return platform
    is_douyin = re.search(                                                        # 短链、长链和裸 aweme_id 都属于抖音。
        r"douyin\.com|iesdouyin\.com|^\d{10,25}$",
        source_text.strip(),
        flags=re.IGNORECASE,
    )
    return "douyin" if is_douyin else "bilibili"                                 # 其余输入保持历史 B站默认。


# --- 判断失败是否可能由用户明确授权的浏览器 Cookie 解决 ---
def needs_cookie_permission(message: str) -> bool:
    normalized = str(message).casefold()                        # 平台与 yt-dlp 的错误大小写并不稳定。
    return any(hint.casefold() in normalized for hint in AUTHENTICATION_HINTS)


# --- 判断输入平台并执行无媒体规范化 ---
def normalize_source(source_text: str, platform: str = "auto", runtime: SkillRuntime | None = None) -> dict:
    active_runtime = runtime or SkillRuntime()                                   # 默认使用 live Skill 与 GPU Python。
    selected_platform = detect_source_platform(source_text, platform)            # 规范化和检查共享同一平台判断。
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


# --- 检查抖音公开元数据，或在明确授权后运行 ASR ---
def inspect_douyin(
    source_text: str,
    *,
    transcribe_model: str | None = None,
    include_ratios: bool = False,
    download_method: str = "auto",
    ratio: str = "1080p",
    watermark: bool = False,
    runtime: SkillRuntime | None = None,
) -> dict:
    active_runtime = runtime or SkillRuntime()                                    # 所有平台脚本仍通过同一运行时执行。
    if include_ratios and transcribe_model:
        raise ValueError("Use --ratios and --transcribe as separate Douyin operations")
    if not transcribe_model and download_method != "auto":
        raise ValueError("--download-method applies only to explicit Douyin transcription")
    if not transcribe_model and ratio != "1080p":
        raise ValueError("--ratio applies only to explicit Douyin transcription")
    if watermark and not (include_ratios or transcribe_model):
        raise ValueError("--watermark requires --ratios or explicit Douyin transcription")
    if transcribe_model:
        arguments = [                                                            # 明确 ASR 才进入下载与转写脚本。
            source_text,
            "--json",
            "--model", transcribe_model,
            "--download-method", download_method,
            "--ratio", ratio,
        ]
        if watermark:
            arguments.append("--watermark")                                      # 水印选择只影响显式媒体路径。
        return active_runtime.run_json_script("douyin_extract.py", arguments, timeout=1800)

    arguments = [source_text, "--inspect"]                                        # 默认只取 SSR 页面公开字段。
    if include_ratios:
        arguments.append("--list-ratios")                                        # 画质探测只发送 Range bytes=0-1。
    if watermark:
        arguments.append("--watermark")                                          # 探测 playwm，而非无水印 play。
    return active_runtime.run_json_script("douyin_ssr.py", arguments, timeout=180)


# --- 统一分派 B站或抖音检查 ---
def inspect_source(
    source_text: str,
    *,
    platform: str = "auto",
    include_subtitles: bool = False,
    transcribe_model: str | None = None,
    include_comments: bool = False,
    cookies: str | None = None,
    include_ratios: bool = False,
    download_method: str = "auto",
    ratio: str = "1080p",
    watermark: bool = False,
    runtime: SkillRuntime | None = None,
) -> dict:
    selected_platform = detect_source_platform(source_text, platform)             # 一次判断决定后续业务指令。
    if selected_platform == "bilibili":
        if include_ratios or watermark or download_method != "auto":
            raise ValueError("Douyin ratio/download options cannot be used with a Bilibili source")
        return inspect_bilibili(
            source_text,
            include_subtitles=include_subtitles,
            transcribe_model=transcribe_model,
            include_comments=include_comments,
            cookies=cookies,
            runtime=runtime,
        )

    if include_subtitles or include_comments or cookies:
        raise ValueError("Douyin inspection does not support --subtitles, --comments, or --cookies")
    return inspect_douyin(
        source_text,
        transcribe_model=transcribe_model,
        include_ratios=include_ratios,
        download_method=download_method,
        ratio=ratio,
        watermark=watermark,
        runtime=runtime,
    )
