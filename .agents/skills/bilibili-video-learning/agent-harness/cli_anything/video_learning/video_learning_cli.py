"""
Video Learning 的 Click 触发入口。
入口只解析命令并调用 core 指令；平台访问、字幕转换、笔记生成和环境检查分别留在业务模块，所有机器结果可用全局 `--json` 获取。
调用示例：cli-anything-video-learning --json source inspect BV1xx411c7mD
"""
from __future__ import annotations  # 支持 Python 3.10+ 的现代类型标注。

import json  # 全局 --json 模式输出单一机器可读文档。
import shlex  # REPL 把一行输入转换为与 shell 一致的参数列表。
from functools import wraps  # 错误边界装饰器保留 Click 命令元数据。

import click  # CLI-Anything 的标准命令组框架。

from cli_anything.video_learning import __version__  # banner 与 --version 使用同一版本。
from cli_anything.video_learning.core.doctor import inspect_runtime  # 真实环境预检指令。
from cli_anything.video_learning.core.note import render_note  # 本地 extraction JSON -> Markdown。
from cli_anything.video_learning.core.source import inspect_bilibili, normalize_source  # 平台来源指令。
from cli_anything.video_learning.core.subtitle import convert_subtitle  # 本地字幕 -> 时间线。
from cli_anything.video_learning.utils.repl_skin import ReplSkin  # 复用 CLI-Anything 官方交互外观。
from cli_anything.video_learning.utils.security import sanitize_text  # CLI 未处理异常统一脱敏后再反馈。


COOKIE_PERMISSION_EXIT_CODE = 21  # Agent 可区分“需要授权”与普通后端故障。


# --- 把业务结果按人类或 Agent 模式反馈 ---
def emit_result(payload: dict, human_text: str | None = None) -> None:
    root_context = click.get_current_context().find_root()
    use_json = bool((root_context.obj or {}).get("use_json"))                   # 全局选项控制所有子命令。
    if use_json:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))            # stdout 只写一个 JSON 文档。
    elif human_text is not None:
        click.echo(human_text)                                                   # 人类模式使用紧凑、可读反馈。
    else:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))            # 没有定制文本时仍提供完整结果。


# --- 把未处理异常转换成稳定退出码和结构化错误 ---
def handle_error(command_function):
    @wraps(command_function)
    def wrapped(*args, **kwargs):
        try:
            return command_function(*args, **kwargs)
        except click.exceptions.Exit:                                            # Click 自己的显式退出不重复包装。
            raise
        except Exception as exc:
            root_context = click.get_current_context().find_root()
            use_json = bool((root_context.obj or {}).get("use_json"))
            safe_error = sanitize_text(str(exc))
            if use_json:
                click.echo(json.dumps({"ok": False, "error": safe_error}, ensure_ascii=False, indent=2))
            else:
                click.echo(f"Error: {safe_error}", err=True)
            raise click.exceptions.Exit(1) from exc                              # Agent 可依赖非零退出码自我修正。

    return wrapped


# --- 根命令只接收触发与全局输出偏好 ---
@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Output one machine-readable JSON document")
@click.version_option(__version__, prog_name="cli-anything-video-learning")
@click.pass_context
def cli(context: click.Context, use_json: bool) -> None:
    """Inspect public video sources and transform authorized local learning material."""
    context.ensure_object(dict)
    context.obj["use_json"] = use_json                                           # 子命令从 root context 读取同一契约。
    if context.invoked_subcommand is None:
        context.invoke(repl)                                                     # CLI-Anything 默认无参数进入 REPL。


# --- 来源命令组 ---
@cli.group("source")
def source_group() -> None:
    """Normalize or inspect Bilibili and Douyin sources."""


@source_group.command("normalize")
@click.argument("source_text")
@click.option("--platform", type=click.Choice(["auto", "bilibili", "douyin"]), default="auto", show_default=True)
@handle_error
def normalize_command(source_text: str, platform: str) -> None:
    """Normalize one URL, ID, or public share text without downloading media."""
    payload = normalize_source(source_text, platform)
    emit_result(payload, json.dumps(payload, ensure_ascii=False, indent=2))


