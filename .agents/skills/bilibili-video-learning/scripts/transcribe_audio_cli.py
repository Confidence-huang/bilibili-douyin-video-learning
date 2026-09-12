"""
把任意本地音/视频文件转写成带时间戳的 JSON（深度归档等外部调用的稳定入口）。
stdout 永远只有一份 JSON 文档：成功 = 转写结果，失败 = {"error": "..."}；进度与诊断全部走 stderr。
调用示例：python transcribe_audio_cli.py --audio "D:/clip.mp4" --model small --language zh
"""
from __future__ import annotations  # 保持现代类型标注兼容 Python 3.10+。

import argparse  # 稳定的命令行契约，供 Obsidian 插件等外部调用方拼参数。
import json  # stdout 契约只允许一份可解析的 JSON 文档。
import sys  # 退出码区分成功与失败，方便调用方重试。
from pathlib import Path  # 统一处理 Windows 路径与存在性检查。

from runtime_output import log  # 进度写 stderr，保证 stdout 的 JSON 不被污染。
from speech_to_text import transcribe_audio_file  # 复用 faster-whisper 优先 + openai-whisper 兜底的同一入口。


# --- 解析命令行参数 ---
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe a local audio/video file to timestamped JSON.")
    parser.add_argument("--audio", required=True, help="本地音/视频文件路径（容器内含音轨即可）")
    parser.add_argument("--model", default="small", help="faster-whisper 模型名，默认 small")
    parser.add_argument("--language", default="zh", help="语言代码，默认 zh；传 auto 让模型自行检测")
    parser.add_argument("--device", default=None, choices=[None, "auto", "cuda", "cpu"], help="留空或 auto = 自动选 GPU")
    return parser.parse_args()


# --- 主流程：转写一次并把结果作为 JSON 打到 stdout ---
def main() -> int:
    args = _parse_args()
    audio = Path(args.audio).expanduser()
    if not audio.is_file():
        print(json.dumps({"error": f"audio file not found: {audio}"}, ensure_ascii=False), flush=True)
        return 1

    device = None if args.device in (None, "auto") else args.device   # auto 交还给 _choose_ctranslate2_device 探测。
    log(f"[cli] transcribing {audio.name} with model={args.model} language={args.language}")
    try:
        result = transcribe_audio_file(audio, model_size=args.model, language=args.language, device=device, log_prefix="cli")
    except Exception as exc:                                          # 任何 ASR 失败都收敛成一行可解析错误。
        log(f"[cli] transcription failed: {exc}")
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
        return 1

    payload = {
        "engine": result.get("engine", ""),
        "device": result.get("device", ""),
        "compute_type": result.get("compute_type"),
        "model_size": result.get("model_size", args.model),
        "duration": result.get("duration", 0),
        "segments": result.get("segments", []),                       # [{from, to, content}]，秒为单位的浮点时间戳。
        "full_text": result.get("full_text", ""),
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)        # stdout 只含这一份 JSON。
    return 0


if __name__ == "__main__":
    sys.exit(main())
