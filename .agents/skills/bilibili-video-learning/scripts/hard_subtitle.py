#!/usr/bin/env python3
r"""
把烧进画面的作者字幕（硬字幕）OCR 成带时间轴的卡片，并自检"我可能漏了什么"。

为什么需要它：
    大量中文口播视频不提供字幕轨道，而是把字幕直接烧进画面。它往往比 ASR 更权威——
    真实案例里，同一段 259.77 秒视频的硬字幕纠正了 ASR 的 14 处同音错误
    （`雪包→血包`、`断亲→断气`、`25岁→45岁`、`经营价值→经济价值`）。
    所以硬字幕应当作为一条独立、可复核的来源，而不是"顺手截几张图看看"。

三个真实陷阱（本模块的核心设计都由此而来）：
    1. 低采样率会漏掉短卡片。2 fps 采样漏掉了只闪 0.4 秒的「更高级」与「病了」，
       前者直接改变句意（"更高级、更耐用、更能持续供血的血包" 变成 "更耐用…"）。
       因此默认 4 fps，并且必须给出"短卡片计数 + 采样风险"的自检，而不是假装完整。
    2. OCR 会稳定认错某些字：`赡→赠`、`白→自`、`干瘪→干`。校正表必须可配置且可审计。
    3. 画面里的水印会被一起 OCR 出来（实测该视频字幕带内有会动的 `VAIKE DKOTIEEKALION`），
       把卡片切得粉碎。因此有置信度门槛与噪声表，且都作用在**行**粒度：
       水印行可以删，同卡里的正文行必须留下——绝不能按"卡片太短"来过滤。

边界：
    - 只接受本地视频文件，不做网络取流；本模块不自动判断"谁对谁错"，
      交叉校验只负责把差异摆全（结论依赖上下文，见 docs/DECISIONS.md D21）。
    - OCR 引擎（rapidocr-onnxruntime）、ffmpeg 与抽帧调用都是可注入的可选依赖：
      没有引擎、没有 ffmpeg、没有真实视频也能把全部逻辑跑完（离线测试就是这么做的）。
    - 噪声过滤只按置信度与正则判定，永不按"文本长度"判定：`更高级`、`病了` 这类
      2-3 字短卡片是正文，必须保留；丢弃的数量与原因一律进自检报告，绝不静默。

调用示例：
    python hard_subtitle.py video.mp4 --fps 4 --emit srt,json -o out/
    python hard_subtitle.py video.mp4 --asr-timeline asr.json --json
"""
from __future__ import annotations  # 允许在返回结构里使用现代类型标注

import argparse  # 稳定命令行契约：本地文件、采样参数、产出与自检输入
import difflib  # 字符级对齐是唯一能穷尽"哪一侧多了正文"的方法
import importlib  # OCR 依赖按需导入，且允许测试注入导入器
import json  # 校正表、ASR 时间轴与 JSON 产出都是 JSON
import re  # 从 ffmpeg 诊断里解析时长与分辨率
import subprocess  # 抽帧与探测都只经过 ffmpeg 进程，不引入 ffprobe 依赖
import sys  # 业务结果写 stdout，诊断与失败写 stderr
from pathlib import Path  # 统一处理带空格与中文的本地路径
from typing import Callable, NamedTuple, Sequence  # 参数对象必须是 NamedTuple（见 D16）

from file_output import write_text_atomically  # 与全仓一致：临时文件 + 原子替换
from media_tools import find_ffmpeg  # 与全仓共用同一套 FFmpeg 解析规则
from normalize_transcript import normalize_segments, to_plain_text, to_srt  # 规范形状与 SRT 的唯一实现
from runtime_output import EXIT_GENERIC_FAILURE, EXIT_SUCCESS, log  # 退出码契约与 stderr 诊断


# --- 可调默认值：每个数字都对应一次真实代价 ---
DEFAULT_FPS = 4.0                              # 2 fps 实测漏掉 0.4 秒卡片「更高级」，默认翻倍
DEFAULT_CHANGE_THRESHOLD = 2.0                 # 带内灰度平均绝对差；低于该值视为同一张卡片
DEFAULT_BAND_BOTTOM_RATIO = 0.25               # 字幕带高度占画面高度的比例
DEFAULT_BAND_OFFSET_RATIO = 0.62               # 字幕带顶边从画面 62% 高度开始
DEFAULT_SHORT_CARD_SECONDS = 0.5               # 短于该值的卡片提示"可能有更短卡片被漏"
DEFAULT_CORRECTIONS = {                        # 只收录真实踩过的三个 OCR 稳定误认
    "赡": "赠",                                # 赡养 -> 赠养（实测同一段视频反复认错）
    "白": "自",                                # 白己 -> 自己
    "干瘪": "干",                              # 干瘪 -> 干（长键必须先于任何单字规则）
}
DEFAULT_MIN_OCR_CONFIDENCE = 0.5               # 低于该分数的识别行视为噪声；rapidocr 正文通常 >0.9
EMIT_FORMATS = ("srt", "txt", "json")          # 产出格式白名单；未知值必须显式失败
OCR_INSTALL_HINT = 'pip install -e ".[hard-subtitle]"'  # 该 extra 已在 pyproject 里定义
PROBE_TIMEOUT_SECONDS = 120                    # 探测只读元数据，超时说明路径/权限有问题
SAMPLE_TIMEOUT_SECONDS = 3600                  # 4 fps 下长视频的抽帧可能很慢，不能按默认 120s 误杀
DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")   # ffmpeg -i 的诊断格式
VIDEO_SIZE_PATTERN = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})")        # 形如 1080x1920
SIGNIFICANT_PATTERN = re.compile(r"[\w\u4e00-\u9fff]")  # 与 verify_transcript/D21 一致：标点差异不算内容差异

