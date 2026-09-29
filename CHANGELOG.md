# Changelog

All notable public changes are recorded here.

## 1.23.0 - 2026-09-29

- **Baselines for every tier, not just the good one.** eval/baselines.json records each case's
  measured CER with a tolerance, and `run_benchmark.py --baseline` fails when a case gets worse
  while printing IMPROVED when one gets better. CI now runs it alongside the `--max-cer` gate: the
  gate covers the `large` tiers that are supposed to pass, and the baseline covers all four cases
  including `small`. Both matter - `small` sits at 0.11 and 0.23, so a pass/fail threshold would
  simply stay red, but a regression there is a real signal, and it was exactly this kind of record
  that showed chunking hurting short audio. See D47.
- **The chunked output now reports its engine and precision.** Real verification of
  `--chunk-length 30` on a 109-second audio showed the chunked result carrying `device` but empty
  `engine` and `compute_type` (D40 had only fixed the device). Both now come from the sub-results, so
  a chunked transcript has the same field shape as a whole-file one.
- **Both CLI paths verified on real audio**: `--chunk-length 30` logged
  "chunked transcription done: 34 segment(s)" with cuda and a populated engine field, and
  `--chunk-length 0` took the single-pass path.
- **A CI mistake of mine, caught by running it**: the new baseline step was inserted into both jobs
  with the PowerShell body, because my platform detection keyed off the wrong line. The ubuntu job
  now runs the bash form and the windows job the PowerShell form.
- Tests: **430 passing** offline cases.

## 1.22.0 - 2026-09-29

- **The regression class from 1.19.0 is now guarded.** tests/test_cli_smoke.py walks every script's
  module-level body with AST, imports the module and asserts that each `name.attribute` it uses
  actually exists - the generalised form of the NameError that passed 343 tests. It never executes a
  main path, so it needs no network, model or ffmpeg and can run in CI. A second check requires
  `main`/`cli_main`/`run`/`parse_args` calls at module level to sit behind a `__main__` guard. See D46.
- **Two false positives in that test's first version, both narrowed and recorded.** It flagged
  `str` as an unimported module (a builtin is not a module) and flagged seven scripts for
  "unguarded module-level calls" when `Path(__file__).resolve().parent`, `re.compile(...)` and
  `sys.path.insert(...)` are exactly the calls that *should* run at import time. A guard test that
  cries wolf gets switched off, so the heuristics were tightened rather than the assertions relaxed.
- **Real golds now gate every pull request.** The actual ASR outputs were trimmed into fixtures
  (segments plus context, 1-5 KB, no audio needed) and both CI jobs run three-quarters of the
  benchmark table: `run_benchmark.py --case bili1-large --case bili2-large --max-cer 0.05`. Any
  change that pushes CER past 0.05 turns the PR red. The `small` tiers are deliberately excluded
  from the gate since they sit at 0.11-0.23 - a gate should only cover tiers that are supposed to
  pass. docs/ACCEPTANCE.md documents the step and the fixtures.
- Tests: **427 passing** offline cases.

## 1.21.0 - 2026-09-29

- **A runtime regression that 343 passing tests did not catch.** v1.19.0 broke
  `transcribe_bilibili.py` at runtime: the file only did `from speech_to_text import
  transcribe_audio_file`, while the new code called `speech_to_text.resolve_model_size(...)`, so the
  CLI died with NameError. Nothing caught it because that script keeps its argparse at module level
  and has no callable entry point, so no test ever executed its main path. The module is now
  imported, a guard test pins that binding, and the record says plainly that **running the CLI for
  real** is what found it. An attempt to refactor the script into a testable `cli_main(argv)` hit a
  slicing error, the script rolled the file back automatically rather than leaving it broken, and the
  smaller guard was chosen instead. See D45.
- **A second Bilibili gold, chosen because it is hard.** BV1Kyas6wEuz (109s: narration, then
  meme-dubbed audio over music) came out 12 rows kept, 2 corrected and **22 unresolved** - past about
  43 seconds both configurations fall apart, so those rows are marked unresolved rather than guessed
  at. Its value is recording the credibility ceiling for that kind of material. Two corrections are
  certain: 相声响起 to 枪声响起 (the starting signal) and 向前农 to 向前冲.
