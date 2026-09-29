#!/usr/bin/env python3
r"""
统一的视频学习 ASR 入口。
这个文件把“音频文件 -> 分段正文”的能力集中起来，避免 B站、抖音和 fallback 脚本各自偷偷回到旧 openai-whisper。
默认路线是 faster-whisper + CTranslate2 + cuda/float16；只有新路线不可用时，才退回 openai-whisper。
转写结束后统一做覆盖率校验：VAD 静默吃掉的语音会被局部补转回来（见 docs/DECISIONS.md D16）。
调用示例：
    from speech_to_text import transcribe_audio_file
    result = transcribe_audio_file("audio.wav", model_size="small", language="zh")
"""
from __future__ import annotations                                               # 允许在返回结构里使用现代类型标注

import sys                                                                       # 输出解释器路径，帮助确认是否来自 uv venv
import traceback                                                                 # fallback 失败时保留可诊断的错误栈
from pathlib import Path                                                         # 统一处理 Windows 音频路径
from typing import NamedTuple                                                    # 参数对象必须能被测试加载器动态加载（见 D16）

import asr_coverage                                                              # 覆盖率校验与局部补转（不依赖任何模型）
from runtime_output import log                                                   # ASR 进度写 stderr，JSON 调用方只读 stdout。


ASR_PARAMS_VERSION = 1                                                           # 参数默认值变化时递增，让旧缓存放弃而不是复用


# --- 一次转写任务的全部可调参数 ---
class TranscriptionSettings(NamedTuple):
    model_size: str = "small"                                                    # 模型尺寸，同时决定精度与显存占用
    language: str = "zh"                                                         # 固定中文可减少自动语言识别开销
    beam_size: int = 1                                                           # 长视频优先速度，术语由笔记阶段校正
    vad_filter: bool = True                                                      # 跳过课堂静音；关掉可避免语音被误判丢弃
    condition_on_previous_text: bool = False                                     # 降低长课串词污染
    coverage_check: bool = True                                                   # 是否校验转写覆盖率并补转可疑空档
    coverage_floor: float = asr_coverage.DEFAULT_COVERAGE_FLOOR                    # 覆盖率低于该值时在诊断里显式提示
    gap_min_seconds: float = asr_coverage.DEFAULT_GAP_MIN_SECONDS                  # 小于该值的空档视为正常停顿
    speech_dbfs: float = asr_coverage.DEFAULT_SPEECH_DBFS                          # 高于该音量的空档判定为“有人在说话”
    max_retry_windows: int = asr_coverage.DEFAULT_MAX_RETRY_WINDOWS                # 单次运行的补转窗口预算

    def identity(self) -> dict:
        return {"params_version": ASR_PARAMS_VERSION, **self._asdict()}           # 缓存键只认这份字典，不认调用点默认值


# --- 读取当前环境里的 ASR 引擎版本 ---
def installed_engine_versions() -> dict:
    versions = {}                                                                # 未安装的引擎直接不出现在身份里，保持确定性
    try:
        import faster_whisper                                                    # 首选引擎
        versions["faster-whisper"] = getattr(faster_whisper, "__version__", "unknown")
    except Exception:                                                            # CI 与纯笔记环境没有安装 ASR 依赖
        pass
    try:
        import whisper                                                           # 兼容兜底引擎
        versions["openai-whisper"] = getattr(whisper, "__version__", "unknown")
    except Exception:
        pass
    return versions


# --- 记录一次 ASR 尝试 ---
def _new_asr_diagnostic(engine: str, ok: bool, message: str, **extra_data) -> dict:
    return {
        "engine": engine,                                                         # 标明是 faster-whisper 还是 openai-whisper
        "ok": ok,                                                                 # 让调用方能直接判断本次尝试是否成功
        "message": message,                                                       # 人类可读的成功/失败原因
        **extra_data,                                                             # 附带 device、compute_type、异常栈等诊断字段
    }


