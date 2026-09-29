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
`small` 档位故意不设闸门（它的 CER 本来就高于 0.05），只作对照。

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
