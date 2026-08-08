#!/usr/bin/env bash
# Verify source or an installed Linux runtime without platform network access.
set -euo pipefail

skill_root="${HOME}/.agents/skills/bilibili-video-learning"
skip_runtime=0
skip_tests=0

while (($#)); do
  case "$1" in
    --skill-root)
      skill_root="${2:?missing value for --skill-root}"
      shift 2
      ;;
    --skip-runtime)
      skip_runtime=1
      shift
      ;;
    --skip-tests)
      skip_tests=1
      shift
      ;;
    --help|-h)
      printf '%s\n' 'Usage: ./verify_linux.sh [--skill-root PATH] [--skip-runtime] [--skip-tests]'
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

skill_root="$(realpath -- "$skill_root")"
required_files=(
  SKILL.md agents/openai.yaml pyproject.toml uv.lock
  scripts/fetch_bilibili.py scripts/runtime_output.py scripts/media_tools.py
  agent-harness/setup.py agent-harness/cli_anything/video_learning/video_learning_cli.py
)
for relative_path in "${required_files[@]}"; do
  [[ -f "$skill_root/$relative_path" ]] || {
    printf 'Required file is missing: %s\n' "$relative_path" >&2
    exit 1
  }
done

validation_python=""
if [[ -x "$skill_root/.venv/bin/python" ]]; then
  validation_python="$skill_root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  validation_python="$(command -v python3)"
else
  printf '%s\n' 'No Python interpreter was found for source validation.' >&2
  exit 1
fi

"$validation_python" - "$skill_root" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve()
excluded = {".venv", ".venv-gpu", "__pycache__", ".pytest_cache"}
files = [path for path in root.rglob("*.py") if not excluded.intersection(path.parts)]
for path in files:
    compile(path.read_bytes(), str(path), "exec")
for path in root.rglob("*"):
    relative_parts = path.relative_to(root).parts
    if excluded.intersection(relative_parts):
        continue
    if path.is_symlink():
        raise RuntimeError(f"Symlink is not allowed inside shared Skill: {path}")
    if path.is_file() and path.suffix.lower() in {".mp3", ".mp4", ".wav", ".mkv", ".pem", ".key"}:
        raise RuntimeError(f"Private/media artifact found: {path}")
print(f"Python syntax: {len(files)} files compiled in memory")
PY

if ((skip_runtime)); then
  printf '%s\n' 'VERIFY_OK (source-only; runtime and tests intentionally skipped)'
  exit 0
fi

runtime_python="$skill_root/.venv/bin/python"
[[ -x "$runtime_python" ]] || {
  printf 'Runtime is missing: %s\n' "$runtime_python" >&2
  exit 1
}
uv pip check --python "$runtime_python"
PYTHONPATH="$skill_root/agent-harness" \
  BILIBILI_VIDEO_LEARNING_ROOT="$skill_root" \
  BILIBILI_VIDEO_LEARNING_PYTHON="$runtime_python" \
  "$runtime_python" -m cli_anything.video_learning --version
if ((!skip_tests)); then
  PYTHONPATH="$skill_root/agent-harness" \
    BILIBILI_VIDEO_LEARNING_ROOT="$skill_root" \
    BILIBILI_VIDEO_LEARNING_PYTHON="$runtime_python" \
    VIDEO_LEARNING_SKIP_INSTALLED_RUNTIME_TESTS=1 \
    "$runtime_python" -m pytest -q "$skill_root/agent-harness/cli_anything/video_learning/tests"
fi
printf '%s\n' 'VERIFY_OK'