# --- 内置噪声表：只匹配水印特征与"不可能成为正文的 ASCII 碎片" ---
# 为什么带否定前瞻：`^[A-Za-z0-9.]{1,3}$` 会把正常英文台词（OK / No / TV / AI / App）一起误杀，
# 而过滤器的目标是水印碎片（bV / b.V / BV / BVM），不是"短"。常见英文词显式放行。
# 为什么水印签名这么具体：该抖音水印是会动的字母 logo，OCR 会稳定产出
# `VAIKE DKOTIEEKALION` 及其变体（DKOTIEEBALION / KOTIEEBVLION / KOFIEEBALION…）。
# 默认表只认这些特征串，绝不使用"整行都是大写 ASCII"这类会误杀英文台词的通则；
# 换一支视频的水印请用可重复的 --noise-pattern 补充。
DEFAULT_NOISE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("douyin_watermark", re.compile(r"VAIKE|VAIBE|DKOTIEE|KOTIEE|KOFIEE|BALION|VLION", re.IGNORECASE)),
    ("short_ascii_fragment", re.compile(
        r"^(?!(?i:ok|no|hi|tv|pc|ai|ip|us|it|is|in|on|at|go|so|my|me|we|he|up|id|app)$)"
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,2}$"                     # 含 `bV-` 这类带连字符的水印碎片
    )),
)


# --- 依赖缺失时给出可执行的安装建议，而不是裸 ImportError ---
class HardSubtitleDependencyError(RuntimeError):
    """硬字幕 OCR 依赖缺失；消息里必须带可复制的安装命令。"""


# --- 一次硬字幕提取任务的全部可调参数 ---
class HardSubtitleSettings(NamedTuple):
    fps: float = DEFAULT_FPS                                   # 采样率越高越不容易漏短卡片，代价是 OCR 次数
    change_threshold: float = DEFAULT_CHANGE_THRESHOLD         # 变化检测阈值，越小越敏感也越容易误触发
    band_bottom_ratio: float = DEFAULT_BAND_BOTTOM_RATIO       # 字幕带高度比例（默认就是画面下方 25%）
    band_offset_ratio: float = DEFAULT_BAND_OFFSET_RATIO       # 字幕带顶边位置（默认 62% 高度处开始）
    band_height: int | None = None                             # 显式像素高度，覆盖比例计算
    short_card_seconds: float = DEFAULT_SHORT_CARD_SECONDS     # 自检里"短卡片"的门槛
    min_ocr_confidence: float = DEFAULT_MIN_OCR_CONFIDENCE     # 低于该分数的识别行按噪声丢弃，绝不静默
    noise_patterns: tuple[str, ...] = ()                       # 额外的噪声正则；内置水印表始终生效

    def identity(self) -> dict:
        return self._asdict()                                  # 产出里回写参数，结论才可复核


# --- 字幕带在画面里的像素位置 ---
class Band(NamedTuple):
    top: int                                                   # 带顶边（从画面顶部算起的像素）
    height: int                                                # 带高度（像素）


# --- 一帧的字幕带像素（灰度、按行序） ---
class FrameSample(NamedTuple):
    timestamp: float                                           # 该帧对应的秒数
    gray: bytes                                                # 带内灰度像素，长度应为 width * height
    width: int
    height: int


# --- 一次识别的结果：文本 + 置信度，让分数能一路走到自检报告 ---
class RecognizedText(NamedTuple):
    text: str                                                  # 识别到的多行文本（换行拼接）
    confidence: float | None = None                            # 整帧最低行分；None 表示识别器不提供分数
    lines: tuple[tuple[str, float | None], ...] = ()           # 每行文本与分数；为空时退回整帧置信度


# --- 把注入的识别器返回值统一成 RecognizedText ---
def coerce_recognition(value) -> RecognizedText:
    if value is None:
        return RecognizedText("", None)
    if isinstance(value, RecognizedText):
        return value
    if isinstance(value, str):
        return RecognizedText(value, None)                     # 注入的假识别器没有分数 -> 不按置信度丢弃
    text = getattr(value, "text", None)
    if text is None:
        return RecognizedText(str(value), None)
    confidence = getattr(value, "confidence", None)
    lines = getattr(value, "lines", ()) or ()
    return RecognizedText(str(text), None if confidence is None else float(confidence), tuple(lines))


# --- 内置噪声表 + 调用方附加正则 ---
def compile_noise_patterns(extra: Sequence[str] | None = None) -> list[tuple[str, re.Pattern]]:
    compiled = list(DEFAULT_NOISE_PATTERNS)
    for expression in extra or ():
        try:
            compiled.append((f"custom:{expression}", re.compile(expression)))
        except re.error as exc:                                # 坏正则必须当场失败，而不是悄悄匹配不到
            raise ValueError(f"invalid noise pattern {expression!r}: {exc}") from exc
    return compiled


# --- 单行是否命中噪声表：命中返回原因标签 ---
def match_noise_label(line: str, patterns: Sequence[tuple[str, re.Pattern]]) -> str | None:
    for label, regex in patterns:
        if regex.search(line):
            return label
    return None


