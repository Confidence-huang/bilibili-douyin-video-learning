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
import json  # 结果与表格都能机读
import sys  # 退出码
from pathlib import Path  # 路径处理
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_asr  # 复用同一套指标，绝不重写第二份实现


GOLD_DIR = SCRIPT_DIR.parent / "eval" / "gold"
COLUMNS = ("case", "cer", "sub", "del", "ins", "coverage", "hallucination", "timeline_median", "punct_f1")


# --- 解析 `名称=产出:金标`，金标允许只写文件名（默认在 eval/gold/ 下找）---
def parse_case(spec: str) -> Dict[str, Any]:
    if "=" not in spec or ":" not in spec.split("=", 1)[1]:
        raise ValueError(f"用例格式应为 名称=产出.json:金标.json，收到 {spec!r}")
    name, paths = spec.split("=", 1)
    hypothesis_path, gold_path = paths.rsplit(":", 1)
    gold = Path(gold_path)
    if not gold.exists() and not gold.is_absolute():
        gold = GOLD_DIR / gold_path
    return {"case": name.strip(), "hypothesis": Path(hypothesis_path), "gold": gold}


# --- 取第一个存在的键（评测报告的字段名跨版本变过，这里防御式取值，不猜死一个）---
def _first(mapping: Dict[str, Any], *keys: str):
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping[key]
    return None


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
    }


# --- 渲染成表格（人看）---
def render_table(rows: List[Dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in rows:
        lines.append("| " + " | ".join(
            "None" if row.get(column) is None else str(row.get(column)) for column in COLUMNS) + " |")
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
        expected, tolerance = reference.get("cer"), reference.get("tolerance", 0.005)
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
    parser.add_argument("--case", action="append", required=True,
                        help="名称=产出.json:金标.json（金标可只写文件名，默认在 eval/gold/ 下找），可重复")
    parser.add_argument("--max-cer", type=float, help="闸门：任用例 CER 超过该值即返回 1（供 CI/发版前用）")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON")
    parser.add_argument("--baseline", help="基线记录文件：读每个用例记录的 CER 与容差，劣化即失败（D47）")
    args = parser.parse_args(argv)

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
    if args.baseline:
        baseline_path = Path(args.baseline)
        baseline_path = baseline_path if baseline_path.exists() else GOLD_DIR.parent / args.baseline
        try:
            problems = compare_baseline(rows, json.loads(baseline_path.read_text(encoding="utf-8")))
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
