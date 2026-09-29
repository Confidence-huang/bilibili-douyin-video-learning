# Changelog

All notable public changes are recorded here.

## 1.5.0 - 2026-09-29

- **ASR coverage guard.** `scripts/speech_to_text.py` now validates every pass before
  returning: it measures the real audio duration and the mean volume of each gap of 2s
  or more, and re-transcribes the loud ones with `vad_filter=False` before merging them
  back onto the timeline. A gap at -12.9 dBFS that had silently lost 5.6 seconds of
  speech is exactly what this catches. The decision, its evidence (coverage before and
  after, the gap list, each retried window with its measured dBFS) and the recovery all
  land in `diagnostics`. `--no-vad` and `--no-coverage-retry` opt out.
  All seven ASR call sites share this entry point, so Douyin and Bilibili are both
  covered; `transcribe_bilibili.py` and `download_audio.py` also publish
  `audio_duration`, `coverage_before` and `coverage_after`. The logic lives in
  `scripts/asr_coverage.py`, which takes the window transcriber as a callback and is
  therefore fully testable without a model, ffmpeg or network access. See D16.
- **VAD is now tunable and recorded.** `TranscriptionSettings` carries
  `vad_min_silence_ms` (default 2000, matching faster-whisper) and `vad_speech_pad_ms`
  (default 400); `douyin_extract.py` exposes `--vad-min-silence-ms`. Previously VAD
  aggressiveness could not be configured at all, and the loss recorded in D16 happened
  under a non-default VAD setting.
- **Cache identity covers how the audio is decoded.** `asr_params` (settings identity
  plus parameter version) and `engines` (installed faster-whisper / openai-whisper
  versions) join the Douyin cache key, and `CACHE_SCHEMA_VERSION` moves to 3. Changing
  `vad_filter`, `beam_size` or a VAD threshold, or upgrading the engine, can no longer
  reuse a transcript produced by different code. See D17.
- **Device availability is proven, not assumed.** `ctranslate2.get_cuda_device_count()`
  can report 1 while the CUDA runtime libraries are missing. Because the failure
  surfaces at the first `encode()` rather than at model construction, the load and the
  first pass now share one `try`; the run retries once on `cpu/int8` and records the
  reason in the new `device_fallback` field. An explicit `device="cuda"` is never
  silently downgraded, and when the openai-whisper fallback also fails the original
  error is re-raised so the root cause is not replaced by "No module named 'torch'".
  See D19.
- **Failure exit codes are now distinguishable.** `utils/exit_codes.py` owns the
  contract (0 ok, 1 unclassified, 20 share page unavailable, 21 cookie permission
  required, 22 network timeout, 23 ratio unavailable, 24 local transcription failed),
  `douyin_extract.py` mirrors the code into its JSON payload, the Bilibili script
  classifies its exception path while keeping 2 as the unclassified fallback, and the
  CLI imports the cookie code instead of repeating the literal 21. See D18.
- Tests: the offline suite grows from 95 to 114 passing cases, all without a model,
  ffmpeg, network access or browser cookies. The real VAD-loss data is committed as
  `tests/fixtures/asr_vad_dropped_speech.json` (180 real segments, no audio, so it
  passes the repository's forbidden-suffix boundary).

## 1.4.2 - 2026-09-14

- Note skeletons moved out of the three backends into `prompts/*.md`, loaded by
  `scripts/prompt_templates.py` and reported as a version (`template=1`). Only the
  section headings come from the template — tables, frontmatter, and the timeline
  stay in code, so the default note is byte-identical to 1.4.1 and can be asserted
  in tests. A missing `prompts/` directory degrades to the built-in headings and
  says so, instead of failing a run that already spent minutes on ASR.
  See `docs/DECISIONS.md` D12.
- Added `agents/claude.yaml` and `agents/gemini.yaml` alongside the existing
  `agents/openai.yaml`. Each host reads its own interface manifest, so the shared
  `display_name` / `short_description` / `brand_color` are now compared by
  `tools/validate_repository.py::validate_host_manifests()` and CI fails on drift.
  See D14.
- `pyproject.toml`: every `>=` floor now carries its reason. One of them is a
  security floor — `requests>=2.32.4` (CVE-2024-47081 / GHSA-9hjg-9r4m-mvj7
  `.netrc` credential leak); the rest are compatibility floors. Added an
  `impersonate` extra so `curl-cffi` can be omitted by users who do not need TLS
  fingerprint impersonation; ASR stays split into CPU-only `asr` and heavy
  `cuda-compat`. See D13.
- `bilibili_deep_archive.py`: metadata-only and deep-archive modes now fetch the view API
  cover image (`附件/bili-media/cover/<bvid>.<ext>`, extension probed from the URL suffix).
- New notes carry the `vault_status` and `promoted_to` contract fields; added
  `ensure_frontmatter_fields` so updating an existing note only backfills missing keys and
  never overwrites an already-promoted status.
- View API failures (62002 / -404 dead links) fail as-is and are counted by the caller.
- Version declarations are now parity-checked across all three sources —
  `pyproject.toml`, `agent-harness/setup.py`, and
  `cli_anything/video_learning/__init__.py` — enforced by
  `tools/validate_repository.py`. Both `setup.py` and `__init__.py` had been left at 1.4.1;
  `__init__.py` is what `--version` and the REPL banner report, so the CLI advertised a stale release.
- Added `tools/cli_smoke.py`, run by CI on both platforms: it proves the installed console
  script starts and that `--version` matches `pyproject.toml`. `doctor status` is treated as
  diagnostic rather than pass/fail: it probes host tooling (FFmpeg, yt-dlp, CUDA) and raises
  when those are absent, so requiring it to succeed would make the check test the runner
  image instead of this repository. A failure inside `doctor` is still surfaced verbatim.
- Added `docs/DECISIONS.md` recording why the project works the way it does, including the
  discarded options and the measured CUDA performance data.

## 1.4.1 - 2026-09-13

- `bilibili_deep_archive.py` hardening: 412/429 rate-limit backoff retry on the view API,
  `--metadata-only` light mode (favorites sync writes note + AI category without
  downloading video), and `--ai-url/--ai-model/--categories` classification args that
  write the `category` YAML field. Plugin bilibili-vault-link 1.0.1 passes these through.


## 1.4.0 - 2026-09-13

- Add `scripts/bilibili_deep_archive.py`: Bilibili deep-archive engine sharing the douyin
  engine's algorithm and note contract (scene peaks + local faster-whisper + aligned
  image-text section + vision captions). yt-dlp download works anonymously for public
  videos; `BILIBILI_COOKIE_FILE` covers members-only content. Note contract key: bvid;
  inbox defaults to $BILIBILI_OBSIDIAN_VAULT/$BILIBILI_OBSIDIAN_FOLDER.


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