# --- 行级过滤：水印常与正文同处一卡，按整卡丢弃会连正文一起丢 ---
def filter_noise_lines(
    lines: Sequence[tuple[str, float | None]],
    *,
    patterns: Sequence[tuple[str, re.Pattern]],
    min_confidence: float | None,
    corrections: dict | None,
) -> tuple[list[str], list[str], int, int]:
    kept: list[str] = []
    reasons: list[str] = []
    removed = 0
    correction_hits = 0
    for line, score in lines:
        text = normalize_ocr_text(line)
        if not text:
            continue
        fixed, hits = apply_corrections_counted(text, corrections)  # 校正先于过滤：先修 OCR 稳定误认，再判噪声
        if min_confidence is not None and score is not None and float(score) < float(min_confidence):
            reasons.append("low_confidence")                   # 低分行按噪声处理，但必须留痕
            removed += 1
            continue
        label = match_noise_label(fixed, patterns)
        if label:
            reasons.append(label)
            removed += 1
            continue
        kept.append(fixed)
        correction_hits += hits                                # 只统计真正保留的行，避免虚高
    return kept, reasons, removed, correction_hits


# --- 把识别结果拆成待过滤的行（优先用带分数的行） ---
def recognition_lines(recognized: RecognizedText) -> list[tuple[str, float | None]]:
    if recognized.lines:
        return [(text, score) for text, score in recognized.lines if normalize_ocr_text(text)]
    text = normalize_ocr_text(recognized.text)
    if not text:
        return []
    return [(line, recognized.confidence) for line in text.splitlines()]  # 无行分时整帧分数作用于每一行


# --- 解析字幕带几何：比例默认，显式像素优先 ---
def resolve_band(frame_height: int, settings: HardSubtitleSettings | None = None) -> Band:
    active = settings or HardSubtitleSettings()
    height_px = int(frame_height)
    if height_px <= 0:
        raise ValueError(f"frame height must be positive, got {frame_height}")
    top = int(round(height_px * active.band_offset_ratio))     # 先从 62% 高度处开始
    top = max(0, min(top, height_px - 1))                      # 比例越界时收敛到画面内，避免 ffmpeg crop 失败
    if active.band_height is not None:
        band_height = int(active.band_height)                  # 显式像素覆盖比例计算
    else:
        band_height = int(round(height_px * active.band_bottom_ratio))
    band_height = max(1, min(band_height, height_px - top))    # 至少 1 像素且不越过画面底边
    return Band(top=top, height=band_height)


# --- 相邻帧在字幕带内的像素差异（平均绝对差） ---
def frame_difference(previous_gray: Sequence[int], current_gray: Sequence[int]) -> float:
    if not previous_gray or not current_gray:
        return 0.0                                             # 空带无法比较，按"没变化"处理
    limit = min(len(previous_gray), len(current_gray))         # 分辨率变化的帧只比公共部分，避免错位
    if limit == 0:
        return 0.0
    numpy = _try_numpy()
    if numpy is not None and isinstance(previous_gray, (bytes, bytearray)) and isinstance(current_gray, (bytes, bytearray)):
        left = numpy.frombuffer(bytes(previous_gray[:limit]), dtype=numpy.uint8).astype(numpy.int16)
        right = numpy.frombuffer(bytes(current_gray[:limit]), dtype=numpy.uint8).astype(numpy.int16)
        return float(numpy.abs(left - right).mean())           # 518k 像素 × 上千帧，纯 Python 会慢到不可用
    total = 0
    for index in range(limit):
        total += abs(int(previous_gray[index]) - int(current_gray[index]))
    return total / limit                                       # 没装 numpy 时的等价慢路径（测试环境常见）


# --- numpy 是可选加速项：没有它逻辑必须完全一致 ---
def _try_numpy():
    try:
        import numpy                                           # rapidocr 依赖它，但本模块不强制要求
        return numpy
    except Exception:
        return None


# --- 载入 OCR 引擎（缺失时抛出带安装建议的错误） ---
def load_ocr_engine(*, importer: Callable[[str], object] | None = None):
    active_importer = importer or importlib.import_module
    try:
        module = active_importer("rapidocr_onnxruntime")
    except ImportError as exc:
        raise HardSubtitleDependencyError(
            "hard-subtitle OCR requires rapidocr-onnxruntime, which is an optional extra. "
            f"Install it from the Skill root with: {OCR_INSTALL_HINT} "
            f"(original error: {exc})"
        ) from exc
    return module.RapidOCR()                                   # onnxruntime 后端，CPU 即可跑


# --- 把 OCR 引擎包装成"一帧 -> RecognizedText"的可注入识别器 ---
def build_recognizer(engine=None, *, importer: Callable[[str], object] | None = None) -> Callable[[FrameSample], RecognizedText]:
    active_engine = engine if engine is not None else load_ocr_engine(importer=importer)

    def recognize(sample: FrameSample) -> RecognizedText:
        return recognize_frame(active_engine, sample)           # 闭包让调用方只关心 FrameSample

    return recognize


# --- 单帧识别：灰度带 -> 多行文本 + 分数 ---
def recognize_frame(engine, sample: FrameSample) -> RecognizedText:
    numpy = _try_numpy()
    if numpy is None:
        raise HardSubtitleDependencyError(
            "recognizing frames requires numpy; it is installed together with the OCR extra. "
            f"Install it with: {OCR_INSTALL_HINT}"
        )
    image = numpy.frombuffer(bytes(sample.gray), dtype=numpy.uint8).reshape(sample.height, sample.width)
    result, _elapsed = engine(image)                           # RapidOCR 约定：返回 (结果列表, 耗时)
    entries = rapidocr_entries(result)
    if not entries:
        return RecognizedText("", None)                         # 空白帧不是错误，只是没有字幕
    scores = [score for _text, score in entries if score is not None]
    text = "\n".join(entry_text for entry_text, _score in entries)
    return RecognizedText(text, min(scores) if scores else None, tuple(entries))


