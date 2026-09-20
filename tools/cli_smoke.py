"""
Smoke-test the packaged CLI entrypoint in CI.

Why this exists: the offline pytest suite imports the harness as a library. That proves the
code is correct but NOT that the installed console script actually starts, that `--version`
matches the released number, or that `doctor status` still emits parseable JSON. A broken
entry point or a version drift would otherwise reach users with all tests green.

Contract under test (deliberately structural, not value-based):
  * `--help` and `--version` exit 0 and print non-empty output.
  * `--version` equals the version declared in pyproject.toml.
  * `doctor status --json` emits a JSON object carrying its documented top-level keys.

`doctor` intentionally reports `ok: false` when yt-dlp or FFmpeg is missing, and CI installs
neither. So this script asserts on shape and exit code, never on `ok` or tool availability --
asserting on `ok` here would fail CI on a healthy checkout.
Run with: python tools/cli_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "bilibili-video-learning"
CLI_NAME = "cli-anything-video-learning"
DOCTOR_KEYS = {"ok", "skill_root", "runtime_python", "tools", "python_modules", "gpu"}


def declared_version() -> str:
    """Read the released version from packaging metadata so drift is caught, not assumed."""
    text = (SKILL_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if match is None:
        raise RuntimeError("pyproject.toml: version declaration not found")
    return match.group(1)


def cli_command() -> list[str]:
    """Resolve the console script, preferring the interpreter that owns this process."""
    scripts_dir = Path(sys.executable).parent
    for suffix in (".exe", ".cmd", ""):
        candidate = scripts_dir / f"{CLI_NAME}{suffix}"
        if candidate.exists():
            return [str(candidate)]
    raise RuntimeError(f"{CLI_NAME} was not installed next to {sys.executable}")


def run_cli(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["BILIBILI_VIDEO_LEARNING_ROOT"] = str(SKILL_ROOT)
    environment["BILIBILI_VIDEO_LEARNING_PYTHON"] = sys.executable
    return subprocess.run(
        [*cli_command(), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",                                                          # CI sets PYTHONUTF8=1 on both platforms.
        errors="replace",
        env=environment,
        check=False,
    )


def assert_simple_output(arguments: list[str]) -> str:
    completed = run_cli(arguments)
    label = " ".join(arguments)
    if completed.returncode != 0:
        raise RuntimeError(f"`{label}` exited {completed.returncode}\n{completed.stderr.strip()}")
    if not completed.stdout.strip():
        raise RuntimeError(f"`{label}` produced no output")
    return completed.stdout.strip()


def main() -> int:
    expected_version = declared_version()

    assert_simple_output(["--help"])

    reported_version = assert_simple_output(["--version"])
    if expected_version not in reported_version:
        raise RuntimeError(
            f"`--version` reported {reported_version!r} but pyproject.toml declares {expected_version}"
        )

    # doctor exits non-zero when required tools are absent, which is expected in CI.
    doctor = run_cli(["--json", "doctor", "status"])
    try:
        payload = json.loads(doctor.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"`doctor status --json` did not emit JSON: {doctor.stdout[:400]!r}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"`doctor status --json` must emit a JSON object, got {type(payload).__name__}")
    missing = sorted(DOCTOR_KEYS - payload.keys())
    if missing:
        raise RuntimeError(f"`doctor status --json` is missing documented keys: {', '.join(missing)}")
    if Path(payload["skill_root"]).resolve() != SKILL_ROOT.resolve():
        raise RuntimeError(f"`doctor` resolved skill_root to {payload['skill_root']!r}")

    print(f"CLI_SMOKE_OK: entrypoint starts, version {expected_version}, doctor schema intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
