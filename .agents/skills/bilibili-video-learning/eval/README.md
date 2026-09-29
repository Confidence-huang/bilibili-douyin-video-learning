# 转写评测（金标集 + 指标）

## 为什么先做这个

没有度量就只能靠感觉调参。本目录把"更准了"变成可复现的数字，**先于任何优化落地**（见 `docs/DECISIONS.md` D25）。

## 金标集

`gold/*.json` **只存文本与时间轴**：仓库边界禁止 `.wav/.mp4` 入库，复现时用 `media.url` 重新取流即可。

| 字段 | 含义 |
|---|---|
| `media.url` / `duration_seconds` | 复现入口与参考时长（覆盖率、RTF 的分母） |
| `reference.paragraphs` / `reference.text` | 人工核对过的正文（逐字稿级） |
| `reference.kind` / `provenance` | 这份"真值"是怎么来的（决定它有多可信） |
| `known_asr_errors` | 已知的同音/漏字错例，用于检查某项改动是否真的修掉了它们 |

现有金标：

| id | 来源 | 真值依据 |
|---|---|---|
| `douyin-7690619057690828986` | 抖音 259.77s 口播 | 作者硬字幕 OCR（4fps）+ 逐帧核对漏采短卡片 + 两遍 ASR 交叉校验 |

新增金标的要求：`reference.text` 必须是**人工核对过**的正文；`provenance` 写清依据；
不要用某次 ASR 输出当真值（那是自证）。

## 指标口径

| 指标 | 定义 | 为什么这样定 |
|---|---|---|
| CER | (替换 + 漏字 + 多字) / 参考字数 | 中文以字为单位更贴近校对成本；多字也罚，避免"多说话"被奖励 |
| 幻觉率 | 参考里完全不存在的新增片段（≥`--min-run` 字）字数 / 分钟 | 只惩罚"编造"，与"漏字"分开计 |
| 覆盖率 | 假设时间轴并集（按参考时长截断）/ 参考时长 | 缺口 = 整段没被转写；重叠只算一次 |
| 时间轴偏移 | 逐段最长公共块对齐后的 `|假设时间 − 金标时间|` 中位数/最大 | 衡量"内容对但时间漂"，不是段边界差 |
| RTF | 耗时 / 音频时长 | GPU 上越小越好 |

> ⚠️ 方向陷阱：`difflib` 里 `a=假设, b=参考`，所以 `insert` 是**漏字**、`delete` 是**多字**。
> `edit_operations()` 已显式翻译一次，并在文档里写明，避免再次搞反（曾经把"漏掉的金标文字"报成"幻觉"）。

## 制作新的金标（半自动）

```bash
# 1) 用两个配置各转写一次（初稿用最好的配置，第二配置只用于分歧清单）
python scripts/transcribe_bilibili.py <BV号> --model large --json > /tmp/primary.json
python scripts/transcribe_bilibili.py <BV号> --model small --json > /tmp/secondary.json

# 2) 生成草稿 + 核对清单
python scripts/eval_gold_draft.py --primary /tmp/primary.json --secondary /tmp/secondary.json \
  --id bilibili-<BV号> --platform bilibili --media-url "<链接>" --duration <秒> \
  -o eval/gold/bilibili-<BV号>.draft.json --worksheet eval/worksheets/bilibili-<BV号>.md

# 3) 只核对清单里被标记的行（低置信 / 复读嫌疑 / 实质分歧），其余抽查
# 4) 改完把 reference.kind 从 semi-automatic-draft 改成 human-verified，并删掉 review 块
```

清单会按 `复读嫌疑 > 低置信 > 实质分歧 > 非实质分歧` 排序，并截断到段落数的 30%（至少 8 条），
同时报告可疑总数——否则"分歧多"的素材会把所有段落都标出来，等于没有优先级（D33）。

## 口径说明（不影响正确性的差异不计入 CER）

- **数字写法**：中文数字与阿拉伯数字归一（`六`≡`6`、`一百`≡`100`、`四十五岁`≡`45岁`）。
  抖音金标上这让 CER 从 0.0214 降到 0.0187（41→36 错）——差的正是写法差异，不是识别错误。
  要严格口径加 `--keep-numerals`。
