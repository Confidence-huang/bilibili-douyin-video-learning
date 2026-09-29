# Video Learning CLI Test Plan

## Part 1: Test Inventory Plan

- `test_core.py`：核心单元测试，使用合成输入和 monkeypatch，不访问网络。
- `test_full_e2e.py`：真实文件与安装态 CLI subprocess 测试，不设置 `cwd`。
- `test_cross_platform.py`：Windows/Linux 虚拟环境、FFmpeg 和 yt-dlp 入口契约。

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

### ASR 覆盖率与设备可用性

- 真实丢字数据（180 段 + 真实时长）只定位到一个 `196.24–201.84s` 空档；开头与结尾的空洞同样能发现。
- 静音空档不补转、补转窗口带上实测音量、预算封顶、超长窗口跳过、拿不到音量时保守跳过。
- 补转窗口必须关闭 VAD；补回的正文进入全文并更新覆盖率；显式关闭校验时保持第一遍结果。
- 重叠或倒置的区间不得把覆盖率算超过 100%。
- VAD 参数必须真实下传引擎，且在关闭 VAD 时不传参；它们变化必须改变缓存身份。
- 设备声称可用但实际不能解码时（惰性或构造期失败）降级到 `cpu/int8` 并保留原因；
  显式 `device="cuda"` 不静默降级；兜底也失败时保留根因错误。

### 分段形状与保真度

- 两套历史字段名（规范 `start/end/text` 与 ASR `from/to/content`）都能识别、往返、互转。
- 未知形状与缺时间戳必须显式失败，并打印实际字段名；可选置信度不得被伪造。
- 时间轴自检能发现重叠、倒置与空文本；SRT 时间戳合法且折行不丢字。
- `verbatim` 模式一个词都不删，`cleaned` 才删填充词且逐条报数；短碎片合并在两趟处理后真实生效（含首段碎片）。
- 切块脚本能直接吃 ASR 形状（旧的 KeyError 回归），产出能被 `build_notes` 消费。
- `--emit` 参数校验、多格式产出、完整转录的授权边界、缓存身份区分保真度与归一模式。

### 多源交叉校验

- 完全一致时零差异；同音替换带正确时间范围；单字差异默认必报且可显式抬高阈值过滤。
- 次源整段缺失被指名到时间范围；真实丢字区间（196.24–201.84s）被完整报出。
- 三种输入格式（规范 JSON / ASR JSON / SRT）；`--fail-on-difference` 返回 25；坏输入清晰失败。

### 置信度与文本归一化

- 逐段置信度被采集，只有低于阈值的区间进入 `low_confidence_spans`；阈值可调且进缓存身份。
- 繁简归一：注入转换器生效；缺 OpenCC 时 `auto` 降级只记录、`on` 显式报错、`off` 不转换。
- 停顿标点只在段边界插入，段内文字原样保留，已有标点不重复添加。

### CUDA 可用性

- 从解释器前缀推导 `nvidia/*/lib` 目录；四个运行时库都能被发现；缺库时给出可执行的安装建议。
- 找到但加载失败要报错且不断链；`LD_LIBRARY_PATH` 幂等追加。
- 可用性三态（无设备 / 设备可见但缺库 / 可用）各自给出不同结论。
- **ASR 入口的调用顺序必须是 prepare → choose_device → model**（真实故障的修复点）。
- `doctor` 报告 `usable` 与 `guidance`；没有 runtime 时保持只报可见性的历史行为。

### 转写质量层（词表 / 前端 / 档位 / 定向重解 / 重新分段）

- 词表解析、优先级、长度截断；元数据抽词与噪声词过滤；空词表传 `None` 而不是空串。
- 音频前端：默认开启高通+响度归一，`normalize=False` 必须是**完全不处理**（与历史行为一致）。
- 档位按实测定义：balanced=最低 CER、timing=词级时间戳、quality=beam5+定向重解；任何档位都不关覆盖率安全网。
- 引擎真的收到 `hotwords`/`word_timestamps`（伪造引擎捕获 kwargs）；未开启时不写入 `words`。
- 定向重解：四种拒绝理由（空结果/覆盖缩水/文本发散/置信度未提升）逐一覆盖，单窗口失败不拖垮其它窗口。
- 重新分段：不丢词硬不变量、停顿与标点切分、时间单调不重叠、无 words 时降级、SRT 每行 ≤ 上限。

### 浏览器取流与本地文件入口

