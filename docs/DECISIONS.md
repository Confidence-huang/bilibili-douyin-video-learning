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

**补充：公开分享页才是更脆弱的一环（真实故障形态，2026-09）**。反复用同一 IP 请求抖音公开视频之后，
分享页出现"页面能取到、但不再暴露 play token"的形态——链路停在 `ssr_pipeline`，而 yt-dlp 兜底又要求新鲜 Cookie，
于是两条公开路径同时失败。**这不是链接错误**：命令返回退出码 `20`（取流不可用）并带完整诊断链，
含义是"稍后重试或换下载方式"。同期 B站完全不受影响，因此不能据此判断工具坏了。
实践建议：优先复用缓存结果，并拉开重复运行的间隔。

**重新评估触发条件**：如果 yt-dlp 对某平台的公开视频支持出现回归，或平台开始提供稳定的免签名取流接口。

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

**B站侧同样补上（真实测试发现）**：`transcribe_bilibili.py` 与 `download_audio.py` 此前在失败时**仍然返回 0**，
调用方只能靠解析 JSON 判断成败。现在两者都在结果里写 `exit_code`（取流失败 20、本机 ASR 失败 24）
并由 CLI 返回之；用不存在的 BV 实测得到进程退出码 `20`，`status: error`、`exit_code: 20` 与 JSON 一致。

**同一天的第二个真实案例**：抖音公开分享页在反复请求后停止暴露 play token，SSR 与 yt-dlp 双双失败；
进程退出码 `20`、JSON `exit_code` 同为 `20`，诊断链逐级可见（`get_ttwid` → `parse_input` →
`resolve_short_url` → `fetch_share_page` 成功 → `ssr_pipeline` 失败）。这正是本决策要达到的效果：
Agent 得到的是"稍后重试或换下载方式"，而不是一个无法解释的 `1`；同期 B站用同一份代码正常运行。

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

## D24. 硬字幕是一条独立来源，但必须以"可能漏采"为前提使用

**决策**：新增 `scripts/hard_subtitle.py` 与可选依赖组 `hard-subtitle`（`rapidocr-onnxruntime`）：
按可配采样率（默认 4 fps）抽取字幕带、**只在像素变化点做 OCR**、连续相同文本合并为卡片、
可选校正表修正 OCR 稳定错字、可选 `--asr-timeline` 做字符级完整性自检，
并输出采样帧数 / 变更点数 / 卡片数 / 覆盖率 / **短于 0.5 秒的卡片数**作为"我可能漏了"的自检。
ffmpeg、OCR 引擎与抽帧调用全部可注入，因此没有引擎、没有 ffmpeg、没有真实视频也能跑完全部逻辑。

**背景（真实数据）**：这条 259.77 秒视频里，硬字幕纠正了 ASR 的 14 处错误
（`雪包→血包`、`只要/直到`、`断亲→断气`、`25岁→45岁`、`一不避体→衣不蔽体`、`经营→经济`…），
它确实比 ASR 更权威。但同一份素材也暴露了两个陷阱：
用 **2 fps** 采样时漏掉了只闪 0.4 秒的「更高级」与「病了」，前者直接改变句意
（"更高级、更耐用、更能持续供血的血包" → "更耐用…"）；
OCR 还会稳定认错特定字（`赡→赠`、`白→自`、`干瘪→干`）。这三件事共同决定了本模块的形状。

**取舍**：
- **默认 4 fps，并把"短卡片计数"作为一等输出**，而不是承诺"提取完整"：
  采样类方法无法自证完整，诚实的做法是把风险变成可读的数字；
- OCR 作为可选依赖（带 onnxruntime 与约 15MB 模型），缺引擎时抛出的错误必须直接给出
  `pip install -e ".[hard-subtitle]"`；
- **只接受本地视频文件，不做网络取流**：取流已有专门模块与风控策略（D6/D18），
  混在一起会让两边都难维护；
- 不做"谁对谁错"的自动判断——那属于 D21 的交叉校验与人工判断，本模块只负责产出一条可复核的来源。

**验证方式**：`tests/test_hard_subtitle.py` 12 个离线用例覆盖：
变更检测合并、**只出现 1 帧的短卡片不丢**（真实回归）、内置与自定义校正表生效、
完整性报告（短卡片计数与覆盖率）、交叉校验报出"字幕缺一句"、
`--emit` 产出合法 SRT 且折行不丢字、缺 OCR 引擎时给出可执行建议、未知 `--emit` 值非零退出、
字幕带按比例与像素解析、视频元数据探测、抽帧调用可注入、整条管道可注入跑通。

**重新评估触发条件**：如果将来接入真正的字幕轨道（平台 CC 或作者上传的 SRT），
本模块应降级为"没有字幕轨道时的兜底"，而不是默认路径。

---

## D25. 度量必须先于优化：金标只存文本，指标口径按"人工校对成本"定义

**决策**：新增 `eval/gold/*.json` 金标集与 `scripts/eval_asr.py` 评测器，把转写质量固定成五个可复现数字：
**CER、幻觉率、覆盖率、时间轴偏移、RTF**。金标**只存文本与时间轴**，媒体用 `media.url` 复现。

**背景**：此前所有关于准确率的结论都来自临时人工核对——知道"雪包应该是血包"，
却回答不了"这次改动让错误少了几个"。没有度量，调 beam、加词典、换模型档位全是盲改；
更危险的是，指标定义错了会把改进报成退步（本轮就发生过一次，见下）。

**指标口径（都按人工校对成本定义，而不是按模型指标好看）**：
- **CER** = (替换 + 漏字 + 多字) / 参考字数。中文以字为单位比 WER 更贴近校对成本；
  **多字也计入分子**，否则"多说话"会被奖励。
- **幻觉率** = 参考里完全不存在的新增片段（≥2 字）字数 / 分钟，与"漏字"分开计。
- **覆盖率** = 假设时间轴并集（按参考时长截断）/ 参考时长；重叠只算一次。
- **时间轴偏移** = 逐段最长公共块对齐后的 `|假设时间 − 金标时间|` 中位数与最大值。
- **RTF** = 耗时 / 音频时长。

**真实踩到的方向陷阱（已写进代码注释与 eval/README）**：`difflib.SequenceMatcher(a=假设, b=参考)` 的
opcode 语义与直觉相反——`insert` 是"参考有、假设缺"=**漏字**，`delete` 才是"假设多出来"=**多字**。
第一版实现按直觉解读，于是把**金标里有、ASR 漏掉**的整段文字报成了"幻觉样例"。
修正后同一份真实数据的结论完全变了：漏字 44 字 / 多字 1 字 / **幻觉 0.0 字每分钟**——
这与事实相符（该配置的病是丢字与同音替换，不是编造）。**指标错了比没有指标更糟。**

