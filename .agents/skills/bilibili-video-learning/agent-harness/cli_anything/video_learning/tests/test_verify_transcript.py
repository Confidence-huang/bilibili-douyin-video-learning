"""
多源交叉校验器的离线测试。
这些测试守住两件事：差异必须是**穷尽**的（不是抽样），以及缺失正文必须被指名到时间范围。
运行示例：python -m pytest cli_anything/video_learning/tests/test_verify_transcript.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 构造与校验时间轴 JSON。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

import pytest  # 提供临时目录与 capsys。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "asr_vad_dropped_speech.json"
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))


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


# --- 完全一致时不该报任何差异 ---
def test_identical_sources_report_no_spans():
    verify = load_script("verify_transcript")
    timeline = [{"start": 0.0, "end": 1.0, "text": "中国男性是天底下最好的血包"}]

    report = verify.compare_timelines(timeline, list(timeline))

    assert report["spans"] == []
    assert report["similarity"] == 1.0
    assert report["verdict"] == "两份来源在有效字符上完全一致"


# --- 同音替换必须被逐条列出，并带上时间范围 ---
def test_replacement_spans_are_listed_with_timestamps():
    verify = load_script("verify_transcript")
    primary = [{"start": 0.0, "end": 2.0, "text": "中国男性是天底下最好的血包"}]
    secondary = [{"from": 0.0, "to": 2.0, "content": "中国男性是天底下最好的雪包"}]

    report = verify.compare_timelines(primary, secondary)

    assert len(report["spans"]) == 1
    span = report["spans"][0]
    assert span["kind"] == "replace"
    assert span["primary_text"] == "血"
    assert span["secondary_text"] == "雪"
    assert span["start"] == pytest.approx(0.0)
    assert report["summary"]["replacement_spans"] == 1


# --- 单字差异默认必须报（同音字就是这样），但可以显式抬高阈值过滤 ---
def test_single_character_differences_are_reported_by_default():
    verify = load_script("verify_transcript")
    primary = [{"start": 0.0, "end": 1.0, "text": "甲乙丙"}]
    secondary = [{"start": 0.0, "end": 1.0, "text": "甲乙丁"}]

    assert len(verify.compare_timelines(primary, secondary)["spans"]) == 1  # 默认不放过一个字
    assert verify.compare_timelines(primary, secondary, min_span_chars=2)["spans"] == []


# --- 次源漏掉整段正文时必须指名到时间范围（这是"漏采检测"用法） ---
def test_missing_transcript_in_secondary_is_reported_as_a_span():
    verify = load_script("verify_transcript")
    primary = [
        {"start": 0.0, "end": 5.0, "text": "开头"},
        {"start": 5.0, "end": 11.0, "text": "哪怕别人对他的生活对他的过往一无所知"},
        {"start": 11.0, "end": 20.0, "text": "结尾"},
    ]
    secondary = [
        {"start": 0.0, "end": 5.0, "text": "开头"},
        {"start": 11.0, "end": 20.0, "text": "结尾"},
    ]

    report = verify.compare_timelines(primary, secondary)

    assert report["summary"]["primary_only_spans"] == 1
    span = report["spans"][0]
    assert span["side"] == "primary_only"
    assert "哪怕别人对他的生活" in span["primary_text"]
    assert span["start"] == pytest.approx(5.0)
    assert "缺少正文" in report["verdict"]


# --- 真实丢字数据：ASR 缺了 196.24–201.84s 那段 ---
def test_real_dropped_speech_is_visible_against_the_caption_source():
    verify = load_script("verify_transcript")
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 用真实产物的相邻两段夹住丢失区间，构造"权威来源有、ASR 没有"的最小对照
    primary = [
        {"start": 193.86, "end": 195.82, "text": "直到我高考最后一场考试结束以后"},
        {"start": 195.58, "end": 196.24, "text": "就是一个loser"},
        {"start": 196.24, "end": 201.84, "text": "哪怕别人对他的生活对他的过往一无所知大家还是会这么想"},
        {"start": 201.84, "end": 202.76, "text": "但有一天他突然累了"},
    ]
    secondary = [segment for segment in fixture["segments"]
                 if segment["to"] < 196.3 or segment["from"] > 201.8]  # 真实 ASR 在 196.24–201.84 没有内容

    report = verify.compare_timelines(primary, secondary, min_span_chars=4)

    largest = max(report["spans"], key=lambda item: len(item["primary_text"]))
    assert "哪怕别人对他的生活" in largest["primary_text"]  # 整段缺失被完整报出
    assert largest["start"] == pytest.approx(196.24, abs=0.1)  # 时间范围指向真实丢失区间


# --- 报告可渲染成 Markdown 表格 ---
def test_markdown_report_contains_the_difference_table():
    verify = load_script("verify_transcript")
    report = verify.compare_timelines(
        [{"start": 0.0, "end": 1.0, "text": "血包"}],
        [{"start": 0.0, "end": 1.0, "text": "雪包"}],
    )

    markdown = verify.render_report(report)

    assert "| 类型 | 时间范围 | 主源 | 次源 |" in markdown
    assert "血" in markdown and "雪" in markdown


# --- 三种输入格式都能读：规范 JSON、ASR JSON、SRT ---
def test_cli_accepts_canonical_json_asr_json_and_srt(tmp_path, capsys):
    verify = load_script("verify_transcript")
    canonical = tmp_path / "canonical.json"
    audio_source = tmp_path / "asr.json"
    subtitle = tmp_path / "captions.srt"
    canonical.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "中国男性是最好的血包"}]), encoding="utf-8")
    audio_source.write_text(json.dumps({"segments": [{"from": 0.0, "to": 1.0, "content": "中国男性是最好的雪包"}]}),
                           encoding="utf-8")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n中国男性是最好的血包\n", encoding="utf-8")

    assert verify.main(["--primary", str(canonical), "--secondary", str(audio_source)]) == 0
    assert "血" in capsys.readouterr().out

    assert verify.main(["--primary", str(subtitle), "--secondary", str(audio_source), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["replacement_spans"] == 1


# --- --fail-on-difference 用专属退出码让调用方决定是否人工复核 ---
def test_fail_on_difference_uses_the_dedicated_exit_code(tmp_path, capsys):
    verify = load_script("verify_transcript")
    exit_codes = load_script("exit_codes") if False else None  # 退出码常量由 runtime_output 导出
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "血包"}]), encoding="utf-8")
    second.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "雪包"}]), encoding="utf-8")

    assert verify.main(["--primary", str(first), "--secondary", str(second)]) == 0  # 默认只报告
    capsys.readouterr()
    assert verify.main(["--primary", str(first), "--secondary", str(second), "--fail-on-difference"]) == 25


# --- 输入文件缺失或格式不支持时必须清晰失败 ---
def test_bad_inputs_fail_clearly(tmp_path, capsys):
    verify = load_script("verify_transcript")
    missing = tmp_path / "nope.json"
    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("hello", encoding="utf-8")

    assert verify.main(["--primary", str(missing), "--secondary", str(missing)]) == 1
    assert "not found" in capsys.readouterr().err

    assert verify.main(["--primary", str(unsupported), "--secondary", str(unsupported)]) == 1
    assert "unsupported timeline format" in capsys.readouterr().err


# --- 新退出码与既有码不得重复 ---
def test_sources_disagree_code_is_distinct():
    from cli_anything.video_learning.utils import exit_codes

    codes = [exit_codes.EXIT_SUCCESS, exit_codes.EXIT_GENERIC_FAILURE, exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE,
             exit_codes.EXIT_COOKIE_PERMISSION_REQUIRED, exit_codes.EXIT_NETWORK_TIMEOUT,
             exit_codes.EXIT_RATIO_UNAVAILABLE, exit_codes.EXIT_TRANSCRIPTION_FAILED,
             exit_codes.EXIT_SOURCES_DISAGREE]

    assert len(set(codes)) == len(codes)
    assert exit_codes.EXIT_SOURCES_DISAGREE == 25
