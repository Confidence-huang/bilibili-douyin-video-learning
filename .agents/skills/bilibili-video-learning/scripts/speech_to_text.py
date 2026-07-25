#!/usr/bin/env python3
r"""
统一的视频学习 ASR 入口。
这个文件把“音频文件 -> 分段正文”的能力集中起来，避免 B站、抖音和 fallback 脚本各自偷偷回到旧 openai-whisper。
默认路线是 faster-whisper + CTranslate2 + cuda/float16；只有新路线不可用时，才退回 openai-whisper。
调用示例：
    from speech_to_text import transcribe_audio_file
    result = transcribe_audio_file("audio.wav", model_size="small", language="zh")
"""
from __future__ import annotations                                               # 允许在返回结构里使用现代类型标注

import sys                                                                       # 输出解释器路径，帮助确认是否来自 uv venv
import traceback                                                                 # fallback 失败时保留可诊断的错误栈
from pathlib import Path                                                         # 统一处理 Windows 音频路径

from runtime_output import log                                                   # ASR 进度写 stderr，JSON 调用方只读 stdout。


# --- 记录一次 ASR 尝试 ---
def _new_asr_diagnostic(engine: str, ok: bool, message: str, **extra_data) -> dict:
    return {
        "engine": engine,                                                         # 标明是 faster-whisper 还是 openai-whisper
        "ok": ok,                                                                 # 让调用方能直接判断本次尝试是否成功
        "message": message,                                                       # 人类可读的成功/失败原因
        **extra_data,                                                             # 附带 device、compute_type、异常栈等诊断字段
    }


# --- 判断 CTranslate2 是否能使用独显 ---
def _choose_ctranslate2_device(requested_device: str | None) -> tuple[str, str, int]:
    import ctranslate2                                                            # CTranslate2 自己判断 CUDA，可避免只看 torch

    cuda_devices = ctranslate2.get_cuda_device_count()                            # RTX 5070 可见时这里应大于 0
    if requested_device == "cpu":                                                  # 调试时允许用户显式要求 CPU
        return "cpu", "int8", cuda_devices                                        # CPU 用 int8，避免慢到不可用
    if requested_device == "cuda" and cuda_devices == 0:                           # 显式要求 CUDA 但不可用，交给上层 fallback
        raise RuntimeError("CTranslate2 reports no CUDA device")
    if cuda_devices > 0:                                                           # 默认优先使用 NVIDIA 独显
        return "cuda", "float16", cuda_devices                                    # 5070 跑 float16 是当前最快的实测路线
    return "cpu", "int8", cuda_devices                                            # 没有 CUDA 时仍保留可运行兜底


# --- 用 faster-whisper 转写 ---
def _transcribe_with_faster_whisper(
    audio_path: Path,
    model_size: str,
    language: str,
    requested_device: str | None,
    log_prefix: str,
) -> dict:
    from faster_whisper import WhisperModel                                       # CTranslate2 后端的 Whisper 实现

    device, compute_type, cuda_devices = _choose_ctranslate2_device(requested_device)  # 决定 cuda/float16 或 CPU/int8
    log(f"[{log_prefix}] Loading faster-whisper '{model_size}' on {device}/{compute_type}...")
    log(f"[{log_prefix}] Python executable: {sys.executable}")
    log(f"[{log_prefix}] Python prefix: {sys.prefix}")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)     # 模型只负责本次音频的识别

    log(f"[{log_prefix}] Transcribing with faster-whisper (language={language})...")
    segment_iterator, transcription_info = model.transcribe(
        str(audio_path),                                                          # faster-whisper 接受字符串路径
        language=language,                                                        # 固定中文可减少自动语言识别开销
        task="transcribe",                                                        # 学习笔记只需要转写，不做翻译
        beam_size=1,                                                              # 长视频优先速度，术语由笔记阶段校正
        vad_filter=True,                                                          # 跳过课堂静音和空白段，减少无效解码
        condition_on_previous_text=False,                                         # 降低长课串词污染
    )

    segments = []                                                                 # 统一输出成 B站脚本已有字段名
    for segment in segment_iterator:                                               # faster-whisper 惰性迭代，遍历时才真正解码
        content = segment.text.strip()                                             # 空白段不进入学习材料
        if content:                                                                # 只保留有正文的片段
            segments.append({
                "from": round(segment.start, 2),                                  # 片段开始秒数，用于回看定位
                "to": round(segment.end, 2),                                      # 片段结束秒数，用于计算覆盖
                "content": content,                                               # ASR 正文
            })

    full_text = "".join(item["content"] for item in segments).strip()              # Douyin Markdown 需要全文字段
    duration = segments[-1]["to"] if segments else 0                               # 没有片段时用 0 兜底
    return {
        "engine": "faster-whisper",                                               # 调用方和最终笔记可明确来源
        "model_size": model_size,                                                  # 保留模型尺寸，便于解释精度/速度
        "device": device,                                                          # 应为 cuda，除非显式 CPU 或无 GPU
        "compute_type": compute_type,                                              # GPU 默认 float16
        "cuda_devices": cuda_devices,                                              # 诊断字段，用于确认独显可见
        "language": getattr(transcription_info, "language", language),             # 模型报告的语言
        "language_probability": getattr(transcription_info, "language_probability", None),  # 语言置信度
        "segments": segments,                                                      # 标准分段正文
        "text": full_text,                                                         # 拼接全文，兼容旧 Douyin 输出
        "duration": duration,                                                      # 片段覆盖到的最后秒数
        "diagnostics": [_new_asr_diagnostic("faster-whisper", True, "ok", device=device, compute_type=compute_type)],
    }