- **One metric in that gold is tautological and says so.** Its punctuation was copied from the large
  draft, so the table's `punct_f1 = 1.0` for bili2-large proves nothing about punctuation quality;
  the artifact states this rather than letting the number stand.
- **New data strengthens the device-aware default**: on the harder material, large scores CER 0.0051
  against small's 0.2298 - a 45x gap - with coverage 0.9982 against 0.9236. Combined with the first
  sample (13x) and the Douyin sample (2.3x), `auto` picking large on CUDA is well supported.
- **The benchmark's timeline column finally works.** It had been reading `None`; the real structure is
  `report["timeline_offset"]` with `median_seconds`, found by inspecting rather than guessing - after
  three wrong guesses in two rounds, each of which surfaced as a silent `None`, the most misleading
  outcome possible.
- Tests: **349 passing** offline cases.

## 1.20.0 - 2026-09-29

- **One command now shows the whole picture.** `scripts/run_benchmark.py` takes repeatable
  `--case "name=hypothesis.json:gold.json"` arguments, evaluates each against the shared metrics and
  prints one table with CER, substitutions, deletions, insertions, coverage, hallucination, median
  timeline offset and punctuation F1. `--max-cer` turns it into a gate that fails the run, which is
  what CI and pre-release checks need. It evaluates only - it never downloads media or runs ASR.
  The real table it produced: bili-large CER 0.0085, bili-small 0.1111, douyin-large 0.0187. See D44.
- **What counts as good enough is now written down.** docs/ACCEPTANCE.md holds the thresholds (CER
  under 0.05, coverage at least 0.99, hallucination under 0.5 characters per minute, median timeline
  offset under 1.0s, chunk consistency at least 0.90, with punctuation explicitly not a gate) plus
  the fixed procedure: store the table before the change, make the change, run it again, and explain
  any column that got worse - or do not merge.
- **Punctuation can now use a model, optionally and off by default.**
  `scripts/punctuate.py` falls back to the rule-based path when the optional backend is absent and
  reports `mode="rules"` honestly; the new `punctuation` extra installs the model for anyone who
  wants it. That keeps "should we add a model" a one-line installation choice rather than a rewrite.
- **Two silent Nones taught a lesson.** The summary script guessed wrong field names twice
  (hallucination rate, median timeline offset) and both times the result was `None` rather than an
  error - the most easily misread outcome, since a reader assumes the metric does not exist. Lookups
  now search nested mappings for semantics (`minute`/`rate`, `median`) and only then give up.
- Tests: **348 passing** offline cases.

## 1.19.0 - 2026-09-29

- **Both platforms now pick the model from the device.** The Bilibili gold showed `large` at CER
  0.0085 against `small` at 0.1111, a 13x gap, and the Douyin gold points the same way (0.0433 for
  small at its best, 0.0187 for large-v3, a 2.3x gap) - so Douyin needed the change too. `--model`
  now defaults to `auto` in all three entry points: `large` when CUDA is available, `small`
  otherwise, and the reason is logged (`model=large (auto:cuda)`) so the choice is auditable.
  Making `large` the unconditional default was rejected because on CPU a 46-minute file goes from
  minutes to hours, which is a real cost for zero benefit to the people waiting on it. Explicit
  `--model` values are still honoured, and `large-v3` is normalised to `large`. See D43.
- **`doctor assets` is its own subcommand**, so the lexicon count, conflict-preference count,
  chunking availability and default, and the template list can be read without pulling in the rest
  of the doctor payload.
- **A mistake worth recording**: the `resolve_model_size(...)` call was inserted inside the
  argument list of the settings constructor, which broke `transcribe_bilibili.py` outright and
  failed eight tests immediately. "It is only one line" is exactly the change that needs the suite
  run, and the suite is what caught it.
- Tests: **343 passing** offline cases.

## 1.18.0 - 2026-09-29

- **Punctuation is now measured, and its ceiling is documented.** `eval_asr` gained a punctuation
  score (the two sides' marks are paired by their position in the mark-free text, matched within a
  three-character window, reported as precision/recall/F1, and reported as "not applicable" when
  the gold carries no punctuation). Against the Douyin gold, whose 217 marks come from a human
  transcript: no punctuation scores 0.0, and the rule-based approach scores **F1 0.313** (179
  marks, precision 0.346, recall 0.286). See D42.
