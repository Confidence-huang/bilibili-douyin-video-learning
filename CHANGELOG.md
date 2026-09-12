# Changelog

All notable public changes are recorded here.

## 1.3.7 - 2026-09-13

- Add per-frame vision captions to `douyin_deep_archive.py` (`--vision`,
  `--vision-url`, `--vision-model`): each key frame is captioned through any
  OpenAI-compatible multimodal endpoint (e.g. local Ollama qwen2.5vl), with
  per-frame failure isolation and a placeholder fallback; replaces the
  "待补视觉说明" placeholders in the generated note.


## 1.3.6 - 2026-09-13

- Add `scripts/douyin_deep_archive.py`: the single deep-archive engine (bridge video +
  adaptive scene frames + local faster-whisper + aligned image-text section) writing the
  exact note contract shared with the Obsidian douyin-vault-link plugin (douyin_id lookup,
  idempotent section replace, frames under 附件/douyin-media/frames/<id>/).
- Include offline unit tests for the engine's pure functions (scene parsing, peak picking,
  alignment, section build, idempotent note update).

## 1.3.5 - 2026-09-13

- Add `scripts/transcribe_audio_cli.py`: a standalone JSON CLI (`--audio/--model/--language/--device`)
  around the shared faster-whisper entry, so external callers (e.g. the Douyin deep-archive
  Obsidian plugin) can run local GPU transcription with clean-stdout JSON and stderr progress.
- Make the in-vault target folder configurable via `BILIBILI_OBSIDIAN_FOLDER` (mirrors
  `BILIBILI_OBSIDIAN_VAULT`); the historical `20_沉淀箱/Bilibili` default still applies when unset.

## 1.3.4 - 2026-09-13

- Add a `gpu` section to `doctor status` reporting CTranslate2 CUDA visibility
  (`available`/`devices`/`error`) without affecting the overall ok verdict, so
  the CPU fallback remains valid on machines without NVIDIA hardware.
- Document the verified RTX 50-series (Blackwell / sm_120) Windows profile:
  faster-whisper `small` at `cuda/float16` transcribed a 49-second Chinese clip
  in ~5.4 s (~9x realtime) using ~3.3 GB VRAM.

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
