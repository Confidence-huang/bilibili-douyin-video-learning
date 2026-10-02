# Bilibili & Douyin Video Learning

[![CI](https://github.com/Confidence-huang/bilibili-douyin-video-learning/actions/workflows/ci.yml/badge.svg)](https://github.com/Confidence-huang/bilibili-douyin-video-learning/actions/workflows/ci.yml)

> A cross-platform Agent Skill that turns accessible Bilibili and Douyin videos into structured learning notes.

这个 Skill 提取公开可访问的视频元数据、字幕和用户明确授权的转写内容，再生成中文摘要、复习笔记、行动清单、问答与 Anki 材料。它不会绕过付费、会员、私密、地区或平台风控限制。

## 先看最短路径

安装完成后，可以在 Codex 中直接调用：

```text
$bilibili-video-learning 帮我学习这个视频：<B站或抖音链接>
```

也可以先检查 CLI 和来源：

```text
cli-anything-video-learning --json doctor status
cli-anything-video-learning --json source inspect "<B站或抖音链接>"
```

仓库名同时包含 Bilibili 和 Douyin；Skill 调用名继续使用 `$bilibili-video-learning`，以兼容现有安装。

## 它由哪些能力组成

转写是最重的一环，但整条链路是「**取流 → 转写/OCR → 文本加工 → 多源校验与融合 → 抽帧图文对照 → 笔记与知识库**」：

| 层 | 能力 | 主要实现 |
|---|---|---|
| 来源解析 | B站链接/`b23.tv`/BV·av ID、抖音分享链接、本地字幕与音视频 | `normalize_bilibili_url.py`、`douyin_extract.py` |
| 取流（B站） | 元数据 + 字幕轨 + 章节 + 评论（yt-dlp/WBI） | `fetch_bilibili.py`、`download_audio.py`、`transcribe_fallback.py` |
| 取流（抖音） | 三条路径：**真浏览器上下文**（推荐）/ 匿名 SSR / yt-dlp；另有**本地文件**入口 | `douyin_browser_fetch.py`、`douyin_ssr.py`、`douyin_extract.py --video` |
| 转写 | faster-whisper + 覆盖率兜底 + 置信度 + 领域词表 + 定向二次解码 + 三档位 | `speech_to_text.py`、`asr_coverage.py`、`asr_refine.py`、`asr_lexicon.py` |
| 硬字幕 OCR | 把烧进画面的作者字幕 OCR 成卡片：变化点检测、校正表、完整性自检、水印噪声过滤 | `hard_subtitle.py` |
| 字幕格式 | SRT / VTT / ASS / B站 JSON / JSON3 → 统一时间轴 | `convert_subtitle.py` |
| 文本加工 | 唯一规范形状、纯文本、SRT、繁简归一、停顿标点、词级重新分段、保真度清洗、分块 | `normalize_transcript.py`、`clean_transcript.py`、`chunk_transcript.py` |
| 校验与融合 | 字符级**穷尽**差异清单；融成一份逐字稿并逐段标 `provenance` | `verify_transcript.py` |
| 深度归档 | 场景评分自适应抽帧 → GPU 转写 → 图文对照 → 幂等写回笔记（两平台共享算法） | `bilibili_deep_archive.py`、`douyin_deep_archive.py` |
| 笔记与知识库 | 笔记渲染（带来源标注）、版本化模板、raw→wiki、多视频综合、Anki 卡片导出 | `build_notes.py`、`prompt_templates.py`、`vault_ingest.py`、`vault_synthesize.py`、`export_anki.py` |
| 质量度量 | 金标集 + CER / 幻觉率 / 覆盖率 / 时间轴偏移 / RTF | `eval_asr.py`、`eval/gold/` |
| 工程底座 | 原子写、结构化诊断与退出码契约、CUDA 运行时预加载、仓库校验与 CI | `file_output.py`、`runtime_output.py`、`cuda_runtime.py`、`tools/validate_repository.py` |

## CLI 参考

```text
cli-anything-video-learning doctor status          # 环境健康（Skill/Python/工具/模块/GPU 可用性）
cli-anything-video-learning source normalize <s>   # 规范化 URL/ID/分享文本，不接触媒体
cli-anything-video-learning source inspect <s>     # 检视来源；只在需要时碰媒体
cli-anything-video-learning subtitle convert <f>   # 字幕格式统一
cli-anything-video-learning note render <json>     # 抽取 JSON → 带来源标注的学习笔记
```

常用后端脚本（插件与批处理直接调用）：

```bash
# 抖音：本地文件直接转写（最稳；无需网络/登录/浏览器）
python .agents/skills/bilibili-video-learning/scripts/douyin_extract.py \
  --video "D:/clips/demo.mp4" --model small --json --no-cache

# 抖音：真浏览器上下文取流（自研 CDP 实现，需专用 profile 登录一次）
python .agents/skills/bilibili-video-learning/scripts/douyin_extract.py \
  "<抖音分享链接>" --download-method browser --browser-profile "<已登录 profile>" --json

# B站：字幕优先，ASR 补位（可用 --fuse 融合两份来源）
python .agents/skills/bilibili-video-learning/scripts/transcribe_bilibili.py BV1ntah6TEe9 --model small --json

# 任意本地音视频 → 带时间戳 JSON（插件使用的稳定入口）
python .agents/skills/bilibili-video-learning/scripts/transcribe_audio_cli.py --audio clip.mp4 --profile balanced

# 质量评测：任何产出 vs 金标
python .agents/skills/bilibili-video-learning/scripts/eval_asr.py \
  --hypothesis out/asr.json --gold .agents/skills/bilibili-video-learning/eval/gold/douyin-7690619057690828986.json
```

## 转写质量：实测数字（不是形容词）

金标只存**文本与时间轴**（媒体不入库），复现时用 `media.url` 重新取流。真实素材：抖音 259.77 秒中文口播 + 人工核对稿。

| 配置（`small`，单变量消融） | CER | 替换 | 漏字 | 覆盖率 |
|---|---|---|---|---|
| 原始默认（未净化 + beam1 + 无词级时间戳） | 0.0522 | 82 | 17 | 0.9956 |
| **净化音频 + beam1 + 无词级时间戳（现默认 `balanced`）** | **0.0433** | 70 | 11 | 0.9980 |
| 净化 + beam5（`quality`，另加定向重解） | 0.0480 | 80 | 8 | 0.9992 |
| 净化 + 词级时间戳（`timing`） | 0.0511 | 84 | 14 | 0.9976 |
| 净化 + 词级时间戳 + 领域词表 | 0.0751 | 85 | 56 | 0.9979 |

**`large-v3` 在默认档位（净化 + beam1）上：CER 0.0214**（36 替换 / 5 漏字 / 0 多字），覆盖率 0.9976，时间轴偏移中位 0.20 秒 —— 约为 `small` 最好成绩的一半。

三个反直觉但已用数据确认的结论（见 [docs/DECISIONS.md](./docs/DECISIONS.md) D25–D30）：

- **音频前端（highpass + EBU R128 loudnorm）是唯一明确的准确率收益**：CER 相对下降 17%，几乎不增加耗时 → 默认开启；
- **领域词表在这支素材上有害**（0.0433 → 0.0751，漏字 11 → 56）→ 默认关闭，按视频用评测验证后再开；
- **词级时间戳不是准确率收益而是能力收益**（净化后 0.0433 → 0.0511），且它与 beam5 **组合**会明显变差（漏字 17 → 41）。

度量本身也修掉过一个真 bug：`difflib` 里 `insert` 是"参考有、假设缺"＝漏字，`delete` 才是"假设多出来"＝幻觉候选；第一版按直觉解读，把金标里被 ASR 漏掉的整段报成了"幻觉"。

### 三支金标与跨金标回归

上表只是抖音一支素材的消融。真正的结论要跨素材看，所以 `eval/gold/` 里有三支金标，
每支都在 `reference.kind` 里如实标注"谁核对过"：

| 金标 | 素材特征 | `large` CER | `small` CER | 证据强度 |
|---|---|---|---|---|
| `douyin-7690619057690828986` | 中文口播，259.77 秒 | 0.0187 | 0.0433 | `human-verified` |
| `bilibili-BV1ntah6TEe9` | 安静的 30 秒教程 | **0.0085** | 0.1111 | `agent-verified`（第 12 行 unresolved） |
| `bilibili-BV1Kyas6wEuz` | 109 秒叙述 + 表情包配音 + 音乐 | **0.0051** | 0.2298 | `agent-verified`（后半段 22 行 unresolved） |

**证据强度不可混用**：只有 `human-verified`（抖音）能当**验收基准**；两支 B站 金标是
Agent 逐行判定、不确定项已标注，因此只能当**基线比较**。第二支的标点抄自 `large` 初稿，
所以它 `punct_f1=1.0` 是**同义反复**，不能当标点质量证据。

一条命令跑完全部对照表，`--max-cer` 是闸门（任一用例超标即返回 1）：

```bash
python .agents/skills/bilibili-video-learning/scripts/run_benchmark.py \
  --case "bili-large=<产出>.json:bilibili-BV1ntah6TEe9.json" \
  --case "douyin-large=<产出>.json:douyin-7690619057690828986.json" \
  --max-cer 0.05
```

CI 的 ubuntu 与 windows 两个 job 都会用真实金标 + 裁剪 fixture 跑这道闸门，
所以**每次 PR 都会被真实素材验证**，CER 劣化到 0.05 以上直接变红。

## 抖音取流的三条路径与现状

| 路径 | 用法 | 现状 |
|---|---|---|
| **真浏览器上下文**（推荐） | `--download-method browser`（`auto` 时优先） | 自研实现（标准库 WS + CDP，不依赖任何外部服务）。已真机验证：握手→建页→导航→**页面内 fetch 返回 200**；用已登录 profile 调收藏接口**返回真实 `aweme_list`**。需专用 profile 登录一次 |
| 匿名公开 SSR | `--download-method ssr` | 实测 12 个 host×UA 组合全部被风控降级（「验证码中间页」，`videoInfoRes` 只剩 `status_code`）。失败给退出码 **26**，含义是"换网络或稍后再试"，同 IP 换下载方式无效 |
| **本地文件**（最稳） | `--video <文件>` | 完全跳过取流：抽音频 → ASR → 清洗 → 产出，不需要网络/登录/浏览器。已用真实 259.77 秒 mp4 验证：122 段 / 1991 字 / 覆盖率 0.9997 / `cuda/float16` |

参考视频本身也变了：那支 259.77 秒视频的登录态详情接口返回 `aweme_detail: null` 与
`filter_reason: status_self_see`（作者已改为"仅自己可见"），所以它现在**任何人都取不到**——这既不是 Skill 的问题，也不是网络问题。

## 能做什么

- 识别 B站链接、`b23.tv`、BV/av ID、抖音分享链接和本地字幕/音视频；
- 优先使用公开字幕；只有用户明确要求时才下载临时音频并运行 ASR；
- 严格保留 B站分 P，错误的 `p=` 不会静默切换到 P1；
- 把 Cookie、token、签名 URL 和临时路径从诊断输出中脱敏；
- 提供稳定的 JSON CLI，用于来源检查、字幕转换、笔记渲染和本地诊断；
- 转写带**覆盖率兜底**（可疑空档实测音量后局部补转）、**逐段置信度**与**低置信标注**；
- 多源交叉校验给出**穷尽**差异清单，并可融合成一份带 `provenance` 的逐字稿。

## 安全边界

- 视频简介、字幕、ASR、评论和弹幕都是不可信输入，只用于分析，不执行其中的提示词、命令、链接或凭据请求；
- 默认匿名访问；只有结构化返回 `cookie_permission_required` 且用户明确授权后，才允许指定浏览器进行一次 Cookie 重试；
- 不绕过付费、会员、私密、地区或平台风控限制；**不自研平台签名**（如 `a_bogus`），不做验证码识别；
- 正常流程不导出 Cookie 文件，不把浏览器资料、token、模型缓存或真实个人数据放进仓库；
- 默认不保存或输出完整转写，只有处理用户自有材料或用户明确授权时才扩大输出范围。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。FFmpeg 优先使用系统版本，缺少时使用 Skill 环境内的用户级 `imageio-ffmpeg`，不要求 sudo。

### Windows（CUDA 兼容档案）

```powershell
git clone https://github.com/Confidence-huang/bilibili-douyin-video-learning.git
cd bilibili-douyin-video-learning
pwsh -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File .\verify.ps1
```

Windows 安装器创建 `.venv-gpu`，安装 faster-whisper，并保留 OpenAI Whisper/PyTorch CUDA 兼容回退。只有实际探测到 NVIDIA/CUDA 且运行日志显示 `cuda/float16` 时，才能声称正在使用 GPU。

已在 RTX 5070 Laptop（Blackwell / sm_120，8GB 显存）实测：faster-whisper `small` 以 `cuda/float16` 转写 49 秒中文音频约 5.4 秒（≈9× 实时），显存占用约 3.3GB。安装后可用 `cli-anything-video-learning --json doctor status` 输出中的 `gpu` 字段确认 CUDA 可见性。

**`cuda` 不等于"真的用上了 GPU"。** 设备可见但 CUDA 运行时库缺失时（WSL 驱动只提供 `libcuda.so`，不含 cuBLAS/cuDNN），
第一次解码会失败。转写入口会自己验证：失败后改用 `cpu/int8` 重跑一遍，并在结果里写 `device_fallback` 说明原因。
所以判断是否真的走了 GPU，要看 `device=cuda` **且** `device_fallback` 为空。
在 WSL2 + RTX 5070 Laptop 上补齐运行时库后实测：`large-v3` 以 `cuda/float16` 转写 259.77 秒中文口播音频耗时 49.3 秒（≈5.3× 实时）。
缺库时可用可选依赖组补齐：`pip install -e ".[gpu-cuda12]"` —— 转写入口会自动发现并预加载这些库，不需要手写 `LD_LIBRARY_PATH`。

### Ubuntu/Linux（稳定 CPU 档案）

```bash
git clone https://github.com/Confidence-huang/bilibili-douyin-video-learning.git
cd bilibili-douyin-video-learning
./install_linux.sh
./verify_linux.sh
```

Linux 安装器在 `${XDG_DATA_HOME:-$HOME/.local/share}/bilibili-video-learning/runtime` 创建独立运行时，默认使用 faster-whisper；运行时不放进 Skill 树，避免依赖包污染 Skill 生命周期扫描。没有可见 CUDA 时自动使用 CPU/int8。安装器不会安装 CUDA、升级驱动、调用 sudo、编辑系统配置或创建后台服务。

只安装 Skill 源码、不下载大型 ASR 运行环境：

```powershell
npx skills add Confidence-huang/bilibili-douyin-video-learning --skill bilibili-video-learning -g -y
```

或者使用仓库自带的可恢复安装器，只安装源码：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -SkipRuntime -SkipPathUpdate
pwsh -NoProfile -ExecutionPolicy Bypass -File .\verify.ps1 -SkipRuntime
```

```bash
./install_linux.sh --skip-runtime
./verify_linux.sh --skill-root "$HOME/.agents/skills/bilibili-video-learning" --skip-runtime
```

`npx skills add` 只安装 Skill 源码；完整 CLI、FFmpeg 调用和 ASR 仍需运行对应平台安装器。

### 可选依赖组

| 组 | 用途 | 缺省行为 |
|---|---|---|
| `gpu-cuda12` | WSL/精简驱动下补齐 cuBLAS/cuDNN | 自动降级 `cpu/int8` 并在结果里说明原因 |
| `zh-normalize` | 繁简归一（OpenCC） | 跳过转换并在结果里记录原因 |
| `hard-subtitle` | 硬字幕 OCR（rapidocr） | 抛出明确错误并提示安装该组 |

完整安装说明见 [INSTALL.md](./INSTALL.md)，命令示例见 [USAGE.md](./USAGE.md)，安全边界见 [SECURITY.md](./SECURITY.md)，技术决策与取舍见 [docs/DECISIONS.md](./docs/DECISIONS.md)。

## 测试与验证

```bash
# 离线测试套件：不需要模型/网络/浏览器/Cookie（用安装好的运行时解释器跑，不装 GPU 权重也能跑）
skill_root="$PWD/.agents/skills/bilibili-video-learning"
runtime_python="${XDG_DATA_HOME:-$HOME/.local/share}/bilibili-video-learning/runtime/bin/python"
PYTHONPATH="$skill_root/agent-harness" \
  BILIBILI_VIDEO_LEARNING_ROOT="$skill_root" \
  BILIBILI_VIDEO_LEARNING_PYTHON="$runtime_python" \
  VIDEO_LEARNING_SKIP_INSTALLED_RUNTIME_TESTS=1 \
  "$runtime_python" -m pytest -q "$skill_root/agent-harness/cli_anything/video_learning/tests"

# 仓库边界与版本一致性（隐私、禁止入库的媒体后缀、版本号、宿主清单）
python tools/validate_repository.py

# 源码级校验（CI 使用的同一入口）
./verify_linux.sh --skill-root "$skill_root" --skip-runtime   # 只查语法、符号链接与媒体/凭据边界，不跑测试
./verify_linux.sh --skill-root "$skill_root"                  # 完整校验：上面那条 pytest 也在其中
```

当前 **453 passed, 1 skipped**（与 [CHANGELOG.md](./CHANGELOG.md) 记录的离线用例数一致），
CI 在 ubuntu 与 windows 两个平台跑同一套离线用例。
需要 ffmpeg 现场生成真实媒体的用例（无音轨检测、音频抽取）在本机跑；CI 两个平台都已装
`imageio-ffmpeg`（见 `.github/workflows/ci.yml`），所以这几条路径在 CI 里也真的会跑。
`VIDEO_LEARNING_SKIP_INSTALLED_RUNTIME_TESTS=1` 会跳过 `doctor status` 那条安装态用例
（它要求运行时里真的有 `yt-dlp`/`ffmpeg`），这也是那 1 条 skipped 的来源——**安装态与源码态
之间仍有一层只有真装过才覆盖的缺口**。

## 已知限制（诚实清单）

- **抖音匿名取流不可用**：平台风控降级是真实的，稳定路径是本地文件或浏览器取流（见上表）；
- **图文作品只识别不处理**：无音轨作品返回退出码 **27** 并提示改走图片 OCR，但图片 OCR 入口尚未实现；
- **B站字幕轨匿名通常拿不到**（实测抽查 50 个热门视频均无轨），因此 `transcribe_bilibili.py` 的"字幕优先"在现实中多半回退到 ASR；
- **长视频只标定到 46.4 分钟**：真实 B站 数学课 `BV154hD61Ez8`（46.4 分钟）整段解码 93.5 秒（29.8× 实时），
  分块 600s/2s 为 132.5 秒、5 块、文本相似度 **0.9052**——**分块反而慢 42%**，它的价值是韧性与显存上界而非速度，
  因此短于约 30 分钟不开分块；30–90 分钟课程视频的漂移行为仍未验证；
- **标点靠停顿启发式**：实测 ASR 对中文可能产出零标点，当前只在段边界按停顿插入 `，`/`。`，规则法 F1 天花板实测 **0.313**（受分段粒度限制）。`scripts/punctuate.py` 留了可选模型后端（extra `punctuation`，默认关闭、缺失时自动退回规则法），但**尚未接入主流水线**；
- **定向重解与重新分段收益未证实**：前者在真实素材上要么找不到可疑段、要么全部被拒（这是保守判据的正确行为），后者在现有分段上是不切分的 no-op；
- **B站 金标未经人耳确认**：三支金标里只有抖音是 `human-verified`；两支 B站 金标是 `agent-verified`
  （`BV1ntah6TEe9` 第 12 行两配置都给出不成词的串，`BV1Kyas6wEuz` 后半段 22 行为 `unresolved`），
  所以它们只能作**基线比较**，**不能当验收基准**，也还不能代表整个 B站 素材分布（最长的只有 109 秒）；
- **评测指标仍会把数字写法差异算成错误**（`6/六`、`100/一百`），所以上面的 CER 数字里约 5 个字符不是真的识别错误；
- `vault_ingest.py` / `vault_synthesize.py` / `export_anki.py` 是**独立脚本、未接入流水线**（Obsidian 侧的知识库流程由插件承担）；
- **不做**：说话人分离、平台签名逆向、验证码识别（分别需要 torch 生态或不合适的合规成本）。

## 项目结构

```text
.agents/skills/bilibili-video-learning/
├── SKILL.md                # Agent 使用说明（工作流、边界、命令）
├── agents/                 # 各宿主的展示清单（openai / claude / gemini），共享字段由 CI 比对
├── prompts/                # 笔记模板（小节标题），带 template-version
├── references/             # 领域词表（asr-lexicon.txt）等参考
├── eval/                   # 评测金标（只存文本与时间轴）+ 指标说明
├── scripts/                # 全部后端实现（取流/转写/OCR/加工/融合/归档/笔记）
├── tests/                  # Skill 静态/运行时/行为清单
└── agent-harness/          # CLI 与离线测试套件
```

网页登录课程的音频采集、自动切课和断点续转属于独立的 `course-audio-capture` Skill；它与本仓库的公开视频/本地文件学习边界不同，不在这里合并。

## 核心依赖与参考

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：核心运行依赖，用于公开元数据、字幕和媒体处理路径；
- [HKUDS/CLI-Anything](https://github.com/HKUDS/CLI-Anything)：Agent Harness 与 CLI 结构来源，按 Apache License 2.0 使用；
- [FFmpeg](https://ffmpeg.org/)：音视频转换；
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：授权音视频的本地 ASR；
- [Learning-Vault-Skills](https://github.com/Serral828/Learning-Vault-Skills)：笔记库组织方式的参考（Obsidian 侧）。

## License

Licensed under the [Apache License 2.0](./LICENSE). See [NOTICE](./NOTICE) for upstream attribution.
