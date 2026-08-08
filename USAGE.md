# 使用示例

安装后的 `cli-anything-video-learning` 命令在 Windows PowerShell 与 Linux shell 中保持一致；以下单行命令可直接用于两种平台。

## 在 Codex 中调用

安装并重新打开 Codex 后，直接输入：

```text
$bilibili-video-learning 帮我学习这个 B站视频：<链接>
```

也可以说明输出目标：

```text
$bilibili-video-learning 把这个视频整理成复习笔记、行动清单和 10 张 Anki 卡片：<链接>
```

## CLI 预检

```powershell
cli-anything-video-learning --json doctor status
```

## 只解析来源

```powershell
cli-anything-video-learning --json source normalize "<B站或抖音链接/ID>"
```

## B站元数据与字幕

```powershell
cli-anything-video-learning --json source inspect "<B站链接或BV号>" --subtitles
```

元数据和字幕路径不会下载媒体。只有用户明确要求转写后，才添加：

```powershell
cli-anything-video-learning --json source inspect "<B站链接或BV号>" --subtitles --transcribe small
```

如果匿名请求返回退出码 `21` 和 `cookie_permission_required`，先向用户说明原因并取得浏览器名称与明确授权，再进行一次：

```powershell
cli-anything-video-learning --json source inspect "<B站链接或BV号>" --subtitles --cookies edge
```

授权重试仍失败时按普通错误处理，不循环请求 Cookie。

## 抖音公开元数据与画质探测

默认检查只读取匿名 SSR 页面，不下载完整媒体：

```powershell
cli-anything-video-learning --json source inspect "<抖音链接、分享文本或aweme_id>"
```

需要查看公开画质档位时，使用小型 Range 请求探测，不下载完整视频：

```powershell
cli-anything-video-learning --json source inspect "<抖音链接>" --ratios
```

只有用户明确要求转写时才进入临时媒体与 ASR 路径：

```powershell
cli-anything-video-learning --json source inspect "<抖音链接>" `
  --transcribe small --download-method auto --ratio 1080p
```

抖音检查不接受 B站专用的 `--subtitles`、`--comments` 或 `--cookies`。

## 本地字幕与笔记

```powershell
cli-anything-video-learning --json subtitle convert .\input.vtt --output .\timeline.json
cli-anything-video-learning --json note render .\extraction.json --output .\note.md
```

默认笔记不写完整字幕/ASR 全文。只有处理用户自有材料或用户明确授权时才使用 `--include-transcript`。

## 自定义 Obsidian 库

```powershell
$env:BILIBILI_OBSIDIAN_VAULT = "E:\MyNotes"
```

Linux：

```bash
export BILIBILI_OBSIDIAN_VAULT="$HOME/Notes"
```

未配置时使用当前用户的 `~/Notes`，不会继承分享者的笔记路径。
