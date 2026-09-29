# 技术决策记录

本文件记录**已经做过的设计决策**，重点是"为什么不那样做"。

它的价值在于：避免同一个坑被踩第二次。每条决策都写明**背景、取舍、实测数据、以及重新评估的触发条件**。
当你打算推翻某条决策时，先找触发条件——条件未到就不要改。

> 约定：本文件记录的是**结论**，不是计划。进行中的计划放在 `docs/superpowers/plans/`。

---

## D1. 运行时 Python 用 `sys.executable`，不依赖 PATH 里的 `python`

**决策**：所有后端脚本通过 `sys.executable -m yt_dlp` 调用，而不是裸 `yt-dlp`。

**背景**：Windows 上同时存在系统 Python、Store Python、`.venv-gpu`，PATH 顺序不可控。
裸 `yt-dlp` 可能解析到另一个解释器里装的旧版本，或干脆找不到。

**取舍**：放弃"用户手动装了 yt-dlp 就能直接用"的便利，换取确定性——
跑脚本的解释器就是跑 yt-dlp 的解释器。

**验证方式**：`tests/test_cross_platform.py::test_bilibili_metadata_uses_skill_python_module`
断言 `commands[0][:3] == [sys.executable, "-m", "yt_dlp"]`；
`::test_douyin_uses_skill_python_module` 覆盖抖音侧同一约定。

**重新评估触发条件**：如果未来要支持 `uv tool install` 的全局 CLI，需要重新设计解析层。

---

## D2. FFmpeg 优先用系统版本，缺失才落到 `imageio-ffmpeg`

**决策**：`scripts/media_tools.py::find_ffmpeg()` 先用 `shutil.which("ffmpeg")`，
失败才用 Skill 环境内的用户级 `imageio-ffmpeg`。

**背景**：Skill 安装不应该要求 sudo 或改动系统。

**取舍**：两段式回退让首次运行有 20MB 左右的隐式下载。
选择接受，因为"不要求 sudo"是硬约束。

**失败行为**：两者都不可用时抛 `RuntimeError`，并把 `imageio_ffmpeg` 的原始异常链上去——
不吞异常，用户能看到真正原因。

**验证方式**：`tests/test_cross_platform.py::test_media_tools_prefers_path_ffmpeg`
与 `::test_media_tools_falls_back_to_imageio_ffmpeg` 分别覆盖两条路径。

**重新评估触发条件**：如果 `imageio-ffmpeg` 体积或首次下载成为高频抱怨。

---

## D3. 默认匿名访问，Cookie 必须由结构化返回触发

**决策**：默认匿名请求。只有当后端返回 `status: cookie_permission_required`
**且**用户明确授权后，才允许指定浏览器做**一次** Cookie 重试。

**背景**：Cookie 等于账号凭据。一旦默认读取浏览器资料，风险面就失控了。

**取舍**：会员/付费内容默认取不到，需要显式授权。
这是刻意的摩擦——用一点点麻烦换取"不会静默动用户凭据"。

**实现位置**：`core/source.py` 返回结构化状态，`video_learning_cli.py` 消费该状态。
`transcribe_fallback.py` 是旧兼容工具，**固定匿名**，不参与授权流程（见 `SECURITY.md`）。

**重新评估触发条件**：不要重新评估。这是安全底线。

---

## D4. Linux 运行时不放进 Skill 树

**决策**：Linux 运行时装在 `${XDG_DATA_HOME:-$HOME/.local/share}/bilibili-video-learning/runtime`，
**不在** Skill 目录下创建 `.venv`。

**背景**：Skill 生命周期扫描会把 Skill 树内的文件当作 Skill 内容。
把几千个依赖包装进 Skill 树，会污染扫描结果，也让 Skill 源码包体积失控。

**取舍**：多一个需要管理的位置，但 Skill 树保持干净。
`install_linux.sh` 还会**主动拒绝**把 runtime 指向 Skill 目标目录（含路径包含关系检查），
由 `tests/test_cross_platform.py::test_linux_installer_rejects_runtime_inside_skill` 守住。

**对比**：Windows 用 `.venv-gpu` 放在 Skill 树内。这是历史包袱，
因为 Windows 侧依赖 Skill 树相对定位来解析解释器。

**重新评估触发条件**：如果要统一两平台布局，需要先解决 Windows 的解释器定位问题。

---

## D5. 版本号必须同步，且由 CI 强制

**决策**：以下版本号必须相同，由 `tools/validate_repository.py::validate_version_parity()`
与 `tools/cli_smoke.py` 双重强制：

1. `.agents/skills/bilibili-video-learning/pyproject.toml` ← 权威来源
2. `.agents/skills/bilibili-video-learning/agent-harness/setup.py`
3. `.agents/skills/bilibili-video-learning/agent-harness/cli_anything/video_learning/__init__.py`

**第 4 处是派生的，不单独校验**：`uv.lock` 的自引用条目由
`uv lock --check` 在 CI 里单独把关，`validate_version_parity` 不重复检查它——
因为 `uv.lock` 是生成物，改它应该走 `uv lock`，而不是文本替换。
两个闸门分工明确：一个管手写声明，一个管生成物的新鲜度。

**背景（真实事故，同一次发版两个文件都漏了）**：commit `6706dec` 把 `pyproject.toml`
从 1.4.1 升到 1.4.2，但：

- `uv.lock` 自引用条目没跟着改 → `uv lock --check` 在 CI 失败（run `34776520268`）；
- `agent-harness/setup.py` 没改 → 而当时**没有任何检查覆盖它**，被静默放过；
- `cli_anything/video_learning/__init__.py` 也没改 → `--version` 和 REPL banner
  对外报的是 **1.4.1**，用户看到的版本号是假的。

三者中只有第一个被 CI 抓到。这说明**只修数据、不加闸门是不够的**——
未被覆盖的位置不会自己暴露。

**取舍**：多一个校验函数、多一个冒烟脚本。换来的是"改版本号漏文件"在 CI 直接失败，
而不是发到用户手里。

