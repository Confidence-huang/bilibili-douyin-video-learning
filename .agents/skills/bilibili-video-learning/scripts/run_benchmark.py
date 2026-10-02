#!/usr/bin/env python3
r"""
跨金标回归：把"若干产出 vs 若干金标"一次跑成对照表（见 docs/DECISIONS.md D44）。

为什么需要它：
    此前每做一个改动，都要手工拼 `eval_asr.py --hypothesis X --gold Y`，再自己把数字抄到一起——
    于是"这次比上次好还是差"只能靠记忆，跨平台（抖音/B站）、跨模型、跨档位的对比尤其容易漏。
    一条命令给出全表，改动前后各跑一次就能看出回归。

用法：
    python run_benchmark.py --case "bili-large=/tmp/bili_lv3.json:eval/gold/bilibili-BV1ntah6TEe9.json" \
                            --case "bili-small=/tmp/bili_small.json:eval/gold/bilibili-BV1ntah6TEe9.json" \
                            --max-cer 0.05
    --case 可重复；格式为 `名称=产出.json:金标.json`（金标可用相对 eval/gold/ 的文件名）。
    `--max-cer` 是闸门：任一用例超标则退出码 1（供 CI/发布前检查用）。

边界：
    - 只做"评测 + 汇总 + 闸门"，**不下载素材、不跑 ASR**（那是别处的职责）；
    - 金标里的 `media.url` 只用于人看，评测不联网。
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import argparse  # 稳定命令行契约
import re  # 版本号格式校验（v1.29.0 这类必须能排序比较）
import json  # 结果与表格都能机读
import sys  # 退出码
import time  # 历史记录的时间戳
from pathlib import Path  # 路径处理
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_asr  # 复用同一套指标，绝不重写第二份实现


GOLD_DIR = SCRIPT_DIR.parent / "eval" / "gold"
COLUMNS = ("case", "cer", "sub", "del", "ins", "coverage", "hallucination", "timeline_median", "punct_f1",
           "norm")


# --- 解析 `名称=产出:金标`，金标允许只写文件名（默认在 eval/gold/ 下找）---
def _resolve_gold(gold_path: str) -> Path:
    """金标可以只写文件名（默认在 eval/gold/ 下找），也可以是绝对路径。"""
    candidate = Path(gold_path)
    if candidate.exists() or candidate.is_absolute():
        return candidate
    return GOLD_DIR / gold_path


def parse_case(spec: str) -> Dict[str, Any]:
    r"""解析 `名称=产出.json:金标.json`。

    **不能简单地按最后一个冒号切**：Windows 盘符本身带冒号（如 `D:\...`），
    两个绝对路径拼在一起时，最后一个冒号可能属于金标路径的盘符 —— 这正是 Windows CI 抓到的 bug
    （产出路径被切成 `...bili1_large.json:D`，退出码 2）。

    规则：从所有冒号里挑一个能让**两侧都存在文件**的分割点；挑不到才退回最后一个冒号
    （文件确实缺失时保持原行为，由调用方报错）。
    """
    if "=" not in spec:
        raise ValueError(f"用例格式应为 名称=产出.json:金标.json，收到 {spec!r}")
    name, rest = spec.split("=", 1)
    colons = [index for index, char in enumerate(rest) if char == ":"]
    if not colons:
        raise ValueError(f"用例格式应为 名称=产出.json:金标.json，收到 {spec!r}")

    chosen = None
    for index in colons:
        hypothesis_path, gold_path = rest[:index], rest[index + 1:]
        if hypothesis_path and gold_path and Path(hypothesis_path).exists() and _resolve_gold(gold_path).exists():
            chosen = (hypothesis_path, gold_path)
            break
    if chosen is None:
        index = colons[-1]
        chosen = (rest[:index], rest[index + 1:])
    return {"case": name.strip(), "hypothesis": Path(chosen[0]), "gold": _resolve_gold(chosen[1])}


# --- 取第一个存在的键（评测报告的字段名跨版本变过，这里防御式取值，不猜死一个）---
def _first(mapping: Dict[str, Any], *keys: str):
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping[key]
    return None


def _normalization_label(normalization: Dict[str, Any]) -> str:
    """把归一模式压成一个短标签：`t2s:opencc+n` / `t2s:fallback+n`。"""
    traditional = str(normalization.get("traditional_to_simplified", "?"))
    numerals = "n" if normalization.get("numerals") else "-"
    return f"t2s:{traditional}+{numerals}"


def _timeline_median(timeline: Any):
    """时间轴中位：报告里可能存标量，也可能只存偏移列表（打印时才算中位），两种都要兜住。"""
    scalar = _first(timeline or {}, "median_seconds", "median_offset_seconds", "median_offset")
    if scalar is not None:
        return scalar
    found = _search_number(timeline, ("median",))
    if found is not None:
        return found
    for value in (timeline or {}).values():
        if isinstance(value, list) and value and all(isinstance(item, (int, float)) for item in value):
            ordered = sorted(value)
            return round(ordered[len(ordered) // 2], 3)
    return None


def _search_number(mapping: Any, hints, depth: int = 0):
    """按语义提示词在（最多三层的）嵌套里找数字：字段名改过几次，猜死一个键名迟早失配。"""
    if not isinstance(mapping, dict) or depth > 2:
        return None
    for key, value in mapping.items():
        if isinstance(value, (int, float)) and any(hint in str(key).lower() for hint in hints):
            return value
        found = _search_number(value, hints, depth + 1)
        if found is not None:
            return found
    return None


# --- 评一个用例：复用 eval_asr 的报告 + 标点指标 ---
def evaluate_case(case: Dict[str, Any]) -> Dict[str, Any]:
    report = eval_asr.evaluate(case["hypothesis"], case["gold"])
    gold_payload = json.loads(case["gold"].read_text(encoding="utf-8"))
    reference_text = (gold_payload.get("reference") or {}).get("text", "")
    hypothesis_text = ""
    for key in ("text", "full_text"):
        if isinstance(report.get("_hypothesis_text"), str):
            break
    try:
        hypothesis_payload = json.loads(Path(case["hypothesis"]).read_text(encoding="utf-8"))
        hypothesis_text = hypothesis_payload.get("text") or hypothesis_payload.get("full_text") or ""
        if not hypothesis_text:
            hypothesis_text = "".join(str(item.get("text") or item.get("content") or "")
                                      for item in hypothesis_payload.get("segments") or [])
    except Exception:
        hypothesis_text = ""
    character = report.get("character_error_rate") or {}
    timeline = report.get("timeline_offset") or report.get("timeline") or {}   # 真实键名是 timeline_offset（看过结构才写死）
    return {
        "case": case["case"],
        "cer": character.get("cer"),
        "sub": character.get("substitutions"), "del": character.get("deletions"), "ins": character.get("insertions"),
        "coverage": (report.get("coverage") or {}).get("coverage"),
        "hallucination": _first(report.get("hallucination") or {}, "characters_per_minute", "rate", "per_minute")
                          if _first(report.get("hallucination") or {}, "characters_per_minute", "rate", "per_minute")
                          is not None else _search_number(report.get("hallucination"), ("minute", "rate")),
        "timeline_median": _timeline_median(timeline),
        "punct_f1": (report.get("punctuation") or {}).get("f1"),
        # 归一模式决定数字：OpenCC 缺失时繁简归一降级，CER 会不同（所以必须与 CER 并排显示）
        "norm": _normalization_label(report.get("normalization") or {}),
    }


# --- 渲染成表格（人看）---
def render_table(rows: List[Dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in rows:
        lines.append("| " + " | ".join(
            "None" if row.get(column) is None else str(row.get(column)) for column in COLUMNS) + " |")
    return "\n".join(lines)


VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")          # 版本必须能被排序比较，别写 "latest" 这类字样


def existing_summary_keys(target: Path) -> set:
    """已写进趋势表的键 (版本, 用例, 模式)：让汇总**幂等**，重复执行不会写第二遍。

    版本号归一化：表里可能写 `v1.29.0` 也可能写 `1.29.0`，两侧都去掉 `v` 再比较，
    否则幂等检查永远不命中（这正是我第一版写错的地方）。
    """
    if not target.exists():
        return set()
    keys = set()
    for line in target.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 6 and VERSION_PATTERN.match(cells[0].lstrip("v")) and cells[1]:
            keys.add((cells[0].lstrip("v"), cells[1], cells[2]))
    return keys


# --- 发版汇总：从历史取每个 用例×模式 的最新值，追加成趋势表的一行（D53）---
def release_summary_rows(entries: List[Dict[str, Any]], release: str) -> List[str]:
    latest: Dict[tuple, Dict[str, Any]] = {}
    for entry in entries:
        if entry.get("cer") is None:
            continue
        latest[(entry.get("case"), entry.get("norm"))] = entry            # 后出现的覆盖先出现的 = 最新
    rows = []
    for (case, norm), entry in sorted(latest.items()):
        rows.append(f"| {release} | {case} | {norm} | {entry['cer']} | {entry.get('coverage')} | {entry['timestamp']} |")
    return rows


# --- 趋势闸门：同一 用例×模式 的末次是否比上一次劣化超过容差（D54）---
def history_regressions(entries: List[Dict[str, Any]], tolerance: float = 0.005) -> List[str]:
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("cer") is not None:
            grouped.setdefault((entry.get("case"), entry.get("norm")), []).append(entry)
    problems = []
    for (case, norm), items in sorted(grouped.items()):
        if len(items) < 2:
            continue                                                       # 只有一次测量不构成趋势
        previous, last = items[-2]["cer"], items[-1]["cer"]
        if last > previous + tolerance:
            problems.append(f"{case} [{norm}]: CER 劣化 {last} > 上次 {previous} + 容差 {tolerance}")
    return problems


# --- 从历史 JSONL 生成趋势表：每个 用例×模式 一行，给出首末值与差值（D52）---
def render_history_report(entries: List[Dict[str, Any]]) -> str:
    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
    for entry in entries:
        key = (entry.get("case"), entry.get("norm"))
        if entry.get("cer") is not None:
            grouped.setdefault(key, []).append(entry)
    if not grouped:
        return "（历史为空：先跑 run_benchmark.py --record-history <file.jsonl> 记录几次）"
    columns = ("case", "norm", "runs", "first_cer", "last_cer", "delta", "first_at", "last_at")
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for (case, norm), items in sorted(grouped.items()):
        first, last = items[0], items[-1]
        delta = round(last["cer"] - first["cer"], 4)
        lines.append("| " + " | ".join(str(value) for value in (
            case, norm, len(items), first["cer"], last["cer"], f"{delta:+}", first.get("timestamp"), last.get("timestamp"))) + " |")
    return "\n".join(lines)


# --- 与基线记录比较：劣化失败，改善要显式报出来（D47）---
def compare_baseline(rows: List[Dict[str, Any]], baseline: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    recorded = {item["case"]: item for item in (baseline.get("cases") or [])}
    for row in rows:
        reference = recorded.get(row["case"])
        if reference is None:
            problems.append(f"{row['case']}: 基线里没有这个用例（新增用例请先记录基线）")
            continue
        expected = reference.get("cer")
        by_mode = reference.get("cer_by_mode") or {}
        if by_mode:                                                       # 优先按"归一模式"取基线（跨环境可比）
            expected = by_mode.get(row.get("norm"))
            if expected is None:
                problems.append(f"{row['case']}: 基线里没有模式 {row.get('norm')} 的记录（新环境请先记录）")
                continue
        tolerance = reference.get("tolerance", 0.005)
        if row.get("cer") is None or expected is None:
            problems.append(f"{row['case']}: CER 缺失，无法与基线比较")
            continue
        if row["cer"] > expected + tolerance:
            problems.append(f"{row['case']}: CER 劣化 {row['cer']} > 基线 {expected} + 容差 {tolerance}")
        elif row["cer"] < expected - tolerance:
            print(f"IMPROVED: {row['case']} CER {expected} -> {row['cer']}（请更新 eval/baselines.json）")
    return problems


# --- CLI ---
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run several hypothesis-vs-gold cases into one comparison table.")
    parser.add_argument("--case", action="append",
                        help="名称=产出.json:金标.json（金标可只写文件名，默认在 eval/gold/ 下找），可重复；"
                             "只有历史/汇总类命令可以不传")
    parser.add_argument("--max-cer", type=float, help="闸门：任用例 CER 超过该值即返回 1（供 CI/发版前用）")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON")
    parser.add_argument("--baseline", help="基线记录文件：读每个用例记录的 CER 与容差，劣化即失败（D47）")
    parser.add_argument("--record-history", help="把本次结果追加到 JSONL 历史文件（每次测量一行，便于日后看趋势）")
    parser.add_argument("--history-report", help="读取 JSONL 历史并打印趋势表（每个 用例×模式 一行）")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="配合 --history-report：末次比上次劣化超过容差即返回 1（看趋势，而不是只看单点基线）")
    parser.add_argument("--regression-tolerance", type=float, default=0.005, help="趋势闸门容差，默认 0.005")
    parser.add_argument("--append-release-summary", help="把历史里每个 用例×模式 的最新值追加到趋势表文件（发版时用）")
    parser.add_argument("--release-version", help="配合 --append-release-summary：本次发布的版本号，如 1.28.0")
    parser.add_argument("--write-baseline", action="store_true",
                        help="把本次结果写回基线文件（改善后一键落库；CI 不要用，见 D49）")
    args = parser.parse_args(argv)

    if args.append_release_summary:
        history_path = Path(args.append_release_summary)
        target_path = Path(args.append_release_summary).parent.parent / "docs" / "BENCHMARKS.md"
        if not history_path.exists():
            print(json.dumps({"error": f"历史文件不存在：{history_path}"}, ensure_ascii=False))
            return 2
        if not args.release_version:
            print(json.dumps({"error": "--append-release-summary 需要 --release-version"}, ensure_ascii=False))
            return 2
        if not VERSION_PATTERN.match(args.release_version):
            print(json.dumps({"error": f"--release-version 需要形如 1.29.0，收到 {args.release_version!r}"},
                             ensure_ascii=False))
            return 2
        entries = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = release_summary_rows(entries, args.release_version)
        if not rows:
            print("（历史为空：没有可汇总的行）")
            return 0
        if not target_path.exists():                                      # 首次创建时给出表头与说明
            target_path.write_text("# 基准趋势（发版汇总）\n\n"
                                   "每次发版时把历史里每个 用例×模式 的**最新值**追加一行（来源：CI 上传的 "
                                   "`benchmark-history` artifact；命令 `run_benchmark.py --append-release-summary`）。\n\n"
                                   "| 版本 | 用例 | 归一模式 | CER | 覆盖率 | 记录时间 |\n|---|---|---|---|---|---|\n",
                                   encoding="utf-8")
        already = existing_summary_keys(target_path)
        fresh = []
        for row in rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if (cells[0].lstrip("v"), cells[1], cells[2]) not in already:
                fresh.append(row)
        if not fresh:
            print(f"（已存在 v{args.release_version} 的行，未重复写入）")
            return 0
        with target_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(fresh) + "\n")
        print(f"APPENDED {len(fresh)} row(s) -> {target_path}")
        return 0
    if args.history_report:
        history_path = Path(args.history_report)
        if not history_path.exists():
            print(json.dumps({"error": f"历史文件不存在：{history_path}"}, ensure_ascii=False))
            return 2
        entries = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(render_history_report(entries))
        if args.fail_on_regression:
            problems = history_regressions(entries, args.regression_tolerance)
            if problems:
                print("FAILED (trend): " + "; ".join(problems), file=sys.stderr)
                return 1
        return 0
    if not args.case:
        print(json.dumps({"error": "需要 --case（或使用 --history-report / --append-release-summary）"},
                         ensure_ascii=False))
        return 2

    try:
        cases = [parse_case(spec) for spec in args.case]
        missing = [str(case[key]) for case in cases for key in ("hypothesis", "gold") if not Path(case[key]).exists()]
        if missing:
            raise FileNotFoundError("找不到这些文件：" + ", ".join(sorted(set(missing))))
        rows = [evaluate_case(case) for case in cases]
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2

    print(json.dumps(rows, ensure_ascii=False, indent=1) if args.json else render_table(rows))
    if args.record_history:
        history_path = Path(args.record_history)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with history_path.open("a", encoding="utf-8") as handle:              # 只追加，不改写历史
            for row in rows:
                handle.write(json.dumps({"timestamp": stamp, **row}, ensure_ascii=False) + "\n")
        print(f"RECORDED history: {history_path}（{len(rows)} 行）")
    if args.baseline:
        baseline_path = Path(args.baseline)
        baseline_path = baseline_path if baseline_path.exists() else GOLD_DIR.parent / args.baseline
        try:
            baseline_document = json.loads(baseline_path.read_text(encoding="utf-8"))
            problems = compare_baseline(rows, baseline_document)
            if args.write_baseline:                                              # 先落库、再判定（首次记录不该被判失败）
                for row in rows:
                    if row.get("cer") is None:
                        continue
                    for item in baseline_document.get("cases") or []:
                        if item.get("case") == row["case"]:
                            item.setdefault("cer_by_mode", {})[row.get("norm")] = row["cer"]
                baseline_path.write_text(json.dumps(baseline_document, ensure_ascii=False, indent=1), encoding="utf-8")
                problems = compare_baseline(rows, baseline_document)             # 用落库后的记录重新判定
                print(f"WROTE baseline: {baseline_path}（本次结果已落库）")
        except Exception as exc:
            print(json.dumps({"error": f"基线读取失败：{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            return 2
        if problems:
            print("FAILED (baseline): " + "; ".join(problems), file=sys.stderr)
            return 1
    if args.max_cer is not None:
        failed = [row for row in rows if row.get("cer") is not None and row["cer"] > args.max_cer]
        if failed:
            print(f"FAILED: {len(failed)} case(s) exceed --max-cer {args.max_cer}: "
                  + ", ".join(f"{row['case']}={row['cer']}" for row in failed), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