- 自研 WS 客户端：用**真 socket** 起极简服务端验证握手与帧编解码（往返内容一致才算通过）。
- CDP 会话：无 id 的事件必须跳过；`error` 必须上抛而不是当空结果。
- 纯函数：aweme_id 解析（链接/裸 ID/modal_id/无关文本）、选档（命中档位→默认）、
  图文作品无播放地址必须明确报错、元数据字段映射。
- 环境：复用已在跑的 CDP 不重启浏览器；找不到浏览器时给可执行提示；端点探测失败返回 `None` 而不抛。
- CLI：缺参数 = 用法错误码；浏览器不可用 = 20 且 JSON 带 `kind=browser_unavailable`。
- 本地文件入口：真实 mp4 端到端（跳取流、`download_method=local`、覆盖率与段落数）。

### 多源融合与无音轨

- 融合：一致→主源、次源补段、主源保留、冲突记 alternatives 与 needs_review、**并集不变量**、provenance 计数。
- B站字幕优先跳过 ASR / 无字幕回退 ASR / `--fuse` 三态。
- 无音轨：用 ffmpeg 现场生成有/无音轨真实文件双向验证；`extract_audio` 抛 `NoAudioTrackError`；
  通用入口返回 27；退出码互不重复。

### 资产盘点与笔记里的分歧清单

- 资产数字必须与真实文件一致（注释不算条目）；分块默认值从源码读出；目录缺失时不抛异常。
- 分歧小节：有报告才渲染、`limit` 生效、提示裁决表改判数；无报告返回空串（不留空小节）。
- 两个 `to_markdown` 必须真的接上该小节。

### 长音频分块

- 短音频不分块；30 分钟 → 3 块且覆盖到结尾；参数退化时被钳制；尾部碎片并入前一块。
- 段级与词级时间戳都按块起点平移；重叠区重复文本只保留一份；默认关闭且进缓存身份。
- 分块路径的子调用必须 `chunk_length=0`（防递归）；未启用时退回整段解码。

### 退出码契约

- 镜像数值与规范逐项相等；镜像与规范的**分类结果**在文档化用例上一致。
- `fetch_bilibili` 自带常量与镜像一致（用测试锁住既有重复副本的漂移）。
- 平台专属异常类型优先于关键字；独立 CLI 真的返回映射后的码，且 JSON 的 `exit_code` 与返回码一致。

### 收尾项（诊断键 / 模型指纹 / 词表）

- 所有 ASR 诊断都带统一的 `step` 键（`asr_engine`），并保留 `engine` 兼容。
- 模型指纹：找不到权重要如实返回 `None`；指纹变化必须改变缓存身份。
- 词表文件只保留"能对应真实错例"的条目。

### 融合裁决与复核清单

- 逐位字符混淆：等长表项拆成位对位（`赠→赡`、`雪→血`），长度不等退回整串包含。
- 双向裁决：主源写错→采信次源；次源写错→维持主源；都不命中→维持主源。
- 冲突规模按"有几位不同"计；"实质冲突"忽略纯虚词差异；短清单 ≤30 且全量 spans 仍在。
- 裁决命中留痕（`chosen_by` / `wrong_form`）；仓库自带裁决表必须可解析。

### 幻觉门与标注

- 三道门各自触发与不触发：压缩比异常、同一 4 字片段重复 ≥3 次、时长-字数比上下限。
- 无窗口时长时跳过时长门，其余门仍生效；干净音频零误报。
- 标注不改写输入文本；`_finalize` 暴露 `suspected_hallucinations` 与对应诊断。
- 门的开关与阈值进入缓存身份。

### 金标制作（半自动草稿与核对清单）

- 三种可疑形态（低置信 / 高压缩比 / 与第二配置分歧）必须被标记；正常段落不能进清单。
- 全片可信时退回"最低十分位"，而不是一个人都不标；区间首尾相接也算重叠。
- 排序与上限：复读嫌疑优先；单字差异不算实质分歧；全片都分歧时只列前 N 并如实报告总数。
- 产物必须自称 `semi-automatic-draft`；无第二配置时可降级；清单含 ⚠ 标记、分歧表与完成步骤。
- 数字归一：`六≡6`、`一百≡100`、`四十五岁≡45岁`；`--keep-numerals` 回到严格口径。

### 转写评测（金标与指标）

- 三个编辑方向（替换/漏字/多字）分别计数，且多字计入 CER 分子。
- 规范化：去掉标点与空白、全角转半角、繁简归一（缺 OpenCC 时降级）。
- 幻觉只统计 ≥ 最短长度的新增片段；单字多出不进幻觉但仍由 CER 罚。
- 覆盖率对重叠区间去重并按参考时长截断；未转写的参考段不计入时间轴匹配。
- 时间轴偏移用"内容相同但整体后移"构造，必须报出真实位移。
- 四种产出格式（规范 JSON / ASR JSON / SRT / TXT）都能被读入；批量模式按金标 id 匹配。
- 仓库内金标集非空且**只含文本**（媒体不入库）。

