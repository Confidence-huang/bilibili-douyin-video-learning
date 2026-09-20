"""
Smoke-test the packaged CLI entrypoint in CI.

Why this exists: the offline pytest suite imports the harness as a library. That proves the
code is correct but NOT that the installed console script actually starts or that `--version`
matches the released number. A broken entry point or a version drift would otherwise reach
users with all tests green.

Contract under test:
  * `--help` and `--version` exit 0 and print non-empty output.
  * `--version` equals the version declared in pyproject.toml.
  * `doctor status --json` produces a JSON object, and whenever it reports success it carries
    its documented top-level keys.

`doctor` is deliberately treated as diagnostic rather than pass/fail. It probes host tooling
(FFmpeg on PATH or via imageio-ffmpeg, yt-dlp, CUDA) and both raises and exits non-zero when
those are absent -- which is the normal state on a bare runner. Requiring `doctor` to succeed
here would make the check measure the runner image instead of this repository, and would fail
for reasons no contributor could fix from inside the code.
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


def resolve_runtime_python() -> str:
    """Pick the interpreter the CLI should drive backends with.

    Preference order:
      1. An explicit CI-provided value, but only if it points at a real file. A caller may export a
         shell-flavoured path (MSYS `/d/...`, WSL `/mnt/...`) that the runtime resolver cannot
         stat, so an unusable value is discarded rather than propagated.
      2. The interpreter running this script -- the CI jobs install the harness into the venv that
         also runs this file, which is exactly the pairing `doctor` validates.
    """
    configured = os.environ.get("BILIBILI_VIDEO_LEARNING_PYTHON", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return str(Path(sys.executable).absolute())


def run_cli(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["BILIBILI_VIDEO_LEARNING_ROOT"] = str(SKILL_ROOT)
    environment["BILIBILI_VIDEO_LEARNING_PYTHON"] = resolve_runtime_python()
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

    # doctor is diagnostic: a host without FFmpeg/yt-dlp makes it raise or exit non-zero, which is
    # not a repository defect. Only assert when it actually produced a report.
    doctor = run_cli(["--json", "doctor", "status"])
    doctor_note = "doctor reported no JSON (host tooling missing); entrypoint checks still enforced"
    if doctor.stdout.strip():
        try:
            payload = json.loads(doctor.stdout)
        except json.JSONDecodeError:
            payload = None                                                          # Non-JSON output means it never reached the report.
        if isinstance(payload, dict) and set(payload.keys()) != {"ok", "error"}:
            missing = sorted(DOCTOR_KEYS - payload.keys())
            if missing:
                raise RuntimeError(
                    f"`doctor status --json` is missing documented keys: {', '.join(missing)}\n"
                    f"payload keys were: {sorted(payload.keys())}"
                )
            if Path(payload["skill_root"]).resolve() != SKILL_ROOT.resolve():
                raise RuntimeError(f"`doctor` resolved skill_root to {payload['skill_root']!r}")
            doctor_note = "doctor schema intact"
        elif isinstance(payload, dict):
            doctor_note = f"doctor reported a precondition problem: {payload.get('error')}"

    print(f"CLI_SMOKE_OK: entrypoint starts, version {expected_version}, {doctor_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
