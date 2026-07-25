# Video Learning CLI：架构与执行边界

## 目标

`cli-anything-video-learning` 是现有 `bilibili-video-learning` Skill 的稳定命令入口。它不重新实现视频平台、FFmpeg 或 ASR，而是把 Skill 已有脚本包装成可安装、可发现、可输出纯 JSON、可通过 subprocess 验收的 CLI。

## 真实后端

| 能力 | 真实后端 | CLI 的职责 |
|---|---|---|
| B站元数据与字幕 | `fetch_bilibili.py`、yt-dlp、Bilibili 公开 API | 固定安全参数、严格分 P、规范错误与 JSON |
| 抖音公开输入解析 | `douyin_ssr.py` | 只解析公开 URL、分享文本和 aweme_id |
| 本地字幕转换 | `convert_subtitle.py` | 统一为 `start/end/text` 时间线 |
| 音视频转写 | FFmpeg、faster-whisper/CTranslate2 | 显式触发，不在元数据命令里隐式下载 |
| 学习笔记 | Skill 的 Markdown 渲染函数 | 默认不复制全文；显式授权后才能包含转录正文 |

## 命令结构

- `source normalize`：解析 B站或抖音输入，不下载媒体。
- `source inspect`：提取 B站元数据；`--subtitles` 只取字幕，`--transcribe` 才允许音频与 ASR。
- `subtitle convert`：把 SRT、VTT、ASS 或平台 JSON 转为统一 JSON。
- `note render`：从本地结果 JSON 生成学习笔记；默认省略完整转录。
- `doctor status`：检查 Skill 根、GPU Python、yt-dlp、FFmpeg、直接依赖和 Obsidian 路径。

## 数据与反馈

这套 CLI 是无项目状态的后端包装器，因此不创建 CLI-Anything 的 project/session/undo 文件。每个命令都遵循同一链条：

`Click 参数触发 → core 指令选择后端 → 真实脚本或工具执行 → 安全层脱敏/身份校验/原子发布 → stdout 返回 JSON/正文，stderr 返回诊断`

全局 `--json` 模式保证 stdout 只有一个 JSON 文档。错误同样返回 JSON，并使用非零退出码；进度、fallback 和工具诊断只写 stderr。

安全层包含四个稳定契约：视频正文只作为不可信资料；错误与诊断统一脱敏；匿名失败可返回退出码 `21` 的 `cookie_permission_required`；最终文件原子替换，抖音缓存和渲染笔记都携带可验证来源身份。

## 安全边界

- `source inspect` 必须使用 yt-dlp 的模拟/跳过下载参数，运行后不得生成媒体文件。
- 不存在或非法的 `p=` 必须失败，不得静默改为 P1。
- Cookie 不进入项目文件、日志或 JSON；优先使用 yt-dlp 的浏览器 Cookie 能力。
- 默认 Markdown 不包含完整字幕或 ASR 全文；`--include-transcript` 只用于用户自有或明确授权的本地处理。
- 联网视频与 ASR 测试是显式 opt-in，默认测试不会访问平台或下载媒体。

## CLI-Anything 适配说明

本 harness 遵循真实后端、Click 命令组、默认 REPL、`--json`、安装态 subprocess 和真实文件验证规范。通用验证器要求的 project/session/export 模块不适用于无状态平台包装器，因此以本文件记录的目标专用结构和测试门槛为准。
