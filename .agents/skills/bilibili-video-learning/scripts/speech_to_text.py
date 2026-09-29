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
import asr_refine                                                                # 可疑区间的定向二次解码（D27）
import asr_lexicon                                                              # 领域词表（见 D26）
import cuda_runtime                                                              # CUDA 运行时库预加载（见 D23）
from runtime_output import log                                                   # ASR 进度写 stderr，JSON 调用方只读 stdout。


ASR_PARAMS_VERSION = 2                                                           # 参数默认值变化时递增，让旧缓存放弃而不是复用


# --- 一次转写任务的全部可调参数 ---
class TranscriptionSettings(NamedTuple):
    model_size: str = "small"                                                    # 模型尺寸，同时决定精度与显存占用
    language: str = "zh"                                                         # 固定中文可减少自动语言识别开销
    beam_size: int = 1                                                           # 长视频优先速度，术语由笔记阶段校正
    vad_filter: bool = True                                                      # 跳过课堂静音；关掉可避免语音被误判丢弃
    vad_min_silence_ms: int = 2000                                                # 与 faster-whisper 默认一致；调小会让 VAD 更容易切断说话
    vad_speech_pad_ms: int = 400                                                  # 人声边界留白，过小会削掉字头字尾
    condition_on_previous_text: bool = False                                     # 降低长课串词污染
    coverage_check: bool = True                                                   # 是否校验转写覆盖率并补转可疑空档
    coverage_floor: float = asr_coverage.DEFAULT_COVERAGE_FLOOR                    # 覆盖率低于该值时在诊断里显式提示
    gap_min_seconds: float = asr_coverage.DEFAULT_GAP_MIN_SECONDS                  # 小于该值的空档视为正常停顿
    speech_dbfs: float = asr_coverage.DEFAULT_SPEECH_DBFS                          # 高于该音量的空档判定为“有人在说话”
    max_retry_windows: int = asr_coverage.DEFAULT_MAX_RETRY_WINDOWS                # 单次运行的补转窗口预算
    low_confidence_logprob: float = -1.0                                          # 与 whisper 默认 logprob 阈值一致
    hotwords: str = ""                                                           # 领域词表（D26）：只做解码偏置，不改写输出
    word_timestamps: bool = False                                                # 词级时间戳：重新分段与帧对齐的前提（D28）
    profile: str = "balanced"                                                    # fast/balanced/quality，见 apply_profile
    refine_low_confidence: bool = False                                          # 是否对低置信区间做定向二次解码（D27）
    normalize_audio: bool = True                                                 # 抽音频时是否做 highpass+loudnorm 净化（D26）

    def identity(self) -> dict:
        return {"params_version": ASR_PARAMS_VERSION, **self._asdict()}           # 缓存键只认这份字典，不认调用点默认值


# --- 转写档位：按"实测 CER / 覆盖率 / 是否要词级时间轴"打包（数字见 docs/DECISIONS.md D26） ---
# 真实抖音音频（259.77s）+ 1917 字金标的实测结果：
#   净化音频 + beam1 + 无词级时间戳 = CER 0.0433（最低）      ← balanced 的默认形态
#   净化音频 + beam5 + 无词级时间戳 = CER 0.0480、覆盖率最高、漏字最少  ← quality
#   净化音频 + beam1 + 词级时间戳   = CER 0.0511，但产出词级时间轴      ← timing
PROFILE_OVERRIDES = {
    "balanced": {"beam_size": 1, "word_timestamps": False, "refine_low_confidence": False},  # 默认：实测最低 CER
    "timing": {"beam_size": 1, "word_timestamps": True, "refine_low_confidence": False},     # 要词级时间戳（重新分段/帧对齐）
    "quality": {"beam_size": 5, "word_timestamps": False, "refine_low_confidence": True},    # 关键素材：最高覆盖率 + 定向重解
}