# --- 用 openai-whisper 兼容兜底 ---
def _transcribe_with_openai_whisper(
    audio_path: Path,
    model_size: str,
    language: str,
    requested_device: str | None,
    log_prefix: str,
) -> dict:
    import torch                                                                  # 旧路线用 torch 判断 CUDA
    import whisper                                                                # 兼容历史脚本的 openai-whisper

    device = requested_device or ("cuda" if torch.cuda.is_available() else "cpu")  # 尽量仍用 GPU fallback
    log(f"[{log_prefix}] Loading openai-whisper '{model_size}' on {device}...")
    model = whisper.load_model(model_size, device=device)                         # 旧模型加载方式

    log(f"[{log_prefix}] Transcribing with openai-whisper (language={language})...")
    whisper_result = model.transcribe(
        str(audio_path),                                                          # openai-whisper 也接受字符串路径
        language=language,                                                        # 固定中文
        verbose=False,                                                            # 不输出逐段冗长日志
        task="transcribe",                                                        # 只转写
        word_timestamps=False,                                                    # 学习笔记只需要段落时间戳
        condition_on_previous_text=False,                                         # 降低长课串词污染
        no_speech_threshold=0.6,                                                   # 沿用旧脚本静音阈值
    )

    segments = []                                                                 # 统一输出结构
    for segment in whisper_result.get("segments", []):                            # openai-whisper 返回列表
        content = segment.get("text", "").strip()                                 # 清理两侧空白
        if content:                                                                # 空白段没有学习价值
            segments.append({
                "from": round(segment["start"], 2),                               # 开始秒数
                "to": round(segment["end"], 2),                                   # 结束秒数
                "content": content,                                               # ASR 正文
            })

    full_text = "".join(item["content"] for item in segments).strip()              # 兼容旧全文字段
    duration = segments[-1]["to"] if segments else 0                               # 没有片段时用 0
    return {
        "engine": "openai-whisper",                                               # 明确这是 fallback，不是首选路线
        "model_size": model_size,                                                  # 模型尺寸
        "device": device,                                                          # fallback 也记录 cpu/cuda
        "compute_type": None,                                                      # openai-whisper 不暴露 CTranslate2 compute_type
        "cuda_devices": 1 if device == "cuda" else 0,                              # 兼容诊断字段
        "language": language,                                                      # 固定输入语言
        "language_probability": None,                                              # 旧路线不统一提供该字段
        "segments": segments,                                                      # 标准分段正文
        "text": full_text,                                                         # 拼接全文
        "duration": duration,                                                      # 最后片段时间
        "diagnostics": [_new_asr_diagnostic("openai-whisper", True, "fallback ok", device=device)],
    }


# --- 对外统一转写入口 ---
def transcribe_audio_file(
    audio_path: str | Path,
    model_size: str = "small",
    language: str = "zh",
    device: str | None = None,
    log_prefix: str = "asr",
    allow_openai_fallback: bool = True,
) -> dict:
    audio = Path(audio_path)                                                       # 把调用方传入的字符串或 Path 统一成 Path
    if not audio.exists():                                                         # 音频不存在时直接失败，避免误报 ASR 问题
        raise FileNotFoundError(f"Audio file not found: {audio}")

    faster_error = None                                                            # 保存新路线失败原因，fallback 后仍写进诊断
    faster_traceback = None                                                        # traceback 必须在 except 内捕获，离开后会被 Python 清空
    try:
        return _transcribe_with_faster_whisper(audio, model_size, language, device, log_prefix)  # 首选新路线
    except Exception as exc:
        faster_error = exc                                                         # 记录失败对象，下面决定是否 fallback
        faster_traceback = traceback.format_exc()                                  # 保留真实失败栈，方便定位 CUDA/模型问题
        log(f"[{log_prefix}] faster-whisper failed: {exc}")
        if not allow_openai_fallback:                                              # 用户或测试可禁止旧路线
            raise

    try:
        fallback_result = _transcribe_with_openai_whisper(audio, model_size, language, device, log_prefix)  # 兼容兜底
        fallback_result["diagnostics"].insert(
            0,
            _new_asr_diagnostic(
                "faster-whisper",
                False,
                str(faster_error),
                traceback=faster_traceback,
            ),
        )                                                                          # 让调用方知道为什么动用了旧路线
        return fallback_result                                                     # 返回 fallback 结果，但 engine 会写明 openai-whisper
    except Exception:
        log(f"[{log_prefix}] openai-whisper fallback also failed")
        raise