**验收线**：逐字稿场景 CER ≤ 10%（`DEFAULT_CER_BUDGET`）。真实基线（抖音 259.77s、真实 ASR fixture）：
CER **0.0402**（32 替换 / 44 漏字 / 1 多字）、幻觉 **0.0 字/分钟**、覆盖率 **0.9759**、
时间轴偏移中位 **0.26s**（匹配 41/44 段）、RTF 0.233。覆盖率与历史记录的 0.9760 吻合，
说明评测器本身可被交叉验证。

**验证方式**：`tests/test_eval_asr.py` 覆盖三个编辑方向的计数、规范化的标点/全角/繁简处理、
幻觉的最短片段门槛（单字多出不算幻觉但仍由 CER 罚）、覆盖率的重叠去重与时长截断、
"内容相同但整体后移 2 秒"能被正确报出、未被转写的参考段不计入匹配、字符时间映射单调、
四种历史产出格式（规范 JSON / ASR JSON / SRT / TXT）都能吃、批量按 id 匹配、
以及**金标集非空且只含文本**（禁止媒体入库）。

**重新评估触发条件**：如果将来接入人工逐字稿标注或多语种，需要为每种语言分别设定 CER 验收线
（中文按字、英文按词），并把金标规模扩到能覆盖不同音质与题材。

---

## D26. 质量开关必须逐个消融验证：音频前端保留，领域词表默认关闭

**决策**：新增三样东西，但**默认值由实测决定**，而不是由"听起来应该有用"决定：
1. **领域词表（hotwords）机制**：内置 `references/asr-lexicon.txt` + `--lexicon` + `--hotwords` + 视频元数据自动抽词；
   **默认关闭**（`hotwords=""`），需要时显式启用。
2. **音频前端**：`highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11`，**默认开启**（`normalize_audio=True`）。
3. **三个档位**（按实测目标命名，不再用 fast/balanced/quality 这种含糊词）：
   `balanced`＝beam1+无词级时间戳（实测最低 CER）、`timing`＝开词级时间戳（换对齐能力）、
   `quality`＝beam5+定向重解（追覆盖率）。任何档位都**不关**覆盖率安全网。

**真实实测（抖音 259.77s 音频 + 1917 字金标，`small`/cuda/float16，单变量消融）**：

| 配置 | CER | 替换 | 漏字 | 多字 | 覆盖率 | RTF |
|---|---|---|---|---|---|---|
| 未净化 + beam1 + wt=off（原默认） | 0.0522 | 82 | 17 | 0 | 0.9956 | 0.047 |
| **净化 + beam1 + wt=off** | **0.0433** | 70 | 11 | 0 | 0.9980 | 0.040 |
| 净化 + beam5 + wt=off | 0.0480 | 80 | 8 | 4 | **0.9992** | 0.066 |
| 净化 + beam1 + wt=on | 0.0511 | 84 | 14 | 0 | 0.9976 | 0.060 |
| 未净化 + beam1 + wt=on | 0.0464 | 77 | 10 | 0 | 0.9980 | 0.042 |
| 净化 + beam1 + wt=on + 词表 | **0.0751** | 85 | 56 | 3 | 0.9979 | 0.040 |
| 未净化 + beam1 + wt=off + 词表 | 0.0558 | 84 | 22 | 0 | 0.9916 | 0.050 |
| 未净化 + beam5 + wt=on | 0.0616 | 76 | 41 | 0 | 0.9979 | 0.055 |

**四条反直觉结论（这是本决策的主要价值）**：
1. **音频前端是唯一明确的准确率收益**：CER 0.0522 → **0.0433**（相对 −17%），替换 82→70、漏字 17→11，
   且几乎不增加耗时（RTF 0.047→0.040）。→ 默认开启。
2. **领域词表在这支视频上有害**：干净配置下 0.0433 → **0.0751**（漏字 11→56）。
   词表偏置会扰动整体解码，收益依素材而定，**不能默认开启**。→ 默认关闭，并明确"用 eval 逐视频验证"。
3. **词级时间戳不是 CER 收益**：未净化时看着有效（0.0522→0.0464），净化后反而变差（0.0433→0.0511）。
   说明"wt 更准"是噪声/交互，它的真实价值是**能力**（重新分段、帧对齐，见 D28）。→ 单独成 `timing` 档。
4. **beam5 与词级时间戳组合有害**：漏字 17→41、CER 0.0616。**两个"看起来都该开"的开关不能一起开。**

**方法论教训**：第一轮做的是**组合实验**（A 基线／B 加词表／C 加词表+档位／D 再叠加前端），
结论是"全都变差、优化无效"——完全错误。改成**单变量消融**后才看清：前端有效、词表有害、
wt 与 beam 有交互。**组合实验会把方向相反的效应互相抵消，从而给出假结论**；这也再次说明 D25 的度量必须先落地。

**取舍与边界**：词表只做解码偏置、**从不改写输出文本**（不会掩盖错误）；档位在参数构造的最后一步应用，
保证覆盖关系确定；词表与档位都进入缓存身份（D17），换配置必然重新转写。

**验证方式**：`tests/test_asr_quality.py` 14 个离线用例覆盖词表解析/优先级/截断、元数据抽词与噪声词过滤、
前端滤镜链开关语义（关闭时必须与历史行为一致，返回空串）、档位映射与未知档位报错、
缓存身份随参数变化、以及 **`hotwords`/`word_timestamps` 真的传到了引擎**（伪造引擎捕获 kwargs）、
未开启时不写入 `words`、空词表传 `None` 而不是空串。真实数字见上表，可用
`scripts/eval_asr.py` 与金标一键复现。

**重新评估触发条件**：换素材类型（音乐重的口播、多人对话、方言、英文）后必须重跑评测再改默认值；
如果平台或模型升级（faster-whisper 新版本、large-v3-turbo），`wt` 与 `beam` 的交互可能反转，同样要重跑。

---

## D28. "没有音轨"必须与"转写失败"分开：图文作品要改走图片 OCR，而不是重试

**决策**：在抽取音频**之前**检测音轨（`media_tools.has_audio_stream`，解析 `ffmpeg -i` 的 stderr，
不引入 ffprobe 依赖）；没有音轨时抛 `NoAudioTrackError` 并走退出码 **27**
（`EXIT_NO_AUDIO_TRACK`）。抖音入口与通用转写入口（`transcribe_audio_cli.py`）都遵守这一契约。

**背景**：抖音除视频外还有**图文作品**（多张图片 + 文案，没有音轨），另有纯音乐/静音卡片。
对它们，"取流成功 → 抽音频 → ASR 产出空文本"这条链路最容易被误判成 ASR 故障：
调用方看到空结果会去重试、换模型、换机器——**全都无效**，因为作品本来就没有语音。
退出码 27 的含义因此是"换什么都解决不了，请改走图片 OCR"，
与 24（本机 ASR 失败，换机器/模型可解）在语义上完全不同。

