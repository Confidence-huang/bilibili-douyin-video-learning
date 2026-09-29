# 验收清单：什么算达标

每次改动前后都跑 `run_benchmark.py`，用同一张表对比。**没有数字的"改好了"不算改好。**

## 一条命令看全貌

```bash
python .agents/skills/bilibili-video-learning/scripts/run_benchmark.py \
  --case "bili-large=<产出>.json:bilibili-BV1ntah6TEe9.json" \
  --case "bili-small=<产出>.json:bilibili-BV1ntah6TEe9.json" \
  --case "douyin-large=<产出>.json:douyin-7690619057690828986.json" \
  --max-cer 0.05
```

`--max-cer` 是闸门：任一用例超标即返回 1（供 CI / 发版前检查）。

## 指标与达标线

| 指标 | 达标线 | 当前实测（真实素材） | 依据 |
|---|---|---|---|
| **CER**（字符错误率） | ≤ 0.05 | 抖音 `large-v3` **0.0187**；B站 `large` **0.0085** | 两条真实金标 |
| **覆盖率**（时间轴覆盖音频） | ≥ 0.99 | 抖音 0.9976；B站 0.999 | 覆盖率兜底开启时 |
| **幻觉**（编造字数/分钟） | ≤ 0.5 | **0.0**（两条素材） | ≥2 字新增片段 |
| **时间轴偏移**（中位） | ≤ 1.0s | 抖音 0.20s；B站 0.0s（最大 0.44s） | 与金标段对齐 |
| **标点 F1** | 参考值，**不列入验收** | **0.313**（规则法天花板已量化） | D42：受分段粒度限制 |
| **长视频分块一致性** | ≥ 0.90 | **0.9052**（46.4 分钟，600s 块） | D40 实测 |

## 多支金标（避免只靠一支素材）

| 金标 | 素材特征 | large CER | small CER | 备注 |
|---|---|---|---|---|
| `douyin-7690619057690828986` | 中文口播，259.77 秒 | 0.0187 | 0.0433 | 人工核对 |
| `bilibili-BV1ntah6TEe9` | 安静的 30 秒教程 | **0.0085** | 0.1111 | agent-verified |
| `bilibili-BV1Kyas6wEuz` | 109 秒叙述 + 表情包配音 + 音乐 | **0.0051** | 0.2298 | 后半段 22 行 unresolved；**标点抄自 large，故 punct_f1=1.0 是同义反复** |

### CI 闸门（每次 PR 都会跑）

`.github/workflows/ci.yml` 的 ubuntu 与 windows 两个 job 都会执行：

```bash
run_benchmark.py \
  --case "bili1-large=<fixtures>/bili1_large.json:bilibili-BV1ntah6TEe9.json" \
  --case "bili2-large=<fixtures>/bili2_large.json:bilibili-BV1Kyas6wEuz.json" \
  --max-cer 0.05
```

产出 fixture 是**真实 ASR 结果的裁剪版**（只留分段与上下文，1–5 KB），因此**不需要音频/模型**即可守金标。
`small` 档位**不设达标闸门**（它本来就高于 0.05），但**设基线闸门**：

```bash
run_benchmark.py --case ...(四例)... --baseline eval/baselines.json
```

即：`large` 要"够好"，**所有档位都不许变差**（改善会打印 `IMPROVED` 并提示更新基线）。
`eval/baselines.json` 记录每例的 CER 与容差。

## 归一模式：数字背后的隐藏维度（务必随数字一起看）

同一个 fixture、同一个模型，**归一模式不同，CER 就不同**：

| 用例 | `t2s:opencc+n`（装了 OpenCC） | `t2s:fallback+n`（缺依赖降级） |
|---|---|---|
| bili1-small | 0.1111 | **0.1282** |
| bili2-small | 0.2298 | **0.3662** |
| bili1-large / bili2-large | 0.0085 / 0.0051 | 相同（产出里无繁体字） |

因此：
- `run_benchmark.py` 的表里**必带 `norm` 列**，任何数字都要连同它一起引用；
- `eval/baselines.json` 按**真实模式**分别记录（`cer_by_mode`），每个环境各比各的；
- 报告的 `normalization.traditional_to_simplified` 是**行为判定**的真实值（`opencc` / `fallback`），
  不是"尝试过"的布尔值——曾经写死 `True`，正是它让跨环境基线永远对不上（D48）。

## 数字来源（每个 CER 都要能追溯）

| 数字 | 测于 | 归一模式 | 命令 |
|---|---|---|---|
| bili1-large 0.0085 / bili2-large 0.0051 | v1.23.0（首次记录基线） | 两种模式相同 | `run_benchmark.py --baseline eval/baselines.json` |
| bili1-small 0.1111 / bili2-small 0.2298 | v1.23.0，`t2s:opencc` 环境 | opencc | 同上（`norm` 列） |
| bili1-small 0.1282 / bili2-small 0.3662 | v1.23.0，CI 的 `t2s:fallback` 环境 | fallback | 同上 |
| 抖音 large-v3 CER 0.0187 | v1.7.0 路线图评测 | 数字归一开启 | `eval_asr.py --hypothesis <产出> --gold douyin-...json` |
| 标点 F1 0.313 | v1.18.0 | 同上 | 同上（`punctuation` 字段） |
| 46.4 分钟分块：93.5s vs 132.5s，相似度 0.9052 | v1.16.0 | 无关 | `docs/experiments/run_experiments.py` |

**目前每个用例只有一个数据点，因此不写趋势**；等同一用例在不同版本上重复测量后再在此表补行。

## 金标的证据强度（不要混用）

| `reference.kind` | 强度 | 用途 |
|---|---|---|
| `semi-automatic-draft` | 未核对 | 只能做 A/B 参考 |
| `agent-verified` | Agent 逐行判定，不确定项已标注 | 可作**基线**比较 |
| `human-verified` | 人工（含人耳）确认 | **验收基准** |

当前：抖音金标 = `human-verified`（人工核对过）；B站 金标 = `agent-verified`（第 12 行 unresolved）。

## 改动流程（固定动作）

1. 改之前先跑一次 `run_benchmark.py`，把表存下来（"改动前"）；
2. 改动 + 单测；
3. 再跑一次，逐列对比：**任何一列变差都要能解释**，解释不了就不合并；
4. 真实素材的结论写进 CHANGELOG 与 `docs/DECISIONS.md`，不放形容词。
