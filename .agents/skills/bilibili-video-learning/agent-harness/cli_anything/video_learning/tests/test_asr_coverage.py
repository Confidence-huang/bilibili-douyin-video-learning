"""
ASR 覆盖率兜底的离线契约测试。
这些测试直接加载 live Skill 脚本，用真实丢字数据和注入式探测验证：
“不能静默丢掉语音、不能在没有证据时补转、不能把补转结果拼错时间轴”。
运行示例：python -m pytest cli_anything/video_learning/tests/test_asr_coverage.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 读取真实丢字 fixture。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录与 fixture。
from types import SimpleNamespace  # 模拟 subprocess.CompletedProcess 的最小字段。

SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "asr_vad_dropped_speech.json"
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

import asr_coverage  # noqa: E402 覆盖率逻辑：与生产脚本共用同一模块实例，便于注入探测。
import pytest  # noqa: E402 提供 monkeypatch 与临时目录。


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


# --- 真实丢字数据：fixture 就是一次真实运行的产物 ---
def load_dropped_speech_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# --- 一次投毒的音频文件：只用于通过 exists() 校验，不被真正解码 ---
def write_placeholder_audio(tmp_path: Path) -> Path:
    audio = tmp_path / "audio.wav"  # 覆盖层不会读它的内容。
    audio.write_bytes(b"RIFF")
    return audio


# --- 真实丢字必须被定位到唯一空档 ---
def test_real_dropped_speech_is_reported_as_one_gap():
    fixture = load_dropped_speech_fixture()  # 180 段真实分段 + 真实时长 259.77s。
    gaps = asr_coverage.find_coverage_gaps(fixture["segments"], fixture["audio_duration"], 2.0)

    assert len(gaps) == 1  # 这份真实产物只有这一处整段丢失。
    assert gaps[0]["start"] == pytest.approx(196.24, abs=0.01)  # 丢失区间起点。
    assert gaps[0]["end"] == pytest.approx(201.84, abs=0.01)  # 丢失区间终点。
    assert gaps[0]["seconds"] == pytest.approx(5.6, abs=0.01)  # 5.6 秒正文被静默丢弃。
    assert asr_coverage.coverage_ratio(fixture["segments"], fixture["audio_duration"]) < 0.98  # 覆盖率能反映这次丢失。


# --- 开头和结尾被吃掉也要能发现 ---
def test_head_and_tail_gaps_are_detected():
    segments = [{"from": 4.0, "to": 10.0, "content": "中段"}]  # 前后各留出可检测的空洞。
    gaps = asr_coverage.find_coverage_gaps(segments, 15.0, 2.0)

    assert [(gap["start"], gap["end"]) for gap in gaps] == [(0.0, 4.0), (10.0, 15.0)]  # 头部与尾部各一条。


# --- 重叠分段不能把覆盖率算超过 100% ---
def test_coverage_ratio_counts_overlap_once():
    segments = [  # 两段完全重叠，真实覆盖只有 2 秒。
        {"from": 0.0, "to": 2.0, "content": "甲"},
        {"from": 0.0, "to": 2.0, "content": "乙"},
    ]

    assert asr_coverage.coverage_ratio(segments, 4.0) == 0.5  # 0.5 而不是 1.0。


# --- 安静的空档是正常停顿，不该补转 ---
def test_quiet_gap_is_not_retried():
    gaps = [{"start": 10.0, "end": 14.0, "seconds": 4.0}]  # 4 秒空档。
    selected = asr_coverage.select_retry_windows("audio.wav", gaps, -35.0, 5, 60.0,
                                                measure=lambda path, start, end: -60.0)

    assert selected == []  # 静音空档不花时间补转。


# --- 有声音的空档必须补转，并把音量证据带上 ---
def test_loud_gap_is_selected_with_volume_evidence():
    gaps = [{"start": 196.24, "end": 201.84, "seconds": 5.6}]  # 真实丢字区间。
    selected = asr_coverage.select_retry_windows("audio.wav", gaps, -35.0, 5, 60.0,
                                                measure=lambda path, start, end: -12.9)

    assert len(selected) == 1  # 命中唯一可疑窗口。
    assert selected[0]["dbfs"] == pytest.approx(-12.9)  # 实测音量写进诊断，判断可复核。


# --- 拿不到音量时保守跳过，不误报 ---
def test_unmeasurable_gap_is_skipped():
    gaps = [{"start": 10.0, "end": 20.0, "seconds": 10.0}]
    selected = asr_coverage.select_retry_windows("audio.wav", gaps, -35.0, 5, 60.0,
                                                measure=lambda path, start, end: None)

    assert selected == []  # 无证据不补转。


# --- 补转预算与超长窗口都必须被限制 ---
def test_retry_budget_and_length_limits_are_enforced():
    gaps = [{"start": float(index * 100), "end": float(index * 100 + 5), "seconds": 5.0} for index in range(8)]
    loud = lambda path, start, end: -10.0  # 全部判定为有人说话。

    capped = asr_coverage.select_retry_windows("audio.wav", gaps, -35.0, 2, 60.0, measure=loud)
    assert len(capped) == 2  # 预算封顶，避免一次运行补转过多。

    long_gaps = [{"start": 0.0, "end": 120.0, "seconds": 120.0}]
    assert asr_coverage.select_retry_windows("audio.wav", long_gaps, -35.0, 5, 60.0, measure=loud) == []  # 超长窗口跳过。


# --- 合并补转结果：替换旧段、保持时间单调、去重 ---
def test_merge_replaces_inside_window_and_keeps_order():
    segments = [
        {"from": 0.0, "to": 5.0, "content": "开头"},
        {"from": 20.0, "to": 25.0, "content": "结尾"},
    ]
    windows = [{"start": 5.0, "end": 20.0}]  # 中间被吞掉的区间。
    retried = [
        {"from": 12.0, "to": 15.0, "content": "补回来的中段"},
        {"from": 12.0, "to": 15.0, "content": "补回来的中段"},  # 重复结果只保留一条。
    ]
    merged = asr_coverage.merge_retried_segments(segments, retried, windows)

    assert [item["content"] for item in merged] == ["开头", "补回来的中段", "结尾"]  # 顺序与去重都正确。
    assert [item["from"] for item in merged] == sorted(item["from"] for item in merged)  # 时间轴单调。


# --- 补转窗口内的旧分段必须被移除 ---
def test_merge_removes_original_segments_inside_window():
    segments = [{"from": 4.0, "to": 6.0, "content": "旧"}, {"from": 30.0, "to": 31.0, "content": "保留"}]
    merged = asr_coverage.merge_retried_segments(
        segments,
        [{"from": 4.5, "to": 5.5, "content": "新"}],
        [{"start": 4.0, "end": 6.0}],
    )

    assert [item["content"] for item in merged] == ["新", "保留"]  # 旧段被新段替换。


# --- ffmpeg 探测：解析时长与平均音量（不需要真的装 ffmpeg） ---
def test_ffmpeg_probes_parse_duration_and_volume(monkeypatch):
    def fake_run(command, **_kwargs):
        if "volumedetect" in command:  # 音量探测走 volumedetect 过滤器。
            return SimpleNamespace(returncode=0, stdout="", stderr="[Parsed_volumedetect_0] mean_volume: -12.9 dB\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="  Duration: 00:04:19.77, start: 0.000000, bitrate: 1050 kb/s\n")

    monkeypatch.setattr(asr_coverage.subprocess, "run", fake_run)  # 生产代码用的是同一 subprocess 模块。
    monkeypatch.setattr(asr_coverage, "find_ffmpeg", lambda: "ffmpeg")  # 不依赖本机是否安装 ffmpeg。

    assert asr_coverage.audio_duration_seconds("audio.wav") == pytest.approx(259.77)  # 4 分 19.77 秒。
    assert asr_coverage.measure_window_dbfs("audio.wav", 196.24, 201.84) == pytest.approx(-12.9)  # 真实丢字窗口音量。


# --- 关键行为：第一遍整段丢字时，自动切出该窗口重新解码并补回 ---
def test_transcribe_audio_file_recovers_dropped_speech(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")  # 加载生产入口。
    fixture = load_dropped_speech_fixture()  # 第一遍就是这份真实丢字结果。
    audio = write_placeholder_audio(tmp_path)  # 只满足存在性校验。
    window_calls: list[dict] = []  # 记录窗口补转的调用次数与切片路径。

    def fake_engine(audio_path, settings, device, log_prefix):
        result = {
            "engine": "faster-whisper",
            "model_size": settings.model_size,
            "device": "cpu",
            "compute_type": "int8",
            "cuda_devices": 0,
            "language": "zh",
            "language_probability": 1.0,
            "segments": [dict(segment) for segment in fixture["segments"]],
            "diagnostics": [{"engine": "faster-whisper", "ok": True, "message": "ok"}],
        }

        def transcribe_window(clip_path):
            window_calls.append({"clip": str(clip_path)})  # 生产代码只交出切片路径。
            return [
                {"from": 0.0, "to": 1.26, "content": "哪怕别人对他的生活"},
                {"from": 1.26, "to": 2.56, "content": "对他的过往一无所知"},
                {"from": 2.56, "to": 3.56, "content": "大家还是会这么想"},
                {"from": 3.56, "to": 4.56, "content": "甚至哪怕是一个所谓成功的男性"},
            ]

        return result, transcribe_window

    monkeypatch.setattr(speech_to_text, "_run_faster_whisper", fake_engine)  # 不加载真实模型。
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: fixture["audio_duration"])
    monkeypatch.setattr(asr_coverage, "measure_window_dbfs", lambda path, start, end: -12.9)  # 实测该窗口有人声。
    monkeypatch.setattr(asr_coverage, "cut_audio_window", lambda path, start, end, out: Path(out))

    result = speech_to_text.transcribe_audio_file(audio, model_size="large-v3")

    assert len(window_calls) == 1  # 只补转这一个可疑窗口。
    assert result["coverage_before"] < result["coverage_after"]  # 覆盖率必须真的被修好。
    assert result["audio_duration"] == pytest.approx(fixture["audio_duration"])  # 暴露覆盖率分母供调用方复核。
    assert len(result["segments"]) > len(fixture["segments"])  # 分段数增加，说明补回了正文。
    assert "哪怕别人对他的生活" in result["text"]  # 补回的正文进入全文。
    assert "甚至哪怕是一个所谓成功的男性" in result["text"]  # 关键论证不再缺失。
    coverage_diagnostics = [item for item in result["diagnostics"] if item.get("step") == "asr_coverage"]
    assert coverage_diagnostics and coverage_diagnostics[0]["ok"] is False  # 明确记录“第一遍确实丢了正文”。
    assert coverage_diagnostics[0]["retried_windows"][0]["dbfs"] == pytest.approx(-12.9)  # 补转依据可复核。


# --- 补转窗口必须以关闭 VAD 的条件重新解码 ---
def test_window_retry_disables_vad(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    recorded: dict = {}  # 保存窗口解码时真实使用的参数。

    class FakeModel:  # 只实现测试关心的 transcribe 接口。
        def __init__(self, *args, **kwargs):
            self.first_call = True

        def transcribe(self, audio, **kwargs):
            if self.first_call:  # 第一遍：整段返回空，模拟真实产物里的整段丢失。
                self.first_call = False
                recorded["first_pass"] = kwargs
                return iter([]), SimpleNamespace(language="zh", language_probability=1.0)
            recorded["window"] = kwargs  # 第二遍：补转窗口。
            return iter([SimpleNamespace(text="补回来的正文", start=0.0, end=1.5)]), SimpleNamespace()

    fake_module = SimpleNamespace(WhisperModel=FakeModel)  # 冒充 faster_whisper 模块。
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: 12.0)
    monkeypatch.setattr(asr_coverage, "measure_window_dbfs", lambda path, start, end: -10.0)
    monkeypatch.setattr(asr_coverage, "cut_audio_window", lambda path, start, end, out: Path(out))

    # 第一遍没有任何分段 -> 整段被判定为空洞，且实测有人声，必然触发窗口补转。
    result = speech_to_text.transcribe_audio_file(write_placeholder_audio(tmp_path))

    assert recorded["first_pass"]["vad_filter"] is True  # 第一遍仍按默认参数解码。
    assert recorded["window"]["vad_filter"] is False  # 补转换一种解码条件，且在隔离上下文里重跑。
    assert "补回来的正文" in result["text"]  # 窗口结果被并回全文。


# --- 显式关闭覆盖率校验时保持历史行为 ---
def test_coverage_check_can_be_disabled(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    fixture = load_dropped_speech_fixture()

    def fake_engine(audio_path, settings, device, log_prefix):
        return (
            {
                "engine": "faster-whisper", "model_size": settings.model_size, "device": "cpu",
                "compute_type": "int8", "cuda_devices": 0, "language": "zh",
                "language_probability": 1.0, "segments": [dict(segment) for segment in fixture["segments"]],
                "diagnostics": [{"engine": "faster-whisper", "ok": True, "message": "ok"}],
            },
            lambda clip: pytest.fail("coverage disabled must not retry any window"),  # 不该被调用。
        )

    monkeypatch.setattr(speech_to_text, "_run_faster_whisper", fake_engine)
    settings = speech_to_text.TranscriptionSettings(coverage_check=False)  # 显式关闭兜底。
    result = speech_to_text.transcribe_audio_file(write_placeholder_audio(tmp_path), settings=settings)

    assert len(result["segments"]) == len(fixture["segments"])  # 分段数与第一遍一致。
    assert result["coverage_before"] is None  # 未校验时不伪造覆盖率数字。


# --- 一次转写任务的参数身份必须覆盖所有会改变结果的开关 ---
def test_settings_identity_covers_cache_relevant_parameters():
    speech_to_text = load_script("speech_to_text")

    default_identity = speech_to_text.TranscriptionSettings().identity()
    changed_vad = speech_to_text.TranscriptionSettings(vad_filter=False).identity()
    changed_beam = speech_to_text.TranscriptionSettings(beam_size=5).identity()

    assert default_identity["params_version"] == speech_to_text.ASR_PARAMS_VERSION  # 参数版本进身份，改默认值即可让旧缓存失效。
    assert default_identity != changed_vad  # VAD 开关变化必须改变身份。
    assert default_identity != changed_beam  # beam 变化必须改变身份。
    assert {"model_size", "language", "beam_size", "vad_filter", "coverage_check"} <= set(default_identity)  # 关键字段齐全。


# --- VAD 激进程度必须真的传到引擎，而不是留在参数对象里 ---
def test_vad_parameters_reach_the_engine(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    captured: dict = {}  # 保存引擎真实收到的参数。

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            return iter([]), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: None)  # 时长未知时跳过覆盖率校验
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    speech_to_text.transcribe_audio_file(
        audio,
        settings=speech_to_text.TranscriptionSettings(vad_min_silence_ms=400, vad_speech_pad_ms=200),
    )

    assert captured["vad_filter"] is True  # 默认仍开启 VAD
    assert captured["vad_parameters"] == {"min_silence_duration_ms": 400, "speech_pad_ms": 200}  # 参数真实生效


# --- 关闭 VAD 时不应再传 VAD 参数 ---
def test_vad_parameters_are_omitted_when_vad_is_disabled(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    captured: dict = {}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            return iter([]), SimpleNamespace(language="zh", language_probability=1.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device", lambda device: ("cpu", "int8", 0))
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: None)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    speech_to_text.transcribe_audio_file(audio, settings=speech_to_text.TranscriptionSettings(vad_filter=False))

    assert captured["vad_filter"] is False
    assert captured["vad_parameters"] is None  # 关掉过滤时不该再传会改变行为的参数


# --- VAD 激进程度属于会改变结果的参数，必须进缓存身份 ---
def test_vad_aggressiveness_changes_the_parameter_identity():
    speech_to_text = load_script("speech_to_text")

    default_identity = speech_to_text.TranscriptionSettings().identity()
    aggressive_identity = speech_to_text.TranscriptionSettings(vad_min_silence_ms=400).identity()

    assert default_identity["vad_min_silence_ms"] == 2000  # 默认值与 faster-whisper 保持一致
    assert default_identity != aggressive_identity  # 调过 VAD 就不能复用旧缓存


# --- 丢字与 VAD 无关：第一遍关闭 VAD 也必须照常校验（消融实验结论，见 D16） ---
def test_coverage_guard_runs_even_when_the_first_pass_had_vad_disabled(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    fixture = load_dropped_speech_fixture()
    retried: list[str] = []  # 记录是否真的发生了窗口补转。

    def fake_engine(audio_path, settings, device, log_prefix):
        return (
            {
                "engine": "faster-whisper", "model_size": settings.model_size, "device": "cpu",
                "compute_type": "int8", "cuda_devices": 0, "language": "zh",
                "language_probability": 1.0, "segments": [dict(segment) for segment in fixture["segments"]],
                "diagnostics": [{"engine": "faster-whisper", "ok": True, "message": "ok"}],
            },
            lambda clip: retried.append(str(clip)) or [{"from": 0.0, "to": 5.6, "content": "补回来的正文"}],
        )

    monkeypatch.setattr(speech_to_text, "_run_faster_whisper", fake_engine)
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: fixture["audio_duration"])
    monkeypatch.setattr(asr_coverage, "measure_window_dbfs", lambda path, start, end: -12.9)
    monkeypatch.setattr(asr_coverage, "cut_audio_window", lambda path, start, end, out: Path(out))

    settings = speech_to_text.TranscriptionSettings(vad_filter=False)  # 第一遍就关掉了 VAD
    result = speech_to_text.transcribe_audio_file(write_placeholder_audio(tmp_path), settings=settings)

    assert retried, "关闭 VAD 并不能保证不丢字，覆盖率校验必须照常执行"
    assert "补回来的正文" in result["text"]  # 补转结果照样并回全文