**取舍**：
- 用 `ffmpeg -i` 的 stderr 解析而不是 ffprobe：本仓已有 ffmpeg 但**从不依赖 ffprobe**（历史约束）；
- 探测失败按"没有音轨"处理：宁可给出明确失败，也不要产出一份空的逐字稿（空产出比失败更误导）；
- 检测放在抽取**之前**：省掉一次无意义的 ffmpeg 转换，也让错误信息指向真正的下一步。

**验证方式**：`tests/test_no_audio.py` 用 **ffmpeg 现场生成**有/无音轨的真实文件（`testsrc` 与 `sine`），
而不是打桩：无音轨文件被识别为无音轨（并确认诊断文本里没有 `Audio:`）、有音轨文件被识别为有音轨
（防止"一律返回 False"的假实现）、探测不存在的路径按无音轨处理、
`douyin_extract.extract_audio` 在无音轨上抛 `NoAudioTrackError` 且报错含"图片 OCR"、
有音轨时真的产出 `16000 Hz` 单声道 WAV、`transcribe_audio_cli` 返回 27 并输出可解析 JSON 错误、
以及全部退出码互不重复。

**未完成的部分（诚实记录）**：图文作品的**图片 OCR 路径**还没接成 CLI。现有素材是文本、
不含真实图片，无法在没有真实图文链接的情况下验证 OCR 识别率，因此这一步留到有真实素材时再做；
本决策只保证"不再把图文作品误判为 ASR 故障"。

**重新评估触发条件**：如果抖音开始为图文作品提供文字版文案接口，应优先取文案而不是 OCR。

---

## D29. 多源融合：产出一份逐字稿并逐段注明出处，而不是两份报告

**决策**：`verify_transcript.py` 从"只报告差异"升级为**可融合**：`fuse_transcripts(primary, secondary)`
按字符级对齐逐段决定取谁，并给每段打 `provenance`；B站入口 `transcribe_bilibili.py` 变成
**字幕优先的单一入口**（`--prefer-subtitles` 默认开、`--fuse` 默认关）。

**provenance 三态语义**：
- 两侧一致 → `subtitle`（主源），完全一致时 `needs_review=0`；
- 只有次源有 → `asr`（**这正是"平台字幕/硬字幕漏掉一句"的场景**，用 ASR 补上）；
- 两侧写法不同 → `mixed`（保留主源文本 + `alternatives` + `needs_review=true`）。

**硬不变量**：任一份来源出现过的有效字符都必须出现在融合稿的正文或 `alternatives` 里，否则抛 `ValueError`
而不是静默产出；另有一次完整性兜底，把字符级对齐漏掉的次源段落原样补成 `recovered_by="coverage_audit"`。
**逐段决定、绝不切半张卡片**：opcode 片段先按 `(来源, 段落下标, 时间窗)` 归组，再整段取回原文。

**真实数据验证（抖音）**：主源＝217 条硬字幕卡片，次源＝180 段真实 ASR：
相似度 **0.9718**、差异 **30** 条（replace 25 / delete 4 / insert 1）、融合稿 **246** 段、
`provenance_counts = {subtitle 87, asr 3, mixed 156}`、**补回 3 段**（其中 2 段由完整性兜底救回），
例如 `68.16–69.66s 六年前他死于一场脑溢血`、`173.32–175.64s 我必须源源不断地给身边的人创造价值`。

**B站的真实结论（与预期相反，必须记录）**：我原以为"B站很多视频有字幕轨"，
但**匿名请求下拿不到**——`BV1xx411c7mD` 的 `fetch_with_subtitles` 与直接 `player/v2` API（cid=62131）
都返回 `subtitles: []`（该视频真实标题是「字幕君交流场所」，并非字幕素材）；
另抽查 50 个热门公开视频，**无一带字幕轨**。所以"字幕优先跳过 ASR"目前**没有真实网络证据**，
只有离线测试 + 用真实抖音卡片伪造轨道在 `BV1ntah6TEe9` 上的实测（`source=subtitle`、`asr_calls=0`、
未下载音频、0.0s，对比该视频真实 ASR 5.1s、`--fuse` 8.05s）。
需要登录 Cookie 才可能看到字幕轨，而那是 D3 明确要求单独授权的能力。

**行为变化（需要调用方注意）**：B站返回的 `segments` 从 `from/to/content` 改为规范
`start/end/text/provenance`，与抖音链路一致。仓库内所有消费方（测试、CLI 摘要、`fuse_transcripts`）
都已适配，但**仓库外直接读 `seg['from']` 的脚本需要同步**。

**取舍**：`needs_review_count` 在真实素材上是 159/246，偏高——因为 ASR 次源是 30 秒长段，
与短卡片逐字符对齐时"部分覆盖"很常见，单字 `alternatives`（`2`/`在`/`得`）还混有少量跨段巧合。
**要看清同音字冲突请用 `spans`（30 条），`needs_review_count` 是"需要人扫一眼的段数"**，
两者不要混用。次源与某段只重合 1 个字不算覆盖（`MIN_SHARED_CHARS`），以压掉这类噪声。

**验证方式**：`tests/test_fusion.py` 15 个离线用例覆盖完全一致、次源补段、主源保留、
replace 记录 alternatives 与 needs_review、**并集不变量**、provenance 计数、
B站字幕优先跳过 ASR / 无字幕回退 ASR / `--fuse` 三态。

**重新评估触发条件**：如果拿到登录态并确认真实字幕轨可用，"字幕优先"应从"能力"升级为"默认推荐路径"；
如果平台改为只提供 AI 字幕，则需要把 AI 字幕降级为"第三来源"参与融合而不是直接采信。

---

## D30. 词级重新分段：不丢词是硬不变量，宁可不切也不猜时间

**决策**：`normalize_transcript.resegment_by_words()` 只在**词边界**切分长段——累计字数达 `max_chars`（默认 28）、
词间停顿 ≥ `sentence_gap_seconds`（默认 0.6s）、或词尾出现强标点（`。！？!?`）；
`normalize_segments` 负责把 `words` 带下去而不是丢掉；`to_srt` 的 `max_line_chars` 变成硬约束（超出就多给行）。

**硬不变量**：输出所有文本拼接（去空白后）必须与输入拼接**逐字一致**；时间单调不减、段间不重叠。
输出文本用**原 content 的连续字符切片**（把词映射成字符区间），而不是拼接词文本，因此正文可逐字回溯。

**降级优先于硬切**：没有 `words`、词文本与正文对不上、词时间轴回退——这三种情况**整段原样保留**，
绝不用字符比例猜时间、绝不产出重叠字幕。残余偏长由 `cues_over_limit` 如实计数。

