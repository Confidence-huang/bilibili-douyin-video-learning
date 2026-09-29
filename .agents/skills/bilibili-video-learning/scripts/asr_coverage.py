#!/usr/bin/env python3
r"""
ASR 覆盖率校验：找出被 VAD 静默吃掉的语音窗口，并在必要时局部重转。

为什么需要这个模块：
    faster-whisper 的 `vad_filter=True` 在"人声 + 背景音乐/噪声"的片段上可能把整段判成静音，
    直接丢弃 5 秒以上的正文；调用方只会看到"少了几句话"，没有任何报错。
    实测案例与阈值依据见 docs/DECISIONS.md D16。

本模块的边界：
    只做"量音频时长 / 量窗口音量 / 找可疑空档 / 合并重转结果"，
    不加载任何 ASR 模型。真正的重转由调用方通过 `transcribe_window` 回调注入，
    因此覆盖率逻辑可以在没有 GPU、没有模型、没有网络的 CI 里被完整测试。

调用示例：
    from asr_coverage import apply_coverage_guard
    report = apply_coverage_guard(audio, segments, transcribe_window=model_window, settings=settings)
"""
from __future__ import annotations                                                   # 允许在返回结构里使用现代类型标注

import re                                                                            # 从 ffmpeg stderr 里解析时长与音量
import subprocess                                                                    # 只用 ffmpeg 探测，不引入 ffprobe 依赖
import tempfile                                                                      # 局部重转的切片落在临时目录，随后必删
from pathlib import Path                                                             # 统一处理 Windows 音频路径

from media_tools import find_ffmpeg                                                  # 与全仓共用同一套 FFmpeg 解析规则
from runtime_output import log                                                       # 覆盖率诊断写 stderr，不污染 JSON stdout


DEFAULT_COVERAGE_FLOOR = 0.97                                                        # 转写覆盖不足 97% 时长就值得怀疑
DEFAULT_GAP_MIN_SECONDS = 2.0                                                        # 小于 2 秒的空档通常是正常停顿
DEFAULT_SPEECH_DBFS = -35.0                                                          # 高于 -35 dBFS 的空档判定为"有人在说话"
DEFAULT_MAX_RETRY_WINDOWS = 5                                                        # 一次运行最多补转 5 个窗口，兜住耗时
DEFAULT_MAX_WINDOW_SECONDS = 60.0                                                    # 超过 1 分钟的可疑段只报告不补转
PROBE_TIMEOUT_SECONDS = 120                                                          # 单个探测命令的超时，避免卡死整条链路

DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}\.\d+)")             # ffmpeg 输入信息里的总时长
MEAN_VOLUME_PATTERN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")           # volumedetect 的平均音量