def apply_profile(settings: "TranscriptionSettings", profile: str) -> "TranscriptionSettings":
    """按档位覆盖可调项；未知档位抛错而不是静默沿用默认值。"""
    overrides = PROFILE_OVERRIDES.get(profile)
    if overrides is None:
        raise ValueError(f"Unknown profile '{profile}'. Choose one of: {', '.join(PROFILE_OVERRIDES)}")
    return settings._replace(profile=profile, **overrides)


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
def _reading_to_segments(reading, *, word_timestamps: bool = True) -> list[dict]:
    segments = []                                                                 # 统一输出成 B站/抖音脚本已有字段名
    for segment in reading:                                                       # faster-whisper 惰性迭代，遍历时才真正解码
        content = segment.text.strip()                                             # 空白段不进入学习材料
        if content:                                                                # 只保留有正文的片段
            segments.append({
                "from": round(segment.start, 2),                                   # 片段开始秒数，用于回看定位
                "to": round(segment.end, 2),                                       # 片段结束秒数，用于计算覆盖
                "content": content,                                                # ASR 正文
                "confidence": _segment_confidence(segment),                        # 平均对数概率，供下游标注不可信区间
                "no_speech_probability": getattr(segment, "no_speech_prob", None),  # 被判定为静音的概率
                "compression_ratio": getattr(segment, "compression_ratio", None),   # 异常高通常意味着复读或幻觉
                "words": _segment_words(segment) if word_timestamps else [],        # 未开启时不写入，避免产出无谓膨胀
            })
    return segments


# --- 取一段的词级时间戳（未开启 word_timestamps 时返回空列表） ---
def _segment_words(segment) -> list[dict]:
    words = []
    for word in getattr(segment, "words", None) or []:
        text = str(getattr(word, "word", "") or "").strip()
        if not text:
            continue
        words.append({"start": round(float(getattr(word, "start", 0.0)), 2),
                      "end": round(float(getattr(word, "end", 0.0)), 2),
                      "word": text})
    return words


# --- 取一段的平均对数概率（不同引擎字段可能缺失） ---
def _segment_confidence(segment) -> float | None:
    value = getattr(segment, "avg_logprob", None)
    return round(float(value), 3) if value is not None else None


# --- 汇总低置信区间：让调用方知道哪些位置需要人工复核 ---
def _low_confidence_spans(segments: list[dict], threshold: float) -> list[dict]:
    spans = []
    for segment in segments:
        confidence = segment.get("confidence")
        if confidence is not None and confidence < threshold:                      # 只标记真的低于阈值的位置
            spans.append({
                "start": segment["from"],
                "end": segment["to"],
                "confidence": confidence,
                "no_speech_probability": segment.get("no_speech_probability"),
                "text": segment["content"][:40],                                   # 只带一小段原文，便于定位
            })
    return spans