**背景**：whisper 的分段跟随它自己的 VAD，实测既有 **27.98 秒的长段**（内部还带逗号），
也有大量 1–2 秒的无标点短段，成稿因此没有句读、SRT 一行过长、与硬字幕卡片和抽帧帧对不齐。

**真实数据的诚实结论**：在现有词级时间戳运行上（large-v3 实测最长段 **5.24s**、
`small` 最长 2.62s，整份产出强标点与逗号各 0 个，词间最大空隙 0.26s），
默认参数下是 **no-op（192→192 段）**；参数扫描证明"切更细也不丢字"：
28 字→192 段、12 字→229 段、8 字→303 段，`dropped_chars` 全程 **0**，SRT 时间戳全部合法、每行 ≤18 字。
所以这个能力的收益**要在分段真的粗的素材上才会显现**（例如仓库 fixture 里那个 27.98 秒首段），
它现在的正确性由 12 个离线用例与不变量守住，而不是靠"看起来更整齐"。

**重新评估触发条件**：如果换用会产出长段与真实词间静音的引擎/档位（例如带标点恢复的模型），
停顿与标点规则将开始触发，届时应重跑同一批真实数据比较 before/after。

---

## D31. 浏览器取流：模仿已验证的思路自己做一份，且不与另一个仓库耦合

**决策**：新增 `scripts/douyin_browser_fetch.py`——**自研**的浏览器取流通道（极简 WebSocket 客户端 +
CDP 会话 + 页面内 `fetch`），并通过 `douyin_extract --download-method browser`（`auto` 时优先）接入。
它只**参考**已有插件的思路（真实浏览器上下文里发请求，签名由站点自己的 JS 算），
**不调用它的服务、不复制它的代码、不把它作为依赖**。桥接（BrowserSkill 同理）属于别人/别的仓库。

**背景**：抖音匿名 SSR 路径会被风控降级（实测 12 个 host×UA 组合全部返回「验证码中间页」，
`videoInfoRes` 只剩 `status_code`）；匿名调 Web API 是 403
`Blocked by ArgusSecurityPlugin Uifid Not Found`，因为 Web API 要求
`a_bogus + timestamp + x-secsdk-web-signature`，而它们只能由站点前端 JS 生成。
"在真浏览器里发这个请求"是唯一被验证过可行的思路，因此把它**内化成本仓库自己的实现**。

**实测验证层级（三个都做了，证据分明）**：
1. **传输层（真机、真浏览器）**：从 WSL 启动 Windows Chrome（专用 profile + `--remote-debugging-port`），
   自研 WS 握手 → `Target.createTarget` → `Page.navigate` → `Runtime.evaluate(awaitPromise)` →
   页面内 `fetch('https://www.douyin.com/robots.txt')` **返回 200**。协议栈完全自证。
2. **登录态 API（真机、真账号）**：用已登录 profile 调 `/aweme/v1/web/aweme/listcollection/`，
   **返回真实 `aweme_list`**（含 `authentication_token` 等字段）→ 说明"借真浏览器拿登录态"这条路成立。
3. **协议层离线**：CI 里没有浏览器，因此 `tests/test_browser_fetch.py` 用**真 socket** 起极简 WS 服务端
   验证握手与帧编解码，用假 ws 验证 CDP 的 id 关联与错误上抛，纯函数测解析/选档/元数据/失败分类。

**途中发现的两件事**：
- **那支 259.77 秒的视频已不可取**：登录态详情接口返回
  `"aweme_detail": null, "filter_detail": {"filter_reason": "status_self_see", "detail_msg": "因作品权限或已被删除"}`——
  作者把它改成了**仅自己可见**。这解释了当天匿名路径的"降级"表象：不只是风控，内容本身也变了。
- **`DETAIL_PARAMS` 必须带全 8 项**（`cookie_enabled`/`browser_language`/`browser_platform`/`browser_name` 等）：
  只带 4 项时接口会返回 `status_code=0` 但 `aweme_detail` 为空——这是"看起来成功其实没数据"的典型陷阱。

**取舍与边界**：
- 用专用登录态目录（默认 `~/.cache/douyin-browser-profile`，`--browser-profile` 可指到已登录 profile），
  **不碰用户主浏览器配置**；首次需在该目录登录一次抖音。
- `base64` 回传上限 120MB，超限明确报错而不是 OOM；不做验证码识别、不绕过登录墙，命中风控如实失败。
- 本机 CDP 的 HTTP 元信息端点**必须绕过代理**（环境里有 `http_proxy` 时 urllib 会把 127.0.0.1 也发给代理）。
- 只依赖标准库（自研 WS + CDP），不引入 playwright/selenium 之流。

**重新评估触发条件**：如果平台开始提供稳定的免签名公开接口，本通道应降级为可选；
如果 Chrome 收紧 `--remote-debugging-port` 的本地策略，则改用管道模式或让用户手动启动实例。

---

## D32. 本地视频文件是一等输入：抖音取流不可靠时的稳定路径

**决策**：`douyin_extract.py` 新增 `--video <本地文件>`，跳过一切取流，直接
抽音频 → ASR → 清洗 → 产出（`md/json/srt/txt`）。缓存身份用**路径+大小+mtime**，
`download_method` 记为 `local`，并写一条 `local_video` 诊断。

**背景**：抖音的公开取流会被限流/风控（D31 记录了实测），而用户手上常常已经有视频文件
（自己下载、或由插件同步时落盘）。过去 CLI 只接受 URL，导致"有文件也走不了管线"。

**真实数据验证**：用真实 259.77 秒 mp4 跑
`douyin_extract.py --video <file> --model small --json` →
**122 段 / 1991 字 / 覆盖率 0.9997 / `cuda/float16` / `download_method=local` / 退出码 0**，
首条诊断为"使用本地视频文件，跳过取流"。

**取舍**：本地文件的元数据只有文件名（没有作者/点赞等），因为那些本来就不在文件里；
不伪造缺失字段，`extractor` 标为 `LocalFile` 让下游能分辨来源。

**重新评估触发条件**：如果取流恢复稳定，`--video` 仍应保留——它是离线/隐私场景的首选。

---

## D33. 金标半自动化：初稿 + 分歧清单，人工只核对被标记的行

**决策**：新增 `scripts/eval_gold_draft.py`——把 ASR 初稿变成「金标草稿 + 核对清单」；
同时修掉评测口径里把**数字写法差异**算成识别错误的偏差（中文数字 → 阿拉伯数字归一）。
金标草稿写到 `eval/gold/<id>.draft.json`，清单写到 `eval/worksheets/<id>.md`。

**为什么（人工成本不可压缩，但可以缩小）**：金标是"改动是否更好"的唯一依据，却卡在逐句核对上
（一支 5 分钟视频 15–30 分钟）。人工真正需要看的只有三类位置：**低置信段**、**高压缩比段**（复读/幻觉嫌疑）、
**两个解码配置给出不同文本的段**。工具把这三类标出来，人工只核对被标记的行。