# --- RapidOCR 结果 -> 按阅读顺序排列的 (文本, 分数) ---
def rapidocr_entries(result) -> list[tuple[str, float | None]]:
    if not result:
        return []
    lines = []
    for entry in result:
        if len(entry) < 2:
            continue                                           # 形状异常的条目直接跳过，不让整帧失败
        box, text = entry[0], str(entry[1]).strip()
        if not text:
            continue
        score: float | None = None
        if len(entry) >= 3:
            try:
                score = float(entry[2])                         # 置信度必须带出来，不能在这里丢掉
            except (TypeError, ValueError):
                score = None
        try:
            top = min(float(point[1]) for point in box)        # 用框顶边定位行
            left = min(float(point[0]) for point in box)       # 同一行内按左边排序
        except (TypeError, ValueError, IndexError):
            top, left = 0.0, 0.0                               # 拿不到几何信息时保持原顺序
        lines.append((round(top / 10.0), left, len(lines), text, score))  # 10px 分桶吸收同一行的轻微抖动
    lines.sort()
    return [(item[3], item[4]) for item in lines]              # 多行必须用换行拼接，不能合成一行


# --- 兼容入口：只要文本 ---
def rapidocr_result_to_text(result) -> str:
    return "\n".join(text for text, _score in rapidocr_entries(result))


# --- 应用校正表（长键优先，避免"干瘪"被拆开） ---
def apply_corrections(text: str, corrections: dict | None) -> str:
    fixed, _hits = apply_corrections_counted(text, corrections)
    return fixed


# --- 应用校正表并统计命中次数（命中数是自检证据） ---
def apply_corrections_counted(text: str, corrections: dict | None) -> tuple[str, int]:
    if not text or not corrections:
        return text, 0
    hits = 0
    for wrong in sorted(corrections, key=len, reverse=True):    # 长键先替换：否则单字规则会破坏多字规则
        right = corrections[wrong]
        if wrong and wrong in text:
            hits += text.count(wrong)
            text = text.replace(wrong, right)
    return text, hits


# --- 读取显式校正表 ---
def load_corrections(path: str | Path) -> dict:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"corrections file not found: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"corrections file must be a JSON object mapping wrong -> right: {source}")
    return {str(key): str(value) for key, value in payload.items()}


# --- 默认表 + 显式文件：文件覆盖同名键，而不是整张替换 ---
def resolve_corrections(path: str | Path | None = None) -> dict:
    merged = dict(DEFAULT_CORRECTIONS)
    if path:
        merged.update(load_corrections(path))
    return merged


# --- OCR 原始文本归一：去空行、保留行结构 ---
def normalize_ocr_text(text) -> str:
    if text is None:
        return ""
    lines = [line.strip() for line in str(text).splitlines()]
    return "\n".join(line for line in lines if line).strip()


# --- 抽取：只按 fps 与带内像素变化决定是否 OCR ---
def build_cards(
    samples: Sequence[FrameSample],
    *,
    recognize: Callable[[FrameSample], RecognizedText | str],
    change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
    corrections: dict | None = None,
    sampling_fps: float | None = None,
    video_duration: float | None = None,
    min_confidence: float | None = None,
    noise_patterns: Sequence[tuple[str, re.Pattern]] | None = None,
) -> tuple[list[dict], dict]:
    if not samples:
        return [], _empty_build_report()
    patterns = tuple(DEFAULT_NOISE_PATTERNS if noise_patterns is None else noise_patterns)

    observations: list[tuple[int, str, str]] = []              # (帧序号, 过滤后文本, 状态)
    corrections_applied = 0
    noise_lines_removed = 0
    line_reasons: dict[str, int] = {}
    previous_gray: Sequence[int] | None = None
    for index, sample in enumerate(samples):
        changed = previous_gray is None                        # 第一帧必须识别，否则整段没有起点
        if not changed:
            changed = frame_difference(previous_gray, sample.gray) >= float(change_threshold)
        previous_gray = sample.gray                            # 无论是否 OCR 都要更新比较基准
        if not changed:
            continue                                           # 相邻帧相同 -> 不做 OCR，这是省时间的关键
        recognized = coerce_recognition(recognize(sample))
        kept_lines, reasons, removed, hits = filter_noise_lines(
            recognition_lines(recognized),
            patterns=patterns,
            min_confidence=min_confidence,
            corrections=corrections,
        )
        for reason in reasons:
            line_reasons[reason] = line_reasons.get(reason, 0) + 1
        noise_lines_removed += removed
        corrections_applied += hits
        if kept_lines:
            observations.append((index, "\n".join(kept_lines), "kept"))
        elif reasons:
            observations.append((index, "", reasons[0]))       # 整卡被判定为噪声，原因必须留痕
        else:
            observations.append((index, "", "empty"))          # 空识别只是"没有字幕"，不算噪声

    runs: list[dict] = []                                      # 连续相同 (文本, 状态) 合并为一个 run
    for index, text, status in observations:
        if runs and runs[-1]["text"] == text and runs[-1]["status"] == status:
            continue                                           # 卡片内的噪声变化不产生新卡片
        runs.append({"text": text, "status": status, "start": round(float(samples[index].timestamp), 3)})

    interval = sampling_interval(samples, sampling_fps)
    for position, run in enumerate(runs):
        if position + 1 < len(runs):
            end = runs[position + 1]["start"]                  # 下一次文本变化就是本卡片的结束时刻
        elif video_duration is not None:
            end = float(video_duration)
        else:
            end = float(samples[-1].timestamp) + interval      # 不知道总时长时，用最后一帧加一个采样间隔
        run["end"] = round(max(end, run["start"]), 3)

    cards: list[dict] = []
    dropped_cards: dict[str, int] = {}
    for run in runs:
        if run["status"] == "kept" and run["text"]:
            cards.append({"start": run["start"], "end": run["end"], "text": run["text"]})
        elif run["status"] == "empty":
            continue                                           # 空文本只是"字幕消失"，不产出卡片也不计噪声
        else:
            dropped_cards[run["status"]] = dropped_cards.get(run["status"], 0) + 1
    report = {
        "sampled_frames": len(samples),
        "change_points": len(observations),
        "ocr_calls": len(observations),                        # 只在该数字上花 OCR 时间
        "cards": len(cards),
        "corrections_applied": corrections_applied,
        "noise_cards_dropped": sum(dropped_cards.values()),    # 绝不静默丢弃：数量与原因都要进报告
        "noise_dropped_reasons": dropped_cards,
        "noise_lines_removed": noise_lines_removed,            # 所有被删噪声行（含整卡被丢弃的那些）
        "noise_line_reasons": line_reasons,
    }
    return cards, report