**为什么需要两层**：`validate_version_parity` 比对静态声明，
`cli_smoke.py` 验证**运行时实际输出**。前者抓不到"声明对了但入口读错了变量"这类问题，
后者抓不到 `setup.py` 这类不参与运行的元数据。两层互补，缺一不可。

**重新评估触发条件**：如果引入 `setuptools-scm` 或 `importlib.metadata` 动态取版本，
第一、二、三处可合并为一处，本决策可整体简化。

---

## D6. 不采用 `you-get` 作为主下载路径

**决策**：主路径用 `yt-dlp`（`sys.executable -m yt_dlp`）。
`you-get` 仍留在依赖里，但不是默认路径。

**背景**：早期版本以 you-get 为主。它在分 P 处理上会静默回退到 P1，
导致用户请求 `p=3` 却拿到 P1 的内容——而且是**静默**的，最难排查。

**取舍**：yt-dlp 的参数面更复杂，但行为可控。
本项目的硬约束是"错误的 `p=` 不会静默切换到 P1"（见 README）。

**重新评估触发条件**：如果 yt-dlp 对某平台的公开视频支持出现回归。

---

## D7. 笔记与字幕一律原子写入

**决策**：所有输出经 `file_output.py::write_text_atomically` / `write_json_atomically`，
同目录临时文件 + 替换，不用直接覆盖写。

**背景**：笔记要写进用户的 Obsidian vault。
如果进程中途失败，直接覆盖会**摧毁已有笔记**——这是不可接受的数据损失。

**取舍**：多一次文件系统操作。完全不值得优化掉。

**验证方式**：`tests/test_core.py::test_atomic_write_failure_preserves_previous_file`
证明写入失败时旧文件完好无损。

**重新评估触发条件**：不要重新评估。

---

## D8. ASR 默认 `faster-whisper`，PyTorch 仅作兼容回退

**决策**：主 ASR 路线是 `faster-whisper`（CTranslate2 后端）。
`openai-whisper` + `torch==2.11.0+cu128` 放在 `cuda-compat` extra 里，仅作回退。

**背景**：实测数据（RTX 5070 Laptop / Blackwell sm_120 / 8GB 显存）：

| 项目 | 数值 |
|---|---|
| 模型 | faster-whisper `small` |
| 精度 | `cuda/float16` |
| 输入 | 49 秒中文音频 |
| 耗时 | 约 5.4 秒（≈9× 实时） |
| 显存占用 | 约 3.3GB |

**取舍**：faster-whisper 依赖 CTranslate2，需要匹配的 CUDA 运行时；
PyTorch 回退体积巨大（数 GB）。所以主路线选轻的，重的按需装。

**严禁**：不得在未实际探测到 NVIDIA/CUDA 且运行日志显示 `cuda/float16` 的情况下声称在用 GPU。
`doctor status` 的 `gpu` 字段是人可复核的确认入口，且 `gpu` **不参与** `ok` 判定——
GPU 不可用是正常降级，不是故障。

**重新评估触发条件**：CTranslate2 对新架构显卡的支持出现缺口时，`cuda-compat` 会变成主路线。

---

## D9. CI 不下载多 GB 的 GPU 运行时

**决策**：CI 只装 `pytest click prompt-toolkit requests setuptools`，
用 `VIDEO_LEARNING_SKIP_INSTALLED_RUNTIME_TESTS=1` 跳过需要真实 ASR 的测试。

**背景**：完整 GPU 依赖是数 GB，CI 每次跑会拖到十几分钟且极易超时。

**取舍**：CI **不验证** GPU 路径。这是已知缺口，靠本地实测数据（见 D8）和
`doctor status` 手工确认来补。

**重新评估触发条件**：如果 GPU 相关代码出现多次回归，考虑加一个 nightly job 专门跑 GPU 路径。

---

## D10. 仓库不含 Node.js 依赖，不受 Node 版本影响

**决策**：本仓库是纯 Python 项目，CI 不装 Node。

**背景**：曾有人（包括本项目的维护者）误以为需要跟进 Node 版本。
实际核查：仓库 44 个 `.py`、0 个 JS/TS，无 `package.json`、无 `.nvmrc`。

**Node 的全部出现位置**：`README.md` 中的一条可选安装命令
`npx skills add ... -g -y`。它不参与 CI、不参与运行时。

**唯一相关约束**：npm 上的 `skills` 包声明 `engines: node >=22.20.0`。

核对于 2026-09-21：`skills@1.7.0` 的 engines 为 `node >=22.20.0`，
而当前 Node 版本线是 **24 LTS**（`24.21.0`）与 **26 Current**（`26.9.0`，非 LTS）。
22/24/26 **都满足**，所以不存在"必须换 Node 版本"这回事。

**重新评估触发条件**：如果未来引入基于 Node 的构建步骤（例如 JS 版 Skill 包装器），
或 `skills` 包把 engines 下限提到超过 22.20.0。

---

## D11. CI 冒烟测试只断言确定性契约，不断言宿主工具

**决策**：`tools/cli_smoke.py` 强制断言 `--help` / `--version` 与版本一致性；
`doctor status` 仅作**诊断**，不作为通过/失败条件。

**背景（真实事故）**：第一版冒烟脚本要求 `doctor status --json` 返回全部文档化键。
CI 立刻在 **两个平台都失败**，报 "missing documented keys"。

排查后确认是两件事叠加：

1. `doctor` 内部调用 `media_tools.find_ffmpeg()`，在没有 FFmpeg 且没有 `imageio-ffmpeg`
   的主机上会**抛异常**，而不是返回一份带 `ok: false` 的报告；
2. 异常被 `handle_error` 包装成 `{"ok": false, "error": ...}`，
   于是一个"环境缺工具"的问题**伪装成**"JSON schema 缺字段"，
   报错信息完全指向错误的方向。

**取舍**：`doctor` 的可用性不再由 CI 保证，改由本地人工确认（与 D9 同一思路）。
换来的是这个检查**不会因为 runner 镜像的差异而红**——
否则任何贡献者都无法从代码里修好它。

**教训（比决策本身更重要）**：
- 断言要打在**确定性契约**上（`--help` 退出码、版本号相等），
  不要打在**宿主环境属性**上（某个工具装没装）。
