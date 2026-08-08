"""Optional installed-runtime acceptance test for the shared FFmpeg path."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from douyin_extract import extract_audio
from media_tools import find_ffmpeg


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bvl-local-media-") as temporary_directory:
        root = Path(temporary_directory)
        video_path = root / "fixture.mp4"
        audio_path = root / "fixture.wav"
        ffmpeg = find_ffmpeg()
        generated = subprocess.run(
            [
                ffmpeg,
                "-f", "lavfi", "-i", "color=c=black:s=160x90:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-shortest", "-c:v", "mpeg4", "-c:a", "aac", "-y", str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if generated.returncode != 0:
            raise RuntimeError("Synthetic local video generation failed")
        extract_audio(str(video_path), str(audio_path))
        with wave.open(str(audio_path), "rb") as wav_file:
            result = {
                "status": "PASS",
                "channels": wav_file.getnchannels(),
                "sampleRate": wav_file.getframerate(),
                "frames": wav_file.getnframes(),
            }
        if result["channels"] != 1 or result["sampleRate"] != 16000 or result["frames"] < 15000:
            raise RuntimeError(f"Unexpected WAV shape: {result}")
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