# --- 空输入时的同形报告，保证调用方不必处理缺字段 ---
def _empty_build_report() -> dict:
    return {
        "sampled_frames": 0, "change_points": 0, "ocr_calls": 0, "cards": 0, "corrections_applied": 0,
        "noise_cards_dropped": 0, "noise_dropped_reasons": {}, "noise_lines_removed": 0,
        "noise_line_reasons": {},
    }


# --- 采样间隔：优先相信真实帧时间，其次用 fps ---
def sampling_interval(samples: Sequence[FrameSample], sampling_fps: float | None = None) -> float:
    if len(samples) >= 2:
        gap = float(samples[1].timestamp) - float(samples[0].timestamp)
        if gap > 0:
            return gap
    if sampling_fps:
        return 1.0 / float(sampling_fps)
    return 1.0 / DEFAULT_FPS


# --- 完整性自检：说出"我可能漏了什么" ---
def assess_completeness(
    cards: Sequence[dict],
    *,
    video_duration: float | None,
    sampling_fps: float,
    sampled_frames: int,
    change_points: int,
    short_card_seconds: float = DEFAULT_SHORT_CARD_SECONDS,
    noise_cards_dropped: int = 0,
    noise_dropped_reasons: dict | None = None,
    noise_lines_removed: int = 0,
    noise_line_reasons: dict | None = None,
) -> dict:
    durations = [max(float(item["end"]) - float(item["start"]), 0.0) for item in cards]
    total = round(sum(durations), 3)
    short_cards = [duration for duration in durations if duration < short_card_seconds]
    interval = round(1.0 / float(sampling_fps), 4) if sampling_fps else None
    coverage_ratio = round(total / float(video_duration), 4) if video_duration else None
    warnings = []
    if interval is not None:
        warnings.append(                                        # 采样风险必须常驻，不能只在发现问题时才说
            f"{sampling_fps} fps 的采样间隔是 {interval} 秒：短于一个间隔的卡片可能整帧被跳过。"
            f"实测 2 fps 漏掉只闪 0.4 秒的「更高级」，直接改变句意"
            f"（“更高级、更耐用…” 变成 “更耐用…”）；需要更高保真度就提高 --fps。"
        )
    if short_cards:
        warnings.append(
            f"有 {len(short_cards)} 张卡片时长 < {short_card_seconds}s（最短 {round(min(short_cards), 3)}s）："
            f"这通常意味着还有更短的卡片落在两次采样之间被漏掉，建议提高 --fps 后重跑再比对。"
        )
    if noise_cards_dropped:
        warnings.append(                                        # 被丢掉的卡片会形成覆盖空洞，必须显式说明
            f"有 {noise_cards_dropped} 张卡片按噪声丢弃（{noise_dropped_reasons or {}}）："
            f"这些时间段不再有卡片覆盖，覆盖率会相应下降——不要当成提取完整。"
        )
    return {
        "sampled_frames": sampled_frames,
        "change_points": change_points,
        "cards": len(cards),
        "cards_total_seconds": total,                          # 卡片总时长
        "video_duration": video_duration,
        "caption_coverage_ratio": coverage_ratio,               # 卡片总时长 / 视频时长（丢弃后自然下降）
        "sampling_fps": sampling_fps,
        "sampling_interval_seconds": interval,
        "short_card_seconds": short_card_seconds,
        "short_cards": len(short_cards),
        "shortest_card_seconds": round(min(durations), 3) if durations else None,
        "noise_cards_dropped": noise_cards_dropped,             # 丢弃计数与原因分布：绝不静默
        "noise_dropped_reasons": dict(noise_dropped_reasons or {}),
        "noise_lines_removed": noise_lines_removed,             # 卡片保留但其中噪声行被删
        "noise_line_reasons": dict(noise_line_reasons or {}),
        "warnings": warnings,
    }