**两条实测教训（都由真实数据暴露）**：
1. **清单必须有优先级与上限**：第一版在 B站 素材（`BV1ntah6TEe9`，large 与 small 相似度仅 **0.8632**）上
   把 **15/15 段全部标出**——等于没有优先级，人还是会整体忽略。现在按
   `复读嫌疑 > 低置信 > 实质分歧 > 非实质分歧` 排序，差异只有单字的不算"实质"，并按段落数的 30%（至少 8 条）截断，
   同时如实报告 `flagged_total`（被截断的仍保留在产物里，不隐藏）。同一素材现在是 **8 条**上榜、7 条截断。
2. **数字写法不是识别错误**：抖音金标上默认口径与严格口径的差异正好是
   `六/6`、`三/3`、`一百/100` —— CER 从 **0.0214 降到 0.0187**（41 → 36 错）。
   因此默认把中文数字与阿拉伯数字视为等价（`--keep-numerals` 可回到严格口径），
   并在报告里显式写出归一化口径，避免"数字好看"被误读。

**产物边界**：`reference.kind = "semi-automatic-draft"`，并带 `review` 块（标记了什么、为什么、还差哪些步骤）。
评测脚本照旧能读它（用于 A/B 比较），但**只有人工核对后把 `kind` 改成 `human-verified`，它才能当验收基准**。
工具**不做任何自动改写**：谁对谁错依赖上下文，它只负责把该看的地方摆全。

**验证方式**：`tests/test_eval_gold_draft.py` 覆盖三种可疑形态、全片可信时退回"最低十分位"、
区间首尾相接也算重叠、单字差异不算实质、排序与上限（复读嫌疑排最前）、
草稿必须自称 draft、无第二配置时可降级、清单含 ⚠/分歧表/完成步骤、CLI 落盘与错误码；
`tests/test_eval_asr.py` 覆盖数字归一（`六→6`、`一百→100`、`四十五岁→45岁`）与严格口径开关。

**重新评估触发条件**：如果将来接入人工标注界面，本工具的产物就是它的输入格式；
如果引入第三个解码配置，分歧判据应从"两两比对"升级为"多数一致"。

---

## D34. 补转结果是唯一能"无中生有"的入口，必须过幻觉门；可疑内容只标注不改写

**决策**：新增 `scripts/asr_hallucination.py`，做两件事：
1. **补转窗口过门**：覆盖率兜底切出来的窗口在关闭两道静音阈值后解码（D16 的既定行为），
   其文本必须先过三道门才允许并入正文——**压缩比 >2.4**、**同一 4 字片段重复 ≥3 次**、
   **时长-字数比异常**（中文口播约 4–6 字/秒；>8 或 <0.5 都不可信）。被拒窗口**只记诊断**，
   结果里出现 `asr_hallucination_gate`，正文保持第一遍内容。
2. **整段标注**：`suspected_hallucinations` 列出"看起来像复读/幻觉"的段落（压缩比异常），
   并在诊断里写 `asr_hallucination_marks`——**只标注、绝不改写文本**。

**背景（这是当前风险最高的一处）**：D16 的补转为了"宁可拿到字"而传
`no_speech_threshold=None` 与 `logprob_threshold=None`，于是音乐段/噪声段也可能被解出文本，
而在此之前**结果没有任何校验就并入正文**。评测器能量化幻觉率（真实素材目前是 0.0 字/分钟），
但管线自己不会拦——数据干净时这个缺口不会被发现，这正是危险之处。

**取舍**：门是保守的——宁可把可疑内容以标注形式交给人工，也不静默并入；
被拒窗口不重试（同窗口换条件重试正是产生幻觉的路径）。`hallucination_gate` 与
`hallucination_compression_ratio` 都进缓存身份，换设置必然重新转写。

**验证方式**：`tests/test_asr_hallucination.py` 覆盖三道门各自的触发与不触发、
无窗口时长时跳过时长门、标注不改写输入、`_finalize` 暴露 `suspected_hallucinations` 与诊断、
干净音频零误报、以及新设置进入缓存身份。真实运行：B站 30.63 秒视频（`small`）得到
14 段 / 覆盖率 0.8501 / **幻觉标注 0 条**（干净素材不误报），字段确实出现在产出里。

**顺带补上的 CI 缺口**：这两个平台原本没装 ffmpeg，导致"无音轨检测/音频抽取"这类需要真实媒体的
用例被跳过——等于没有持续回归保护。现在 CI 依赖里加 `imageio-ffmpeg`（跨平台、无需 sudo），
`media_tools.find_ffmpeg()` 直接可用，那几条用例在 CI 里也真的运行。

**重新评估触发条件**：如果引入标点恢复或第二个 ASR 引擎，幻觉判据应扩展为"多配置一致性"；
如果真实素材上开始出现被门拒掉的**真实语音**（假阳性），阈值需要按素材重新标定。

---

## D35. 融合的第三判据：已验证的"错→对"裁决表；复核清单按差异规模排序

**决策**：`fuse_transcripts` 不再"永远信主源"，而是加一张 `references/conflict-preferences.txt`
（每行 `错误写法→正确写法`）作为**第三判据**；同时把复核清单从"全部冲突"改成**排序后的短清单**。

**为什么需要第三判据**：两类错误机制不同，靠"信谁"无法一概而论——
- 硬字幕是**作者原文**，在**同音字**上比 ASR 准（实测：`血包/雪包`、`供血/工学`、`断气/断亲`、
  `衣不蔽体/一不避体`、`娘俩/两俩`、`享福/想福`、`经济价值/经营价值` 共 11 处，方向都是"作者对"）；
- 但 OCR 会**形近字**认错（`赡/赠`、`白/自`），这类反而是 ASR 对。
所以用一张"人工确认过的错→对表"裁决：**哪一侧含正确写法就用哪一侧**，并记录 `chosen_by` 与命中原因。

**关键技术细节（第一版做错、被真实数据抓出来的）**：`difflib` 的 opcode 是**字符级**的——
`赠养→赡养` 这种两字表项在冲突片段的 `"赠"`/`"赡"` 上**匹配不到**。因此等长表项会被拆成
**逐位字符混淆**（`赠→赡`、`雪→血`），在 replace 时**位对位**比较两侧写了什么。
长度不等的表项才退回整串包含判断。

**复核清单**：真实素材上冲突 159 条（段级），直接全列等于没有清单。现在按**冲突区字符数**排序
（多字优先），只列**实质冲突**（多字差异，或单字差异但不是虚词——`的/得` 这类不计），
上限 30 条；全量 `spans` 仍然保留，短清单只回答"先看哪些"。实测：**159 → 21 条**，
榜单前几位正是 `从小到大/创造了`、`供血/工学`、`享福/想服`、`直到/只要`。