- 错误信息必须能区分"结构不对"和"前置条件缺失"。当前 `cli_smoke.py`
  遇到 `{"ok": false, "error": ...}` 会直接把 `error` 原文抛出来，
  不再谎报 schema 问题。

**验证方式**：注入 `__version__ = "9.9.9"` 时脚本必须失败；
在**不含 FFmpeg 的干净 PATH** 下必须通过。

**重新评估触发条件**：如果 CI 明确安装了 FFmpeg 与 yt-dlp，
可以把 `doctor` 提升为强制断言。

---

## D12. 笔记骨架外置到 `prompts/*.md`，但默认输出保持字节不变

**决策**：笔记的小节标题来自 `.agents/skills/bilibili-video-learning/prompts/*.md`，
由 `scripts/prompt_templates.py` 加载。模板缺失或不可读时**降级到内置骨架**，不抛异常。

**背景**：同一套中文小节标题原本硬编码在三个后端里
（`build_notes.py`、`fetch_bilibili.py`、`douyin_extract.py`）。
改一个标题要在三个文件里找字符串，且没有任何东西保证它们一致。

**取舍（关键）**：模板化只替换**小节标题**，不接管表格、YAML frontmatter、时间轴格式。
原因是笔记里混着 `datetime.now()`，任何整段模板渲染都会让输出随运行时间变化，
从而无法用测试断言"重构没有改变默认输出"。
只换标题，就可以把默认输出钉成一个**逐字节可比**的契约。

**失败行为**：`prompts/` 目录缺失时，`build_notes._template_sections()` 返回
`"builtin-fallback"` 并继续出笔记。这是刻意的：
笔记是流水线的最后一步，前面已经花了几分钟的 ASR，不能因为一个模板文件丢了就整单失败。
降级**会被报告**（stderr 一行 `template=<版本>`），所以它不是静默的。

**验证方式**：
- `tests/test_prompt_templates.py::test_build_notes_keeps_the_original_default_headings`
  钉住原骨架的七个标题与时间轴格式；
- `::test_douyin_note_falls_back_when_template_is_missing` 断言模板缺失时仍能出笔记；
- `::test_template_name_must_be_a_bare_stem` 阻止路径穿越式模板名。

**重新评估触发条件**：如果要让用户自定义**整篇**模板（含表格），
需要先解决 `datetime.now()` 造成的不可比问题（例如把时间作为参数注入）。

---

## D13. 依赖版本下限必须写明原因

**决策**：`pyproject.toml` 里每个 `>=` 下限都在紧邻注释或本表里给出理由，
不接受"顺手升一下"的裸下限。

**背景**：版本下限有两种来源——**安全修复**和**功能/兼容性需要**。
两者在 `pyproject.toml` 里长得一模一样（都是 `>=`），
于是下一个人无法判断"能不能降下来"或"这个数字是不是随便写的"。

**当前全部下限及理由**：

| 依赖 | 下限 | 性质 | 理由 |
|---|---|---|---|
| `click` | 8.1.7 | 功能 | 本 CLI 依赖的 `click.Choice` 与 context object 行为 |
| `imageio-ffmpeg` | 0.6.0 | 功能 | 首个自带 FFmpeg 支持抖音画质探测所需 `-f` 探测的版本 |
| `prompt-toolkit` | 3.0.48 | 修复 | 修复 Windows Terminal + CJK 输入下的 resize/重绘崩溃 |
| `requests` | 2.32.4 | **安全** | 移除 CVE-2024-47081 / GHSA-9hjg-9r4m-mvj7 的 `.netrc` 凭据泄漏行为 |
| `setuptools` | 75.0.0 | 兼容 | 移除在 Python 3.12+ 上失效的内置 `pkg_resources` 路径 |
| `you-get` | 0.4.1743 | 功能 | 仅保留为历史 fallback（见 D6），非主路径 |
| `yt-dlp` | 2026.7.4 | 功能 | 首个支持对抖音使用 `--impersonate chrome-110:windows-10` 的版本 |
| `faster-whisper` | 1.2.1 | 功能 | `asr` extra；提供本机实测的 CTranslate2 加速路径（见 D8） |
| `curl-cffi` | 0.15.0 | 功能 | `impersonate` extra；与 yt-dlp 声明的 impersonation 版本对齐 |
| `pytest` | 8.4.1 | 功能 | `dev` 组；`monkeypatch` 与临时目录夹具的现行行为 |

**注意**：`requests` 下限是**唯一有安全含义**的一条。改动它需要重新核对
CVE-2024-47081 的修复范围，而不是只看能否解析。

**取舍**：注释让 `pyproject.toml` 变长。换来的是"这个数字不能动"这件事本身可读。

**重新评估触发条件**：依赖自身发布新的 CVE 时，
**追加新行**而不是改写旧行的理由——历史理由仍然有效，只是不再充分。

---

## D14. 三个宿主的 interface 清单必须一致，由 CI 比对

**决策**：`agents/openai.yaml`、`agents/claude.yaml`、`agents/gemini.yaml`
三个文件都必须存在，且 `display_name` / `short_description` / `brand_color`
三个字段必须与 `openai.yaml` 完全相同。由
`tools/validate_repository.py::validate_host_manifests()` 强制。

**背景**：Skill 的**行为**只有一个来源（`SKILL.md`），三个宿主都读它。
但**展示信息**是每个宿主读各自的文件。这意味着一个显示名要写在三个地方，
而"改名只改了一个宿主"是这类多宿主项目最典型的静默漂移。

**为什么是三份而不是一份 + 生成**：
各宿主的清单格式并不完全一致（Claude 用 `invocation_name`，
OpenAI 用 `$skill-name` 形式的 `default_prompt`），
强行共用一个文件会需要一层转换脚本，而转换脚本本身也要维护。
现在的做法是**允许各自的私有字段不同，但把共享字段钉死**——
重复仍然是重复，但重复的部分不会漂移。

**校验边界**：只比对共享的三个字段，以及"必须引用 Skill 名"。
不比对 `default_prompt` 的措辞，因为各宿主的调用语法本来就不同
（OpenAI 用 `$bilibili-video-learning`，Claude/Gemini 用裸名）。
把措辞也钉死会阻止为新宿主写自然的提示语。

