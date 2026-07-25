"""
把最终 Markdown、JSON 与轻量缓存安全地写到本地文件。
所有写入先在目标目录生成临时文件、刷新到磁盘，再用一次原子替换发布；调用示例：write_text_atomically("note.md", markdown)。
"""
from __future__ import annotations  # 保持现代类型标注兼容 Python 3.10+。

import json  # JSON 缓存复用同一套原子发布过程。
import os  # fsync 与 replace 保证发布前数据落盘、发布动作不可见地完成。
import tempfile  # 临时文件必须与目标同目录，Windows 才能可靠原子替换。
from pathlib import Path  # 同时接受字符串路径和 Path，并统一解析父目录。
from typing import Any  # JSON 写入接受字典、列表等可序列化数据。


# --- 原子发布一份 UTF-8 文本 ---
def write_text_atomically(destination: str | Path, content: str) -> Path:
    target = Path(destination).expanduser().resolve()             # 目标绝对路径进入返回值与错误信息。
    target.parent.mkdir(parents=True, exist_ok=True)              # 临时文件和目标必须位于同一文件系统。
    temporary_path: Path | None = None                            # finally 只清理本次创建的未发布临时文件。
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.stem}-",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)                        # 完整内容先写进外部不可见的临时文件。
            temporary_file.flush()                               # 把 Python 文本缓冲推送给操作系统。
            os.fsync(temporary_file.fileno())                     # 替换前要求操作系统提交当前文件内容。
            temporary_path = Path(temporary_file.name)            # 关闭句柄后 Windows 才允许 replace。
        os.replace(temporary_path, target)                        # 同目录替换保证读者只看到旧版或完整新版。
        temporary_path = None                                    # 发布成功后 finally 不再尝试删除目标。
        return target
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()                              # 写入或替换失败时不遗留半成品。


# --- 原子发布一份结构化 JSON ---
def write_json_atomically(destination: str | Path, payload: Any) -> Path:
    content = json.dumps(payload, ensure_ascii=False, indent=2)   # 先完整序列化，失败时不会触碰旧文件。
    return write_text_atomically(destination, content)            # 文本发布逻辑只维护一份。