**取舍**：裁决表只收录**在 D21 交叉校验里被人工确认过**的条目，不做自动学习——
自动生成"错→对"表会把模型的错误固化下来。清单截断只影响"先看哪些"，不隐藏任何冲突。

**验证方式**：`tests/test_fusion.py` 新增 6 个用例覆盖逐位混淆拆分、双向裁决、都不命中的默认行为、
冲突规模计算、"实质冲突"忽略虚词、报告的排序短清单与全量 spans 并存、裁决命中留痕、
以及仓库自带裁决表可解析。真实数据验证：217 硬字幕卡片 × 180 段真实 ASR →
裁决表命中 **11 处**（方向全部正确）、实质冲突 **21 条**（此前全量 159）。

**重新评估触发条件**：如果引入第三来源（AI 字幕），裁决应升级为"多数一致"；
如果裁决表增长到几百条，需要按来源类型（OCR/同音）分表并给出各自的置信度。

---

## D36. 四件收尾：词表只留实测条目、诊断键统一、模型指纹进缓存、未引用脚本可查

**决策**（一次收尾，四件事互相独立但都属"把隐式约定变成显式规则"）：

**1. 领域词表收敛：删掉所有凭常识加的条目。** `references/asr-lexicon.txt` 现在只保留
**能对应真实错例**的 10 条（血包/雪包、供血/工学、经济价值/经营价值、衣不蔽体/一不避体、
断气/断亲、娘俩/两俩、享福/想福、干瘪、干瘪的皮囊、从小到大）。原先那 10 条"通用高频易错词"
（认知/情绪价值/底层逻辑/幸存者偏差…）**零实测支持**，而词表整体在实测中是**有害**的
（0.0433→0.0751，漏字 11→56，见 D26）——未经验证的词只会增加噪声。文件头写明收录标准：
**要加词请附上"哪支视频、错成什么"**。

**2. 诊断键统一。** ASR 引擎诊断原先只有 `engine`，而覆盖率/重解诊断用 `step`；
现在统一为 `step: "asr_engine"`（保留 `engine` 兼容既有调用方），调用方读一个键即可。

**3. 模型权重指纹进缓存身份。** `identity()` 增加 `model_fingerprint`（模型权重文件的
大小+mtime，按模型尺寸缓存，找不到时如实为 `None`）。此前缓存键只认**模型名**，
同一个 `large-v3` 换了 revision 后会错误复用旧缓存。只读元数据、不读内容，代价可忽略。

**4. 未引用脚本变成可见项。** `tools/validate_repository.py` 新增**非致命**报告：
扫描 `scripts/*.py`，按**模块名**（不是文件名——`from file_output import ...` 不会写 `.py`）
在仓库所有文本里查引用，列出"没有任何入口引用"的脚本。当前列出三个：
`bilibili_deep_archive.py`（由插件调用，属正常）、`export_anki.py`、`vault_synthesize.py`。
**故意不失败**：外部调用方（插件、用户脚本）无法被 CI 看到，硬失败会造成假警报。

**这次收尾也暴露了两处我自己引入的缺陷，都由测试/CI 抓住**：
- `speech_to_text.py` 里用 `os.environ` 但该文件**只导入过 `pathlib`**，缺 `import os` → 9 个用例失败；
- `validate_repository.py` 里新函数被追加在 `if __name__ == "__main__":` **之后** → `NameError`。

**重新评估触发条件**：词表若重新扩张，应同时给出评测证据（开/关词表的 CER 对比）；
如果出现外部调用方清单，未引用报告可升级为白名单式硬检查。

---

## D37. 退出码契约的镜像模块；未引用脚本改为显式白名单

**决策**：新增 `scripts/exit_contract.py`，让 `scripts/` 下的独立 CLI 与 harness 共用同一套退出码，
并把它做成**镜像 + 测试锁定**的结构；同时把"没有仓库内入口的脚本"从"长期挂警告"改为**显式声明**。

**为什么需要镜像，而不是直接 import**：契约的规范定义在
`agent-harness/cli_anything/video_learning/utils/exit_codes.py`，但独立脚本常被
`python scripts/xxx.py` 直接调用，此时 harness 未必装在解释器里——硬依赖会让脚本在
"只装了源码"的机器上直接崩。于是镜像模块**先尝试导入规范实现**（装了就用规范的），
**缺失时退回本地镜像**，并且显式暴露 `mirror_classify_failure` 供测试对比。

**测试锁定两层一致性**（只对齐数值不够，语义漂移同样有害）：
- `MIRROR_VALUES` 与规范常量**逐项相等**；
- `classify_failure` 与 `mirror_classify_failure` 在文档化用例上**分类结果相同**
  （超时→22、平台风控→26、无音轨→27、其它→1）。实测两者完全一致。

**顺带发现的既有重复**：`fetch_bilibili.py` 早已自带一份 `EXIT_*` 常量与分类函数。
**不改它**——改动的风险高于收益；改为**用测试锁定它与镜像数值一致**，漂移会立刻变红。

**未引用脚本改为白名单**：`tools/validate_repository.py` 的报告新增
`INTENTIONALLY_LIBRARY_ONLY`（`export_anki.py`、`vault_ingest.py`、`vault_synthesize.py`、
`bilibili_deep_archive.py`）。理由分两类：前三者**有意**只作为库给别的宿主调用，
`bilibili_deep_archive.py` 由**插件**调用、CI 看不到外部调用方。
同时在 `SKILL.md` 里显式写出深度归档脚本与库脚本的存在与定位——**让"有意保留"变成可审计的事实，
而不是无声的游离文件**。改动后报告项清零。

**验证方式**：`tests/test_exit_contract.py` 覆盖镜像数值一致、镜像与规范分类一致、
`fetch_bilibili` 常量与镜像一致、平台专属异常类型优先、独立 CLI 真的返回映射后的码
（并校验 JSON 里的 `exit_code` 与返回码一致）。真实调用：
`douyin_ssr.py` 在超时场景返回 **22** 且 JSON 带同名 `exit_code`。

**重新评估触发条件**：如果 harness 变成独立可分发的包且始终可用，镜像可退化为一次性迁移脚本；
如果出现更多"有意保留为库"的脚本，白名单应附上每条的理由（当前以注释形式给出）。

---

## D38. 长音频分块：`chunk_length` 终于接线，两条不变量必须守住

**决策**：新增 `scripts/asr_chunking.py`（规划 / 平移 / 去重，纯函数），并把
`TranscriptionSettings.chunk_length`（默认 **0 = 关闭**）真正接到转写路径：按块切片
（复用 `asr_coverage.cut_audio_window`，16k 单声道）→ 逐块转写（**子调用强制关闭分块**，防递归）
→ 按时移合并回一条时间轴。