# --- 用 faster-whisper 转写 ---
def _run_faster_whisper(
    audio_path: Path,
    settings: TranscriptionSettings,
    requested_device: str | None,
    log_prefix: str,
) -> tuple[dict, object]:
    cuda_report = cuda_runtime.prepare_cuda_libraries()                            # 必须在 import ctranslate2 之前预加载运行时库
    if cuda_report["preloaded"]:
        log(f"[{log_prefix}] Preloaded CUDA runtime: {len(cuda_report['preloaded'])} libraries")
    from faster_whisper import WhisperModel                                       # CTranslate2 后端的 Whisper 实现

    device, compute_type, cuda_devices = _choose_ctranslate2_device(requested_device)  # 决定 cuda/float16 或 CPU/int8
    log(f"[{log_prefix}] Loading faster-whisper '{settings.model_size}' on {device}/{compute_type}...")
    log(f"[{log_prefix}] Python executable: {sys.executable}")
    log(f"[{log_prefix}] Python prefix: {sys.prefix}")

    def load_model(active_device: str, active_compute_type: str):
        log(f"[{log_prefix}] Transcribing with faster-whisper (language={settings.language})...")
        active_model = WhisperModel(settings.model_size, device=active_device, compute_type=active_compute_type)
        reading, info = active_model.transcribe(                                   # 惰性迭代：必须在这里消费才会暴露设备错误
            str(audio_path),                                                          # faster-whisper 接受字符串路径
            language=settings.language,                                               # 固定中文可减少自动语言识别开销
            task="transcribe",                                                        # 学习笔记只需要转写，不做翻译
            beam_size=settings.beam_size,                                             # 由 settings 决定，逐字稿场景可调大
            hotwords=settings.hotwords or None,                                       # 领域词表做解码偏置（D26）
            word_timestamps=settings.word_timestamps,                                 # 词级时间戳供重新分段（D28）
            vad_filter=settings.vad_filter,                                           # 跳过课堂静音和空白段，减少无效解码
            vad_parameters={                                                          # VAD 的激进程度直接决定会不会丢掉整句话
                "min_silence_duration_ms": settings.vad_min_silence_ms,                # 小于该长度的静音不切断
                "speech_pad_ms": settings.vad_speech_pad_ms,                          # 人声两侧保留的缓冲
            } if settings.vad_filter else None,                                       # 关闭 VAD 时不应传参
            condition_on_previous_text=settings.condition_on_previous_text,            # 降低长课串词污染
        )
        return active_model, _reading_to_segments(reading, word_timestamps=settings.word_timestamps), info                     # 返回模型、第一遍正文与语言信息

    device_fallback = None                                                        # 记录“声称有 CUDA 但实际不可用”的真实原因
    try:
        model, segments, transcription_info = load_model(device, compute_type)     # 构造 + 首次解码一起验证设备
    except Exception as exc:
        if device != "cuda" or requested_device == "cuda":                        # 显式要求 CUDA 时不静默降级，交给上层报错
            raise
        log(f"[{log_prefix}] CUDA unusable ({exc}); retrying on cpu/int8")          # 例如缺 libcublas.so.12 的 CPU 机器
        device, compute_type = "cpu", "int8"                                       # 与 _choose_ctranslate2_device 的无 GPU 分支保持一致
        device_fallback = str(exc)                                                # 写进诊断，避免“以为在用 GPU”
        model, segments, transcription_info = load_model(device, compute_type)     # 同一份音频改用 CPU 再跑一遍

    def transcribe_window(clip_path: Path) -> list[dict]:
        window_reading, _ = model.transcribe(                                      # 把丢掉的窗口单独切出来重新解码，上下文完全不同
            str(clip_path),
            language=settings.language,
            task="transcribe",
            beam_size=max(settings.beam_size, 5),                                  # 补转是可疑区间，用更强的解码条件
            hotwords=settings.hotwords or None,                                    # 窗口更短，词表偏置收益更明显
            vad_filter=False,                                                      # 丢字原因不是 VAD（见 D16），这里只是换一种解码条件重试
            condition_on_previous_text=False,
        )
        return _reading_to_segments(window_reading, word_timestamps=settings.word_timestamps)

    result = {
        "engine": "faster-whisper",                                               # 调用方和最终笔记可明确来源
        "model_size": settings.model_size,                                        # 保留模型尺寸，便于解释精度/速度
        "device": device,                                                          # 应为 cuda，除非显式 CPU 或无 GPU
        "compute_type": compute_type,                                              # GPU 默认 float16
        "cuda_devices": cuda_devices,                                              # 诊断字段，用于确认独显可见
        "device_fallback": device_fallback,                                        # 非空说明 CUDA 不可用、已改用 CPU
        "cuda_runtime": {"found": sorted(cuda_report["found"].keys()),
                         "preloaded": cuda_report["preloaded"],
                         "guidance": cuda_report.get("guidance")},                 # 缺库时给出可执行建议
        "profile": settings.profile,                                               # 档位：fast/balanced/quality
        "hotwords": asr_lexicon.describe_hotwords(settings.hotwords),               # 只记规模与截断，不记敏感正文
        "word_timestamps": settings.word_timestamps,                               # 是否产出词级时间戳
        "language": getattr(transcription_info, "language", settings.language),     # 模型报告的语言
        "language_probability": getattr(transcription_info, "language_probability", None),  # 语言置信度
        "segments": segments,                                                      # 标准分段正文
        "diagnostics": [_new_asr_diagnostic("faster-whisper", True, "ok", device=device, compute_type=compute_type,
                                            device_fallback=device_fallback)],
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
def _finalize(result: dict, guard: dict, *, low_confidence_threshold: float) -> dict:
    segments = guard["segments"]                                                   # 覆盖率校验后的最终分段
    report = guard["report"]                                                       # 判断依据原样交给调用方
    result["segments"] = segments                                                  # 覆盖掉第一遍可能缺字的分段
    result["text"] = "".join(item["content"] for item in segments).strip()          # 全文必须由最终分段推出，否则仍是缺字版本
    result["duration"] = segments[-1]["to"] if segments else 0                      # 语义保持历史行为：最后一个片段的结束秒数
    result["audio_duration"] = report.get("audio_duration")                        # 新增：音频真实时长，供调用方复核覆盖率
    result["coverage_before"] = report.get("coverage_before")                      # 新增：补转前的覆盖率
    result["coverage_after"] = report.get("coverage_after")                        # 新增：补转后的覆盖率
    result["diagnostics"].append(_new_coverage_diagnostic(report))                  # 覆盖率结论写进统一诊断
    low_confidence = _low_confidence_spans(segments, low_confidence_threshold)      # 低置信区间交给人工复核
    result["low_confidence_spans"] = low_confidence
    if low_confidence:
        result["diagnostics"].append({
            "step": "asr_confidence",
            "ok": False,                                                           # 有可疑区间就要让调用方看见
            "message": f"{len(low_confidence)} segment(s) below logprob {low_confidence_threshold}",
            "threshold": low_confidence_threshold,
            "spans": low_confidence,
        })
    return result


# --- 对可疑区间做定向二次解码：只替换判定为更好的区间（D27） ---
def _refine_result(audio_path: Path, guard: dict, engine_runner, settings: "TranscriptionSettings") -> tuple[dict, list]:
    import tempfile                                                               # 切片落在临时目录，用完即删

    refine_settings = asr_refine.RefineSettings(logprob_threshold=settings.low_confidence_logprob)

    def refine_window(start: float, end: float) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="asr-refine-") as workspace:
            clip_path = Path(workspace) / "window.wav"
            asr_coverage.cut_audio_window(audio_path, start, end, clip_path)       # 复用覆盖率补转的切窗口实现
            return engine_runner(clip_path)                                        # 复用同一引擎回调，解码条件由引擎决定

    diagnostics: list = []
    refined = asr_refine.apply_refinement(guard["segments"], refine_window, refine_settings, diagnostics)
    guard["segments"] = refined["segments"]                                        # 只替换被接受的区间，其余逐字不动
    guard["refine_report"] = refined["report"]
    return guard, diagnostics


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
        except Exception as fallback_error:
            log(f"[{log_prefix}] openai-whisper fallback also failed: {fallback_error}")
            raise faster_error from fallback_error                                 # 保留根因：真正失败的是主路线，兜底只是没救回来

    if not active.coverage_check:                                                  # 关闭校验时保留第一遍结果，但重解仍可独立生效
        guard = {"segments": result["segments"],
                 "report": {"checked": False, "skipped_reason": "coverage check disabled"}}
    else:
        guard = _apply_coverage(audio, result, engine_runner, active)

    return _finish(result, audio, guard, engine_runner, active, log_prefix)


