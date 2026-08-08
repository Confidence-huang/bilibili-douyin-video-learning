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