# --- 字符级交叉校验：字幕与 ASR 各自多了什么 ---
def cross_check_with_asr(cards: Sequence[dict], asr_segments) -> dict:
    caption = normalize_segments(list(cards))                  # 卡片本来就是规范形状，这里只做统一
    asr_source = asr_segments.get("segments") if isinstance(asr_segments, dict) else asr_segments
    asr = normalize_segments(asr_source or [])                 # 字段可能是 from/to/content，交给适配层
    caption_stream = _character_stream(caption)
    asr_stream = _character_stream(asr)
    caption_text = "".join(item[0] for item in caption_stream)
    asr_text = "".join(item[0] for item in asr_stream)

    matcher = difflib.SequenceMatcher(None, caption_text, asr_text, autojunk=False)
    caption_only: list[dict] = []
    asr_only: list[dict] = []
    replacements: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":                                    # 字幕有、ASR 没有 -> ASR 漏了正文
            caption_only.append({
                "start": _span_start(caption_stream, i1),
                "end": _span_end(caption_stream, i2),
                "text": caption_text[i1:i2],
            })
        elif tag == "insert":                                  # ASR 有、字幕没有 -> 字幕漏了正文
            asr_only.append({
                "start": _span_start(asr_stream, j1),
                "end": _span_end(asr_stream, j2),
                "text": asr_text[j1:j2],
            })
        else:                                                  # 替换通常是同音字，需要人工按上下文判断
            replacements.append({
                "start": _first_time(_span_start(caption_stream, i1), _span_start(asr_stream, j1)),
                "end": _first_time(_span_end(caption_stream, i2), _span_end(asr_stream, j2), prefer_last=True),
                "caption_text": caption_text[i1:i2],
                "asr_text": asr_text[j1:j2],
            })

    caption_extra = bool(caption_only or replacements)         # 替换算两边的差异，所以两侧都"多"
    asr_extra = bool(asr_only or replacements)
    if not caption_extra and not asr_extra:
        verdict = "identical"
    elif caption_extra and asr_extra:
        verdict = "both_ways"
    elif caption_extra:
        verdict = "caption_covers_asr"
    else:
        verdict = "asr_covers_caption"

    return {
        "caption": _describe_stream(caption, caption_text),
        "asr": _describe_stream(asr, asr_text),
        "similarity": round(matcher.ratio(), 4),
        "caption_only": caption_only,
        "asr_only": asr_only,
        "replacements": replacements,
        "verdict": verdict,                                    # 一句话结论，调用方不必自己归纳
    }


# --- 字符 -> 时间 的流：只对齐有效字符，标点与空白不参与 ---
# 为什么忽略标点：ASR 与 OCR 的标点体系不同（"," vs "，" vs "、"），
# 全算差异会让一堆逗号淹没真正的同音字（血/雪），这是 D21 已经踩过的坑。
def _character_stream(segments: Sequence[dict]) -> list[tuple[str, float, float]]:
    stream: list[tuple[str, float, float]] = []
    for item in segments:
        for character in item["text"]:
            if SIGNIFICANT_PATTERN.match(character):
                stream.append((character, float(item["start"]), float(item["end"])))
    return stream


# --- 差异片段的起始时间 ---
def _span_start(stream: Sequence[tuple[str, float, float]], index: int) -> float | None:
    if 0 <= index < len(stream):
        return round(stream[index][1], 2)
    return round(stream[-1][2], 2) if stream else None         # 越过末尾时退化到最后一帧的结束时间


# --- 差异片段的结束时间 ---
def _span_end(stream: Sequence[tuple[str, float, float]], index: int) -> float | None:
    if 0 < index <= len(stream):
        return round(stream[index - 1][2], 2)
    return round(stream[0][1], 2) if stream else None


# --- 两侧都有值时取更保守的一侧（起始取更早、结束取更晚） ---
def _first_time(primary: float | None, secondary: float | None, *, prefer_last: bool = False) -> float | None:
    values = [value for value in (primary, secondary) if value is not None]
    if not values:
        return None
    return max(values) if prefer_last else min(values)


# --- 一份来源的摘要 ---
def _describe_stream(segments: Sequence[dict], text: str) -> dict:
    return {
        "segments": len(segments),
        "chars": len(text),
        "start": segments[0]["start"] if segments else None,
        "end": segments[-1]["end"] if segments else None,
    }


# --- 探测本地视频的时长与分辨率（只读元数据，不解码） ---
def probe_video(video: str | Path, *, ffmpeg_path: str | None = None, run=None) -> dict:
    runner = run or subprocess.run
    ffmpeg = ffmpeg_path or find_ffmpeg()
    command = [ffmpeg, "-hide_banner", "-i", str(video), "-f", "null", "-"]
    try:
        completed = runner(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg could not probe {video}: {exc}") from exc
    diagnostics = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    duration_match = DURATION_PATTERN.search(diagnostics)
    size_match = VIDEO_SIZE_PATTERN.search(diagnostics)
    if not size_match:
        raise RuntimeError("could not read video dimensions from ffmpeg; is this a readable local video file?")
    hours, minutes, seconds = duration_match.groups() if duration_match else (0, 0, 0)
    return {
        "duration": round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3),
        "width": int(size_match.group(1)),
        "height": int(size_match.group(2)),
    }


