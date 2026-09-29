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

# --- 词表/裁决表：每条都必须带证据注释（把"要加词请附实测"从注释变成规则，见 D39）---
EVIDENCE_FILES = ("references/asr-lexicon.txt", "references/conflict-preferences.txt")


def report_unevidenced_reference_entries() -> list[str]:
    offenders = []
    for relative in EVIDENCE_FILES:
        path = SKILL_ROOT / relative
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "#" not in stripped:                                               # 没有证据注释 → 记下位置
                offenders.append(f"{relative}:{number}")
    return offenders


# --- 提示词模板必须有 template-version，且同名模板版本不得互相矛盾（见 D39）---
def report_prompt_template_versions() -> list[str]:
    problems = []
    seen: dict[str, str] = {}
    for path in sorted((SKILL_ROOT / "prompts").glob("*.md")):
        versions = re.findall(r"template-version:\s*(\S+)", path.read_text(encoding="utf-8", errors="replace"))
        if not versions:
            problems.append(f"{path.name}:missing-template-version")
            continue
        if len(set(versions)) > 1:
            problems.append(f"{path.name}:contradictory-versions")
        seen[path.name] = versions[0]
    return problems





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


# --- Keep every declared version in lockstep ---
def declared_version(text: str, pattern: re.Pattern[str], label: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise RuntimeError(f"{label}: version declaration not found")
    return match.group(1)


def validate_version_parity() -> None:
    """Every version declaration must agree, so a bump cannot silently miss one file."""
    version_pattern = re.compile(r'^\s*version\s*=\s*"([^"]+)"', flags=re.MULTILINE)
    dunder_pattern = re.compile(r'^__version__\s*=\s*"([^"]+)"', flags=re.MULTILINE)
    harness = SKILL_ROOT / "agent-harness"
    declared = {
        "pyproject.toml": declared_version(
            (SKILL_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
            version_pattern,
            "pyproject.toml",
        ),
        "agent-harness/setup.py": declared_version(
            (harness / "setup.py").read_text(encoding="utf-8"),
            version_pattern,
            "agent-harness/setup.py",
        ),
        # The CLI banner and `--version` read this constant, not the packaging metadata.
        "cli_anything/video_learning/__init__.py": declared_version(
            (harness / "cli_anything" / "video_learning" / "__init__.py").read_text(encoding="utf-8"),
            dunder_pattern,
            "cli_anything/video_learning/__init__.py",
        ),
    }
    if len(set(declared.values())) != 1:
        details = "\n".join(f"  {name}: {version}" for name, version in declared.items())
        raise RuntimeError(f"version declarations disagree:\n{details}")


# --- Keep every host-side interface manifest in agreement ---
def validate_host_manifests() -> None:
    """All host manifests must agree on the shared presentation fields.

    `openai.yaml` is the original; `claude.yaml` and `gemini.yaml` exist because
    each host reads its own file. Duplicating a display name across three files is
    exactly how a rename ends up applied to one host and forgotten in the others,
    so the shared subset is compared here instead of trusted.
    """
    agents_dir = SKILL_ROOT / "agents"
    shared_keys = ("display_name", "short_description", "brand_color")
    manifests: dict[str, dict] = {}
    for host in ("openai", "claude", "gemini"):
        path = agents_dir / f"{host}.yaml"
        if not path.is_file():
            raise RuntimeError(f"missing host manifest: agents/{host}.yaml")
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("interface"), dict):
            raise RuntimeError(f"agents/{host}.yaml must define an `interface:` mapping")
        manifests[host] = payload["interface"]

    reference_name = manifests["openai"].get("display_name")
    for host, interface in manifests.items():
        for key in shared_keys:
            value = interface.get(key)
            if not value:
                raise RuntimeError(f"agents/{host}.yaml is missing interface.{key}")
            if value != manifests["openai"][key]:
                raise RuntimeError(
                    f"agents/{host}.yaml interface.{key} is {value!r}, "
                    f"but agents/openai.yaml declares {manifests['openai'][key]!r}"
                )
        invocation = interface.get("invocation_name") or str(interface.get("default_prompt") or "")
        if "bilibili-video-learning" not in invocation:
            raise RuntimeError(
                f"agents/{host}.yaml does not reference the Skill name {reference_name!r} "
                "through invocation_name or default_prompt"
            )


# --- Run all publication gates and provide one compact success line ---
def main() -> int:
    paths = publishable_paths()                                                    # Discovery happens once for consistent counts.
    validate_tracked_content(paths)
    validate_skill_frontmatter()
    validate_no_nested_skill_entrypoints(paths)
    validate_cli_skill_parity()
    validate_version_parity()
    validate_host_manifests()
    for label, offender_list in (("unevidenced reference entries", report_unevidenced_reference_entries()),
                                 ("prompt template version problems", report_prompt_template_versions())):
        if offender_list:
            print(f"NOTE: {label}: " + ", ".join(offender_list))                  # 非致命：只提示
    orphans = report_unreferenced_scripts()                                       # 非致命：只提示，不让 CI 变红
    if orphans:
        print("NOTE: scripts not referenced by any entry point (consider wiring or removing): "
              + ", ".join(orphans))
    print(
        f"REPOSITORY_OK: {len(paths)} publishable files passed privacy, Skill, "
        "parity, version, and host-manifest checks"
    )
    return 0


# --- 未被任何入口引用的脚本：只报告、不失败（避免半成品悄悄留在仓库里，见 D36）---
INTENTIONALLY_LIBRARY_ONLY = {
    "export_anki.py",            # 独立脚本：Obsidian/Anki 侧流程由插件驱动，仓库内无入口是有意为之
    "vault_synthesize.py",       # 同上：知识库综合，仓库内不接线
    "vault_ingest.py",
    "bilibili_deep_archive.py",  # 由插件调用；SKILL.md 里有引用说明，CI 看不到外部调用方
}


def report_unreferenced_scripts() -> list[str]:
    scripts = sorted((SKILL_ROOT / "scripts").glob("*.py"))
    corpus = []
    for path in SKILL_ROOT.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".yaml", ".yml", ".toml", ".json"}:
            try:
                corpus.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    blob = "\n".join(corpus)
    orphans = []
    for script in scripts:
        if script.name in INTENTIONALLY_LIBRARY_ONLY:                             # 有意保留为库的脚本不算游离
            continue
        # 按**模块名**匹配：`from file_output import ...` 这种导入不会写 .py 后缀
        if blob.count(script.stem) <= 1:                                          # 只出现自己这一处 = 没人引用
            orphans.append(script.name)
    return orphans


if __name__ == "__main__":
    raise SystemExit(main())
