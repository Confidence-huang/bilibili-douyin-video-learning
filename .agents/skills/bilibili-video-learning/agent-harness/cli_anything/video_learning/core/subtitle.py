"""Subtitle command: convert a user-authorized local subtitle into one JSON timeline."""
from __future__ import annotations  # 使用现代类型标注。

import json  # 保存统一时间线 JSON。
from pathlib import Path  # 规范输入与输出路径。

from cli_anything.video_learning.utils.skill_runtime import SkillRuntime  # 复用 live 转换脚本。


# --- 转换一份真实本地字幕文件 ---
def convert_subtitle(source_path: str, output_path: str | None = None, runtime: SkillRuntime | None = None) -> dict:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Subtitle file not found: {source}")
    active_runtime = runtime or SkillRuntime()
    converter = active_runtime.load_script("convert_subtitle")                 # 不复制解析实现，直接调用生产脚本。
    segments = converter.detect_and_parse(str(source))
    result = {"source": str(source), "segment_count": len(segments), "segments": segments, "output": None}
    if output_path:
        output = Path(output_path).expanduser().resolve()
        file_output = active_runtime.load_script("file_output")                 # JSON 也通过同目录临时文件原子发布。
        result["output"] = str(file_output.write_json_atomically(output, segments))
    return result
