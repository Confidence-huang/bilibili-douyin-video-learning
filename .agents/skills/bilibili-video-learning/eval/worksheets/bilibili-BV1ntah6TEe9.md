# 金标核对清单：bilibili-BV1ntah6TEe9

- 段落数：**15**；可疑段落共 **15** 个，本清单按优先级列出前 **8** 个（其余请在 JSON 的 review.flagged_paragraphs 之外抽查；这样清单不会长到没人看）
- 与另一配置的相似度：**0.8632**
- 初稿来源：初稿 = faster-whisper large；分歧清单 = faster-whisper small；**等待人工核对**

## 逐段（⚠ = 需要你确认）

| # | 时间 | 标记 | 正文 |
|---|---|---|---|
| 0 | 0.0–1.82s | ⚠ disagreement_substantive | 钻玉米地不滑脸教程 |
| 1 | 1.82–3.92s | ⚠ disagreement_substantive | 秋衣展开遮住脸 |
| 2 | 3.92–5.86s | ⚠ disagreement_substantive | 长袖绕后打成结 |
| 3 | 5.86–7.84s | ⚠ disagreement_with_secondary | 衣服下摆向上翻 |
| 4 | 7.84–9.8s |  | 面前衣服向下拉 |
| 5 | 9.8–11.5s |  | 微调一下头顶 |
| 6 | 11.5–13.56s |  | 确保眼睛能露出来 |
| 7 | 13.56–15.68s |  | 最后兜里掏出墨镜 |
| 8 | 15.68–18.38s |  | 戴到脸上保护眼睛 |
| 9 | 18.38–19.92s |  | 一见秋衣就解决了 |
| 10 | 19.92–21.3s |  | 玉米叶滑脸的问题 |
| 11 | 21.3–24.12s | ⚠ disagreement_substantive | 不管是在玉米地里穿行 |
| 12 | 24.12–26.34s | ⚠ disagreement_substantive | 还是在里面巴不敢缠 |
| 13 | 26.34–28.38s | ⚠ disagreement_substantive | 秋衣往脸上一套 |
| 14 | 28.38–30.6s | ⚠ disagreement_substantive | 玉米叶将不会存在 |

## 分歧清单（两个配置写出不同内容的地方）

| 类型 | 时间 | 主配置 | 次配置 |
|---|---|---|---|
| None | 0.0–1.82s | 地 | 蒂 |
| None | 1.82–3.92s | 遮住 | 蜘蛛 |
| None | 3.92–5.86s | 结 | 截 |
| None | 9.8–11.5s | 调 | 条 |
| None | 15.68–18.38s | 戴 | 带 |
| None | 18.38–19.92s | 见 | 件 |
| None | 19.92–21.3s | 叶 | 蒂 |
| None | 21.3–24.12s | 地 | 蒂 |
| None | 24.12–26.34s | 巴不敢缠 | 发布感禅 |
| None | 28.38–30.6s | 叶将 | 葉將 |
| None | 28.38–30.6s | 会 | 不 |

## 完成步骤

1. 逐行核对被标记的段落（低置信 / 复读嫌疑 / 两个配置分歧），其余行抽查即可
2. 确认专有名词与术语（人名、机构、产品名），必要时补进 references/asr-lexicon.txt
3. 确认数字：本工具只标不改，数字写法差异已由评测口径归一（见 D33）
4. 补上被漏掉的整句（覆盖率兜底只看时间轴，看不出一句话被吞掉）
5. 改完后把 reference.kind 从 semi-automatic-draft 改成 human-verified，并删掉 review 块
