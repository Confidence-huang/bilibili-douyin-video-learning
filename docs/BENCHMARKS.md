# 基准趋势（发版汇总）

每次发版时把历史里每个 `用例 × 归一模式` 的**最新值**追加一行（来源：CI 上传的 `benchmark-history` artifact；
命令 `run_benchmark.py --append-release-summary <history.jsonl> --release-version X`）。
**历史文件不入库**（它是测量台账），入库的只有这份发版汇总。

下表前几行是**回溯填写**的已实测数字（标注它们实际测于哪个版本），不是自动生成的——
自动汇总从 v1.28.0 起生效。

| 版本 | 用例 | 归一模式 | CER | 覆盖率 | 记录时间 |
|---|---|---|---|---|---|
| v1.17.0 | bilibili-BV1ntah6TEe9 (large) | t2s:opencc+n | 0.0085 | 0.999 | 2026-09-29（金标建立时实测） |
| v1.17.0 | bilibili-BV1ntah6TEe9 (small) | t2s:opencc+n | 0.1111 | 0.8501 | 同上 |
| v1.17.0 | bilibili-BV1Kyas6wEuz (large) | t2s:opencc+n | 0.0051 | 0.9982 | 2026-09-29（第二支金标） |
| v1.17.0 | bilibili-BV1Kyas6wEuz (small) | t2s:opencc+n | 0.2298 | 0.9236 | 同上 |
| v1.23.0 | bilibili-BV1ntah6TEe9 (small) | t2s:fallback+n | 0.1282 | 0.8501 | 2026-09-29（CI 环境实测） |
| v1.23.0 | bilibili-BV1Kyas6wEuz (small) | t2s:fallback+n | 0.3662 | 0.9236 | 同上 |

> 抖音金标（`large-v3`，数字归一开启）CER **0.0187**、覆盖率 0.9976，测于 v1.7.0 路线图评测；
> 标点 F1 **0.313** 测于 v1.18.0。它们不在上表内，因为上表按"发版自动汇总"的粒度记录。