### 硬字幕 OCR

- 变更检测合并卡片；**只出现 1 帧的短卡片不丢**（真实回归）。
- 内置与自定义校正表生效；完整性自检给出短卡片计数与覆盖率。
- **置信度门槛与噪声正则生效，且以"合法短卡片不受影响"为回归保护**（`更高级`、`病了` 必须保留）。
- 被删噪声行与整卡丢弃都要计数（`noise_lines_removed` / `noise_line_reasons` / `noise_cards_dropped`），绝不静默。
- 交叉校验能报出"字幕缺一句"；`--emit` 产出合法 SRT 且折行不丢字；缺 OCR 引擎给出可执行建议。

### 退出码契约

- 各类别退出码互不重复，成功码为 0，已有授权码仍为 21。
- 分享页不可用、网络超时、画质不可用、本地转写失败各自映射到专属码，未分类异常保持通用码。
- 抖音 `main()` 的退出码与 JSON `exit_code` 一致；B站异常分支保留 2 号兜底；CLI 与后端引用同一常量。

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

执行日期：2026-08-08

安装环境：

- Linux CLI Python：`${XDG_DATA_HOME:-$HOME/.local/share}/bilibili-video-learning/runtime/bin/python`
- Windows CLI Python：`<skill-root>\.venv-gpu\Scripts\python.exe`
- 安装方式：平台安装器调用 `uv pip install --python <CLI-Python> --no-deps -e <skill-root>/agent-harness`
- 安装入口：`cli-anything-video-learning` 1.15.0
- 运行约束：`CLI_ANYTHING_FORCE_INSTALLED=1`，测试不得回退到源码模块

执行命令：

```text
python -m pytest cli_anything/video_learning/tests -q
```

最终结果：

```text
301 passed, 1 skipped
```

验收覆盖：

- 301 个离线/安装态测试通过，覆盖安全 yt-dlp 参数、严格分 P、字幕解析、Cookie 风险授权、默认省略全文、诊断脱敏、原子写入、来源身份、抖音 SSR/缓存和跨平台运行时入口。
- 1.5.0 新增三组断言：ASR 覆盖率兜底（`test_asr_coverage.py`，含真实 VAD 丢字 fixture 的 196.24–201.84s 空档、
  静音不补转、预算封顶、超长窗口跳过、重叠区间只算一次、补转窗口关闭 VAD、VAD 参数真实下传、关闭校验时保持原样）；
  退出码契约（`test_exit_codes.py`，含抖音 `main()` 的 20/24 码与 JSON 一致性、B站异常分支保留 2 号兜底）；
  CUDA 不可用时的降级（`test_cross_platform.py`，覆盖惰性解码失败、构造期失败、显式 CUDA 不降级、兜底失败时保留根因）。
- 轻量 CI 环境跳过唯一要求完整 ASR 运行时的 doctor 测试；默认套件不联网、不下载媒体、不读取浏览器 Cookie，也不启动模型推理。
- 隔离 Linux 完整安装验收通过：依赖一致、CLI 1.15.0、本地合成 MP4 转 16 kHz 单声道 WAV、doctor 找到外置 runtime、yt-dlp、imageio-ffmpeg、faster-whisper 和 CTranslate2。
- Skill Creator 校验通过；Skill Lifecycle Manager 的 Static、Runtime、Behavior 三层验证均通过。
- Windows CUDA 完整安装与真实 GPU ASR 留给 Windows GitHub Actions 和显式授权的真实硬件 smoke；本地 Linux 验收不冒充 Windows GPU 证据。
- 1.5.0 的真实端到端复核（不进入离线套件，证据留在本轮记录中）：抖音 259.77 秒音频与 B站 30.6 秒视频
  在缺 CUDA 运行时的机器上降级到 `cpu/int8` 并成功；补齐 cuBLAS/cuDNN 后同一台机器上
  `large-v3` + `cuda/float16` 转写 259.77 秒耗时 49.3 秒（5.27× 实时），`device_fallback` 为空。

未执行的平台联网、媒体下载和 GPU ASR smoke：这些检查可能访问站点、下载媒体或占用 GPU，不属于本轮默认离线回归。对应风险仍由 `doctor status`、命令构造测试和后续用户授权的真实视频任务覆盖。
