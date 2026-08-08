# Windows 与 Linux 安装说明

## 共同前置条件

```text
Python 3.12+
uv 0.11+
Git
```

FFmpeg 优先从 PATH 发现；若主机没有安装，运行环境会使用 `imageio-ffmpeg` 的用户级二进制。安装器不需要管理员权限或 sudo。

## Windows

在 PowerShell 中执行：

```powershell
git clone https://github.com/Confidence-huang/bilibili-douyin-video-learning.git
cd bilibili-douyin-video-learning
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
powershell -ExecutionPolicy Bypass -File .\verify.ps1
```

默认安装到 `%USERPROFILE%\.agents\skills\bilibili-video-learning`，运行时位于 `.venv-gpu`。安装器启用 `asr` 与 `cuda-compat` 两个依赖档案；后者保留 OpenAI Whisper/PyTorch CUDA 回退，但 GPU 是否可用仍须通过 `nvidia-smi`、`torch.cuda.is_available()` 和实际 ASR 日志确认。

自定义目标：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 `
  -DestinationRoot "E:\AgentSkills\bilibili-video-learning"
```

目标名必须为 `bilibili-video-learning`。安装器拒绝磁盘根目录、用户主目录和含命令解释字符的路径。

## Ubuntu/Linux

```bash
git clone https://github.com/Confidence-huang/bilibili-douyin-video-learning.git
cd bilibili-douyin-video-learning
./install_linux.sh
./verify_linux.sh
```

默认安装到 `~/.agents/skills/bilibili-video-learning`，运行时位于 `${XDG_DATA_HOME:-$HOME/.local/share}/bilibili-video-learning/runtime`，CLI 包装器位于 `~/.local/bin`。外置运行时可避免 Python 依赖携带的 Skill 文件污染主 Skill 的生命周期扫描。Linux 安装器：

1. 只启用 `asr` 依赖档案；
2. 没有可见 CUDA 时由 faster-whisper 自动使用 CPU/int8；
3. 不安装 CUDA、不升级显卡驱动、不调用 sudo；
4. 不编辑 shell 启动文件，不创建 daemon 或 systemd 服务；
5. 不读取或迁移 Windows 浏览器 Cookie/配置文件。

自定义目标：

```bash
./install_linux.sh \
  --destination-root "$HOME/.local/share/agent-skills/bilibili-video-learning" \
  --runtime-root "$HOME/.local/share/bilibili-video-learning/runtime" \
  --command-bin "$HOME/.local/bin"
./verify_linux.sh \
  --skill-root "$HOME/.local/share/agent-skills/bilibili-video-learning" \
  --runtime-root "$HOME/.local/share/bilibili-video-learning/runtime"
```

## 只安装源码

此模式可以让 Agent 发现 Skill，但不能运行完整 CLI 或 ASR。

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -SkipRuntime -SkipPathUpdate
powershell -ExecutionPolicy Bypass -File .\verify.ps1 -SkipRuntime
```

Linux：

```bash
./install_linux.sh --skip-runtime
./verify_linux.sh --skill-root "$HOME/.agents/skills/bilibili-video-learning" --skip-runtime
```

## 可恢复更新与回退

两个安装器都会先把现有目标移动为同级的 `bilibili-video-learning.backup-<时间>`，再发布新源码，不会静默覆盖。验证失败时先保留现场；需要回退时退出正在使用该 Skill 的 Agent/终端，把当前目录改名，再把最近一次完整备份恢复为 `bilibili-video-learning`。

不要删除备份，直到新版本完成源码、运行时和代表性行为验证。正式安装更新优先通过 Skill Lifecycle Manager 的预览、精确批准、事务安装和回滚流程完成。