**验证方式**：故障注入三次，全部按预期失败——
`claude.yaml` 改 `brand_color`、`gemini.yaml` 改 `display_name`、删除 `gemini.yaml`。
消息分别指明"哪个文件的哪个字段与 openai.yaml 不一致"。

**重新评估触发条件**：如果某个宿主开始支持从 `SKILL.md` 读取展示信息，
该宿主的清单文件可以删掉，本决策相应收窄。

---

## D15. CI action 版本按各自仓库的 tag 策略引用

**决策**：`actions/checkout`、`actions/setup-python` 用浮动主版本 tag（`@v7`），
`astral-sh/setup-uv` 用完整版本（`@v10.1.0`）。

**背景（真实事故）**：为消掉 GitHub 的 "Node.js 20 is deprecated" 警告，
把三个 action 都升到了"最新主版本"，其中 `astral-sh/setup-uv@v10` 让 CI 在
**job setup 阶段**就失败，耗时 8 秒：

```text
Unable to resolve action `astral-sh/setup-uv@v10`, unable to find version `v10`
```

**根因**：`setup-uv` 在 **v8.0.0** 的发布说明里明确写了
"Remove update-major-minor-tags workflow"（发布的标题就叫
"Immutable releases and secure tags"）。
也就是说它**主动不再维护浮动主版本 tag**，`v10` 这个 ref 从来没被创建过。
核对仓库 tag 列表确认：存在 `v10.0.0` / `v10.0.1` / `v10.1.0`，但不存在 `v10`。
`actions/checkout` 和 `actions/setup-python` 仍然维护浮动主版本 tag，`@v7` 正常解析。

**教训**："最新主版本"不是所有 action 都支持的引用方式。
在跨仓库统一升级 action 版本之前，必须**先确认该仓库的 tag 策略**——
有的仓库把浮动 tag 当作供应链风险主动去掉了。

**取舍**：`setup-uv` 钉完整版本意味着补丁升级需要手动改。
这是刻意的：该 action 既然拒绝浮动 tag，跟着它钉死才是一致的做法。

**重新评估触发条件**：如果 `setup-uv` 恢复发布浮动主版本 tag，
或者我们决定统一改用 commit SHA 固定（供应链更严的做法）。

---

## D16. 静默丢掉的多秒语音必须被运行时校验发现并局部补转

**决策**：`scripts/speech_to_text.py` 在转写结束后统一做覆盖率校验，**不对任何单一开关做假设**。
用 ffmpeg 量出音频总时长，以及每个 ≥2 秒空档的平均音量；把「空档 + 音量高于 -35 dBFS」判定为被整段丢掉的语音，
切出该窗口用 `vad_filter=False` 重新解码并并回时间轴。补转结论与判断依据（覆盖率前后、空档清单、每个窗口的实测音量）
全部写进 `diagnostics`。默认开启，`--no-vad` 与 `--no-coverage-retry` 可显式关闭。
校验逻辑放在 `scripts/asr_coverage.py`，转写引擎通过回调注入，
因此这段逻辑在**没有 GPU、没有模型、没有 ffmpeg 的 CI 里也能被完整测试**。

**背景（真实事故）**：一条 4 分 19 秒（259.77 秒）的抖音口播视频，
在 `faster-whisper large-v3` + CPU/int8 + `beam_size=5` + `word_timestamps=True` + `initial_prompt`
+ `vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200}` 下，
第一遍输出在 `196.24 → 201.84s` 出现 5.6 秒空档，丢掉的是：

```text
哪怕别人对他的生活、对他的过往一无所知，大家还是会这么想。
甚至哪怕是一个所谓成功的男性，但有一天他突然累了、穷了、病了、失去价值了。
```

实测该窗口 `mean_volume = -12.9 dBFS`、峰值触顶 0 dB —— **是响亮的说话声，不是静音**。
该配置在 CPU/int8 上两次运行**逐段文本完全一致**（180 段 / 1890 字 / 覆盖率 0.9760 / 同一空档），
说明"同配置可复现、跨配置位置漂移"，而不是偶发噪声。

**单因素消融（GPU/float16，全量音频，每个变体跑一次）**：

| 变体 | 段数 | 覆盖率 | ≥2 秒空档 |
|---|---|---|---|
| V0 原始配置（beam5 + word_timestamps + prompt + VAD 400/200） | 180 | 0.9767 | `101.88–107.32`（5.44s） |
| V1 去掉 `word_timestamps` | 175 | 0.9981 | 无 |
| V2 去掉 `initial_prompt` | 199 | 0.9550 | `121.74–128.02`、`199.78–204.56` |
| V3 `beam_size` 5 → 1 | 174 | 0.9976 | 无 |
| V4 VAD 回默认 2000/400 | 172 | 0.9742 | `52.42–57.98`（5.56s） |
| V5 **关闭 VAD** | 172 | 0.9742 | `52.42–57.98`（5.56s，**与 V4 完全相同**） |

三条结论：

1. **多秒丢失是真实的，且在多种配置下都会发生**（覆盖率 0.955–0.977），不是单次事故；
2. **它不是 VAD 造成的**：V5 关闭 VAD 后，空档位置与长度与 V4 逐字节相同。
   丢失发生在解码/分段层面，`word_timestamps=False`（V1）与 `beam_size=1`（V3）在本轮里没有空档，
   但每个变体只跑一次，只能记为**相关**，不足以断言唯一成因；
3. **丢失位置每次不同**（CPU/int8 在 196.24s，GPU/float16 在 101.88s，另有 52.42s、121.74s、199.78s）。
   因此固定时刻的测试抓不到它——**只有运行时校验才靠得住**，这也是本决策存在的核心理由。

**本实现的第一版是错的（已修正）**：最初把覆盖率校验用 `vad_filter` 门控，理由写的是"没开 VAD 就不会被吞"。
V5 直接推翻了这个前提。门控已删除：**无论第一遍是否启用 VAD，覆盖率校验一律执行**，
并加了回归用例 `test_coverage_guard_runs_even_when_the_first_pass_had_vad_disabled`。

