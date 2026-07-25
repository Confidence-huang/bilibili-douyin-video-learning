# Windows 安装说明

## 获取源码

```powershell
git clone https://github.com/Confidence-huang/bilibili-douyin-video-learning.git
cd bilibili-douyin-video-learning
```

## 前置条件

在 PowerShell 中确认：

```powershell
uv --version
ffmpeg -version
```

需要 GPU ASR 时再确认：

```powershell
nvidia-smi
```

## 默认安装

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
```

默认目标为：

```text
%USERPROFILE%\.agents\skills\bilibili-video-learning
```

如果同名目录已存在，安装器先把它移动到带时间戳的同级备份，再安装新版本；不会静默覆盖旧 Skill。安装器随后：

1. 使用 `uv sync --locked` 在 Skill 内创建 `.venv-gpu`；
2. 把 CLI-Anything harness 以 editable 方式安装进该环境；
3. 在 `%LOCALAPPDATA%\bilibili-video-learning\bin` 写入小型命令包装器；
4. 仅在需要时把该命令目录追加到用户 PATH。

## 只安装源码

如果只想检查或让 Codex 看到 Skill，不下载大型 ASR 依赖：

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -SkipRuntime -SkipPathUpdate
powershell -ExecutionPolicy Bypass -File .\verify.ps1 -SkipRuntime
```

此模式不能运行视频 CLI 或 ASR。

## 自定义目标

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 `
  -DestinationRoot "E:\AgentSkills\bilibili-video-learning"
```

目标目录名必须仍为 `bilibili-video-learning`。安装器拒绝磁盘根目录、用户主目录和含命令解释字符的路径。

## 安装后验证

```powershell
powershell -ExecutionPolicy Bypass -File .\verify.ps1
```

验证默认不访问视频平台、不读取浏览器 Cookie、不下载媒体、不启动 GPU ASR。它会核对清单、检查脱敏边界、编译 Python 源码、检查依赖、确认 CLI 版本，并运行 40 项本地/安装态测试。

## 回退

如果需要回退，先退出正在使用该 Skill 的 Codex/终端，再把当前目录改名，然后把安装器显示的 `bilibili-video-learning.backup-<时间>` 改回 `bilibili-video-learning`。不要删除备份，直到新版本完成正常使用验证。