- **A regression I introduced, caught twice.** Raising the comma threshold to 0.35s felt like
  "fewer, better marks"; it collapsed F1 from 0.313 to 0.033. The existing test caught it first -
  it asserts that even a 0.1s gap gets a mark - and the new metric quantified it afterwards. Both
  had to exist; either alone would have led me to the wrong conclusion.
- **No punctuation model dependency.** The ceiling is set by the segmentation: Chinese speech
  carries roughly one mark per ten characters while ASR segments arrive about every 1.4 seconds, so
  boundaries alone cannot supply enough marks. The syntax clues (sentence-final particles,
  conjunction openings) are kept because they are principled and free, but they did not measurably
  raise F1 - 0.3131 with them against 0.3133 without - and that is stated rather than spun.
- **`--chunk-length` and `--chunk-overlap` reach the command line.** The chunking from D38 was only
  reachable from code; `transcribe_audio_cli.py` now exposes both, defaulting to off so existing
  behaviour is unchanged.
- Tests: **340 passing** offline cases.

## 1.17.0 - 2026-09-29

- **Bilibili finally has a gold set, and it says who verified it.** The 30.63s sample
  BV1ntah6TEe9 was drafted with large and checked line by line against small plus context
  consistency. Thirteen of fifteen rows were kept (four of them identical in both configurations),
  one was corrected (一见秋衣 to 一件秋衣, where small was right), and one is explicitly
  `unresolved`: both configurations produced word salad there and no audio listening was done, so
  it is not being guessed at. A second uncertainty is recorded rather than hidden - both
  configurations agree on 滑脸, which is semantically suspicious (划脸/刮脸), and text alone
  cannot settle it. See D41.
- **`reference.kind` is `agent-verified`, deliberately not `human-verified`.** The method, the
  per-row basis, the unresolved rows and what a human should spot-check all live in the artifact,
  so the strength of the evidence is visible instead of implied.
- **First Bilibili baseline**, measured against that gold: `small` scores CER **0.1111** (13 errors
  over 117 characters), coverage 0.8501, hallucination 0.0 characters/minute, median timeline
  offset 0.52s. Until now every measurement in this repository came from the Douyin material.
- Tests: **336 passing** offline cases.

## 1.16.0 - 2026-09-29

- **First real 30+ minute calibration, on material found in the wild.** A 46.4 minute Bilibili
  maths lecture was transcribed both ways with `small` on cuda. Whole-file: 93.5s (29.8x real
  time), 1561 segments, 13809 characters. Chunked at 600s with a 2s overlap: 132.5s (21.0x),
  1512 segments, 13794 characters, 5 chunks. Text similarity 0.9052 with a character difference
  of just -15 (0.11%), so at this length 600s chunks are equivalent in content - but chunking was
  **42% slower**, because each chunk reloads the model. Chunking buys resilience and a memory
  ceiling, not speed. On this GPU a 46 minute file decodes whole without trouble. See D40.
- **Audio front end measured on two more materials.** On a clean lecture slice the front end is
  text-neutral: 844 characters and 80 segments either way, byte-identical output. On a music
  track both settings produced empty output - sung vocals are outside what this path transcribes,
  which is a capability boundary worth stating rather than hiding. Neither result contradicts the
  17% CER gain measured earlier, because that measurement had a gold transcript and these do not.
