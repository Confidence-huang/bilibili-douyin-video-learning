"""
Validate the public repository before CI accepts a change.
The check reads only Git-tracked files, rejects private/runtime artifacts and
machine-specific paths, validates the main Skill frontmatter, rejects nested
Skill entrypoints, and confirms that the two packaged CLI guides are byte-identical.
Run with: python tools/validate_repository.py
"""
from __future__ import annotations  # Modern type hints keep the validation data explicit.

import re  # Privacy patterns catch owner paths, local proxies, and credential shapes.
import subprocess  # Git supplies the exact public file boundary instead of a broad disk scan.
from pathlib import Path  # Repository-relative paths remain portable across CI and Windows.

import yaml  # Skill frontmatter uses the same YAML format consumed by agent hosts.


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]  # tools/ is one level below the Git root.
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "bilibili-video-learning"
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".json", ".txt", ".ps1", ".sh"}
FORBIDDEN_SUFFIXES = {".mp3", ".mp4", ".wav", ".mkv", ".pem", ".key"}
MACHINE_PATTERN = re.compile(
    r"C:\\Users\\[^\\]+|[D-F]:\\(?:CodexProjects|Notes|Archive|Study)|127\.0\.0\.1:\d{2,5}",
    flags=re.IGNORECASE,
)
CREDENTIAL_PATTERN = re.compile(
    r"BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}",
)


# --- Read the exact Git publication boundary ---
def publishable_paths() -> list[Path]:
    completed = subprocess.run(
        [
            "git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z",
            "--cached", "--others", "--exclude-standard",
        ],                                                                         # Local candidates and committed CI files share one gate.
        capture_output=True,
        check=True,
    )
    relative_paths = completed.stdout.decode("utf-8").split("\0")                 # Git paths are UTF-8 in this repository.
    return [REPOSITORY_ROOT / path for path in relative_paths if path]


# --- Reject private, generated, or machine-bound tracked content ---
def validate_tracked_content(paths: list[Path]) -> None:
    failures: list[str] = []                                                       # CI reports every bad file in one run.
    for path in paths:
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.lower() in {".env", "cookies.txt", "cookie.txt"}:
            failures.append(f"forbidden tracked artifact: {relative_path}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue                                                              # Binary license/assets are outside text scanning.

        text = path.read_text(encoding="utf-8-sig")                                # BOM-aware UTF-8 matches Windows PowerShell files.
        if MACHINE_PATTERN.search(text):
            failures.append(f"machine-specific path or proxy: {relative_path}")
        if CREDENTIAL_PATTERN.search(text):
            failures.append(f"possible credential material: {relative_path}")

    if failures:
        raise RuntimeError("\n".join(failures))                                    # A failed boundary must stop publication.


# --- Validate the main Agent Skill identity ---
def validate_skill_frontmatter() -> None:
    skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    if not skill_text.startswith("---\n"):
        raise RuntimeError("SKILL.md must start with YAML frontmatter")
    _, frontmatter_text, _body = skill_text.split("---", 2)                       # Only the first metadata block is authoritative.
    frontmatter = yaml.safe_load(frontmatter_text)
    if not isinstance(frontmatter, dict):
        raise RuntimeError("SKILL.md frontmatter must be a YAML mapping")
    if frontmatter.get("name") != "bilibili-video-learning":
        raise RuntimeError("SKILL.md name must remain bilibili-video-learning")
    if not str(frontmatter.get("description") or "").strip():
        raise RuntimeError("SKILL.md description must not be empty")


# --- Keep one lifecycle-visible Skill entrypoint ---
def validate_no_nested_skill_entrypoints(paths: list[Path]) -> None:
    main_skill = SKILL_ROOT / "SKILL.md"
    nested_skills = sorted(
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in paths
        if path.name == "SKILL.md" and path != main_skill and SKILL_ROOT in path.parents
    )
    if nested_skills:
        joined = "\n".join(f"nested Skill entrypoint: {path}" for path in nested_skills)
        raise RuntimeError(joined)


# --- Keep both CLI guide installation paths synchronized ---
def validate_cli_skill_parity() -> None:
    packaged_skill = SKILL_ROOT / "agent-harness" / "cli_anything" / "video_learning" / "skills" / "CLI_GUIDE.md"
    source_skill = SKILL_ROOT / "agent-harness" / "skills" / "cli-anything-video-learning" / "CLI_GUIDE.md"
    if packaged_skill.read_bytes() != source_skill.read_bytes():
        raise RuntimeError("The two cli-anything-video-learning CLI_GUIDE.md files differ")


# --- Run all publication gates and provide one compact success line ---
def main() -> int:
    paths = publishable_paths()                                                    # Discovery happens once for consistent counts.
    validate_tracked_content(paths)
    validate_skill_frontmatter()
    validate_no_nested_skill_entrypoints(paths)
    validate_cli_skill_parity()
    print(f"REPOSITORY_OK: {len(paths)} publishable files passed privacy, Skill, and parity checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())                                                       # Nonzero exceptions fail CI visibly.
