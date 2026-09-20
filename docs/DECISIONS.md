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

## D5. 版本号必须四处一致，且由 CI 强制

**决策**：以下四处版本号必须相同，由 `tools/validate_repository.py::validate_version_parity()`
与 `tools/cli_smoke.py` 双重强制：

1. `.agents/skills/bilibili-video-learning/pyproject.toml`
2. `.agents/skills/bilibili-video-learning/agent-harness/setup.py`
3. `.agents/skills/bilibili-video-learning/agent-harness/cli_anything/video_learning/__init__.py`
4. `uv.lock` 自引用条目

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
Node 22 与 Node 26 **都满足**。

**重新评估触发条件**：如果未来引入基于 Node 的构建步骤（例如 JS 版 Skill 包装器）。

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