@source_group.command("inspect")
@click.argument("source_text")
@click.option("--subtitles", is_flag=True, help="Fetch accessible subtitle tracks without media download")
@click.option("--transcribe", "transcribe_model", type=click.Choice(["tiny", "base", "small", "medium", "large"]), help="Explicitly allow audio download and ASR")
@click.option("--comments", is_flag=True, help="Request comments through the existing yt-dlp backend")
@click.option("--cookies", help="Browser name or Netscape cookie file; values are never echoed")
@handle_error
def inspect_command(source_text: str, subtitles: bool, transcribe_model: str | None, comments: bool, cookies: str | None) -> None:
    """Inspect Bilibili metadata; media is touched only when --transcribe is present."""
    payload = inspect_bilibili(
        source_text,
        include_subtitles=subtitles,
        transcribe_model=transcribe_model,
        include_comments=comments,
        cookies=cookies,
    )
    if payload.get("status") == "cookie_permission_required":
        emit_result(payload, payload["message"])                              # 状态先输出，再用专用退出码结束。
        raise click.exceptions.Exit(COOKIE_PERMISSION_EXIT_CODE)
    human_text = (
        f"{payload.get('title', '(untitled)')}\n"
        f"Author: {payload.get('author', '')}\n"
        f"Selected part: P{payload.get('selected_page', payload.get('requested_page', 1))}\n"
        f"Subtitle tracks: {len(payload.get('subtitles', []))}"
    )
    emit_result(payload, human_text)


# --- 字幕命令组 ---
@cli.group("subtitle")
def subtitle_group() -> None:
    """Convert user-authorized local subtitle files."""


@subtitle_group.command("convert")
@click.argument("source_path", type=click.Path(exists=True, dir_okay=False, path_type=str))
@click.option("--output", "output_path", type=click.Path(dir_okay=False, path_type=str), help="Write normalized timeline JSON")
@handle_error
def convert_subtitle_command(source_path: str, output_path: str | None) -> None:
    """Convert SRT, VTT, ASS, Bilibili JSON, or JSON3 into one timeline."""
    payload = convert_subtitle(source_path, output_path)
    destination = payload.get("output") or "stdout"
    emit_result(payload, f"Converted {payload['segment_count']} segments -> {destination}")


# --- 笔记命令组 ---
@cli.group("note")
def note_group() -> None:
    """Render local extraction JSON into source-labelled learning Markdown."""


@note_group.command("render")
@click.argument("source_path", type=click.Path(exists=True, dir_okay=False, path_type=str))
@click.option("--output", "output_path", type=click.Path(dir_okay=False, path_type=str), help="Write Markdown to this path")
@click.option("--include-transcript", is_flag=True, help="Include full transcript only for owned or explicitly authorized content")
@handle_error
def render_note_command(source_path: str, output_path: str | None, include_transcript: bool) -> None:
    """Render notes; the default output never reproduces the full transcript."""
    payload = render_note(source_path, output_path, include_transcript=include_transcript)
    human_text = payload.get("markdown") or f"Saved note to: {payload['output']}"
    emit_result(payload, human_text)


# --- 运行环境命令组 ---
@cli.group("doctor")
def doctor_group() -> None:
    """Inspect the exact local Skill, Python environment, tools, and direct modules."""


@doctor_group.command("status")
@handle_error
def doctor_status_command() -> None:
    """Report machine-readable backend health and fail when a hard dependency is missing."""
    payload = inspect_runtime()
    human_lines = [
        f"Skill: {payload['skill_root']}",
        f"Python: {payload['runtime_python']}",
        f"yt-dlp: {payload['tools']['yt-dlp']['path']}",
        f"ffmpeg: {payload['tools']['ffmpeg']['path']}",
        f"Obsidian: {payload['obsidian_vault']}",
        f"Status: {'OK' if payload['ok'] else 'FAILED'}",
    ]
    emit_result(payload, "\n".join(human_lines))
    if not payload["ok"]:
        raise click.exceptions.Exit(1)                                            # doctor 不把缺失依赖当成成功。


# --- 默认交互式入口 ---
@cli.command("repl", hidden=True)
def repl() -> None:
    """Run a CLI-Anything styled interactive shell over the same subcommands."""
    skin = ReplSkin("video-learning", version=__version__)
    skin.print_banner()
    prompt_session = skin.create_prompt_session()
    command_help = {
        "source normalize <input>": "normalize Bilibili/Douyin URL, ID, or share text",
        "source inspect <bilibili>": "inspect metadata; add --subtitles or explicit --transcribe",
        "subtitle convert <file>": "convert a local subtitle to normalized JSON",
        "note render <result.json>": "render learning Markdown without full transcript by default",
        "doctor status": "verify the live Skill and hard dependencies",
    }
    while True:
        try:
            line = skin.get_input(prompt_session).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() in {"exit", "quit"}:
            break
        if line.lower() in {"help", "?"}:
            skin.help(command_help)
            continue
        try:
            cli.main(args=shlex.split(line), prog_name="cli-anything-video-learning", standalone_mode=False)
        except (click.ClickException, click.exceptions.Exit) as exc:
            if getattr(exc, "exit_code", 0) not in (0, None):
                skin.error(f"Command failed with exit code {exc.exit_code}")
    skin.print_goodbye()


# --- console_scripts 调用入口 ---
def main() -> None:
    cli(prog_name="cli-anything-video-learning")
