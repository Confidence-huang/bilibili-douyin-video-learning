#!/usr/bin/env python3
r"""
转写评测：把"更准了"变成可复现的数字（CER / 幻觉率 / 覆盖率 / 时间轴偏移 / 实时率）。

为什么需要它（本轮评估的第一结论）：
    此前所有结论都来自临时人工核对——知道"雪包应该是血包"，却无法回答"这次改动让错误少了几个"。
    没有度量，任何调参都是盲改：改大 beam、加词典、换模型档位，谁有效、代价多少，全靠感觉。
    所以度量必须先于优化落地，并且金标必须**只存文本与时间轴**（仓库禁止 .wav/.mp4 入库），
    复现时用金标里的 media.url 重新取流即可。

指标口径（都按"人工校对成本"来定义，而不是按模型指标好看）：
    - CER：字符级错误率 = (替换+删除+插入) / 参考字数；中文以字为单位比 WER 更贴近校对成本。
    - 幻觉率：参考文本里**完全不存在**的新增片段（≥ min_run 字）字数 / 分钟；这是"看起来通顺但纯属编造"的量。
    - 覆盖率：假设时间轴在 [0, 音频时长] 内的并集占比；缺口就是"某段话根本没被转写"。
    - 时间轴偏移：每段参考文本匹配到的最优假设段落的起始时间差（中位数/最大），衡量"内容对但时间漂"。
    - 实时率 RTF：耗时 / 音频时长；GPU 上越小越好（本机 large-v3 ≈ 0.19，即 5.3× 实时）。

调用示例：
    python eval_asr.py --hypothesis asr.json --gold eval/gold/douyin-...json --elapsed-seconds 60.5
    python eval_asr.py --hypothesis-dir out/ --gold-set eval/gold --json
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import argparse  # 稳定的命令行契约：CI 与本机复现都用同一条命令
import difflib  # 字符级对齐是 CER 与幻觉判定的共同基础
import json  # 报告输出为 JSON，便于与历史数字对比
import os  # 批量模式遍历目录
import re  # 规范化与 SRT 解析
import statistics  # 时间轴偏移取中位数，避免个别段位拖偏结论
import sys  # 退出码区分"指标超标"与"输入有问题"
from pathlib import Path  # 统一处理 Windows/WSL 路径
from typing import Any, Dict, Iterable, List, Optional

# --- 让脚本既能在仓库里运行，也能被测试动态加载 ---
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CJK_LATIN_DIGITS = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]+")  # 只保留汉字/字母/数字
FULLWIDTH = str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
                          "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")
DEFAULT_MIN_RUN = 2          # 单字新增多半是语气/同音噪声，≥2 字才计为"编造片段"
DEFAULT_CER_BUDGET = 0.10    # 逐字稿场景的验收线：字符错误率 ≤10%


# --- 繁简转换器只建一次（时间轴匹配会对上千对文本调用规范化） ---
_SIMPLIFIER_CACHE: List[Any] = []


def _simplify(text: str) -> str:
    if not _SIMPLIFIER_CACHE:
        try:
            import normalize_transcript

            _SIMPLIFIER_CACHE.append(normalize_transcript.load_simplifier())
        except Exception:
            _SIMPLIFIER_CACHE.append(None)                   # 缺 OpenCC 时退化为不转换（与 D22 的降级原则一致）
    converter = _SIMPLIFIER_CACHE[0]
    if converter is None:
        return text
    try:
        return converter.convert(text)
    except Exception:
        return text


# --- 规范化：只留下会被人工校对的字符 ---
def normalize_text(text: str, *, simplify: bool = True) -> str:
    cleaned = (text or "").translate(FULLWIDTH)
    cleaned = CJK_LATIN_DIGITS.sub("", cleaned)
    return _simplify(cleaned) if simplify else cleaned


# --- 读入任意一种产出格式，统一成规范分段 ---
def load_segments(path: str | Path) -> List[Dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8", errors="replace")
    if source.suffix.lower() == ".srt":
        return load_srt_segments(text)
    if source.suffix.lower() == ".txt":
        return [{"start": 0.0, "end": 0.0, "text": text}]
    payload = json.loads(text)
    return segments_from_payload(payload)


# --- 从 JSON 载荷里认出游两种历史形状 ---
def segments_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = payload.get("paragraphs") or payload.get("segments") or payload.get("reference", {}).get("paragraphs") or []
        if not raw and isinstance(payload.get("reference"), dict):
            reference = payload["reference"]
            return [{"start": 0.0, "end": 0.0, "text": reference.get("text", "")}]
    else:
        raw = []
    segments = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start", item.get("from", 0.0))
        end = item.get("end", item.get("to", 0.0))
        content = item.get("text", item.get("content", ""))
        segments.append({"start": float(start or 0.0), "end": float(end or 0.0), "text": str(content or "")})
    return segments


# --- SRT 解析（评测也要能吃字幕产物） ---
def load_srt_segments(text: str) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        match = None
        body_start = 0
        for index, line in enumerate(lines):
            match = SRT_TIME.search(line)
            if match:
                body_start = index + 1
                break
        if not match:
            continue
        start = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
        end = int(match.group(5)) * 3600 + int(match.group(6)) * 60 + int(match.group(7)) + int(match.group(8)) / 1000
        segments.append({"start": start, "end": end, "text": "".join(lines[body_start:])})
    return segments


# --- 字符级编辑统计（CER 与幻觉的同一份底稿） ---
def edit_operations(hypothesis: str, reference: str) -> Dict[str, Any]:
    """注意方向：a=假设、b=参考。因此 opcode 的语义与直觉相反，必须显式翻译一次。

    - replace：两侧都有但不同 → 替换
    - insert（参考有、假设没有）→ **漏字**（VAD 丢段、模型吞字的签名）
    - delete（假设有、参考没有）→ **多字**（幻觉候选：编造、串词、重复）
    """
    matcher = difflib.SequenceMatcher(a=hypothesis, b=reference, autojunk=False)
    substitutions = missing = extra = 0
    extra_runs: List[str] = []
    missing_runs: List[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            substitutions += max(i2 - i1, j2 - j1)
        elif tag == "insert":                                  # 参考里有、假设里缺
            missing += j2 - j1
            missing_runs.append(reference[j1:j2])
        elif tag == "delete":                                  # 假设里多出来
            extra += i2 - i1
            extra_runs.append(hypothesis[i1:i2])
    return {"substitutions": substitutions, "deletions": missing, "insertions": extra,
            "extra_runs": extra_runs, "missing_runs": missing_runs}


# --- CER：分母用参考字数，插入也算错（否则"多说话"会被奖励） ---
def character_error_rate(hypothesis: str, reference: str) -> Dict[str, Any]:
    reference_chars = len(reference)
    operations = edit_operations(hypothesis, reference)
    errors = operations["substitutions"] + operations["deletions"] + operations["insertions"]
    return {
        "cer": round(errors / reference_chars, 4) if reference_chars else None,
        "reference_chars": reference_chars,
        "hypothesis_chars": len(hypothesis),
        "errors": errors,
        "substitutions": operations["substitutions"],
        "deletions": operations["deletions"],              # 漏字：参考里有、假设里没有
        "insertions": operations["insertions"],            # 多字：假设里编造/串词
        "missing_runs": operations["missing_runs"],
        "extra_runs": operations["extra_runs"],
    }


# --- 幻觉：参考里完全不存在的新增片段字数与每分钟速率 ---
def hallucination_rate(hypothesis: str, reference: str, duration_seconds: Optional[float],
                       *, min_run: int = DEFAULT_MIN_RUN) -> Dict[str, Any]:
    operations = edit_operations(hypothesis, reference)
    runs = [run for run in operations["extra_runs"] if len(run) >= min_run]  # 幻觉 = 假设多出来的内容
    chars = sum(len(run) for run in runs)
    minutes = (duration_seconds or 0) / 60 if duration_seconds else None
    return {
        "hypothesis_runs": len(runs),
        "hypothesis_chars": chars,
        "min_run": min_run,
        "chars_per_minute": round(chars / minutes, 2) if minutes else None,
        "examples": runs[:5],
    }


# --- 覆盖率：假设时间轴的并集占参考时长的比例（重叠段只算一次） ---
def timeline_coverage(segments: Iterable[Dict[str, Any]], duration_seconds: Optional[float]) -> Dict[str, Any]:
    duration = float(duration_seconds or 0)
    intervals = sorted((max(0.0, float(s.get("start", 0.0))), float(s.get("end", 0.0))) for s in segments)
    merged: List[List[float]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(min(end, duration) - min(start, duration) for start, end in merged) if duration else sum(e - s for s, e in merged)
    return {
        "duration_seconds": duration or None,
        "covered_seconds": round(covered, 2),
        "coverage": round(covered / duration, 4) if duration else None,
        "segments": len(list(intervals)),
    }


# --- 逐字符时间映射：把假设文本的每个字符位置换算成时间 ---
def build_character_timeline(segments: List[Dict[str, Any]]) -> tuple[str, List[float]]:
    normalized_parts: List[str] = []
    times: List[float] = []
    for segment in segments:
        text = normalize_text(segment.get("text", ""))
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", 0.0))
        span = max(end - start, 0.0)
        normalized_parts.append(text)
        for index in range(len(text)):
            ratio = index / max(len(text) - 1, 1)          # 段内按字符位置线性插值
            times.append(start + span * ratio)
    return "".join(normalized_parts), times


# --- 时间轴偏移：先做真对齐（最长公共块），再比较"同一句话"的时间差 ---
def timeline_offset(hypothesis: List[Dict[str, Any]], reference: List[Dict[str, Any]],
                    *, min_match_ratio: float = 0.5) -> Dict[str, Any]:
    hypothesis_text, times = build_character_timeline(hypothesis)
    if not hypothesis_text:
        return {"matched": 0, "considered": 0, "median_seconds": None, "max_seconds": None}

    deltas: List[float] = []
    considered = 0
    for gold in reference:
        gold_text = normalize_text(gold.get("text", ""))
        if not gold_text:
            continue
        considered += 1
        matcher = difflib.SequenceMatcher(a=gold_text, b=hypothesis_text, autojunk=False)
        match = matcher.find_longest_match(0, len(gold_text), 0, len(hypothesis_text))
        if match.size < max(4, int(len(gold_text) * min_match_ratio)):
            continue                                       # 匹配太短说明这段话在假设里根本没被转写
        deltas.append(abs(times[match.b] - float(gold.get("start", 0.0))))
    if not deltas:
        return {"matched": 0, "considered": considered, "median_seconds": None, "max_seconds": None}
    return {"matched": len(deltas), "considered": considered,
            "median_seconds": round(statistics.median(deltas), 2), "max_seconds": round(max(deltas), 2)}


# --- 把一次评测的输入、输出与耗时汇总成一份报告 ---
def evaluate(hypothesis_path: str | Path, gold_path: str | Path, *, elapsed_seconds: Optional[float] = None,
             min_run: int = DEFAULT_MIN_RUN) -> Dict[str, Any]:
    gold_payload = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    gold_segments = (gold_payload.get("reference", {}).get("paragraphs")
                     if isinstance(gold_payload, dict) else None) or segments_from_payload(gold_payload)
    duration = None
    if isinstance(gold_payload, dict):
        duration = (gold_payload.get("media") or {}).get("duration_seconds")

    hypothesis_segments = load_segments(hypothesis_path)
    hypothesis_text = "".join(segment.get("text", "") for segment in hypothesis_segments)
    reference_text = (gold_payload.get("reference", {}).get("text") if isinstance(gold_payload, dict) else "") \
        or "".join(segment.get("text", "") for segment in gold_segments)

    normalized_hypothesis = normalize_text(hypothesis_text)
    normalized_reference = normalize_text(reference_text)
    report = {
        "gold_id": gold_payload.get("id") if isinstance(gold_payload, dict) else Path(gold_path).stem,
        "platform": gold_payload.get("platform") if isinstance(gold_payload, dict) else None,
        "hypothesis": str(hypothesis_path),
        "character_error_rate": character_error_rate(normalized_hypothesis, normalized_reference),
        "hallucination": hallucination_rate(normalized_hypothesis, normalized_reference, duration, min_run=min_run),
        "coverage": timeline_coverage(hypothesis_segments, duration),
        "timeline_offset": timeline_offset(hypothesis_segments, gold_segments),  # 真对齐后的时间差，不是段边界差
        "real_time_factor": None,
    }
    if elapsed_seconds and duration:
        report["real_time_factor"] = round(float(elapsed_seconds) / float(duration), 3)
    report["passed"] = bool(report["character_error_rate"]["cer"] is not None
                            and report["character_error_rate"]["cer"] <= DEFAULT_CER_BUDGET)
    return report


# --- Markdown 渲染：给人看的一行结论 + 指标表 ---
def render_markdown(report: Dict[str, Any]) -> str:
    cer = report["character_error_rate"]
    lines = [
        f"## {report.get('gold_id')}（{report.get('platform') or '?'}）",
        "",
        f"- CER：**{cer['cer']}**（{cer['errors']} 错 / {cer['reference_chars']} 参考字；"
        f"替换 {cer['substitutions']}、删除 {cer['deletions']}、插入 {cer['insertions']}）",
        f"- 幻觉：**{report['hallucination']['chars_per_minute']} 字/分钟**"
        f"（≥{report['hallucination']['min_run']} 字的新增片段 {report['hallucination']['hypothesis_runs']} 处）",
        f"- 覆盖率：**{report['coverage']['coverage']}**（{report['coverage']['covered_seconds']}s / {report['coverage']['duration_seconds']}s）",
        f"- 时间轴偏移：中位数 **{report['timeline_offset']['median_seconds']}s**、"
        f"最大 {report['timeline_offset']['max_seconds']}s"
        f"（匹配 {report['timeline_offset']['matched']}/{report['timeline_offset']['considered']} 段）",
        f"- 实时率 RTF：**{report['real_time_factor']}**",
        f"- 是否达到 {DEFAULT_CER_BUDGET:.0%} 的 CER 验收线：{'是' if report['passed'] else '否'}",
    ]
    if report["hallucination"]["examples"]:
        lines.append(f"- 幻觉样例：{'、'.join(report['hallucination']['examples'])}")
    return "\n".join(lines) + "\n"


# --- 批量：一个假设目录对一整个金标集 ---
def evaluate_directory(hypothesis_dir: str | Path, gold_dir: str | Path, *, elapsed_seconds: Optional[float] = None) -> List[Dict[str, Any]]:
    reports = []
    for gold_path in sorted(Path(gold_dir).glob("*.json")):
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        gold_id = gold.get("id") or gold_path.stem
        candidates = [p for p in sorted(Path(hypothesis_dir).glob("*")) if gold_id in p.name or gold_path.stem in p.name]
        if not candidates:
            continue
        reports.append(evaluate(candidates[0], gold_path, elapsed_seconds=elapsed_seconds))
    return reports


# --- CLI ---
def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate ASR output against a text-only gold set (CER / hallucination / coverage / RTF).")
    parser.add_argument("--hypothesis", help="待评测的产出：规范 JSON / ASR JSON / SRT / TXT")
    parser.add_argument("--gold", help="金标 JSON（只含文本与时间轴）")
    parser.add_argument("--hypothesis-dir", help="批量：假设文件目录（文件名需含金标 id）")
    parser.add_argument("--gold-set", default=str(SCRIPT_DIR.parent / "eval" / "gold"), help="批量：金标目录")
    parser.add_argument("--elapsed-seconds", type=float, help="本次转写耗时，用于计算 RTF")
    parser.add_argument("--min-run", type=int, default=DEFAULT_MIN_RUN, help="多长的插入片段才算幻觉（默认 2 字）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    parser.add_argument("-o", "--output", help="把报告写到文件")
    args = parser.parse_args()

    if args.hypothesis_dir:
        reports = evaluate_directory(args.hypothesis_dir, args.gold_set, elapsed_seconds=args.elapsed_seconds)
        payload: Any = reports
        text = "\n".join(render_markdown(report) for report in reports) or "没有匹配到任何金标"
    elif args.hypothesis and args.gold:
        report = evaluate(args.hypothesis, args.gold, elapsed_seconds=args.elapsed_seconds, min_run=args.min_run)
        payload, text = report, render_markdown(report)
    else:
        parser.error("需要 --hypothesis 与 --gold，或 --hypothesis-dir 与 --gold-set")

    rendered = json.dumps(payload, ensure_ascii=False, indent=1) if args.json else text
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"报告已写入 {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
