#!/usr/bin/env bash
# Install the public Skill transactionally into one user-scoped Linux location.
set -euo pipefail

package_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_skill_root="$package_root/.agents/skills/bilibili-video-learning"
destination_root="${HOME}/.agents/skills/bilibili-video-learning"
command_bin="${HOME}/.local/bin"
skip_runtime=0

usage() {
  printf '%s\n' \
    "Usage: ./install_linux.sh [--destination-root PATH] [--command-bin PATH] [--skip-runtime]" \
    "Installs only in user-scoped paths; never invokes sudo or edits shell startup files."
}

while (($#)); do
  case "$1" in
    --destination-root)
      destination_root="${2:?missing value for --destination-root}"
      shift 2
      ;;
    --command-bin)
      command_bin="${2:?missing value for --command-bin}"
      shift 2
      ;;
    --skip-runtime)
      skip_runtime=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

destination_root="$(realpath -m -- "$destination_root")"
command_bin="$(realpath -m -- "$command_bin")"
user_root="$(realpath -m -- "${HOME}")"
if [[ "$(basename -- "$destination_root")" != "bilibili-video-learning" ]]; then
  printf 'Destination must end with bilibili-video-learning: %s\n' "$destination_root" >&2
  exit 2
fi
if [[ "$destination_root" == "/" || "$destination_root" == "$user_root" ]]; then
  printf 'Refusing broad installation target: %s\n' "$destination_root" >&2
  exit 2
fi
if [[ "$command_bin" == "/" || "$command_bin" == "$user_root" ]]; then
  printf 'Refusing broad command directory: %s\n' "$command_bin" >&2
  exit 2
fi
if [[ ! -f "$source_skill_root/SKILL.md" ]]; then
  printf 'Packaged Skill is missing under: %s\n' "$source_skill_root" >&2
  exit 2
fi

stamp="$(date -u +%Y%m%d-%H%M%S)-$$"
stage_root="${destination_root}.installing-${stamp}"
backup_root=""
mkdir -p -- "$(dirname -- "$destination_root")"
mkdir -- "$stage_root"
tar \
  --exclude='./.venv' --exclude='./.venv-*' \
  --exclude='./__pycache__' --exclude='*/__pycache__' \
  --exclude='./.pytest_cache' --exclude='*/.pytest_cache' \
  --exclude='*.pyc' --exclude='*.egg-info' \
  -C "$source_skill_root" -cf - . | tar -C "$stage_root" -xf -
if [[ ! -f "$stage_root/SKILL.md" ]]; then
  printf 'Staged Skill is incomplete: %s\n' "$stage_root" >&2
  exit 1
fi
if [[ -e "$destination_root" ]]; then
  backup_root="${destination_root}.backup-${stamp}"
  mv -- "$destination_root" "$backup_root"
  printf 'Backed up existing Skill: %s\n' "$backup_root"
fi
if ! mv -- "$stage_root" "$destination_root"; then
  if [[ -n "$backup_root" && ! -e "$destination_root" ]]; then
    mv -- "$backup_root" "$destination_root"
  fi
  exit 1
fi
printf 'Installed Skill source: %s\n' "$destination_root"

if ((skip_runtime)); then
  printf '%s\n' 'Source-only installation complete. Runtime and CLI were intentionally skipped.'
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv was not found. Install uv, reopen the shell, then rerun this installer.' >&2
  exit 1
fi

runtime_python="$destination_root/.venv/bin/python"
UV_PROJECT_ENVIRONMENT="$destination_root/.venv" \
  uv sync --project "$destination_root" --locked --python 3.12 --extra asr
uv pip install --python "$runtime_python" --no-build-isolation --no-deps -e "$destination_root/agent-harness"

mkdir -p -- "$command_bin"
wrapper="$command_bin/cli-anything-video-learning"
escaped_skill_root="$(printf '%q' "$destination_root")"
escaped_runtime_python="$(printf '%q' "$runtime_python")"
{
  printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
  printf 'export BILIBILI_VIDEO_LEARNING_ROOT=%s\n' "$escaped_skill_root"
  printf 'export BILIBILI_VIDEO_LEARNING_PYTHON=%s\n' "$escaped_runtime_python"
  printf 'exec %s -m cli_anything.video_learning "$@"\n' "$escaped_runtime_python"
} > "$wrapper"
chmod 0755 -- "$wrapper"
printf 'Installed CLI wrapper: %s\n' "$wrapper"
case ":${PATH}:" in
  *":${command_bin}:"*) ;;
  *) printf 'Add this directory to PATH if needed: %s\n' "$command_bin" ;;
esac
printf 'Run verification: ./verify_linux.sh --skill-root %q\n' "$destination_root"
