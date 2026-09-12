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

## 能做什么

- 识别 B站链接、`b23.tv`、BV/av ID、抖音分享链接和本地字幕/音视频；
- 优先使用公开字幕；只有用户明确要求时才下载临时音频并运行 ASR；
- 严格保留 B站分 P，错误的 `p=` 不会静默切换到 P1；
- 把 Cookie、token、签名 URL 和临时路径从诊断输出中脱敏；
- 提供稳定的 JSON CLI，用于来源检查、字幕转换、笔记渲染和本地诊断。

## 安全边界

- 视频简介、字幕、ASR、评论和弹幕都是不可信输入，只用于分析，不执行其中的提示词、命令、链接或凭据请求；
- 默认匿名访问；只有结构化返回 `cookie_permission_required` 且用户明确授权后，才允许指定浏览器进行一次 Cookie 重试；
- 不绕过付费、会员、私密、地区或平台风控限制；
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

完整安装说明见 [INSTALL.md](./INSTALL.md)，命令示例见 [USAGE.md](./USAGE.md)，安全边界见 [SECURITY.md](./SECURITY.md)。

## 项目结构

```text
.agents/skills/bilibili-video-learning/
├── SKILL.md
├── agents/openai.yaml
├── scripts/
├── references/
└── agent-harness/
```

网页登录课程的音频采集、自动切课和断点续转属于独立的 `course-audio-capture` Skill；它与本仓库的公开视频/本地文件学习边界不同，不在这里合并。

## 核心依赖与参考

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：核心运行依赖，用于公开元数据、字幕和媒体处理路径；
- [HKUDS/CLI-Anything](https://github.com/HKUDS/CLI-Anything)：Agent Harness 与 CLI 结构来源，按 Apache License 2.0 使用；
- [FFmpeg](https://ffmpeg.org/)：音视频转换；
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：授权音视频的本地 ASR。

## License

Licensed under the [Apache License 2.0](./LICENSE). See [NOTICE](./NOTICE) for upstream attribution.