**为什么不能只用覆盖率阈值**：这条视频补转前的覆盖率是 0.9760，高于任何“看起来正常”的经验线；
V2 的覆盖率甚至低到 0.9550 却只丢两段。只按比例报警一定会漏掉小空档，也会在短片段被误报。
「时间轴空洞 + 实测音量」才是有因果关系的判据，比例只作为诊断信息展示。

**代价与上限**：每个可疑窗口要多付一次 ffmpeg 切片 + 一次短解码。因此设了三个上限：
一次运行最多补转 5 个窗口、单个窗口不超过 60 秒、**拿不到音量时不补转**（宁可不报，不误报）。
补转结果进入缓存，同一视频不会重复付这份成本。

**验证方式**：
- 离线单测（`test_asr_coverage.py`）：真实丢字产物只定位到一个 `196.24–201.84s` 空档；
  静音空档不补转、补转窗口带实测音量、预算封顶、超长窗口跳过、重叠区间只算一次、
  关闭校验时保持原样、**关闭 VAD 时仍然校验**。
- 真实音频端到端（本机 GPU，真实 ffmpeg 探测 + 真实解码）：用那份真实产出的分段跑校验，
  实测时长 259.74s、该窗口音量 **-12.9 dBFS**（与独立测量一致），真实解码补回 4 段，
  覆盖率 **0.9760 → 0.9975**，四句探针全部回来，补转耗时 1.3 秒。

**已知取舍**：补转窗口一律 `vad_filter=False`。丢字成因尚未定位到唯一因素，所以这不是"关掉元凶"，
而是"换一种解码条件、在隔离上下文里重跑"；理论上存在把音乐当人声的幻听风险，
缓解手段是"先实测音量、只补转被判定为响亮的窗口"，且补回文本与第一遍一样接受笔记阶段复核。

**为什么不把参数对象写成 `@dataclass`**：本仓测试用 `importlib` 动态加载脚本且不注册 `sys.modules`，
`@dataclass` 会在 `sys.modules.get(cls.__module__).__dict__` 处抛 `AttributeError`。
改用 `typing.NamedTuple`（同样不可变，且有 `_asdict()` 可用于缓存身份）。

**重新评估触发条件**：
- 如果上游定位并修掉这个解码层面的整段丢失，可把 `coverage_check` 默认改为关闭；
- 如果补转耗时成为长视频的主要成本，可改为"只报告不补转"；
- 如果将来为了逐字稿开启 `word_timestamps`（V1 显示它与更大的空档相关），应把覆盖率校验视为必需项而不是可选项。

---

## D17. 缓存身份必须包含转写参数与引擎版本

**决策**：抖音缓存身份在 schema / platform / source / 期望视频 ID / 模型 / 语言 / 下载方式 / 画质 / 水印之外，
再加入 `asr_params`（来自 `TranscriptionSettings.identity()`：参数版本 + 模型 + beam + VAD +
`condition_on_previous_text` + 覆盖率开关与阈值）和 `engines`（当前环境里 faster-whisper / openai-whisper 的版本）。
`CACHE_SCHEMA_VERSION` 由 2 升到 3。

**背景**：v2 的身份只覆盖"取流与模型选择"，不覆盖"怎么解码"，于是有两类静默错误：
把 `vad_filter` 从 `True` 改成 `False`（正是 D16 的修复方向）之后，旧缓存仍会被判定为有效并复用，
用户拿到的还是丢字版本；升级 faster-whisper 补丁版本会改变识别结果，但缓存键不变。

**取舍**：引擎版本进键意味着"升级依赖后第一次运行必然重新转写"，
牺牲一次缓存命中，换取"缓存内容与产生它的代码一致"。参数身份由 `TranscriptionSettings.identity()` 统一导出，
不把默认值抄在缓存层——否则调用点的默认值与缓存键会各自漂移。

**验证方式**：`test_core.py::test_douyin_cache_key_tracks_asr_parameters_and_engine_versions`
断言 beam、VAD 与引擎版本变化都会改变缓存键；
`::test_douyin_cache_rejects_legacy_identity_without_asr_parameters` 断言 v2 信封不会被 v3 请求复用。

**重新评估触发条件**：如果参数身份继续膨胀（例如引入标点模型版本、提示词 hash），
应改为对整份 `TranscriptionSettings` 求稳定哈希，而不是继续逐字段展开。

---

## D18. 退出码是给 Agent 看的契约，必须能区分“该重试”和“该换机器”

**决策**：退出码集中到 `agent-harness/.../utils/exit_codes.py` 一处定义，并由 `runtime_output.py` 对所有后端脚本可见：
`0` 成功 / `1` 未分类 / `20` 分享页取流不可用 / `21` 需要 Cookie 授权 / `22` 网络超时 /
`23` 画质不可用 / `24` 本地转写失败。`classify_failure()` 先按异常类型判断，再对第三方异常按文本兜底。
抖音主入口 `douyin_extract.main()` 改用分类结果，并把同一个码写进结构化 JSON 的 `exit_code` 字段。

**背景**：此前几乎每个失败都返回 `1`（B站脚本是“异常 = 2，后端声明错误 = 1”）。
Agent 拿到 `1` 无法判断下一步该做什么：等一会儿重试、请用户授权 Cookie、降画质，还是换一台有 GPU 的机器。
结果是两种坏行为——对永久失败反复重试，或对临时失败直接放弃。

**边界与取舍**：
- 只改**上层入口**（`douyin_extract.main`）和 **B站脚本的异常分支**。`douyin_ssr.main` 保持不变：
  它是诊断后端，已有测试固定其“取流失败 = 1”的行为，改它属于另一件事，不混进本次改动。
- B站脚本的异常分支保留 `2` 作为“未分类异常”的兜底，只有分类成功时才用 20/22/24，
  避免静默改掉一个可能已被外部依赖的数字。
- `classify_failure` 对 `requests` 这类第三方异常只能按模块名与文本兜底，这一点写在代码注释里，
  不假装它是精确判断。

