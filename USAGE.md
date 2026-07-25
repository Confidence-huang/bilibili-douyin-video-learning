# 使用示例

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

未配置时使用当前用户的 `~/Notes`，不会继承分享者的笔记路径。
