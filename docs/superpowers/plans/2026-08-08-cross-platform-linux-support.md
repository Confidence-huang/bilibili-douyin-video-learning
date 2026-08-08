# Cross-platform Linux Support Implementation Plan

> **For Codex:** Implement this plan test-first, keeping the public Skill name and Windows behavior backward compatible.

**Goal:** Upgrade the existing `bilibili-video-learning` repository from Windows-first to a single Windows/Linux Skill distribution without replacing either live installation during development.

**Architecture:** Keep one platform-neutral Python core under the Skill root. Put host-specific environment creation, wrappers, and verification in separate Windows and Linux entrypoints. Resolve runtime Python and FFmpeg through explicit interfaces so backend scripts do not depend on PATH layout or hard-coded host paths.

**Tech Stack:** Python 3.12, uv, PowerShell, Bash, pytest, GitHub Actions, faster-whisper/CTranslate2, optional OpenAI Whisper/PyTorch CUDA fallback.

---

## Task 1: Freeze cross-platform runtime contracts

**Files:**
- Create: `.agents/skills/bilibili-video-learning/agent-harness/cli_anything/video_learning/tests/test_cross_platform.py`
- Modify: `.agents/skills/bilibili-video-learning/agent-harness/cli_anything/video_learning/utils/skill_runtime.py`
- Create: `.agents/skills/bilibili-video-learning/scripts/media_tools.py`

1. Add failing tests proving `.venv` and `.venv-gpu` runtime discovery works on POSIX and Windows-shaped layouts.
2. Add failing tests proving FFmpeg prefers PATH and falls back to `imageio-ffmpeg` with a clear error.
3. Run the focused tests and record the expected failures.
4. Implement the smallest runtime and media resolver changes.
5. Re-run the focused tests until green.

## Task 2: Make backend process invocation portable

**Files:**
- Modify: `.agents/skills/bilibili-video-learning/scripts/douyin_extract.py`
- Modify: `.agents/skills/bilibili-video-learning/scripts/download_audio.py`
- Modify: `.agents/skills/bilibili-video-learning/scripts/fetch_bilibili.py`
- Modify: `.agents/skills/bilibili-video-learning/scripts/transcribe_bilibili.py`
- Modify: `.agents/skills/bilibili-video-learning/scripts/transcribe_fallback.py`
- Test: `.agents/skills/bilibili-video-learning/agent-harness/cli_anything/video_learning/tests/test_cross_platform.py`

1. Add tests for `sys.executable -m yt_dlp` and the shared FFmpeg resolver.
2. Confirm the tests fail against the Windows-only implementation.
3. Route every backend through the shared resolvers without weakening Cookie or page-selection boundaries.
4. Run focused and existing offline tests.

## Task 3: Split runtime profiles without duplicating the Skill

**Files:**
- Modify: `.agents/skills/bilibili-video-learning/pyproject.toml`
- Modify: `.agents/skills/bilibili-video-learning/uv.lock`
- Modify: `install_windows.ps1`
- Create: `install_linux.sh`
- Create: `verify_linux.sh`
- Modify: `verify.ps1`

1. Move ASR and CUDA-only dependencies into named optional profiles.
2. Keep Windows installing the ASR + CUDA profiles into `.venv-gpu`.
3. Add a transactional Linux installer that installs ASR into `.venv`, creates a user-scoped CLI wrapper, and never uses sudo or edits system configuration.
4. Add source-only and runtime verification for Linux.
5. Regenerate and check the uv lock.

## Task 4: Add lifecycle verification and dual-platform CI

**Files:**
- Create: `.agents/skills/bilibili-video-learning/skill.manifest.yaml`
- Create: `.agents/skills/bilibili-video-learning/tests/runtime.py`
- Create: `.agents/skills/bilibili-video-learning/tests/behavior.py`
- Create: `.agents/skills/bilibili-video-learning/tests/local_media.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tools/validate_repository.py`

1. Add portable static/runtime/behavior verification using the active interpreter placeholder.
2. Run Ubuntu and Windows source/offline jobs without downloading multi-GB GPU packages in CI.
3. Keep privacy, Skill identity, CLI guide parity, and lock checks mandatory.
4. Run Skill Creator validation and Skill Lifecycle Manager verification from the checkout.

## Task 5: Document one canonical cross-platform Skill

**Files:**
- Modify: `.agents/skills/bilibili-video-learning/SKILL.md`
- Modify: `README.md`
- Modify: `INSTALL.md`
- Modify: `USAGE.md`
- Modify: `CHANGELOG.md`

1. Change positioning from Windows-first to Windows/Linux.
2. Document CPU/int8 Linux defaults and Windows CUDA verification without promising GPU availability.
3. Preserve `$bilibili-video-learning` invocation compatibility.
4. State that rendered paid-course capture remains the separate `course-audio-capture` Skill.

## Task 6: Validate, publish a branch, and open a draft PR

1. Run repository validation, lock check, all offline tests, Linux source/runtime verification, and credential/path scans.
2. Review the exact diff and confirm no live Skill directory was changed.
3. Commit only the intended repository files.
4. Push `codex/linux-cross-platform`.
5. Open a draft PR describing compatibility, test evidence, and deferred live-install migration.