# --- 覆盖率校验（独立一步，便于与定向重解正交组合） ---
def _apply_coverage(audio: Path, result: dict, engine_runner, settings: "TranscriptionSettings") -> dict:
    return asr_coverage.apply_coverage_guard(
        audio,
        result["segments"],
        transcribe_window=engine_runner,                                           # 由引擎注入“窗口 -> 分段”的实现
        coverage_floor=settings.coverage_floor,
        gap_min_seconds=settings.gap_min_seconds,
        speech_dbfs=settings.speech_dbfs,
        max_windows=settings.max_retry_windows,
    )


# --- 收尾：定向重解 → 汇总字段 → 诊断（与覆盖率校验正交） ---
def _finish(result: dict, audio: Path, guard: dict, engine_runner, settings: "TranscriptionSettings",
            log_prefix: str) -> dict:
    refine_diagnostics: list = []
    if settings.refine_low_confidence:                                             # quality 档位才开启：多花时间换准确率
        log(f"[{log_prefix}] Refining suspicious spans (targeted re-decode)...")
        guard, refine_diagnostics = _refine_result(audio, guard, engine_runner, settings)

    finalized = _finalize(result, guard, low_confidence_threshold=settings.low_confidence_logprob)
    finalized["diagnostics"].extend(refine_diagnostics)                            # 重解的接受/拒绝理由都要留痕
    if guard.get("refine_report") is not None:
        finalized["refine_report"] = guard["refine_report"]                        # 逐段替换理由，供人工复核
    return finalized
