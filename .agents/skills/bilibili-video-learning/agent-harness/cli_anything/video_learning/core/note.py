"""Note command: render a local extraction result while enforcing the default no-full-transcript boundary."""
from __future__ import annotations  # 使用现代类型标注。

import json  # 读取真实 extraction JSON。
from pathlib import Path  # 规范本地输入与输出路径。

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime  # 复用 Skill 自己的 Markdown 渲染函数。


SOURCE_IDENTITY_VERSION = 1  # 标记格式变化时可显式拒绝旧身份，而不是猜测字段含义。


# --- 从提取结果生成可比较的来源身份 ---
def build_source_identity(payload: dict) -> dict:
    is_douyin = payload.get("platform") == "douyin"
    platform = "douyin" if is_douyin else "bilibili"                       # 老 B站结果没有 platform 字段。
    video_id = (
        payload.get("video_id") or payload.get("aweme_id")
        if is_douyin
        else payload.get("bvid") or payload.get("video_id")
    )
    page = None if is_douyin else payload.get("selected_page") or payload.get("requested_page") or payload.get("page_num") or 1
    method = payload.get("transcription_method") or payload.get("transcript_source") or payload.get("transcription_engine") or "metadata"
    source_type = "subtitle" if method == "api" else "asr" if any(name in str(method).casefold() for name in ("whisper", "speech")) else "metadata"
    return {
        "schema": SOURCE_IDENTITY_VERSION,
        "platform": platform,
        "video_id": str(video_id or ""),
        "page": page,
        "source_type": source_type,
    }


# --- 把来源身份嵌入最终 Markdown 供后续复用校验 ---
def attach_source_identity(markdown: str, source_identity: dict) -> str:
    marker_json = json.dumps(source_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"<!-- video-learning-source: {marker_json} -->\n{markdown}"       # 标记不包含标题或正文，避免转义风险。


# --- 从本地结果 JSON 生成学习笔记 ---
def render_note(
    source_path: str,
    output_path: str | None = None,
    *,
    include_transcript: bool = False,
    runtime: SkillRuntime | None = None,
) -> dict:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Extraction JSON not found: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    active_runtime = runtime or SkillRuntime()
    if payload.get("platform") == "douyin":
        renderer = active_runtime.load_script("douyin_extract")
    else:
        renderer = active_runtime.load_script("fetch_bilibili")
    source_identity = build_source_identity(payload)                            # 平台、视频、分 P、正文来源共同定义身份。
    markdown = renderer.to_markdown(payload, include_transcript=include_transcript)
    markdown = attach_source_identity(markdown, source_identity)
    result = {
        "source": str(source),
        "output": None,
        "characters": len(markdown),
        "include_transcript": include_transcript,
        "source_identity": source_identity,
        "markdown": markdown if output_path is None else None,
    }
    if output_path:
        output = Path(output_path).expanduser().resolve()
        file_output = active_runtime.load_script("file_output")                 # 与后端直写路径共用同一原子发布实现。
        result["output"] = str(file_output.write_text_atomically(output, markdown))
    return result
