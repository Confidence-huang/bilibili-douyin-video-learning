# Transcript Cleaning Reference

## Common Chinese ASR Errors

| ASR Output | Likely Correct | Context |
|------------|---------------|---------|
| 什么/神马/神么 | 什么 | Generic |
| 一样/一阳 | 一样 | Generic |
| 都/多/度 | 都 or 多 | Context-dependent |
| 是/事/室/市 | 是 (usually) | Context-dependent |
| 在/再/载 | 在 (usually) | Context-dependent |
| 的/地/得 | 的 (usually) | Context-dependent |
| 他/她/它 | 他 (usually) | Check speaker context |
| 不/部/布/步 | 不 (usually) | Context-dependent |
| 会/回/惠/汇 | 会 (usually) | Context-dependent |

## Mixed Chinese-English Handling

- Preserve English terms, code identifiers, and brand names as-is
- Common patterns: `Python`, `API`, `GPU`, `Linux`, `TCP/IP`
- Chinese ASR often mangles English → mark as `[疑似: original]`

## Code, Commands, and Paths

Patterns to preserve exactly:
- File paths: `/usr/local/bin`, `C:\Program Files\...`
- Commands: `pip install`, `git clone`, `npm run build`
- Code: `def foo():`, `import torch`, `const x = 1`
- URLs: `https://...`
- Version numbers: `v2.0.1`, `Python 3.11`

ASR often converts:
- `git` → `get`/`给他`
- `pip` → `皮普`/`pipe`
- `npm` → `NPM`/`恩皮安`
- `API` → `A P I`/`诶批爱`

## Confidence Marking

| Mark | When to Use |
|------|-------------|
| `[疑似: word]` | Low confidence on a specific word |
| `[听不清]` | Audio is unclear, can't determine words |
| `[可能: alternative]` | Two plausible readings |
| `[原文不明确]` | Source material is ambiguous |

## Merging Rules

- Merge consecutive lines from same speaker
- Combine segments <1 second with adjacent segments
- Don't merge across obvious topic boundaries
- Preserve timestamps every ~30 seconds for navigation
