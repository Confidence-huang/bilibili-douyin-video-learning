# Video Learning CLI Test Plan

## Part 1: Test Inventory Plan

- `test_core.py`：核心单元测试，使用合成输入和 monkeypatch，不访问网络。
- `test_full_e2e.py`：真实文件与安装态 CLI subprocess 测试，不设置 `cwd`。

## Unit Test Plan

### B站安全元数据

- 断言 yt-dlp 元数据命令包含 `--skip-download`，且不包含 `--no-simulate`。
- 断言缺省页为 P1，非法页、零页和不存在的分 P 明确失败。
- 断言字幕格式按 JSON/SRT/VTT 可解析格式选择，不再只接受 SRT。

### 字幕转换

- 覆盖带小时和不带小时的 WebVTT 时间戳。
- 覆盖 SRT、Bilibili JSON 和 yt-dlp JSON3。
- 覆盖 cue settings、空 cue 与无效输入。

### Cookie 与路径

- 只统计实际可导出的明文 Cookie。
- 全部为 DPAPI 密文时失败，且不覆盖既有输出。
- 明文导出必须有风险确认，覆盖既有文件还必须有独立授权。
- Obsidian 默认路径来自环境变量或当前用户的 `~/Notes`，不继承分享者路径。

### 输出与缓存

- Markdown 默认不包含全文，显式选项才加入时间线正文。
- 运行日志写 stderr，JSON stdout 不被进度文本污染。
- 抖音缓存键对相同输入稳定，对模型、语言或下载参数变化敏感。
- 抖音 SSR 元数据检查不下载媒体；画质探测标记重复载荷并且只向更低画质回退。
- 抖音 `auto` 下载在 SSR 失败后进入 yt-dlp fallback，统一 CLI 只有显式 `--transcribe` 才进入 ASR。
- `--keep-audio` 能传入转写指令并控制清理。

## E2E Test Plan

### Workflow: 从任意目录解析来源

- 通过已安装的 `cli-anything-video-learning --json source normalize` 解析 B站 URL 和抖音分享文本。
- 验证退出码、单一 JSON 文档、平台、BV/aweme 字段和分 P。

### Workflow: 本地字幕转时间线

- 在临时目录创建真实 `.vtt` 文件。
- 通过已安装命令转换并验证段数、时间、文本与输出文件内容。

### Workflow: 本地结果生成受限笔记

- 创建真实 metadata/subtitle JSON。
- 默认渲染后验证 Markdown 文件存在、来源信息存在、完整转录不存在。
- 带 `--include-transcript` 时验证正文按时间线出现。

### Workflow: 本机真实后端预检

- 通过已安装命令运行 `doctor status`。
- 验证真实 Skill 根、GPU Python、yt-dlp 和 FFmpeg 路径，而不是只信退出码。

## Network/GPU Opt-In Boundary

平台接口、媒体下载和完整 ASR 会消耗网络、时间或 GPU，只在设置 `VIDEO_LEARNING_LIVE_TEST_URL` 后运行独立 smoke。默认套件必须离线可重复，并通过命令构造、真实工具探测、真实本地文件和安装态 subprocess 保证高风险契约。

## Part 2: Test Results

执行日期：2026-07-25

安装环境：

- CLI Python：`<skill-root>\.venv-gpu\Scripts\python.exe`
- 安装方式：`uv pip install --python <CLI-Python> -e <skill-root>\agent-harness[test]`
- 安装入口：`cli-anything-video-learning` 1.2.0
- 运行约束：`CLI_ANYTHING_FORCE_INSTALLED=1`，测试不得回退到源码模块

执行命令：

```powershell
python -m pytest cli_anything\video_learning\tests -v -s --tb=no
```

最终结果：

```text
53 passed in 3.71s
```

验收覆盖：

- 42 个核心单测全部通过：安全 yt-dlp 参数、严格分 P、JSON3/VTT、Cookie 风险授权、默认省略全文、不可信正文规则、CLI/后端错误脱敏、Cookie 授权状态、原子写入、来源身份、抖音 SSR 元数据/画质/fallback/缓存身份和音频清理。
- 11 个安装态 subprocess 测试通过：包含 1.2.0 版本、纯 JSON、非法分 P、字幕/笔记原子输出、来源身份与真实 doctor。
- 当前总计 53 个离线/安装态测试；默认套件不联网、不下载媒体、不读取浏览器 Cookie，也不启动 GPU ASR。
- 10 个安装态 E2E 全部通过：帮助、B站/抖音解析、3 种非法 `p=`、真实 VTT 文件、两种笔记渲染和真实 doctor 后端检查。
- 从 `C:\Windows\Temp` 运行安装命令：P2 返回退出码 0；`p=0` 返回退出码 1 与 `{\"ok\": false}`；doctor 找到真实 Skill、GPU Python、yt-dlp 与 FFmpeg。
- 两份新 CLI `SKILL.md` 通过 `quick_validate.py`；原中文 Skill 用 `python -X utf8` 调用同一验证器后通过。第一次原 Skill 校验的 GBK `UnicodeDecodeError` 属于验证器调用编码问题，不是 Skill 内容失败。
- GitHub CI 使用轻量环境运行 52 项离线测试并跳过唯一的完整安装态 doctor；本机发布门槛使用既有验证运行时完成全部 53 项。

未执行的平台联网、媒体下载和 GPU ASR smoke：这些检查可能访问站点、下载媒体或占用 GPU，不属于本轮默认离线回归。对应风险仍由 `doctor status`、命令构造测试和后续用户授权的真实视频任务覆盖。