**背景**：`chunk_length` 此前**全仓未被使用**，实测最长素材 259.77 秒，而 B站 课程视频动辄 30–90 分钟。
整段解码有三个已知风险：显存/内存随长度增长、单次失败要全部重来、解码器在超长上下文里更容易漂移。

**两条不变量**：
1. **时间轴单调且对齐原音频**：每块结果按块起点平移，段级与**词级**时间戳都要平移（否则词与段对不上）；
2. **重叠区不重复**：相邻块重叠（默认 2 秒）以免切在词中间丢字；合并时后一块重复的文本只保留前一块那份，
   并把被判重的段标记出来（不静默丢弃）。

**默认关闭是关键取舍**：`chunk_length=0` 时行为与引入分块前**完全一致**，并且即使有人直接调用分块函数，
未启用也只做一次整段解码。

**两个被测试逼出来的边界改进（第一版是错的）**：
- **尾部碎片块**：1800 秒 @600 秒块会多出 6 秒碎块，单独解一块要付一次模型初始化成本 → 现在尾部不足块长 1/4 时并入前一块；
- **非正块长**：`plan_chunks(duration, 0, …)` 曾因"最小值钳制"硬切成 5 秒块 → 现在明确返回"不分块"。

**验证方式**：`tests/test_asr_chunking.py` 覆盖短音频不分块、30 分钟 → 3 块且覆盖尾部、参数退化钳制、
段级与词级时间平移、重叠去重、默认关闭且进缓存身份、**子调用必须 `chunk_length=0`**（防递归）、
不适用时退回整段解码。

**重新评估触发条件**：拿到 30 分钟以上真实素材后需重新标定块长（显存/漂移/成本平衡点）；
若出现跨块指代断裂，应改为带上下文的滑窗而非独立分块。

---

## D39. 把隐式状态变成可见项：doctor 资产盘点、分歧清单进笔记、两条校验规则

**决策**（四件事，共同点是"把已经存在但看不见的东西暴露出来"）：

**1. `doctor status` 盘点 Skill 资产。** 新增 `core/skill_assets.py`，`doctor status` 输出里多一段
`skill_assets`：词表条目数、冲突裁决表条目数、分块是否可用、**分块默认值（从源码读出）**、提示词模板清单。
关键取舍：**数字来自真实文件**（`count_entries` 只数非注释行；默认值用正则从 `speech_to_text.py` 读），
不写死一份会和代码脱节的说法。

**2. 分歧清单进笔记。** 新增 `scripts/review_section.py`，把 `verify_transcript` 的
`needs_review_top` 渲染成笔记末尾的「需要人工确认的差异」小节（时间 / 本稿 / 另一来源 / 差异字数），
并提示有多少处已由裁决表改判。**只呈现、不自动改判**；没有校验信息时**返回空串**，不产生空小节。

**3. 提示词模板版本校验。** 校验器新增规则：`prompts/*.md` 必须声明 `template-version`，
同名模板不得出现互相矛盾的版本。

**4. 词表/裁决表的证据字段校验。** 校验器新增规则：`references/asr-lexicon.txt` 与
`references/conflict-preferences.txt` 的每条**非注释行都必须带 `#` 证据注释**——
把"要加词请附实测"从文件头的劝告变成**机器检查**。当前两个文件全部合规（10 条词表 + 11 条裁决表）。

**这次踩的坑（连续第三次同类）**：我又把新函数追加到了 `if __name__ == "__main__":` **之后**，
而且反复搬动后文件里出现了**两个** `__main__` 块，靠前那个在 `main` 定义前就调用它 →
`NameError`。教训明确：**校验器自己也需要"被导入并运行一次"的测试**，否则它坏了没人知道。
另外，分歧小节第一版在 `note render` 的子进程里因导入失败而让整篇笔记渲染失败 →
现在它是**可选增强**（`try/except` 包裹），加载不到只是没有这一节，绝不影响笔记产出。

**验证方式**：`tests/test_skill_assets.py` 覆盖资产数字与真实文件一致、分块默认值来自源码、
目录缺失时不抛异常、分歧小节渲染/上限/改判提示、无报告时返回空串、两个 `to_markdown` 确已接线；
校验器自身运行输出 `REPOSITORY_OK: 124 files` 且**无任何 NOTE**（说明两条新规则当前全部满足）。

**重新评估触发条件**：如果 `doctor` 的资产段继续增长，应改为独立子命令 `doctor assets`；
如果分歧小节让笔记过长，应按"差异字数 ≥3"再收一档。

---

## D40. 自己去找 B站 素材做实测：分块标定、前端行为，以及实验抓出的两个缺陷

**决策**：不再等用户提供素材，直接用 B站 排行榜/搜索 API 选真实素材（46.4 分钟数学课 `BV154hD61Ez8`、
64 秒民谣 `BV1zKZrYAEi8`）跑三组实验，产物落在 **`docs/experiments/`**（`outputs/` 被 .gitignore 排除，证据必须放在被跟踪的目录）（脚本 + 原始 JSON + SUMMARY.md）。
同时**给校验器本身加了回归测试**——因为我在同一轮里连续三次把函数追加到
`if __name__ == "__main__":` 之后，最后一次还留下两个 `__main__` 块。

**实测结论（详细数据见 SUMMARY.md）**：
- **长视频（46.4 分钟）**：整段解码 **93.5s（29.8× 实时）**、1561 段、13809 字；分块（600s/2s）**132.5s（21.0×）**、
  1512 段、13794 字、5 块；**文本相似度 0.9052、字数差仅 −15（0.11%）**。**分块反而慢 42%**，
  因为每块都会重新加载模型——**分块的价值是韧性与显存上界，不是速度**。
- **音频前端**：干净口播上**逐字相同**（844 字 / 80 段，两种设置完全一致）；音乐素材两种设置都产出**空文本**
  （歌唱人声未被有效转写，属能力边界）。这与 D26 的"前端 17% CER 收益"不矛盾：那条基于**有金标**的抖音素材，
  今天的实验没有金标，只能得出"文本层面中性"。
- **覆盖率兜底**：整段解码的诊断里确实出现 `asr_coverage`（守卫生效）。

**实验抓出的两个真实缺陷（都已修）**：
1. **`device` 误报**：分块路径返回 `device: None`（实际在 cuda 上跑），因为取的是函数参数而非子调用的实际设备；
2. **子诊断丢失**：合并后只剩 `asr_chunking`，每块的覆盖率/幻觉门结论被丢弃，审计链断裂。

两条都**只有真实素材 + 真实运行才能发现**：假引擎的单元测试不可能看见设备字段或子诊断。