- **繁简**：默认做 t2s 归一（缺 OpenCC 时降级）。

## 用法

```bash
# 单条（--elapsed-seconds 用于算 RTF）
python scripts/eval_asr.py --hypothesis out/asr.json \
  --gold eval/gold/douyin-7690619057690828986.json --elapsed-seconds 60.5

# 批量：假设文件名需含金标 id
python scripts/eval_asr.py --hypothesis-dir out/ --gold-set eval/gold --json -o report.json
```

假设文件支持四种形状：规范 JSON（`start/end/text`）、ASR JSON（`from/to/content`）、SRT、TXT。

## 当前基线（真实数据，可复现）

真实抖音音频（259.77s）+ 上述金标，输入为仓库内的真实 ASR fixture（VAD 开启、会丢字的配置）：

| 指标 | 数值 |
|---|---|
| CER | **0.0402**（32 替换 / 44 漏字 / 1 多字，共 77 错） |
| 幻觉率 | **0.0 字/分钟** |
| 覆盖率 | **0.9759**（与历史记录的 0.9760 吻合，说明评测器可交叉验证） |
| 时间轴偏移 | 中位 **0.26s** / 最大 3.06s（匹配 41/44 段） |
| RTF | 0.233（`small`，含模型加载） |

## 已测配置对比（large-v3，净化音频，真实 259.77s + 1917 字金标）

| 档位 | CER | 替换 | 漏字 | 多字 | 覆盖率 | 时间轴中位 | 说明 |
|---|---|---|---|---|---|---|---|
| **`balanced`** | **0.0214** | 36 | 5 | 0 | 0.9976 | 0.20s | 默认档：实测最低 CER，约 `small` 最好成绩（0.0433）的一半 |
| `timing` | — | — | — | — | — | — | 换取词级时间戳：192 段中 186 段带词，最长段 5.24s，SRT 每行 ≤18 字 |
| `quality`（门槛取真实 p10+0.02＝-0.086） | 0.0219 | — | — | — | — | — | 找到 **4 个可疑段，全部被拒**（理由均为 `confidence_not_improved`）：机制生效且判据如实拒绝了无益替换 |

置信度分布（large-v3，201 段）：min -0.112 / p10 -0.106 / 中位 -0.073——
**模型对这段素材高度自信，没有低置信段**；这解释了为什么默认门槛（-1.0）永远不开火，
也说明"把门槛调低直到计数器动起来"是错误做法（D27）。

## 已测配置对比（单变量消融，`small`/cuda）

| 配置 | CER | 替换 | 漏字 | 多字 | 覆盖率 |
|---|---|---|---|---|---|
| 未净化 + beam1 + 无词级时间戳（原默认） | 0.0522 | 82 | 17 | 0 | 0.9956 |
| **净化 + beam1 + 无词级时间戳（现默认）** | **0.0433** | 70 | 11 | 0 | 0.9980 |
| 净化 + beam5 + 无词级时间戳（`quality`） | 0.0480 | 80 | 8 | 4 | 0.9992 |
| 净化 + beam1 + 词级时间戳（`timing`） | 0.0511 | 84 | 14 | 0 | 0.9976 |
| 净化 + beam1 + 词级时间戳 + 词表 | 0.0751 | 85 | 56 | 3 | 0.9979 |
| 未净化 + beam5 + 词级时间戳 | 0.0616 | 76 | 41 | 0 | 0.9979 |

结论：**音频前端有效（−17% 相对 CER）；词表在这支视频上有害；词级时间戳是能力收益而非 CER 收益；
beam5 与词级时间戳组合有害**。方法与完整讨论见 `docs/DECISIONS.md` D26。

**验收线**：逐字稿场景 CER ≤ 10%（`DEFAULT_CER_BUDGET`）。当前基线已达标，因此后续优化的目标是
**在 CER 不变差的前提下修掉已知错例、提高覆盖率、降低幻觉**，并用上表逐项证明。