**真实测试补上的一课**：第一版实现只把 `DouyinSSRDownloadError` 认作取流失败，而最常见的真实故障——
SSR 与 yt-dlp **两条路径都失败**——抛的是普通 `RuntimeError`，于是实测退出码落回 `1`，Agent 仍分不清
"稍后重试"和"换个下载方式"。现已新增 `DouyinDownloadUnavailableError` 承载同一份 JSON 诊断体并纳入分类元组；
坏输入的真实命令现在返回 `20`（此前为 `1`），JSON 里的 `exit_code` 与之一致。

**验证方式**：`tests/test_exit_codes.py` 断言各码互不重复、四类故障各自映射正确、
抖音 `main()` 在取流失败与转写失败时分别返回 `20` 与 `24` 且 JSON `exit_code` 与之一致、
转写步骤把底层异常包装成 `TranscriptionFailedError`、B站未分类异常仍返回 `2`、
CLI 授权码与后端引用同一常量。

**重新评估触发条件**：如果宿主（Codex/Claude）开始按退出码做自动重试策略，需要把码表写进 SKILL.md 并冻结；
如果 `douyin_ssr.py` 也需要独立交给 Agent 使用，再统一它的退出码。

---

## D19. 设备可用性必须由一次真实解码证明，而不是由 `get_cuda_device_count()` 声明

**决策**：`_run_faster_whisper` 把“构造模型 + 第一遍解码”放进同一个 `try`。
如果自动选中的 `cuda`/`float16` 组合真的跑不起来，就改用 `cpu`/`int8` 重跑一遍，
并把原因写进结果字段 `device_fallback` 和诊断条目。用户显式传 `device="cuda"` 时不降级。
另外，openai-whisper 兜底也失败时改为 `raise faster_error from fallback_error`，**保留根因**。

**背景（WSL 真机复现）**：在 WSL2（`6.18.26.1-microsoft-standard-WSL2`）上 GPU 直通是好的——
`/dev/dxg` 存在、`/usr/lib/wsl/lib/libcuda.so.1` 在加载器路径里、`ctranslate2.get_cuda_device_count()` 返回 1。
缺的是 **CUDA 运行时库**：WSL 驱动只提供 `libcuda.so`，不含 cuBLAS / cuDNN。于是链路变成：

1. 设备枚举通过 → 代码选中 `cuda`/`float16`；
2. `WhisperModel(...)` 构造也通过；
3. **第一次 `encode()` 才抛** `Library libcublas.so.12 is not found or cannot be loaded`；
4. 旧兜底是 openai-whisper，而 Linux 档案不装 torch → 整条 ASR 失败，而 CPU/int8 本可以跑完。

**关键细节**：错误发生在第一次解码，不在构造期。只包住 `WhisperModel(...)` 的 try/except 抓不到它——
第一版修复就是这样漏掉的，是端到端实测把它抓了出来。

**代价**：多包一层会把“能加载但算不动”也算作设备不可用，于是多付一次 CPU 模型加载；
换来的是拿到正文，而不是整条失败。

**验证方式（两个方向都有真实证据）**：
- 缺 CUDA 运行时：日志显示 `CUDA unusable (Library libcublas.so.12 ...); retrying on cpu/int8`，
  抖音 259.77 秒音频与 B站 30.6 秒视频都在 `cpu/int8` 下跑通（B站结果 `status: ok`、`device: cpu`）。
- CUDA 运行时齐全（`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`）：同一台机器上
  `large-v3` + `cuda/float16` 转写 259.77 秒音频耗时 **49.3 秒（5.27× 实时）**，`device_fallback` 为 `None`，
  说明没有误判降级。
- 单元测试：`test_cross_platform.py` 三个用例分别覆盖惰性失败降级、构造期失败降级、显式 CUDA 不降级。

**重新评估触发条件**：如果 ctranslate2 提供官方的设备自检 API，或宿主机保证 CUDA 运行时随驱动一起提供，
可改成启动时一次性探测并缓存结果，省掉失败时的那次重跑。

---

## D20. 跨脚本传递的分段只有一个形状：`start/end/text`

**决策**：新增 `scripts/normalize_transcript.py` 作为唯一的形状适配层，规范形状为
`{"start", "end", "text"}`（可选 `confidence`）。ASR 边界内部的 `{"from", "to", "content"}` 只在
`speech_to_text.py` 里出现，跨脚本一律先转成规范形状。不认识的形状抛 `TranscriptSchemaError` 并打印实际字段名，
不做猜测。`clean_transcript.py` / `chunk_transcript.py` / 抖音输出的 SRT 与 TXT 都由这一层产出。

**背景**：同一条链路里长期并存两套字段名——字幕解析产出 `start/end/text`，ASR 产出 `from/to/content`，
而清洗与切分脚本只认前者。后果是抖音的转写结果（当时还只有 `start/text`）喂进 `clean_transcript.py`
直接 `KeyError`，这两个脚本因此**从未被任何生产代码调用**：SKILL.md 工作流第 5 步声称的"清洗时间戳、
去重复填充词、按主题切分"实际只停留在文档里。B 站侧更直白：`fetch_bilibili.py` 把 ASR 段
（`from/to/content`）直接塞进字幕列表，与平台字幕段混在同一个 `subtitles` 数组里。

**保真度必须显式**（同一条决策的另一半）：旧 `clean_transcript.py` 会在任何场景下丢弃
"整段等于填充词"的分段，其中包含 `这个`——而"这个社会对于好男人的定义"里的 `这个` 是语义成分。
现在拆成 `--fidelity verbatim|cleaned`：**verbatim 默认、绝不删词**（只合并 <0.5s 碎片、去相邻重复），
cleaned 才允许删填充词并**逐条报数**。逐字稿、字幕、可引用文本一律用 verbatim。

**取舍**：规范形状要求 `end`，因此 1.5 之前保存的抖音结果（只有 `start/text`）会被拒绝，
错误信息里直接说明"这是旧结果、重跑一次即可"。选择拒绝而不是用 `start + 0` 猜一个 end——
猜出来的时间轴会污染下游字幕与覆盖率计算，而重跑的成本只有一次提取。
缓存身份同时升到 schema 4 并纳入 `fidelity` 与归一模式，所以旧缓存不会被复用。

