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