# --- 探测音频总时长 ---
def audio_duration_seconds(audio_path: str | Path) -> float | None:
    command = [                                                                       # 用 `-f null -` 只解码不落盘
        find_ffmpeg(), "-hide_banner", "-i", str(audio_path), "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:                              # FFmpeg 缺失或超时都属于"无法校验"
        log(f"[asr-coverage] duration probe unavailable: {exc}")
        return None
    match = DURATION_PATTERN.search(completed.stderr or "")                           # 时长只出现在 stderr 的输入信息块
    if match is None:
        log("[asr-coverage] duration not found in ffmpeg output")                     # 解析失败时返回 None，绝不让它中断转写
        return None
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return round(hours * 3600 + minutes * 60 + seconds, 2)


# --- 探测某个时间窗口的平均音量 ---
def measure_window_dbfs(audio_path: str | Path, start: float, end: float) -> float | None:
    window = max(end - start, 0.1)                                                    # 零长度窗口会让 ffmpeg 直接报错
    command = [
        find_ffmpeg(), "-hide_banner", "-loglevel", "info",
        "-ss", f"{start:.2f}", "-t", f"{window:.2f}",                                 # 先定位再解码，避免整段音频重解
        "-i", str(audio_path), "-vn", "-af", "volumedetect", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:                              # 拿不到音量就不做判断，保持保守
        log(f"[asr-coverage] volume probe unavailable: {exc}")
        return None
    match = MEAN_VOLUME_PATTERN.search(completed.stderr or "")
    return float(match.group(1)) if match else None


# --- 找出转写段之间的可疑空档 ---
def find_coverage_gaps(segments: list[dict], audio_duration: float, min_gap_seconds: float) -> list[dict]:
    ordered = sorted(segments, key=lambda item: float(item["from"]))                   # 先按开始时间排序，空档才可比较
    gaps: list[dict] = []                                                             # 每个元素形如 {"start", "end", "seconds"}
    if not ordered:                                                                   # 整段音频一个字都没识别出来
        return [{"start": 0.0, "end": round(audio_duration, 2), "seconds": round(audio_duration, 2)}]

    if float(ordered[0]["from"]) >= min_gap_seconds:                                  # 开头被吃掉的情况
        gaps.append({"start": 0.0, "end": round(float(ordered[0]["from"]), 2),
                     "seconds": round(float(ordered[0]["from"]), 2)})
    for previous, current in zip(ordered, ordered[1:]):                                # 段与段之间被吃掉的情况
        start, end = float(previous["to"]), float(current["from"])
        if end - start >= min_gap_seconds:
            gaps.append({"start": round(start, 2), "end": round(end, 2), "seconds": round(end - start, 2)})
    tail_start = float(ordered[-1]["to"])                                              # 结尾被吃掉的情况
    if audio_duration - tail_start >= min_gap_seconds:
        gaps.append({"start": round(tail_start, 2), "end": round(audio_duration, 2),
                     "seconds": round(audio_duration - tail_start, 2)})
    return gaps


# --- 计算转写覆盖的时长比例 ---
def coverage_ratio(segments: list[dict], audio_duration: float) -> float:
    if audio_duration <= 0:                                                            # 时长未知时不做除法，返回满覆盖避免误报
        return 1.0
    covered = 0.0                                                                      # 已合并区间的总长度
    open_start = None                                                                  # 当前连续区间的起点
    open_end = None                                                                    # 当前连续区间的终点
    for segment in sorted(segments, key=lambda item: float(item["from"])):              # 按开始时间排序后才能线性合并
        start = max(float(segment["from"]), 0.0)                                       # 越界开始时间夹到 0
        end = min(float(segment["to"]), audio_duration)                                # 越界结束时间夹到音频末尾
        if end <= start:                                                               # 空区间或倒置区间不参与统计
            continue
        if open_end is None or start > open_end:                                       # 与上一段不重叠：结算上一段，开启新段
            if open_end is not None:
                covered += open_end - open_start
            open_start, open_end = start, end
        else:                                                                          # 与上一段重叠：只把终点向后扩
            open_end = max(open_end, end)
    if open_end is not None:                                                           # 结算最后一段
        covered += open_end - open_start
    return round(min(covered / audio_duration, 1.0), 4)


# --- 从空档中挑出"确实有人在说话"的窗口 ---
def select_retry_windows(
    audio_path: str | Path,
    gaps: list[dict],
    speech_dbfs: float = DEFAULT_SPEECH_DBFS,
    max_windows: int = DEFAULT_MAX_RETRY_WINDOWS,
    max_window_seconds: float = DEFAULT_MAX_WINDOW_SECONDS,
    measure=None,                                                                      # 允许测试注入音量探测
) -> list[dict]:
    measure = measure or measure_window_dbfs                                           # 调用时才解析，避免默认参数提前绑定
    selected: list[dict] = []                                                          # 只保留值得花时间补转的窗口
    for gap in gaps:
        if len(selected) >= max_windows:                                               # 超出预算的窗口只记录不补转
            log(f"[asr-coverage] retry budget reached, skipping gap {gap['start']}-{gap['end']}s")
            break
        if gap["seconds"] > max_window_seconds:                                        # 过长空档可能是纯音乐，补转代价也不划算
            log(f"[asr-coverage] gap {gap['start']}-{gap['end']}s is too long to retry")
            continue
        dbfs = measure(audio_path, gap["start"], gap["end"])                            # 唯一能区分"静音"和"被误判的语音"的证据
        if dbfs is None:                                                               # 量不出音量时保守跳过，避免误报
            continue
        if dbfs > speech_dbfs:
            selected.append({**gap, "dbfs": dbfs})                                     # 带上音量，诊断里能自证判断依据
    return selected


# --- 切出待补转的音频窗口 ---
def cut_audio_window(audio_path: str | Path, start: float, end: float, out_path: str | Path) -> Path:
    window = max(end - start, 0.1)                                                     # 与音量探测保持一致的最小窗口
    command = [
        find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.2f}", "-t", f"{window:.2f}",                                  # 先定位再解码，切片更快
        "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",                      # ASR 只需要 16k 单声道
        "-c:a", "pcm_s16le", str(out_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to cut {start}-{end}s window: {completed.stderr.strip()}")
    return Path(out_path)


# --- 把补转结果并回原时间轴 ---
def merge_retried_segments(segments: list[dict], retried: list[dict], windows: list[dict]) -> list[dict]:
    def inside_any_window(segment: dict) -> bool:                                       # 原段落在补转窗口内即视为已被替换
        middle = (float(segment["from"]) + float(segment["to"])) / 2
        return any(window["start"] <= middle <= window["end"] for window in windows)

    merged = [segment for segment in segments if not inside_any_window(segment)]        # 先移掉被覆盖的旧段
    merged.extend(retried)                                                             # 再放入重转得到的新段
    merged.sort(key=lambda item: float(item["from"]))                                   # 时间轴必须单调，下游按时间取用
    deduplicated: list[dict] = []                                                      # 相邻重复文本只保留更长的一条
    for segment in merged:
        previous = deduplicated[-1] if deduplicated else None
        if previous is not None and previous["content"].strip() and previous["content"].strip() == segment["content"].strip():
            previous["to"] = max(float(previous["to"]), float(segment["to"]))
            continue
        deduplicated.append(dict(segment))
    return deduplicated


# --- 覆盖率的统一入口：先找空档，再决定是否补转 ---
def apply_coverage_guard(
    audio_path: str | Path,
    segments: list[dict],
    transcribe_window,                                                                  # 回调：把窗口音频转成 [{from,to,content}]
    *,
    vad_filter: bool = True,                                                            # 只有在 VAD 开启时才存在"被 VAD 吃掉"的风险
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
    gap_min_seconds: float = DEFAULT_GAP_MIN_SECONDS,
    speech_dbfs: float = DEFAULT_SPEECH_DBFS,
    max_windows: int = DEFAULT_MAX_RETRY_WINDOWS,
    max_window_seconds: float = DEFAULT_MAX_WINDOW_SECONDS,
    audio_duration: float | None = None,                                                # 允许调用方复用已探测的时长
    measure_window=None,                                                                # 允许测试注入音量探测
) -> dict:
    measure_window = measure_window or measure_window_dbfs                              # 调用时才解析，避免默认参数提前绑定
    audio_duration = audio_duration if audio_duration is not None else audio_duration_seconds(audio_path)
    report = {                                                                          # 无论是否补转，都把判断依据交给调用方
        "checked": False,
        "audio_duration": audio_duration,
        "coverage_before": None,
        "coverage_after": None,
        "gaps": [],
        "retried_windows": [],
        "skipped_reason": None,
    }
    if audio_duration is None:                                                          # 量不到时长就无法校验，明确写在报告里
        report["skipped_reason"] = "audio duration unavailable"
        return {"segments": segments, "report": report}
    if not vad_filter:                                                                  # 未启用 VAD 时不会出现整段被吞，直接给出覆盖率
        report["checked"] = True
        report["coverage_before"] = report["coverage_after"] = coverage_ratio(segments, audio_duration)
        report["skipped_reason"] = "vad disabled, nothing to recover"
        return {"segments": segments, "report": report}

    gaps = find_coverage_gaps(segments, audio_duration, gap_min_seconds)                # 第一步：时间轴上的空洞
    report["checked"] = True
    report["gaps"] = gaps
    report["coverage_before"] = coverage_ratio(segments, audio_duration)
    windows = select_retry_windows(audio_path, gaps, speech_dbfs, max_windows, max_window_seconds,
                                   measure=measure_window) if gaps else []
    if not windows:                                                                     # 没有可疑窗口时工作到此为止
        report["coverage_after"] = report["coverage_before"]
        if gaps and coverage_ratio(segments, audio_duration) < coverage_floor:
            report["skipped_reason"] = "gaps are silent or beyond retry budget"         # 说明"看到空洞但确认不是人声"
        return {"segments": segments, "report": report}

    recovered: list[dict] = []                                                          # 收集所有补转出来的新片段
    with tempfile.TemporaryDirectory(prefix="asr-coverage-") as workspace:
        for index, window in enumerate(windows):                                        # 逐个窗口补转，单个失败不影响其余窗口
            clip = Path(workspace) / f"window_{index:02d}.wav"
            try:
                cut_audio_window(audio_path, window["start"], window["end"], clip)
                for segment in transcribe_window(clip):                                 # 回调返回相对切片的时间，需要加回偏移
                    recovered.append({
                        "from": round(window["start"] + float(segment["from"]), 2),
                        "to": round(window["start"] + float(segment["to"]), 2),
                        "content": segment["content"],
                    })
            except Exception as exc:                                                    # 补转失败只降级为诊断，不推翻已经拿到的正文
                log(f"[asr-coverage] retry failed for {window['start']}-{window['end']}s: {exc}")
                window["error"] = str(exc)
            report["retried_windows"].append(window)

    merged = merge_retried_segments(segments, recovered, windows)                        # 合并后重新计算覆盖率
    report["coverage_after"] = coverage_ratio(merged, audio_duration)
    report["recovered_segments"] = len(recovered)
    if recovered:
        log(f"[asr-coverage] recovered {len(recovered)} segments from {len(windows)} window(s)")
    return {"segments": merged, "report": report}
