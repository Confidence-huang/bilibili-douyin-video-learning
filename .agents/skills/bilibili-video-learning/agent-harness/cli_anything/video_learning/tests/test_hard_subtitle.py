"""
烧录字幕（硬字幕）OCR 提取的离线契约测试。

这些测试全部不依赖 ffmpeg、不依赖 OCR 引擎、不依赖真实视频：
抽帧、OCR 与 ffmpeg 进程都是注入点。守住四件事——
"短卡片不能漏"、"OCR 稳定误认必须可校正"、"必须自己说清可能漏了什么"、
"产出必须复用既有 SRT/TXT 实现而不是第二份"。
运行示例：python -m pytest cli_anything/video_learning/tests/test_hard_subtitle.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 构造校正表、ASR 时间轴并校验 JSON 产出。
import re  # 校验 SRT 时间戳格式。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 模拟 subprocess.CompletedProcess 的最小字段。

import pytest  # 提供 monkeypatch、tmp_path 与 capsys。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

SRT_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")  # SRT 的合法时间行。


# --- 加载一个 live Skill 脚本 ---
def load_script(name: str):
    script_path = SCRIPTS_DIR / f"{name}.py"  # 测试名直接映射真实脚本文件。
    module_name = f"video_learning_test_{name}"  # 独立模块名避免测试之间污染缓存。
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:  # 文件无法加载时给出明确的测试失败原因。
        raise RuntimeError(f"Could not load script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 造一批字幕带帧：同一个值代表同一张卡片 ---
def make_samples(values, *, fps: float = 4.0, width: int = 8, height: int = 2):
    interval = 1.0 / fps  # 采样间隔决定"只出现一帧"的卡片有多长。
    return [
        load_hard_subtitle().FrameSample(
            timestamp=round(index * interval, 3),
            gray=bytes([value]) * (width * height),  # 整带同值，差异比较的结果就是两个值之差的绝对值。
            width=width,
            height=height,
        )
        for index, value in enumerate(values)
    ]


_HARD_SUBTITLE = None  # 同一个测试会话只加载一次，避免重复 exec。


# --- 复用同一个动态加载的模块实例 ---
def load_hard_subtitle():
    global _HARD_SUBTITLE
    if _HARD_SUBTITLE is None:
        _HARD_SUBTITLE = load_script("hard_subtitle")
    return _HARD_SUBTITLE


# --- 造一个看起来真实存在的视频占位文件（不写入仓库） ---
def write_placeholder_video(tmp_path: Path) -> Path:
    video = tmp_path / "clip.mp4"  # 只满足 is_file() 校验，注入的抽帧器不会读它。
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return video


# --- 变化检测与卡片合并：连续相同帧只算一张卡片 ---
def test_change_detection_merges_consecutive_frames():
    hard = load_hard_subtitle()
    samples = make_samples([10, 10, 10, 40, 40, 60, 60, 60])
    texts = {10: "甲", 40: "乙", 60: "丙"}

    cards, report = hard.build_cards(samples, recognize=lambda sample: texts[sample.gray[0]],
                                     change_threshold=2.0, video_duration=2.0)

    assert [(card["start"], card["end"], card["text"]) for card in cards] == [
        (0.0, 0.75, "甲"),   # 前三帧相同 -> 只 OCR 一次，结束于下一次变化的时刻。
        (0.75, 1.25, "乙"),
        (1.25, 2.0, "丙"),   # 最后一张卡片结束于视频时长。
    ]
    assert report["sampled_frames"] == 8
    assert report["change_points"] == 3  # 只在 3 个变化点做了 OCR。
    assert report["ocr_calls"] == 3
    assert report["cards"] == 3


# --- 真实回归：只出现一帧（1/fps 秒）的卡片不能被漏掉 ---
def test_single_frame_card_is_not_lost():
    hard = load_hard_subtitle()
    # 第 5 帧插入「更高级」，前后都是同一张卡片——正是 2 fps 漏掉的那类短卡片。
    samples = make_samples([10, 10, 10, 10, 10, 40, 10, 10, 10, 10])
    texts = {10: "更耐用", 40: "更高级"}

    cards, report = hard.build_cards(samples, recognize=lambda sample: texts[sample.gray[0]],
                                     change_threshold=2.0, video_duration=2.5)

    short = [card for card in cards if card["text"] == "更高级"]
    assert len(short) == 1, "只闪一帧的卡片必须进入结果，否则句意会被静默改写"
    assert short[0]["start"] == pytest.approx(1.25)
    assert short[0]["end"] - short[0]["start"] == pytest.approx(0.25)  # 正好一个采样间隔 = 1/fps。
    assert report["change_points"] == 3  # 进入与离开各算一次变化。


# --- 校正表：内置默认表只含三个真实误认，显式文件覆盖同名键 ---
def test_default_and_custom_corrections_are_applied(tmp_path):
    hard = load_hard_subtitle()
    samples = make_samples([10, 10])
    recognize = lambda sample: "赡养白己干瘪"

    default_cards, _ = hard.build_cards(samples, recognize=recognize, corrections=hard.DEFAULT_CORRECTIONS)
    assert default_cards[0]["text"] == "赠养自己干"  # 赡->赠、白->自、干瘪->干。

    corrections_file = tmp_path / "corrections.json"
    corrections_file.write_text(json.dumps({"赡": "善"}, ensure_ascii=False), encoding="utf-8")
    merged = hard.resolve_corrections(corrections_file)

    assert merged["赡"] == "善"       # 显式文件覆盖同名键。
    assert merged["白"] == "自"       # 未被覆盖的默认项仍然生效。
    custom_cards, report = hard.build_cards(samples, recognize=recognize, corrections=merged)
    assert custom_cards[0]["text"] == "善养自己干"
    assert report["corrections_applied"] >= 3  # 命中次数是自检证据。


# --- 完整性自检：短卡片计数、覆盖率与采样风险都要有数字 ---
def test_completeness_report_counts_short_cards_and_coverage():
    hard = load_hard_subtitle()
    cards = [
        {"start": 0.0, "end": 1.0, "text": "长卡片"},
        {"start": 1.0, "end": 1.25, "text": "短卡片"},  # 0.25s < 0.5s。
    ]

    report = hard.assess_completeness(cards, video_duration=10.0, sampling_fps=4.0,
                                      sampled_frames=40, change_points=4)

    assert report["sampled_frames"] == 40
    assert report["change_points"] == 4
    assert report["cards"] == 2
    assert report["cards_total_seconds"] == pytest.approx(1.25)
    assert report["caption_coverage_ratio"] == pytest.approx(0.125)
    assert report["sampling_interval_seconds"] == pytest.approx(0.25)
    assert report["short_cards"] == 1
    assert report["shortest_card_seconds"] == pytest.approx(0.25)
    assert any("更高级" in warning for warning in report["warnings"])  # 采样风险明确写出真实案例。


# --- 交叉校验：字幕缺一句、ASR 有时必须被指名 ---
def test_cross_check_reports_a_sentence_the_caption_misses():
    hard = load_hard_subtitle()
    caption = [
        {"start": 0.0, "end": 2.0, "text": "中国男性是天底下最好的血包"},
        {"start": 4.0, "end": 6.0, "text": "大家还是会这么想"},
    ]
    asr = [
        {"from": 0.0, "to": 2.0, "content": "中国男性是天底下最好的血包"},
        {"from": 2.0, "to": 4.0, "content": "哪怕别人对他的生活对他的过往一无所知"},  # 字幕整段缺失。
        {"from": 4.0, "to": 6.0, "content": "大家还是会这么想"},
    ]

    report = hard.cross_check_with_asr(caption, asr)

    assert report["verdict"] == "asr_covers_caption"  # 结论必须直接可读。
    assert report["caption_only"] == []
    assert report["replacements"] == []
    assert len(report["asr_only"]) == 1
    span = report["asr_only"][0]
    assert span["text"] == "哪怕别人对他的生活对他的过往一无所知"
    assert span["start"] == pytest.approx(2.0)  # 时间戳来自 ASR 分段。
    assert span["end"] == pytest.approx(4.0)
    assert report["similarity"] < 1.0

    identical = hard.cross_check_with_asr(caption, list(caption))
    assert identical["verdict"] == "identical"
    assert identical["asr_only"] == [] and identical["caption_only"] == []

    homophone = hard.cross_check_with_asr(
        [{"start": 0.0, "end": 2.0, "text": "最好的血包"}],
        [{"start": 0.0, "end": 2.0, "text": "最好的雪包"}],
    )
    assert homophone["verdict"] == "both_ways"  # 替换差异两侧都算"多"，需要人工判断。
    assert homophone["replacements"][0]["caption_text"] == "血"
    assert homophone["replacements"][0]["asr_text"] == "雪"


# --- 产出：SRT 时间戳格式正确、折行不丢字，且 SRT/TXT 复用既有实现 ---
def test_emit_writes_valid_srt_without_losing_characters(monkeypatch, tmp_path, capsys):
    hard = load_hard_subtitle()
    video = write_placeholder_video(tmp_path)
    output_dir = tmp_path / "out"
    texts = {
        10: "中国男性是天底下最好的血包",                                  # 14 字，不折行。
        40: "更高级更耐用更能持续供血的血包以及别的补充说明文字用来强制折行测试",  # 远超 24 字，必然折行。
        60: "结尾",
    }

    monkeypatch.setattr(hard, "sample_band_frames",
                        lambda path, settings: (make_samples([10, 10, 10, 40, 40, 60, 60, 60]), 2.0))
    monkeypatch.setattr(hard, "build_recognizer", lambda: (lambda sample: texts[sample.gray[0]]))

    exit_code = hard.main([str(video), "--emit", "srt,txt,json", "-o", str(output_dir)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Saved to:" in captured.out

    srt_text = (output_dir / "hard_subtitle.srt").read_text(encoding="utf-8")
    blocks = [block for block in srt_text.strip().split("\n\n") if block.strip()]
    emitted_texts = []
    for block in blocks:
        lines = block.splitlines()
        assert SRT_TIMESTAMP.match(lines[1]), f"非法 SRT 时间戳行：{lines[1]}"
        emitted_texts.append("".join(lines[2:]))  # 去折行后必须与原文本逐字一致。
    assert emitted_texts == [texts[10], texts[40], texts[60]]
    assert any("\n" in "".join(block.splitlines()[2:]) or len(block.splitlines()) > 3 for block in blocks)

    txt_text = (output_dir / "hard_subtitle.txt").read_text(encoding="utf-8")
    assert txt_text == to_plain_text_reference(hard, [texts[10], texts[40], texts[60]])

    payload = json.loads((output_dir / "hard_subtitle.json").read_text(encoding="utf-8"))
    assert [card["text"] for card in payload["cards"]] == [texts[10], texts[40], texts[60]]
    assert payload["report"]["cards"] == 3  # JSON 同时带卡片列表与自检报告。
    assert payload["report"]["short_cards"] == 0


# --- TXT 必须与 normalize_transcript.to_plain_text 的语义一致 ---
def to_plain_text_reference(hard, texts) -> str:
    from normalize_transcript import to_plain_text  # 参考实现就是全仓唯一实现。
    return to_plain_text([{"start": 0.0, "end": 1.0, "text": text} for text in texts])


# --- OCR 引擎缺失：必须是带安装建议的清晰错误 ---
def test_missing_ocr_engine_raises_actionable_error(monkeypatch, tmp_path, capsys):
    hard = load_hard_subtitle()

    def failing_importer(name):
        raise ImportError(f"No module named '{name}'")

    with pytest.raises(hard.HardSubtitleDependencyError) as error:
        hard.load_ocr_engine(importer=failing_importer)
    message = str(error.value)
    assert "rapidocr" in message
    assert 'pip install -e ".[hard-subtitle]"' in message  # 安装建议必须可直接复制执行。

    video = write_placeholder_video(tmp_path)
    monkeypatch.setattr(hard, "sample_band_frames", lambda path, settings: (make_samples([10, 20]), 1.0))

    def failing_engine(**kwargs):
        raise hard.HardSubtitleDependencyError(f"install with {hard.OCR_INSTALL_HINT}")

    monkeypatch.setattr(hard, "load_ocr_engine", failing_engine)
    exit_code = hard.main([str(video), "--json"])
    captured = capsys.readouterr()

    assert exit_code != 0  # 失败必须返回非零。
    assert "hard-subtitle" in captured.err  # 失败原因写 stderr，stdout 保持干净。


# --- 未知 --emit 值：必须先报错并返回非零，而不是静默忽略 ---
def test_unknown_emit_value_fails_with_nonzero_exit(tmp_path, capsys):
    hard = load_hard_subtitle()
    video = write_placeholder_video(tmp_path)  # 参数校验发生在读视频之前，文件内容无关紧要。

    exit_code = hard.main([str(video), "--emit", "pdf", "-o", str(tmp_path / "out")])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert "unsupported --emit" in captured.err
    assert not (tmp_path / "out").exists()  # 坏参数不产生半成品目录。


# --- 字幕带几何：比例默认，显式像素优先，越界必须收敛 ---
def test_resolve_band_uses_ratios_and_explicit_pixels():
    hard = load_hard_subtitle()

    band = hard.resolve_band(1000, hard.HardSubtitleSettings())
    assert (band.top, band.height) == (620, 250)  # 从 62% 高度开始，高 25%。

    explicit = hard.resolve_band(1000, hard.HardSubtitleSettings(band_height=100))
    assert (explicit.top, explicit.height) == (620, 100)  # 显式像素覆盖比例。

    clamped = hard.resolve_band(1000, hard.HardSubtitleSettings(band_height=9000))
    assert (clamped.top, clamped.height) == (620, 380)  # 不越过画面底边。

    tiny = hard.resolve_band(100, hard.HardSubtitleSettings())
    assert (tiny.top, tiny.height) == (62, 25)


# --- ffmpeg 探测：解析时长与分辨率（不需要真的装 ffmpeg） ---
def test_probe_video_parses_duration_and_size():
    hard = load_hard_subtitle()
    diagnostics = (
        "  Duration: 00:04:19.77, start: 0.000000, bitrate: 1050 kb/s\n"
        "  Stream #0:0: Video: h264, yuv420p, 1080x1920, 30 fps\n"
    )

    def fake_run(command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr=diagnostics)

    info = hard.probe_video("clip.mp4", ffmpeg_path="ffmpeg", run=fake_run)

    assert info["duration"] == pytest.approx(259.77)  # 真实视频时长。
    assert (info["width"], info["height"]) == (1080, 1920)


# --- 抽帧调用可注入：crop 只取字幕带、灰度化、按帧切片 ---
def test_sample_band_frames_uses_injected_ffmpeg_and_slices_frames():
    hard = load_hard_subtitle()
    recorded: list[list[str]] = []
    raw = b"\x01" * 100 + b"\x02" * 100 + b"\x03" * 100  # 3 帧，每帧 4x25 灰度像素。

    def fake_run(command, **_kwargs):
        recorded.append(command)
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    samples, duration = hard.sample_band_frames(
        "clip.mp4",
        hard.HardSubtitleSettings(fps=2.0),
        ffmpeg_path="ffmpeg",
        run=fake_run,
        probe=lambda path: {"duration": 2.0, "width": 4, "height": 100},
    )

    assert "-vf" in recorded[0]
    assert "fps=2.0,crop=4:25:0:62,format=gray" in recorded[0]  # 只取带内区域并灰度化。
    assert duration == pytest.approx(2.0)
    assert [sample.timestamp for sample in samples] == [0.0, 0.5, 1.0]
    assert [sample.gray[0] for sample in samples] == [1, 2, 3]
    assert all(sample.width == 4 and sample.height == 25 for sample in samples)


# --- 端到端（离线）：注入抽帧 + 注入 OCR，产出卡片、自检与交叉校验 ---
def test_extract_cards_pipeline_is_fully_injectable(tmp_path):
    hard = load_hard_subtitle()
    video = write_placeholder_video(tmp_path)
    texts = {10: "中国男性是天底下最好的血包", 40: "更高级", 60: "结尾"}

    result = hard.extract_cards(
        video,
        hard.HardSubtitleSettings(fps=4.0),
        frame_sampler=lambda path, settings: (make_samples([10, 10, 40, 60, 60]), 2.0),
        recognizer=lambda sample: texts[sample.gray[0]],
        asr_timeline=[{"from": 0.0, "to": 1.0, "content": "中国男性是天底下最好的雪包"}],
    )

    assert [card["text"] for card in result["cards"]] == [texts[10], texts[40], texts[60]]
    assert result["report"]["sampled_frames"] == 5
    assert result["report"]["cards"] == 3
    assert result["settings"]["fps"] == 4.0  # 参数回写，结论可复核。
    cross_check = result["report"]["cross_check"]
    assert cross_check["verdict"] == "both_ways"  # 同音字差异必须被摆出来。
    assert cross_check["replacements"][0]["asr_text"] == "雪"
