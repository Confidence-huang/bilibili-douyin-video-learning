# Changelog

All notable public changes are recorded here.

## 1.3.3 - 2026-08-08

- Isolate legacy embedded-runtime resolver tests from a real user-level XDG
  runtime so the installed Linux verification suite remains deterministic.

## 1.3.2 - 2026-08-08

- Move the default Linux Python runtime outside the lifecycle-scanned Skill tree
  so dependency packages cannot appear as nested Skills.
- Add explicit `--runtime-root` installation and verification support, reject a
  runtime placed inside the Skill destination, and retain environment-based
  runtime discovery for wrappers and custom installations.

## 1.3.1 - 2026-08-08

- Renamed the two embedded CLI reference documents from `SKILL.md` to
  `CLI_GUIDE.md` so lifecycle scanners expose only the repository's main
  `bilibili-video-learning` Skill.
- Added a publication gate that rejects any future nested `SKILL.md` entrypoint.

## 1.3.0 - 2026-08-08

- Added one canonical Windows/Linux Skill distribution without changing the `$bilibili-video-learning` invocation name.
- Added transactional Linux source/runtime installation, verification, and a user-scoped CLI wrapper with no sudo or system configuration changes.
- Split ASR and Windows CUDA compatibility dependencies into explicit uv profiles while preserving the existing Windows `.venv-gpu` route.
- Added shared FFmpeg and runtime-Python resolution for `.venv` and `.venv-gpu` layouts.
- Added layered Skill lifecycle probes and Ubuntu/Windows offline CI jobs.
- Kept rendered logged-in web-course capture in the separate `course-audio-capture` Skill.

## 1.2.0 - 2026-07-25

- Added platform-dispatched `source inspect` for Bilibili and Douyin.
- Added anonymous Douyin SSR metadata inspection and optional ranged ratio probes.
- Added mocked Douyin metadata, ratio, fallback, cache, CLI, and no-hidden-download tests.
- Added Windows GitHub Actions CI for source, privacy, lock, Skill-parity, and offline tests.
- Hardened the optional plaintext Cookie-export utility with explicit acknowledgement and overwrite gates.
- Moved pytest to the development dependency group, removed unused direct torchaudio/torchvision dependencies, and updated the yt-dlp floor.
- Added supported-version and private vulnerability-reporting guidance.

## 1.1.0 - 2026-07-22

- Added centralized diagnostic sanitization, structured Cookie permission, atomic output, and source-identity cache validation.
- Published the first sanitized public source package with the CLI-Anything harness.