# --- 把覆盖率报告转成调用方可读的诊断 ---
def _new_coverage_diagnostic(report: dict) -> dict:
    recovered = report.get("recovered_segments") or 0                             # 补转回来的片段数量
    return {
        "step": "asr_coverage",                                                   # 与取流、转写诊断区分开
        "ok": recovered == 0,                                                     # 补转过说明第一遍确实丢了正文
        "message": (
            f"coverage {report.get('coverage_before')} -> {report.get('coverage_after')}"
            + (f", recovered {recovered} segments" if recovered else "")
            + (f", skipped: {report['skipped_reason']}" if report.get("skipped_reason") else "")
        ),
        "audio_duration": report.get("audio_duration"),                            # 让调用方能复核覆盖率分母
        "coverage_before": report.get("coverage_before"),
        "coverage_after": report.get("coverage_after"),
        "gaps": report.get("gaps", []),                                            # 时间轴上的空洞，含秒数与起止
        "retried_windows": report.get("retried_windows", []),                       # 实际补转的窗口及其实测音量
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


# --- 把一次 faster-whisper 解码结果整理成统一分段 ---
def _reading_to_segments(reading) -> list[dict]:
    segments = []                                                                 # 统一输出成 B站/抖音脚本已有字段名
    for segment in reading:                                                       # faster-whisper 惰性迭代，遍历时才真正解码
        content = segment.text.strip()                                             # 空白段不进入学习材料
        if content:                                                                # 只保留有正文的片段
            segments.append({
                "from": round(segment.start, 2),                                   # 片段开始秒数，用于回看定位
                "to": round(segment.end, 2),                                       # 片段结束秒数，用于计算覆盖
                "content": content,                                                # ASR 正文
            })
    return segments


# --- 用 faster-whisper 转写 ---
def _run_faster_whisper(
    audio_path: Path,
    settings: TranscriptionSettings,
    requested_device: str | None,
    log_prefix: str,
) -> tuple[dict, object]:
    from faster_whisper import WhisperModel                                       # CTranslate2 后端的 Whisper 实现

    device, compute_type, cuda_devices = _choose_ctranslate2_device(requested_device)  # 决定 cuda/float16 或 CPU/int8
    log(f"[{log_prefix}] Loading faster-whisper '{settings.model_size}' on {device}/{compute_type}...")
    log(f"[{log_prefix}] Python executable: {sys.executable}")
    log(f"[{log_prefix}] Python prefix: {sys.prefix}")
    model = WhisperModel(settings.model_size, device=device, compute_type=compute_type)  # 模型只负责本次音频的识别

    log(f"[{log_prefix}] Transcribing with faster-whisper (language={settings.language})...")
    reading, transcription_info = model.transcribe(
        str(audio_path),                                                          # faster-whisper 接受字符串路径
        language=settings.language,                                               # 固定中文可减少自动语言识别开销
        task="transcribe",                                                        # 学习笔记只需要转写，不做翻译
        beam_size=settings.beam_size,                                             # 由 settings 决定，逐字稿场景可调大
        vad_filter=settings.vad_filter,                                           # 跳过课堂静音和空白段，减少无效解码
        condition_on_previous_text=settings.condition_on_previous_text,            # 降低长课串词污染
    )
    segments = _reading_to_segments(reading)                                      # 第一遍正文

    def transcribe_window(clip_path: Path) -> list[dict]:
        window_reading, _ = model.transcribe(                                      # 补转窗口一律关闭 VAD：这正是它上次丢字的原因
            str(clip_path),
            language=settings.language,
            task="transcribe",
            beam_size=settings.beam_size,
            vad_filter=False,                                                      # 已实测窗口有声音，不需要再让 VAD 判断一次
            condition_on_previous_text=False,
        )
        return _reading_to_segments(window_reading)

    result = {
        "engine": "faster-whisper",                                               # 调用方和最终笔记可明确来源
        "model_size": settings.model_size,                                        # 保留模型尺寸，便于解释精度/速度
        "device": device,                                                          # 应为 cuda，除非显式 CPU 或无 GPU
        "compute_type": compute_type,                                              # GPU 默认 float16
        "cuda_devices": cuda_devices,                                              # 诊断字段，用于确认独显可见
        "language": getattr(transcription_info, "language", settings.language),     # 模型报告的语言
        "language_probability": getattr(transcription_info, "language_probability", None),  # 语言置信度
        "segments": segments,                                                      # 标准分段正文
        "diagnostics": [_new_asr_diagnostic("faster-whisper", True, "ok", device=device, compute_type=compute_type)],
    }
    return result, transcribe_window


# --- 用 openai-whisper 兼容兜底 ---
def _run_openai_whisper(
    audio_path: Path,
    settings: TranscriptionSettings,
    requested_device: str | None,
    log_prefix: str,
) -> tuple[dict, object]:
    import torch                                                                  # 旧路线用 torch 判断 CUDA
    import whisper                                                                # 兼容历史脚本的 openai-whisper

    device = requested_device or ("cuda" if torch.cuda.is_available() else "cpu")  # 尽量仍用 GPU fallback
    log(f"[{log_prefix}] Loading openai-whisper '{settings.model_size}' on {device}...")
    model = whisper.load_model(settings.model_size, device=device)                 # 旧模型加载方式

    log(f"[{log_prefix}] Transcribing with openai-whisper (language={settings.language})...")
    whisper_result = model.transcribe(
        str(audio_path),                                                          # openai-whisper 也接受字符串路径
        language=settings.language,                                               # 固定中文
        verbose=False,                                                            # 不输出逐段冗长日志
        task="transcribe",                                                        # 只转写
        word_timestamps=False,                                                    # 学习笔记只需要段落时间戳
        condition_on_previous_text=settings.condition_on_previous_text,            # 降低长课串词污染
        no_speech_threshold=0.6,                                                   # 沿用旧脚本静音阈值
    )
    segments = [
        {"from": round(segment["start"], 2), "to": round(segment["end"], 2),
         "content": segment.get("text", "").strip()}
        for segment in whisper_result.get("segments", [])                          # openai-whisper 返回列表
        if segment.get("text", "").strip()                                         # 空白段没有学习价值
    ]

    def transcribe_window(clip_path: Path) -> list[dict]:
        window_result = model.transcribe(                                          # 补转窗口关闭两道静音阈值：窗口音量已实测
            str(clip_path),
            language=settings.language,
            verbose=False,
            task="transcribe",
            word_timestamps=False,
            condition_on_previous_text=False,
            no_speech_threshold=None,                                              # 不再让旧路线二次判定“这是静音”
            logprob_threshold=None,                                                # 低置信度也不跳过，宁可拿到字再人工复核
        )
        return [
            {"from": round(segment["start"], 2), "to": round(segment["end"], 2),
             "content": segment.get("text", "").strip()}
            for segment in window_result.get("segments", [])
            if segment.get("text", "").strip()
        ]

    result = {
        "engine": "openai-whisper",                                               # 明确这是 fallback，不是首选路线
        "model_size": settings.model_size,                                        # 模型尺寸
        "device": device,                                                          # fallback 也记录 cpu/cuda
        "compute_type": None,                                                      # openai-whisper 不暴露 CTranslate2 compute_type
        "cuda_devices": 1 if device == "cuda" else 0,                              # 兼容诊断字段
        "language": settings.language,                                             # 固定输入语言
        "language_probability": None,                                              # 旧路线不统一提供该字段
        "segments": segments,                                                      # 标准分段正文
        "diagnostics": [_new_asr_diagnostic("openai-whisper", True, "fallback ok", device=device)],
    }
    return result, transcribe_window


# --- 把结果补上覆盖率字段与覆盖后的全文 ---
def _finalize(result: dict, guard: dict) -> dict:
    segments = guard["segments"]                                                   # 覆盖率校验后的最终分段
    report = guard["report"]                                                       # 判断依据原样交给调用方
    result["segments"] = segments                                                  # 覆盖掉第一遍可能缺字的分段
    result["text"] = "".join(item["content"] for item in segments).strip()          # 全文必须由最终分段推出，否则仍是缺字版本
    result["duration"] = segments[-1]["to"] if segments else 0                      # 语义保持历史行为：最后一个片段的结束秒数
    result["audio_duration"] = report.get("audio_duration")                        # 新增：音频真实时长，供调用方复核覆盖率
    result["coverage_before"] = report.get("coverage_before")                      # 新增：补转前的覆盖率
    result["coverage_after"] = report.get("coverage_after")                        # 新增：补转后的覆盖率
    result["diagnostics"].append(_new_coverage_diagnostic(report))                  # 覆盖率结论写进统一诊断
    return result


# --- 对外统一转写入口 ---
def transcribe_audio_file(
    audio_path: str | Path,
    model_size: str = "small",
    language: str = "zh",
    device: str | None = None,
    log_prefix: str = "asr",
    allow_openai_fallback: bool = True,
    settings: TranscriptionSettings | None = None,
) -> dict:
    audio = Path(audio_path)                                                       # 把调用方传入的字符串或 Path 统一成 Path
    if not audio.exists():                                                         # 音频不存在时直接失败，避免误报 ASR 问题
        raise FileNotFoundError(f"Audio file not found: {audio}")

    active = settings or TranscriptionSettings(model_size=model_size, language=language)  # 未显式传参数时沿用历史默认值

    faster_error = None                                                            # 保存新路线失败原因，fallback 后仍写进诊断
    faster_traceback = None                                                        # traceback 必须在 except 内捕获，离开后会被 Python 清空
    engine_runner = None                                                           # 成功的引擎会把窗口补转回调一并交回来
    try:
        result, engine_runner = _run_faster_whisper(audio, active, device, log_prefix)  # 首选新路线
    except Exception as exc:
        faster_error = exc                                                         # 记录失败对象，下面决定是否 fallback
        faster_traceback = traceback.format_exc()                                  # 保留真实失败栈，方便定位 CUDA/模型问题
        log(f"[{log_prefix}] faster-whisper failed: {exc}")
        if not allow_openai_fallback:                                              # 用户或测试可禁止旧路线
            raise

    if engine_runner is None:                                                      # 新路线失败，进入兼容兜底
        try:
            fallback_result, engine_runner = _run_openai_whisper(audio, active, device, log_prefix)  # 兼容兜底
            fallback_result["diagnostics"].insert(
                0,
                _new_asr_diagnostic(
                    "faster-whisper",
                    False,
                    str(faster_error),
                    traceback=faster_traceback,
                ),
            )                                                                      # 让调用方知道为什么动用了旧路线
            result = fallback_result                                               # 后续覆盖率校验对新旧路线一视同仁
        except Exception:
            log(f"[{log_prefix}] openai-whisper fallback also failed")
            raise

    if not active.coverage_check:                                                  # 调用方显式关闭校验时保持历史行为
        return _finalize(result, {"segments": result["segments"], "report": {"checked": False, "skipped_reason": "coverage check disabled"}})

    guard = asr_coverage.apply_coverage_guard(
        audio,
        result["segments"],
        transcribe_window=engine_runner,                                           # 由引擎注入“窗口 -> 分段”的实现
        vad_filter=active.vad_filter,                                              # 未启用静音过滤时不存在被吞的风险
        coverage_floor=active.coverage_floor,
        gap_min_seconds=active.gap_min_seconds,
        speech_dbfs=active.speech_dbfs,
        max_windows=active.max_retry_windows,
    )
    return _finalize(result, guard)
