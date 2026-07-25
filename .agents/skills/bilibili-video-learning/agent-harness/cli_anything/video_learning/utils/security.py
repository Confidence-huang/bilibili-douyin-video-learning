"""
清理 CLI 错误与诊断中的凭据、签名链接和临时运行路径。
正文、字幕和普通业务字段不会经过本模块；调用示例：safe_error = sanitize_text(str(exc))。
"""
from __future__ import annotations  # 支持 Python 3.10+ 的类型标注。

import re  # 识别 header、键值秘密、URL 与 Windows/POSIX 临时路径。
import tempfile  # 当前系统临时目录比硬编码用户名路径更准确。
from typing import Any  # 诊断树可能由字典、列表和字符串嵌套组成。
from urllib.parse import parse_qsl, urlsplit, urlunsplit  # URL 脱敏保留安全的主机与路径。


SENSITIVE_QUERY_MARKERS = (                              # 出现这些键时整段 query/fragment 都视为签名材料。
    "access_token", "auth", "authorization", "cookie", "expires", "key", "msToken",
    "refresh_token", "secret", "sessdata", "sign", "signature", "token", "x-bogus", "a_bogus",
)
DIAGNOSTIC_FIELD_MARKERS = (                             # 只清理诊断字段，避免改写真实字幕或转写正文。
    "diagnostic", "error", "hint", "message", "stderr", "warning",
)


# --- 判断 URL 查询参数是否携带签名或访问凭据 ---
def _has_sensitive_query(query: str) -> bool:
    query_keys = [key.casefold() for key, _value in parse_qsl(query, keep_blank_values=True)]
    return any(marker.casefold() in key for key in query_keys for marker in SENSITIVE_QUERY_MARKERS)


# --- 清除 URL 用户信息与签名查询参数 ---
def _sanitize_url(match: re.Match[str]) -> str:
    raw_url = match.group(0)                                      # 正则已把尾随标点排除在 URL 之外。
    try:
        parsed = urlsplit(raw_url)                                # 解析失败时不把原始可疑链接写回日志。
        safe_netloc = parsed.netloc.rsplit("@", 1)[-1]           # user:password@host 永远不应出现在诊断里。
        has_signature = _has_sensitive_query(parsed.query)       # 普通 `?p=2` 保留，签名 query 整体删除。
        safe_query = "" if has_signature else parsed.query
        safe_fragment = "" if has_signature else parsed.fragment
        return urlunsplit((parsed.scheme, safe_netloc, parsed.path, safe_query, safe_fragment))
    except Exception:
        return "[sensitive URL hidden]"                           # 错误处理自身保持 fail-closed。


# --- 清理一段错误或诊断文本 ---
def sanitize_text(value: str, limit: int = 4000) -> str:
    text = str(value)                                             # 异常对象与普通字符串走同一安全出口。
    text = re.sub(r"https?://[^\s<>\"']+", _sanitize_url, text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(\b(?:access[_-]?token|authorization|bili_jct|cookie|refresh[_-]?token|secret|sessdata|sign|token)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[sensitive value hidden]",
        text,
    )
    text = re.sub(
        r"(?im)^(\s*(?:proxy-authorization|authorization|set-cookie|cookie)\s*:\s*).*$",
        r"\1[sensitive value hidden]",
        text,
    )

    temporary_root = re.escape(tempfile.gettempdir().rstrip("\\/"))
    text = re.sub(
        rf"(?i){temporary_root}(?:[\\/][^\s,;\]\)}}]+)*",
        "[temporary path hidden]",
        text,
    )
    text = re.sub(r"(?i)(?<!\w)/tmp(?:/[^\s,;\]\)}}]+)*", "[temporary path hidden]", text)
    return text[:limit] + ("..." if len(text) > limit else "")   # 后端失控时仍限制单条错误体积。


# --- 只递归清理结果中的诊断分支 ---
def sanitize_diagnostics(value: Any, inside_diagnostics: bool = False) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            key_is_diagnostic = inside_diagnostics or any(marker in str(key).casefold() for marker in DIAGNOSTIC_FIELD_MARKERS)
            cleaned[key] = sanitize_diagnostics(child, key_is_diagnostic)
        return cleaned
    if isinstance(value, list):
        return [sanitize_diagnostics(child, inside_diagnostics) for child in value]
    if isinstance(value, tuple):
        return tuple(sanitize_diagnostics(child, inside_diagnostics) for child in value)
    if isinstance(value, str) and inside_diagnostics:
        return sanitize_text(value)                               # 字幕、标题和正文等非诊断字符串保持原样。
    return value
