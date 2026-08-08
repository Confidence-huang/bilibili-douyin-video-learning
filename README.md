# Bilibili & Douyin Video Learning

[![CI](https://github.com/Confidence-huang/bilibili-douyin-video-learning/actions/workflows/ci.yml/badge.svg)](https://github.com/Confidence-huang/bilibili-douyin-video-learning/actions/workflows/ci.yml)

A cross-platform Windows/Linux Agent Skill that turns accessible Bilibili and Douyin videos into structured learning notes.

这个 Skill 可以提取公开可访问的视频元数据、字幕和授权转写内容，并生成中文摘要、复习笔记、行动清单、问答与 Anki 材料。它不会绕过付费、会员、私密、地区或平台风控限制。

## 主要能力

- 识别 B站链接、`b23.tv`、BV/av ID、抖音分享链接和本地字幕/音视频。
- 优先使用公开字幕；只有用户明确要求时才下载临时音频并运行 ASR。
- 严格保留 B站分 P，错误的 `p=` 不会静默切换到 P1。
- 把 Cookie、token、签名 URL 和临时路径从诊断输出中脱敏。
- 提供稳定的 JSON CLI，用于 B站/抖音来源检查、字幕转换、笔记渲染和本地诊断。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。FFmpeg 优先使用系统版本，缺少时使用 Skill 环境内的用户级 `imageio-ffmpeg`，不要求 sudo。

### Windows（CUDA 兼容档案）

```powershell
git clone https://github.com/Confidence-huang/bilibili-douyin-video-learning.git
cd bilibili-douyin-video-learning
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
powershell -ExecutionPolicy Bypass -File .\verify.ps1
```

Windows 安装器创建 `.venv-gpu`，安装 faster-whisper，并保留 OpenAI Whisper/PyTorch CUDA 兼容回退。只有实际探测到 NVIDIA/CUDA 且运行日志显示 `cuda/float16` 时，才能声称正在使用 GPU。

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
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -SkipRuntime -SkipPathUpdate
powershell -ExecutionPolicy Bypass -File .\verify.ps1 -SkipRuntime
```

```bash
./install_linux.sh --skip-runtime
./verify_linux.sh --skill-root "$HOME/.agents/skills/bilibili-video-learning" --skip-runtime
```

`npx skills add` 只安装 Skill 源码；完整 CLI、FFmpeg 调用和 ASR 仍需运行对应平台安装器。

完整安装说明见 [INSTALL.md](INSTALL.md)，命令示例见 [USAGE.md](USAGE.md)，安全边界见 [SECURITY.md](SECURITY.md)。

## 使用

重新打开 Codex 后输入：

```text
$bilibili-video-learning 帮我学习这个视频：<B站或抖音链接>
```

也可以直接检查 CLI：

```text
cli-anything-video-learning --json doctor status
cli-anything-video-learning --json source inspect "<B站或抖音链接>"
```

仓库名同时包含 Bilibili 和 Douyin；Skill 调用名继续使用 `$bilibili-video-learning`，以兼容现有安装。

## 项目结构

```text
.agents/skills/bilibili-video-learning/
├── SKILL.md
├── agents/openai.yaml
├── scripts/
├── references/
└── agent-harness/
```

仓库不包含虚拟环境、Cookie、token、浏览器资料、媒体文件、模型缓存、个人笔记或完整转写输出。

网页登录课程的音频采集、自动切课和断点续转属于独立的 `course-audio-capture` Skill；它与本仓库的公开视频/本地文件学习边界不同，不在这里合并。

## 核心依赖与参考

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：核心运行依赖，用于公开元数据、字幕和媒体处理路径。
- [HKUDS/CLI-Anything](https://github.com/HKUDS/CLI-Anything)：Agent Harness 与 CLI 结构来源，按 Apache License 2.0 使用。
- [FFmpeg](https://ffmpeg.org/)：音视频转换。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：授权音视频的本地 ASR。

## License

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for upstream attribution.