**验证方式**：`tests/test_normalize_transcript.py` 覆盖两种形状识别、往返转换、未知形状失败、缺时间戳失败、
置信度不被伪造、时间轴自检、SRT 时间戳与折行不丢字、verbatim 绝不删词、cleaned 删词必报数、
短碎片合并真实生效（旧代码只算了 duration 没用）、切块脚本能吃 ASR 形状（旧的 KeyError 回归）、
切块产出能被 `build_notes` 消费、`--emit` 参数校验、多格式产出与完整转录的授权边界、缓存身份区分保真度。
真实端到端：抖音真实分享链接 `--fidelity cleaned --emit md,json,srt,txt --include-transcript` 产出 4 个文件；
JSON 段落字段为 `start/end/text`、`full_text` 与分段拼接逐字一致；SRT 207 条时间戳全部合法；
清洗报告为 `208 -> 207`（合并 1 个碎片、删词 0）。真实数据回归：180 段 ASR 形状直接喂给清洗脚本，
得到 179 段、1890 字不变、`dropped_fillers=0`——过去这一步是 `KeyError`。

**重新评估触发条件**：如果将来要接入平台官方字幕轨道作为主源，
规范形状需要增加"来源"字段（如 `source: subtitle|asr`）以便交叉校验。

---

## D21. 转写正确性靠多源交叉校验，而不是靠"更信任某个模型"

**决策**：新增 `scripts/verify_transcript.py`，把两份来源做字符级对齐（`difflib.SequenceMatcher`），
输出**穷尽**的差异清单：每条含类型（replace / delete / insert）、时间范围、两侧文本。
默认 `--min-span-chars=1`——**同音字差异常常正好是一个字**（`血/雪`），阈值调高会把最该看的差异过滤掉。
`--fail-on-difference` 返回新退出码 25（`EXIT_SOURCES_DISAGREE`），让调用方按码决定是否需要人工复核。

**背景（真实数据）**：在 259.77 秒的真实视频上比对本项目自己的两条产出——硬字幕 OCR（220 张卡片）
与 ASR（180 段）：相似度 **0.9547**、**40 条**差异（9 条仅主源有、5 条仅次源有、26 条替换）。清单里既有 ASR 的
同音错字（`血/雪`、`再/在`、`从小到大/创造了`、`供血/工学`、`赡/赠`），也有**主源自己的问题**：

- `insert 45.5s  ASR='高级更'` —— 正是人工核对时漏掉的那张 0.4 秒短卡片「更高级」；
- `replace 53.5s 字幕='赠' ASR='赡'` —— 硬字幕 OCR 认错的字。

也就是说这份清单**独立复现了两次人工发现**，并且一次给出全部 40 处，而不是抽样几处。

**取舍**：不做自动纠正。谁对谁错依赖上下文（`可怜/可连` 同音且都讲得通），工具只负责把差异摆全；
也不引入对齐模型：字符级 `difflib` 对中文已经够用，且零依赖、可在无网络无模型的 CI 里跑完。

**验证方式**：`tests/test_verify_transcript.py` 覆盖完全一致（零差异）、同音替换带正确时间戳、
单字差异默认必报且可显式过滤、次源整段缺失被指名到时间范围、真实丢字区间（`196.24–201.84s`）被完整报出、
Markdown 表格渲染、三种输入格式（规范 JSON / ASR JSON / SRT）、`--fail-on-difference` 返回 25、
坏输入清晰失败、新退出码与既有码不重复。

**重新评估触发条件**：如果将来接入标点恢复或第三方纠错模型，
它们应当作为**第三个来源**参与本工具的对齐，而不是替换掉某一份来源。

---

## D22. 文本归一化必须显式且可降级：繁简与停顿句读

**决策**：`normalize_transcript.py` 增加两个纯文本级能力，并通过 `--simplify {auto,on,off}`（默认 auto）
接入抖音管道；归一模式与清洗模式一样进入缓存身份。

- **繁简归一**：装了 OpenCC 就转，没装就**跳过并记录原因**（`auto`）；显式要求 `on` 却缺依赖才报错。
  依赖是可选包（`opencc-python-reimplemented`），不进主依赖。
- **停顿句读**：`join_with_pause_punctuation()` 只在**段与段的边界**按静音长度插入 `，` / `。`，
  **绝不改动段内文字**，因此每一句都能回到原始分段。可读版本写进 `readable_text`，
  而 `full_text` 保持逐字拼接不变——两者语义不同，不能混用。

**背景**：
1. 同一个模型在不同设备/精度下字形不同：实测 `large-v3` 输出简体，而 `large-v3-turbo` 在同一段音频上输出
   「存在的價值是給別人創造價值的那和豬眷裡養肥了再殺的豬」。逐字稿里混着繁简会让检索、引用与去重全部失效。
2. whisper 的中文标点极不均匀：28 秒长段内部带逗号，而大量 1–2 秒短段完全没有标点，
   成稿于是变成一长串没有句读的文本。段间静音长度是唯一可靠的句读线索。

**取舍**：没有引入标点恢复模型（FunASR 的 ct-punc 等）。它们会带来 torch 级别的依赖，
与"Linux 档案不装 torch、安装器不碰 CUDA"的既有决策冲突；而停顿启发式并不完美
（唱歌、快速换气、朗读停顿都会被误判为句末）。因此它**只影响可读渲染**，
不影响落盘分段、SRT 时间轴与覆盖率计算。

**验证方式**：`tests/test_normalize_transcript.py` 覆盖注入转换器生效、
缺 OpenCC 时 `auto` 降级只记录 / `on` 显式报错、`off` 不做转换、
停顿标点只在边界插入且段内文字原样保留、已有标点不重复添加、归一模式进缓存身份。

**重新评估触发条件**：如果标点质量成为笔记可读性的主要瓶颈，
可以引入本地标点模型，但它必须走可选依赖组并在缺失时降级——本决策的"可降级"约束不变。

---

## D23. WSL 上"设备可见"不等于"GPU 可用"：运行时库必须被发现并预加载