**可执行结论**：短于 ~30 分钟不开分块；30 分钟以上可用（文本等价，换取韧性/显存上界）；
前端保持默认开启（口播类中性、有金标时曾收益）；音乐/歌唱素材是当前能力边界。

**重新评估触发条件**：显存更大的机器上应重测"整段 vs 分块"的性价比；
若音乐/歌唱成为常用场景，需要单独的方案（不在当前范围）。

---

## D41. B站 金标由 Agent 判定（`agent-verified`），并且**不冒充人工核对**

**决策**：B站 30.63 秒素材 `BV1ntah6TEe9` 的金标完成，`reference.kind = "agent-verified"`——
**不是** `human-verified`。判定方法、逐行依据与不确定项都写进产物的 `review` 块。

**方法（可复现）**：初稿取 `large`（cuda-float16/balanced），逐行与 `small` 同配置对照，
再用上下文一致性裁定；每行记录 `decision`（keep/correct/unresolved）、`chosen` 与 `basis`。

**判定结果**：15 行中 **13 行 keep**（其中 4 行两配置逐字一致）、**1 行 correct**
（`一见秋衣` → `一件秋衣`，small 正确）、**1 行 unresolved**（第 12 行，两配置都给出不成词的串：
`巴不敢缠` / `发布感禅`，缺音频细听，**不擅自定稿**）。另记一条不确定：
`滑脸` 两配置一致但语义可疑（可能是 `划脸/刮脸`），仅凭文本无法判定。

**第一次 B站 度量基线**（同一支视频 vs 该金标）：

| 模型 | CER | 覆盖率 | 幻觉 | 时间轴偏移 |
|---|---|---|---|---|
| `small` | **0.1111**（13 错 / 117 字） | 0.8501 | **0.0 字/分钟** | 中位 0.52s |

**为什么这比"没有金标"强、比"human-verified"弱**：它让 B站 侧第一次有了可比较的基线
（此前所有度量都来自抖音），而且判定依据逐行可审计；但它**没有经过人耳确认**，
因此不确定性被显式记录（`unresolved_rows`、`uncertainty_notes`、`human_spot_check_focus`），
`kind` 字段也不会假装成人核对。

**重新评估触发条件**：人耳确认第 12 行与 `滑/划` 两处后，把 `kind` 改为 `human-verified`
并删除不确定项；`agent-verified` 与 `human-verified` 的区别应保持可见，不应被静默合并。

---

## D42. 标点：先用规则法并**量化它的天花板**；不引入模型依赖；`--chunk-length` 暴露到 CLI

**决策一：标点继续用规则法，并把它测到位。** 新增标点质量指标（`eval_asr.punctuation_scores`：
把两侧标点按"去标点后的字符位置"配对、±3 字窗口内算命中，给出 P/R/F1；金标无标点时返回"不适用"），
并用抖音金标（217 处标点）量化规则法：

| 规则 | 标点数 | P | R | F1 |
|---|---|---|---|---|
| 不插标点（原始 ASR） | 0 | 0.0 | 0.0 | **0.0** |
| **边界即插 + 语气词/连词升级（采用）** | 179 | 0.346 | 0.286 | **0.313** |
| 抬高逗号阈值到 0.35 秒（我第一版，**退步**） | 24 | 0.167 | 0.018 | 0.033 |

**决策二：不引入标点模型依赖。** 天花板来自数据本身：中文口播标点密度约"每 10 字一个"，
而 ASR 分段粒度约 1.4 秒/段，**边界数量远远不够**；规则法做到 F1≈0.31 已接近该粒度的上限。
要再上一个台阶只有两条路——标点模型（可选依赖，按 `gpu-cuda12`/`zh-normalize` 的方式做成
可选依赖组 + 优雅降级）或更细的分段（词级时间戳也解决不了段内标点）。
当前标点不是验收项，所以**先不做**；一旦标点成为需求，按上面的可选依赖方式加。

**我犯的错与它被抓住的方式（值得记录）**：我第一版把逗号阈值抬高到 0.35 秒，
直觉上"标点更少更准"，实际把 F1 从 0.313 砸到 0.033。**先被既有测试抓住**
（`test_pause_punctuation_only_touches_segment_boundaries` 断言 0.1 秒也要插），
再被新指标量化确认。两件事都做了才叫"验证"，只靠其中一件都会得出错的结论。

**决策三：`--chunk-length` / `--chunk-overlap` 暴露到 `transcribe_audio_cli.py`。**
D38 的分块能力此前只有代码路径能启用；现在命令行可用（默认 0 = 不分块，既有行为不变），
配合 D40 的实测结论（30 分钟以上、且需要韧性与显存上界时才建议开启）。

**重新评估触发条件**：标点若进入验收项 → 加可选依赖组；若 ASR 引擎开始输出更细的时间戳 →
重测规则法的天花板是否抬高。

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
| D24 | 硬字幕独立来源 + 漏采自检 | 可（若接入真实字幕轨道） |
| D25 | 度量先于优化，口径按校对成本 | 可（若多语种需分别设线） |
| D26 | 质量开关逐个消融；前端保留、词表默认关闭 | 可（换素材需重跑评测） |
| D27 | 定向二次解码：四条判据 + 拒绝也是结论 | 可（若能拿到更可靠的置信度） |
| D28 | 无音轨单独成档（27），改走图片 OCR | 可（若平台提供图文文案） |
| D29 | 多源融合 + per-span provenance | 可（若确认字幕轨可用） |
| D30 | 词级重新分段：不丢词是硬不变量 | 可（若引擎自带标点/长段） |
| D31 | 浏览器取流自研实现，不耦合其它仓库 | 可（若平台放开免签名接口） |
| D32 | 本地视频文件是一等输入 | 可（取流恢复后仍保留） |
| D33 | 金标半自动化 + 数字归一 | 可（若接入标注界面/第三配置） |
| D34 | 补转结果过幻觉门；可疑只标注 | 可（阈值需按素材重标定） |
| D35 | 融合第三判据（裁决表）+ 复核清单排序 | 可（若引入第三来源） |
| D36 | 词表收敛/诊断键/模型指纹/未引用报告 | 可（词表扩张需附评测证据） |
| D37 | 退出码镜像模块 + 未引用脚本白名单 | 可（harness 可分发时退化为迁移脚本） |
| D38 | 长音频分块接线（默认关闭） | 可（需长素材重新标定块长） |
| D39 | doctor 资产盘点 / 分歧清单进笔记 / 两条校验规则 | 可（doctor 段增长则拆子命令） |
| D40 | B站 素材实测：分块标定 / 前端行为 / 实验抓出的缺陷 | 可（换更大的显存重测） |
| D41 | B站 金标 = agent-verified（不冒充人工） | 可（人耳确认后转 human-verified） |
| D42 | 标点用规则法并量化天花板；分块参数进 CLI | 可（标点成验收项则加可选依赖） |
