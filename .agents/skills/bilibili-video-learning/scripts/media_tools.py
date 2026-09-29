"""Resolve the FFmpeg executable for every media pipeline in this Skill."""
from __future__ import annotations

import shutil
from pathlib import Path


def _imageio_ffmpeg_executable() -> str:
    """Return the user-scoped imageio-ffmpeg binary without hiding import errors."""
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def find_ffmpeg() -> str:
    """Prefer a host FFmpeg, then use the Skill-owned user-scoped fallback."""
    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        return path_ffmpeg

    try:
        bundled_ffmpeg = _imageio_ffmpeg_executable()
    except Exception as error:
        raise RuntimeError(
            "FFmpeg is unavailable. Install the Skill runtime or provide ffmpeg on PATH."
        ) from error

    if not Path(bundled_ffmpeg).is_file():
        raise RuntimeError(f"imageio-ffmpeg returned a missing binary: {bundled_ffmpeg}")
    return bundled_ffmpeg


# --- ASR 音频前端滤镜链：抖音叠 BGM、B站音乐开场都会污染解码，先做基础净化 ---
ASR_HIGHPASS_HZ = 70                 # 去掉 70Hz 以下的车噪/桌面震动，人声基频不受影响
ASR_LOUDNESS_TARGET = -16            # EBU R128 目标响度：让安静段落与响亮段落进入同一动态范围
ASR_TRUE_PEAK = -1.5                 # 限峰，避免 loudnorm 后削波
ASR_LOUDNESS_RANGE = 11              # 动态范围上限；口播类内容足够
SAMPLE_RATE = 16000                  # Whisper 系模型的期望采样率


def build_audio_filter_chain(*, normalize: bool = True, denoise: bool = False, highpass_hz: int = ASR_HIGHPASS_HZ) -> str:
    """返回 ffmpeg -af 的滤镜链；normalize=False 表示**完全不处理**（与历史行为一致）。

    取舍：用 ffmpeg 内置滤镜而不是神经网络降噪——零新依赖、完全可复现；
    denoise 默认关闭，因为 afftdn 在纯人声上会引入轻微金属感，收益不稳定。
    实测提醒（D26）：在已经干净的口播素材上，loudnorm 反而让 CER 变差，因此默认关闭、按源启用。
    """
    if not normalize:
        return ""
    filters = []
    if highpass_hz:
        filters.append(f"highpass=f={highpass_hz}")
    if denoise:
        filters.append("afftdn=nf=-25")
    if normalize:
        filters.append(f"loudnorm=I={ASR_LOUDNESS_TARGET}:TP={ASR_TRUE_PEAK}:LRA={ASR_LOUDNESS_RANGE}")
    return ",".join(filters)