- **The experiment caught two defects of mine that unit tests could not.** The chunked path
  reported `device: None` while actually running on cuda (it read the function argument instead of
  the sub-call's real device), and it dropped every per-chunk diagnostic, so the coverage and
  hallucination-gate verdicts for each chunk vanished on merge. Both are fixed, with assertions.
- **The validator now has its own regression test.** I appended functions after the
  `if __name__ == "__main__":` block three times in one round, and the last time left two such
  blocks, the earlier one calling `main` before it was defined - and nothing failed, because the
  validator is the thing that runs other people's checks. A test now imports it and runs `main()`.
- Tests: **336 passing** offline cases.

## 1.15.0 - 2026-09-29

- **`doctor status` now reports Skill assets**: how many lexicon entries and verified conflict
  preferences exist, whether chunking is available and - read from the source rather than hard
  coded - what its default is, plus the prompt template list. The numbers come from the real
  files, so they cannot drift away from the code they describe. See D39.
- **The review list reaches the note.** `verify_transcript` already produced a ranked
  `needs_review_top`, but it only existed inside JSON. Notes now end with a "需要人工确认的差异"
  section listing timestamp, this transcript's wording, the other source's wording and the size
  of the difference, plus how many conflicts a verified table already ruled on. It presents, it
  does not rewrite, and it disappears entirely when there is nothing to review.
- **Two rules became machine checks.** Every prompt template must declare a `template-version`
  and they must not contradict each other; every non-comment line in the lexicon and the conflict
  preference table must carry a `#` evidence comment, turning "to add a word, cite the case" from
  a request in a header into something CI enforces. Both files currently comply.
- The same mistake landed for the third time and is recorded: new functions were appended after
  the `if __name__ == "__main__":` block, and after repeated moves that file ended up with two
  such blocks, the earlier one calling `main` before it was defined. The lesson is that the
  validator itself needs a test that imports and runs it. Separately, the first version of the
  review section made `note render` fail outright when the optional module could not be imported
  in the CLI subprocess; it is now wrapped so a missing module means a missing section, never a
  failed note.
- Tests: **333 passing** offline cases.

## 1.14.0 - 2026-09-29

- **`chunk_length` finally does something.** The setting existed but was never read anywhere in the
  repository, so every transcription decoded the whole file at once - fine for the longest material
  measured so far (259.77s), risky for the 30-90 minute lectures that are the main use case, where a
  single failure means starting over and long contexts drift more. `scripts/asr_chunking.py` now
  plans overlapping chunks, the pipeline cuts and transcribes them (with chunking explicitly
  disabled in the sub-calls so it cannot recurse), and the results are shifted back onto one
  timeline. Both segment and word timestamps are shifted, otherwise words would no longer line up
  with their segments. See D38.
- **Off by default, deliberately.** `chunk_length=0` keeps the previous behaviour exactly, and even
  a direct call to the chunked entry point falls back to a single pass when chunking is not enabled.
- Overlapping chunks exist so a cut cannot land mid-word; when the overlap makes the next chunk
  re-transcribe text that is already there, only the first copy is kept and the dropped segment is
  marked rather than silently discarded.
- **Two edge cases the tests forced out of the first version**: a 30-minute file at 600s per chunk
  produced a six-second sliver chunk (now absorbed into its predecessor when the tail is under a
  quarter of the chunk), and a non-positive chunk length was clamped to five seconds instead of
  meaning "do not chunk".
- Tests: **326 passing** offline cases.

## 1.13.0 - 2026-09-29

- **The standalone CLIs share one exit-code contract.** `scripts/exit_contract.py` lets
  `douyin_ssr.py` (and any other script run directly) return the same codes as the harness -
  timeout 22, platform risk control 26, missing audio 27 and so on - instead of a blanket 1. It
  imports the canonical implementation when the harness is installed and falls back to a local
  mirror otherwise, because `python scripts/douyin_ssr.py` must work on a machine that only has
  the sources. A test locks both the values and the *classification results* against the
  canonical implementation, since matching numbers with differing semantics would be worse than
  no mirror at all. See D37.
- **A pre-existing duplicate, left alone on purpose.** `fetch_bilibili.py` already carried its own
  exit-code constants and classifier. Changing it carried more risk than value, so instead a test
  now pins its constants to the mirror: drift will fail CI rather than pass unnoticed.
- **Scripts with no in-repo entry point are declared, not nagged.** The validator's report gained
  an `INTENTIONALLY_LIBRARY_ONLY` list (`export_anki.py`, `vault_ingest.py`,
  `vault_synthesize.py`, `bilibili_deep_archive.py`) - the first three are libraries for other
  hosts, the last is invoked by the plugin, which CI cannot see - and `SKILL.md` now states both
  facts so "intentionally kept" is auditable. The report is empty again.
- Tests: **317 passing** offline cases.

## 1.12.0 - 2026-09-29

- **The domain lexicon now only holds entries with a real error behind them.** The ten
  "common misspellings" that were added from intuition (认知, 情绪价值, 底层逻辑, ...) are gone:
  the lexicon measured harmful as a whole (0.0433 to 0.0751, missing characters 11 to 56, D26),
  so unverified words only add noise. Ten entries remain, each traceable to a concrete case
  (血包/雪包, 供血/工学, 经济价值/经营价值, ...), and the file states the rule: to add a word,
  say which video and what it misheard. See D36.
- **Model revision is part of the cache identity.** The key only knew the model *name*, so the
  same `large-v3` with different weights would reuse a stale cache. `identity()` now carries a
  fingerprint built from the weight file's size and mtime - metadata only, no content read.
- **ASR engine diagnostics carry the same `step` key** as the coverage and refine diagnostics
  (`step: "asr_engine"`, with `engine` kept for existing callers).
- **Scripts nobody references are now visible.** `tools/validate_repository.py` reports, without
  failing, the scripts under `scripts/` that no in-repo file mentions - matching on the module
  name, since `from file_output import ...` carries no `.py` suffix. It currently lists
  `bilibili_deep_archive.py` (called by the plugin, which CI cannot see), `export_anki.py` and
  `vault_synthesize.py`. Deliberately non-fatal: external callers are invisible to CI and a hard
  failure would be a false alarm.
- Two self-inflicted defects caught by the tests while landing this, recorded because the lesson
  matters: `speech_to_text.py` used `os.environ` while only importing `pathlib` (nine failures),
  and the new validator function was appended after the `if __name__ == "__main__":` block.
- Tests: **311 passing** offline cases.

## 1.11.0 - 2026-09-29

- **Fusion can correct, not just annotate.** Fusion used to keep the primary wording on every
  conflict, so the primary's own OCR errors were inherited - even though the two sources fail
  differently: author captions are right about homophones (血包/雪包, 供血/工学, 断气/断亲,
  衣不蔽体/一不避体, 娘俩/两俩, 享福/想福, 经营价值/经济价值 - 11 verified cases) while OCR misreads
  similar-looking characters (赡/赠, 白/自). A verified wrong-to-right table
  (`references/conflict-preferences.txt`) now acts as a third criterion: whichever side carries
  the correct form wins, and every ruling is recorded with `chosen_by` and the matched form.
  On the real pair the table fires 11 times, all in the right direction. See D35.
- **The short review list is finally short.** Every conflict being listed is the same as none
  being listed: the real pair produced 159. Conflicts are now ranked by the size of the
  conflicting region, only substantive ones are listed (multi-character differences, or
  single-character differences that are not function words - 的/得 does not count), capped at 30,
  while the full `spans` list stays authoritative. Real data: **159 to 21**, led by
  从小到大/创造了, 供血/工学, 享福/想服, 直到/只要.
- **A character-level trap, caught by real data.** difflib produces character-level opcodes, so a
  two-character table entry like 赠养→赡养 never matched the 赠/赡 conflict; equal-length entries
  are now split into per-position character confusions and compared position by position.
- Tests: **307 passing** offline cases.

## 1.10.0 - 2026-09-29

- **Retry windows now pass a hallucination gate.** The coverage guard cuts out suspicious gaps
  and re-decodes them with both silence thresholds disabled (D16), which is the only place in
  the pipeline that can invent text - and until now its output was merged into the transcript
  unchecked. A window is now accepted only if its compression ratio is sane, it does not repeat
  the same 4-character fragment three or more times, and its characters-per-second is plausible
  for Chinese speech (roughly 4-6/s; above 8 or below 0.5 is rejected). Rejected windows are
  recorded in an `asr_hallucination_gate` diagnostic and the transcript keeps the first pass.
- **Suspected hallucinations are marked, never rewritten.** `suspected_hallucinations` lists
  segments whose compression ratio suggests repetition or invention, with an
  `asr_hallucination_marks` diagnostic. The text itself is never altered by the tool - a human
  decides. Both the gate switch and its threshold enter the cache identity.
- **CI can finally run the media tests.** Neither job installed ffmpeg, so the cases that
  generate real media (missing-audio detection, audio extraction) were silently skipped and had
  no continuous regression protection. `imageio-ffmpeg` is now a CI test dependency, which is
  cross-platform and needs no sudo. See D34.
- Verified on a real 30.63s Bilibili video (`small`): 14 segments, coverage 0.8501, zero
  hallucination marks - clean audio produces no false positives - with the new field present in
  the output. Tests: **301 passing** offline cases.

## 1.9.0 - 2026-09-29

- **Semi-automatic gold sets.** `scripts/eval_gold_draft.py` turns an ASR draft into a gold
  draft plus a human review worksheet: it flags low-confidence paragraphs, high-compression
  (repetition/hallucination) paragraphs and paragraphs where two decode configurations
  disagree, so a reviewer checks the marked rows instead of every sentence. The product says
  what it is - `reference.kind = "semi-automatic-draft"` - and only becomes a benchmark after a
  human flips it to `human-verified`. See D33.
- **The first real worksheet exposed a usability defect and it is fixed**: on the 30.63s
  Bilibili sample (large vs small similarity 0.8632) the list flagged 15 of 15 paragraphs, which
  is the same as having no priority at all. Flags are now ranked
  (repetition > low confidence > substantive disagreement > minor disagreement), single-character
  differences are not treated as substantive, and the list is capped at 30% of paragraphs
  (minimum 8) while still reporting the true total. That sample now shows 8 rows instead of 15.
- **Numeral style no longer counts as a recognition error.** Chinese numerals are normalised
  before scoring (`六`≡`6`, `一百`≡`100`, `四十五岁`≡`45岁`); on the Douyin gold this moves CER from
  0.0214 to **0.0187** (41 to 36 errors) - the difference was exactly the numeral spellings, not
  misrecognition. `--keep-numerals` restores the strict comparison, and the report states which
  convention was used. See D33.
- First Bilibili gold draft shipped: `eval/gold/bilibili-BV1ntah6TEe9.draft.json` with
  `eval/worksheets/bilibili-BV1ntah6TEe9.md`, covering a 30.63s oral video (large: 15 paragraphs,
  coverage 0.999, cuda). It awaits the human pass described in the worksheet.
- Tests: **291 passing** offline cases.

## 1.8.0 - 2026-09-29

- **Douyin fetch through a real browser context, implemented in this repository.** The
  anonymous SSR chain is degraded by platform risk control (12 host/UA combinations all return
  the "验证码中间页" shell with `videoInfoRes` reduced to `status_code`) and the Web API needs
  `a_bogus`-style signatures that only the site's own JS can produce. `scripts/douyin_browser_fetch.py`
  therefore imitates the one proven approach natively - a minimal standard-library WebSocket
  client plus CDP drives a real Chromium, navigates to the video page and calls the logged-in
  Web API from inside it - without calling or copying any other project's service.
  `douyin_extract --download-method browser` wires it in, and `auto` prefers it.
  Verified on real hardware in three layers: transport (handshake → createTarget → navigate →
  in-page fetch returned 200), logged-in API (the favourites endpoint returned a real
  `aweme_list`), and offline protocol tests against a real socket. See D31.
- **`--video <local file>` is now a first-class input.** Douyin fetching is rate-limited, and the
  file is often already on disk; the pipeline now skips fetching entirely and goes straight to
  audio → ASR → cleaning → artifacts, with the cache identity taken from path+size+mtime.
  Verified on the real 259.77s mp4: 122 segments, 1991 characters, coverage 0.9997,
  cuda/float16, exit 0. See D32.
- Two findings worth keeping: the 259.77s reference video is no longer fetchable because the
  author set it to self-only (`filter_reason: status_self_see`, `aweme_detail: null`), and the
  detail API returns `status_code=0` with an empty detail unless all eight web parameters are
  sent - a "looks successful but carries no data" trap.
- Also fixed: the CDP HTTP metadata endpoint now bypasses proxies, because environments with
  `http_proxy` set route 127.0.0.1 through the proxy and silently fail.
- Tests: **281 passing** offline cases.

## 1.7.0 - 2026-09-29

- **Measurement first.** `eval/gold/` ships a text-only gold set (media stays out of the
  repository; `media.url` reproduces it) and `scripts/eval_asr.py` reports CER, hallucination
  rate, coverage, timeline offset and RTF. Writing its tests caught a real definition bug:
  with `difflib.SequenceMatcher(a=hypothesis, b=reference)` an `insert` opcode is text the
  reference has and the hypothesis lacks - an omission - while `delete` is the extra
  hypothesis text. The first version read them by intuition and reported gold text that ASR
  had dropped as "hallucination". Real baseline: CER 0.0402 on the Douyin gold. See D25.
- **Audio front-end on by default.** `highpass=f=70` plus EBU R128 `loudnorm` is the one clear
  accuracy win in this release: CER 0.0522 -> **0.0433** (-17% relative) with no measurable
  time cost. Single-variable ablation was required to see it - the first pass ran combined
  configurations and concluded that *every* optimisation made things worse. See D26.
- **Domain lexicon, off by default.** `references/asr-lexicon.txt` plus `--lexicon`/`--hotwords`
  and terms harvested from title and tags bias decoding against homophone errors
  (雪包/血包, 经营/经济, 一不避体/衣不蔽体). Measured *worse* on the gold (0.0433 -> 0.0751 with
  omissions 11 -> 56), so it stays opt-in and must be verified per video. See D26.
- **Profiles named after what they optimise**: `balanced` (default, lowest measured CER),
  `timing` (adds word timestamps, which are a capability rather than a CER win), `quality`
  (beam 5 plus targeted re-decode, highest coverage). No profile may disable the coverage
  safety net. Word timestamps and beam 5 are actively harmful *together* (omissions 17 -> 41).
- **Targeted re-decode of suspicious spans.** Only spans that look uncertain are re-decoded,
  and a replacement is accepted only if it is non-empty, keeps >=60% of the span duration,
  shares >=50% of its characters with the original and improves confidence by >=0.15. Every
  decision, including each rejection, is recorded. On the gold the mechanism either finds
  nothing (small model: all segments above -0.18) or fires and rejects everything with
  `confidence_not_improved` (large-v3 with a p10-derived threshold) - which is the intended
  behaviour, and better than lowering the threshold until a counter moves. See D27.
- **Re-segmentation at word boundaries** with a hard invariant: no character may be lost or
  reordered, timings stay monotonic and non-overlapping, and anything inconsistent degrades to
  the original segment instead of guessing. On current material (max segment 5.24s) it is a
  no-op; parameter sweeps confirm `dropped_chars == 0` as cuts get finer. See D30.
- **Fusion with per-span provenance.** `fuse_transcripts` produces one transcript from two
  sources, labelling each span `subtitle`/`asr`/`mixed`, keeping the primary wording with
  `alternatives` on disagreement, and refusing to drop content that either source contained
  (it raises instead, with a completeness audit re-attaching missed segments). Real Douyin pair:
  similarity 0.9718, 30 spans, 246 segments, 3 segments recovered. Bilibili gains a
  subtitle-first entry; the honest caveat is that no real subtitle track could be obtained
  anonymously (50 popular videos checked), so that path rests on unit tests plus a synthetic
  track on a real video. Bilibili segments are now canonical (`start/end/text/provenance`). See D29.
- **Image posts no longer masquerade as ASR failures.** A missing audio track is detected
  before extraction and exits **27** (`EXIT_NO_AUDIO_TRACK`) with a message pointing at image
  OCR; 27 is distinct from 24 because changing machine or model cannot help. Verified with real
  ffmpeg-generated media in both directions. See D28.
- Real large-v3 numbers on the gold: **CER 0.0214**, coverage 0.9976, timeline offset median
  0.20s - about half the best small-model CER. Tests: **270 passing** offline cases, no model,
  ffmpeg, network or cookies required for the suite.

## 1.6.1 - 2026-09-29

- **Burned-in caption extraction.** `scripts/hard_subtitle.py` reads the caption band, OCRs
  only frames whose pixels changed, merges identical neighbours into timestamped cards, and
  applies a configurable correction table for characters OCR misreads consistently (赡/赠,
  白/自, 干瘪/干). It reports a completeness self-check - sampled frames, change points, card
  count, coverage, and how many cards were shorter than 0.5s - because a sampling method
  cannot prove completeness; at 2fps the 0.4s cards "更高级" and "病了" were lost and the
  former changes the meaning of its sentence. Default sampling is 4fps. An optional
  `--asr-timeline` runs a character-level self-check and reports which side is missing text.
  Local files only - fetching stays in its own module. See D24.
- **Captions are filtered for noise, never silently.** Real Douyin videos overlay a moving
  watermark inside the caption band, which OCR reads as text: on a 20s clip it produced 46
  cards of which 31 were shorter than 0.5s. `--min-ocr-confidence` (default 0.5) drops
  low-confidence recognitions, and a built-in watermark pattern table plus repeatable
  `--noise-pattern` regexes drop watermark text and short ASCII fragments. The filters are
  deliberately based on confidence and configured patterns only - never on card length - so
  legitimate two-or-three-character cards such as 更高级 and 病了 survive. Every dropped card
  is counted in the report (`noise_cards_dropped`), and the coverage figure falls honestly
  rather than being padded.
- **The Bilibili pipeline now fails loudly.** `transcribe_bilibili.py` and
  `download_audio.py` printed errors and still exited 0, so an agent had to parse JSON to
  notice a failure. Both now record `exit_code` (20 when the media could not be fetched, 24
  when the local ASR failed) and the CLI returns it, matching the Douyin contract. Verified
  with a real nonexistent BV (exits 20) and a real video with a subtitle track (exits 0).
  See D18.
- Tests: 188 passing offline cases (up from 163 at 1.6.0).

## 1.6.0 - 2026-09-29

- **One canonical segment shape.** Subtitle parsing produced `start/end/text` while ASR
  produced `from/to/content`, and `clean_transcript.py` only understood the first, so it
  raised KeyError on every Douyin transcript and neither it nor `chunk_transcript.py` was
  ever called by production code. `scripts/normalize_transcript.py` is now the single
  adapter (canonical `start/end/text`, optional `confidence`), owns plain-text and SRT
  rendering, and raises a named error naming the fields it saw instead of guessing.
  Results saved before this release carry only `start/text` and are rejected with a hint to
  re-run; the cache schema moves to 4. See D20.
- **Fidelity is explicit.** The old cleaner dropped any segment whose text was exactly a
  filler word, including `这个` — which is semantic in "这个社会对于好男人的定义".
  `--fidelity verbatim` (default) never deletes a word; `cleaned` also drops filler-only
  segments and reports each one. The Douyin pipeline now actually runs the cleaning step.
  See D20.
- **Multi-format emit.** `--emit md,json,srt,txt` writes the requested artifacts atomically
  next to the Markdown. SRT and TXT are full transcripts, so they keep the existing
  `--include-transcript` boundary.
- **Cross-validation instead of trusting one model.** `scripts/verify_transcript.py` aligns
  two sources character by character and prints the exhaustive difference list with
  timestamps (type, range, both texts). `--min-span-chars` defaults to 1 because homophone
  differences are often a single character (`血/雪`). `--fail-on-difference` returns the new
  `EXIT_SOURCES_DISAGREE` (25). On the real video it reports similarity 0.9718 against the
  corrected transcript and independently reproduces two findings previously reached by hand.
  See D21.
- **Per-segment confidence.** `avg_logprob`, `no_speech_prob` and `compression_ratio` are
  kept, and the result lists `low_confidence_spans` plus an `asr_confidence` diagnostic. The
  threshold lives in `TranscriptionSettings` (default -1.0, matching whisper) and therefore
  in the cache identity. Nothing is rewritten automatically.
- **Optional text normalisation.** `--simplify auto/on/off` converts Traditional to
  Simplified when OpenCC is installed, records why when it is not, and only fails when
  explicitly required. Pause punctuation inserts `，`/`。` only at segment boundaries and
  never touches text inside a segment, feeding `readable_text` while `full_text` stays
  verbatim. The `zh-normalize` extra carries OpenCC. See D22.
- **WSL GPU actually works.** The WSL driver ships `libcuda.so` but not cuBLAS/cuDNN, so
  device enumeration succeeded while the first `encode()` failed with
  "Library libcublas.so.12 is not found". `scripts/cuda_runtime.py` discovers and preloads
  the `nvidia/*` wheels with `ctypes.CDLL(..., RTLD_GLOBAL)` in dependency order before
  CTranslate2 is imported (mutating `LD_LIBRARY_PATH` cannot help the current process, and
  the load order matters). The `gpu-cuda12` extra installs the three wheels; `doctor status`
  now reports `usable` and an actionable `guidance` instead of mere visibility. See D23.
- **Failure classification fix.** The most common real failure — both public download paths
  failing — raised a plain RuntimeError and still exited 1. It now raises
  `DouyinDownloadUnavailableError` and exits 20. See D18.
- Tests: the offline suite grows from 114 to 163 passing cases; still no model, ffmpeg,
  network access or browser cookies required.

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
  (default 400); `douyin_extract.py` exposes `--vad-min-silence-ms`. The guard no longer
  assumes VAD is the culprit: coverage is checked whatever the first pass used, because
  the ablation in D16 disproved that assumption.
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