**决策**：
1. 新增 `scripts/cuda_runtime.py`：在 `nvidia/*/lib`（Windows 为 `nvidia/*/bin`）里发现 CUDA 运行时库，
   用 `ctypes.CDLL(..., RTLD_GLOBAL)` **按依赖顺序预加载**（cudart → cublasLt → cublas → cudnn），
   并把目录写进 `LD_LIBRARY_PATH` 供子进程使用。ASR 入口在 `import ctranslate2` **之前**调用它。
2. 新增可选依赖组 `gpu-cuda12`（`nvidia-cublas-cu12` / `nvidia-cudnn-cu12` / `nvidia-cuda-runtime-cu12`）。
   安装器仍然不装 CUDA、不改驱动、不调 sudo——这三个是 pip 轮子，不是驱动。
3. `doctor status` 从"报告可见性"改成"报告可用性"：同时给出 `devices`、`runtime_libraries`、`usable`
   和 `guidance`（不可用时直接带上确切的安装命令）。

**背景（真实排查过程，完整记录以免重来）**：在 WSL2（`6.18.26.1-microsoft-standard-WSL2`）上，
`/dev/dxg` 存在、`/usr/lib/wsl/lib/libcuda.so.1` 在加载器路径里、`ctranslate2.get_cuda_device_count()` 返回 **1**；
Linux 侧没有 `nvidia-smi`（只有 Windows 侧的 `/mnt/c/Windows/System32/nvidia-smi.exe`）。
看起来一切正常，但转写会在**第一次 `encode()`** 抛：

```text
RuntimeError: Library libcublas.so.12 is not found or cannot be loaded
```

根因：**WSL 驱动只提供 `libcuda.so`（驱动 API），不含 cuBLAS / cuDNN（计算库）**。
而旧代码与 `doctor` 都只看设备枚举，于是把"看得见"当成了"能用"。
更麻烦的是失败点在第一次推理而不是模型构造，所以只包住 `WhisperModel(...)` 的 try/except 抓不到它（D19 记录了这一点）。

**两个必须踩对的实现细节**：
1. **改 `os.environ["LD_LIBRARY_PATH"]` 对当前进程无效**——glibc 只在进程启动时读一次。
   真正管用的是 `ctypes.CDLL(绝对路径, RTLD_GLOBAL)` 预加载；环境变量只对子进程有意义。两者都做。
2. **加载顺序有依赖**：cublasLt 依赖 cublas，cudnn 依赖 cudart/cublas。顺序错了会得到
   "cannot open shared object file"，看起来像"没装"，实际是顺序问题。

**取舍**：三个 nvidia 轮子约 700MB，因此作为**可选**依赖，CPU 用户完全不需要；
`usable` 也**不参与 `doctor` 的 `ok` 判定**——没有 GPU 仍能用 CPU 跑完，只是慢，
把可选能力算进整体健康会让"能跑"被误报成"坏了"。

**验证方式**：
- `tests/test_cuda_runtime.py`：从 `sys.prefix` 推导目录、四个库都能发现、缺库时给出可执行建议、
  找到但加载失败要报错且不断链、`LD_LIBRARY_PATH` 幂等追加、可用性三态（无设备 / 设备可见但缺库 / 可用）、
  **ASR 入口的调用顺序必须是 prepare → choose_device → model**、`doctor` 报告 `usable` 与 `guidance`。
- 真实端到端（本机 WSL2 + RTX 5070 Laptop，命令用 `env -u LD_LIBRARY_PATH` 显式清掉手设变量）：
  装库前 `device_count=1` 但 `usable=false`，建议里给出 `pip install -e ".[gpu-cuda12]"`；
  装库后四个库按依赖顺序预加载、`usable=true`；ASR 实际跑在 `cuda/float16`
  （`device_fallback` 为空、`cuda_runtime.preloaded` 为 4），259.77 秒音频用 `small` 模型 60.5 秒完成（≈4.3× 实时）。

**重新评估触发条件**：如果 WSL 驱动开始自带 cuBLAS/cuDNN，或 ctranslate2 提供官方设备自检 API，
可删掉预加载逻辑只保留 `doctor` 的可用性判断。

---

## 决策索引

| 编号 | 主题 | 是否可推翻 |
|---|---|---|
| D1 | 解释器解析用 `sys.executable` | 可（需重新设计解析层） |
| D2 | FFmpeg 两段式回退 | 可 |
| D3 | 默认匿名 + 显式 Cookie 授权 | **不可**（安全底线） |
| D4 | Linux 运行时隔离于 Skill 树 | 可（需先解决 Windows 定位） |
| D5 | 版本号四处一致 + CI 强制 | 可（若改用 setuptools-scm） |
| D6 | yt-dlp 为主下载路径 | 可 |
| D7 | 原子写入 | **不可**（数据安全底线） |
| D8 | faster-whisper 主 + PyTorch 回退 | 可 |
| D9 | CI 不跑 GPU 路径 | 可（若回归频发） |
| D10 | 无 Node 依赖 | 可（若引入 JS 构建） |
| D11 | CI 冒烟只断言确定性契约 | 可（若 CI 装齐工具） |
| D12 | 笔记骨架外置 + 降级不失败 | 可（若需整篇模板） |
| D13 | 依赖下限必须写明原因 | 可（若改用带注释的锁文件工具） |
| D14 | 三宿主 interface 清单一致 | 可（若宿主改读 SKILL.md） |
| D15 | CI action 按各自 tag 策略引用 | 可（若改用 commit SHA） |
| D16 | 静默丢掉的多秒语音必须被运行时校验发现 | 可（若上游修掉解码层丢失） |
| D17 | 缓存身份含转写参数与引擎版本 | 可（若改为整体哈希） |
| D18 | 退出码区分故障类别 | 可（若宿主按码自动重试） |
| D19 | 设备可用性由真实解码证明 | 可（若上游提供自检 API） |
| D20 | 分段只有一个形状 + 保真度显式 | 可（若接入平台字幕来源字段） |
| D21 | 多源交叉校验代替信任单一模型 | 可（若接入第三个来源） |
| D22 | 文本归一化显式且可降级 | 可（若引入本地标点模型） |
| D23 | WSL 上"可见"≠"可用"，运行时库预加载 | 可（若驱动自带或上游提供自检） |