# --- 抽帧：只取字幕带并灰度化，降低 OCR 成本 ---
def sample_band_frames(
    video: str | Path,
    settings: HardSubtitleSettings | None = None,
    *,
    ffmpeg_path: str | None = None,
    run=None,
    probe: Callable[[str | Path], dict] | None = None,
) -> tuple[list[FrameSample], float | None]:
    active = settings or HardSubtitleSettings()
    runner = run or subprocess.run
    info = probe(video) if probe is not None else probe_video(video, ffmpeg_path=ffmpeg_path, run=runner)
    band = resolve_band(info["height"], active)
    ffmpeg = ffmpeg_path or find_ffmpeg()
    frame_bytes = int(info["width"]) * band.height
    if frame_bytes <= 0:
        raise ValueError("resolved subtitle band is empty; check --band-* values")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vf", f"fps={active.fps},crop={info['width']}:{band.height}:0:{band.top},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        completed = runner(command, capture_output=True, timeout=SAMPLE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg could not sample frames from {video}: {exc}") from exc
    if getattr(completed, "returncode", 0) != 0:
        detail = (completed.stderr or b"")
        detail = detail.decode("utf-8", "replace") if isinstance(detail, bytes) else str(detail)
        raise RuntimeError(f"ffmpeg frame sampling failed: {detail.strip()[-500:]}")
    raw = completed.stdout or b""
    count = len(raw) // frame_bytes
    interval = 1.0 / float(active.fps) if active.fps else 1.0 / DEFAULT_FPS
    samples = [
        FrameSample(
            timestamp=round(index * interval, 3),              # ffmpeg fps 过滤器按固定间隔出帧
            gray=raw[index * frame_bytes:(index + 1) * frame_bytes],
            width=int(info["width"]),
            height=band.height,
        )
        for index in range(count)
    ]
    return samples, info.get("duration")


# --- 对外统一入口：本地视频 -> 卡片 + 自检报告 ---
def extract_cards(
    video: str | Path,
    settings: HardSubtitleSettings | None = None,
    *,
    corrections: dict | None = None,
    frame_sampler: Callable[[str | Path, HardSubtitleSettings], tuple[list[FrameSample], float | None]] | None = None,
    recognizer: Callable[[FrameSample], RecognizedText | str] | None = None,
    video_duration: float | None = None,
    asr_timeline=None,
) -> dict:
    path = Path(video)
    if not path.is_file():
        raise FileNotFoundError(f"video not found: {path}")
    active = settings or HardSubtitleSettings()
    sampler = frame_sampler or sample_band_frames                # 测试注入点：没有 ffmpeg 也能跑完整链路
    samples, measured_duration = sampler(path, active)
    duration = video_duration if video_duration is not None else measured_duration
    active_corrections = DEFAULT_CORRECTIONS if corrections is None else dict(corrections)
    if recognizer is None and samples:
        recognizer = build_recognizer()                          # 只有真的要 OCR 时才要求引擎存在
    recognize = recognizer or (lambda sample: "")
    patterns = compile_noise_patterns(active.noise_patterns)     # 内置水印表 + --noise-pattern 附加项

    cards, card_report = build_cards(
        samples,
        recognize=recognize,
        change_threshold=active.change_threshold,
        corrections=active_corrections,
        sampling_fps=active.fps,
        video_duration=duration,
        min_confidence=active.min_ocr_confidence,
        noise_patterns=patterns,
    )
    report = dict(card_report)
    report.update(assess_completeness(
        cards,
        video_duration=duration,
        sampling_fps=active.fps,
        sampled_frames=len(samples),
        change_points=card_report["change_points"],
        short_card_seconds=active.short_card_seconds,
        noise_cards_dropped=card_report["noise_cards_dropped"],
        noise_dropped_reasons=card_report["noise_dropped_reasons"],
        noise_lines_removed=card_report["noise_lines_removed"],
        noise_line_reasons=card_report["noise_line_reasons"],
    ))
    if asr_timeline is not None:
        # 既接受已经解析好的分段，也接受 --asr-timeline 那种 JSON 路径，避免调用方各写一遍加载逻辑。
        segments = load_timeline(asr_timeline) if isinstance(asr_timeline, (str, Path)) else asr_timeline
        report["cross_check"] = cross_check_with_asr(cards, segments)
    result = {
        "video": str(path),
        "duration": duration,
        "settings": active.identity(),                           # 参数回写，结论才能被复核
        "cards": cards,
        "report": report,                                        # 自检报告的规范位置
        "completeness": report,                                  # 同一份报告的别名，调用方按习惯取名
    }
    if "cross_check" in report:
        result["cross_check"] = report["cross_check"]            # 结论提到顶层，调用方不必知道它藏在 report 里
    return result


# --- 读取 ASR 时间轴（规范形状或 from/to/content，或完整结果的 segments） ---
def load_timeline(path: str | Path) -> list[dict]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"timeline not found: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    segments = payload.get("segments") if isinstance(payload, dict) else payload
    if not segments:
        raise ValueError(f"{source} contains no non-empty 'segments' list")
    return normalize_segments(segments)


# --- --emit 白名单校验 ---
def parse_emit_formats(emit: str | None) -> list[str]:
    if not emit:
        return []
    requested = [item.strip().lower() for item in emit.split(",") if item.strip()]
    unknown = [item for item in requested if item not in EMIT_FORMATS]
    if unknown:
        raise ValueError(f"unsupported --emit value(s) {unknown}; choose from {sorted(EMIT_FORMATS)}")
    ordered: list[str] = []
    for item in requested:
        if item not in ordered:
            ordered.append(item)                                 # 去重但保留调用方的顺序
    return ordered


# --- 产出：SRT/TXT 复用 normalize_transcript，JSON 带上自检报告 ---
def write_emitted_artifacts(result: dict, output_dir: str | Path, formats: Sequence[str]) -> list[Path]:
    written: list[Path] = []
    base = Path(output_dir)
    for fmt in formats:
        if fmt == "srt":
            payload = to_srt(result["cards"])                    # 绝不重写一份 SRT：折行与时间戳只有一处实现
        elif fmt == "txt":
            payload = to_plain_text(result["cards"])
        elif fmt == "json":
            payload = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            raise ValueError(f"unsupported --emit value {fmt!r}; choose from {sorted(EMIT_FORMATS)}")
        written.append(write_text_atomically(base / f"hard_subtitle.{fmt}", payload))
    return written


# --- 人类可读摘要：把"可能漏了什么"放在最显眼处 ---
def render_summary(result: dict) -> str:
    report = result["report"]
    lines = [
        f"# 硬字幕 OCR：{report['cards']} 张卡片", "",
        f"- 视频：{result['video']}",
        f"- 时长：{result['duration']}s；采样帧 {report['sampled_frames']}；变化点（OCR 次数）{report['change_points']}",
        f"- 卡片总时长：{report['cards_total_seconds']}s（占视频 {report['caption_coverage_ratio']}）",
        f"- 短卡片（< {report['short_card_seconds']}s）：{report['short_cards']}；最短 {report['shortest_card_seconds']}s",
        f"- 噪声丢弃：整卡 {report['noise_cards_dropped']}（{report['noise_dropped_reasons']}）；"
        f"卡片内删除噪声行 {report['noise_lines_removed']}",
    ]
    for warning in report.get("warnings", []):
        lines.append(f"- ⚠ {warning}")
    cross_check = report.get("cross_check")
    if cross_check:
        lines.append(f"- 与 ASR 交叉校验：相似度 {cross_check['similarity']}；结论 {cross_check['verdict']}")
    return "\n".join(lines)


# --- 命令行入口 ---
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Extract burned-in (hard) subtitles from a local video via OCR.")
    parser.add_argument("video", help="Local video file path (this tool never streams from the network)")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help=f"Sampling rate (default {DEFAULT_FPS}: 2 fps missed the 0.4s card '更高级')")
    parser.add_argument("--change-threshold", type=float, default=DEFAULT_CHANGE_THRESHOLD,
                        help="Mean absolute gray difference inside the band that counts as a change (default 2.0)")
    parser.add_argument("--band-bottom-ratio", type=float, default=DEFAULT_BAND_BOTTOM_RATIO,
                        help="Subtitle band height as a fraction of frame height (default 0.25)")
    parser.add_argument("--band-offset-ratio", type=float, default=DEFAULT_BAND_OFFSET_RATIO,
                        help="Top edge of the band as a fraction of frame height (default 0.62)")
    parser.add_argument("--band-height", type=int, default=None,
                        help="Explicit band height in pixels; overrides --band-bottom-ratio")
    parser.add_argument("--corrections", help="JSON object {'wrong': 'right'} overriding the built-in table")
    parser.add_argument("--min-ocr-confidence", type=float, default=DEFAULT_MIN_OCR_CONFIDENCE,
                        help=f"Drop OCR lines scoring below this (default {DEFAULT_MIN_OCR_CONFIDENCE}); drops are counted")
    parser.add_argument("--noise-pattern", action="append", default=None,
                        help="Extra noise regex; repeatable. Built-in watermark/short-ASCII filters always apply")
    parser.add_argument("--asr-timeline", help="ASR transcript JSON used for the character-level cross-check")
    parser.add_argument("-o", "--output", help="Output directory for --emit artifacts")
    parser.add_argument("--emit", help="Comma separated artifacts to write: srt,txt,json")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result on stdout")
    args = parser.parse_args(argv)

    try:
        formats = parse_emit_formats(args.emit)                  # 先校验产出参数，坏参数不该先花时间抽帧
    except ValueError as exc:
        print(f"[hard_subtitle] ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC_FAILURE
    if formats and not args.output:
        print("[hard_subtitle] ERROR: --emit requires -o/--output <dir>", file=sys.stderr)
        return EXIT_GENERIC_FAILURE

    settings = HardSubtitleSettings(
        fps=args.fps,
        change_threshold=args.change_threshold,
        band_bottom_ratio=args.band_bottom_ratio,
        band_offset_ratio=args.band_offset_ratio,
        band_height=args.band_height,
        min_ocr_confidence=args.min_ocr_confidence,
        noise_patterns=tuple(args.noise_pattern or ()),
    )
    try:
        corrections = resolve_corrections(args.corrections)
        asr_timeline = load_timeline(args.asr_timeline) if args.asr_timeline else None
        result = extract_cards(args.video, settings, corrections=corrections, asr_timeline=asr_timeline)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[hard_subtitle] ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC_FAILURE

    report = result["report"]
    log(f"[hard_subtitle] cards={report['cards']} short_cards={report['short_cards']} "
        f"frames={report['sampled_frames']} ocr_calls={report['change_points']} "
        f"noise_cards_dropped={report['noise_cards_dropped']}")
    for warning in report.get("warnings", []):
        log(f"[hard_subtitle] {warning}")                        # 采样风险写 stderr，不污染 stdout 的 JSON

    if formats:
        for path in write_emitted_artifacts(result, args.output, formats):
            if args.json:
                log(f"Saved to: {path}")                         # --json 时 stdout 必须始终是可解析的 JSON
            else:
                print(f"Saved to: {path}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not formats:
        print(render_summary(result))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
