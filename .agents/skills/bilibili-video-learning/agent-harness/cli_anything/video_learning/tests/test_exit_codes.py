"""
退出码契约的离线测试。
这些测试验证“Agent 能不能只靠退出码区分该重试、该授权、还是该换机器”，
以及抖音与 B站两条链路是否都用了同一份判断。
运行示例：python -m pytest cli_anything/video_learning/tests/test_exit_codes.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import json  # 校验失败时的结构化 JSON 输出。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。

import pytest  # 提供 monkeypatch 与 capsys。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

from cli_anything.video_learning.utils import exit_codes  # noqa: E402 契约的唯一定义处。


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


# --- 造一个"看起来来自 requests"的异常类型 ---
def requests_style_error(message: str) -> BaseException:
    error_type = type("ReadTimeout", (Exception,), {"__module__": "requests.exceptions"})
    return error_type(message)


# --- 每个故障类别必须落到不同退出码 ---
def test_failure_categories_have_distinct_exit_codes():
    codes = [
        exit_codes.EXIT_SUCCESS,
        exit_codes.EXIT_GENERIC_FAILURE,
        exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE,
        exit_codes.EXIT_COOKIE_PERMISSION_REQUIRED,
        exit_codes.EXIT_NETWORK_TIMEOUT,
        exit_codes.EXIT_RATIO_UNAVAILABLE,
        exit_codes.EXIT_TRANSCRIPTION_FAILED,
        exit_codes.EXIT_SOURCES_DISAGREE,
    ]

    assert len(set(codes)) == len(codes)  # 任何一个码都不能与其他码撞车。
    assert exit_codes.EXIT_SUCCESS == 0  # 成功码保持 0，兼容所有 shell 约定。
    assert exit_codes.EXIT_COOKIE_PERMISSION_REQUIRED == 21  # 已有的授权码不得改变。


# --- 本地 ASR 失败：换网络重试没有意义 ---
def test_transcription_failure_maps_to_dedicated_code():
    assert exit_codes.classify_failure(exit_codes.TranscriptionFailedError("no CUDA device")) == \
        exit_codes.EXIT_TRANSCRIPTION_FAILED


# --- 分享页取流失败：可以重试或换下载方式 ---
def test_share_page_failure_maps_to_ssr_code():
    class FakeSSRError(RuntimeError):
        pass

    assert exit_codes.classify_failure(FakeSSRError("share page missing"), (FakeSSRError,)) == \
        exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE
    assert exit_codes.classify_failure(FakeSSRError("share page missing")) == \
        exit_codes.EXIT_GENERIC_FAILURE  # 调用方没有声明该类型时不得靠猜。


# --- 网络故障：标准库与 requests 都要认 ---
def test_network_failures_map_to_timeout_code():
    assert exit_codes.classify_failure(TimeoutError("timed out")) == exit_codes.EXIT_NETWORK_TIMEOUT
    assert exit_codes.classify_failure(ConnectionError("reset")) == exit_codes.EXIT_NETWORK_TIMEOUT
    assert exit_codes.classify_failure(requests_style_error("Read timed out")) == exit_codes.EXIT_NETWORK_TIMEOUT
    assert exit_codes.classify_failure(RuntimeError("curl: timeout after 60s")) == exit_codes.EXIT_NETWORK_TIMEOUT


# --- 画质不可用：应提示降档而不是当成网络问题 ---
def test_ratio_failure_maps_to_ratio_code():
    assert exit_codes.classify_failure(ValueError("Unsupported ratio '2160p'")) == exit_codes.EXIT_RATIO_UNAVAILABLE
    assert exit_codes.classify_failure(RuntimeError("No public play ratio worked for requested ratio 1080p")) == \
        exit_codes.EXIT_RATIO_UNAVAILABLE


# --- 认不出来就老实返回通用失败 ---
def test_unknown_failure_stays_generic():
    assert exit_codes.classify_failure(RuntimeError("something odd")) == exit_codes.EXIT_GENERIC_FAILURE


# --- 抖音主入口：取流失败返回可区分的退出码，并写进 JSON ---
def test_douyin_main_reports_share_page_exit_code(monkeypatch, capsys):
    douyin = load_script("douyin_extract")  # 直接测真实 main()，不复制业务判断。
    failure = douyin.douyin_ssr.DouyinSSRDownloadError(
        "fixture public page failure",
        [{"step": "fetch_share_page", "ok": False, "message": "fixture"}],
    )
    monkeypatch.setattr(douyin, "extract_douyin", lambda *a, **k: (_ for _ in ()).throw(failure))

    exit_code = douyin.main(["--json", "7654321098765432100"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE  # Agent 可以据此重试或换 --download-method。
    assert payload["exit_code"] == exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE  # 结构化输出与退出码一致。
    assert payload["error"] == "fixture public page failure"  # 原有错误信息保持不变。


# --- 抖音主入口：本地 ASR 失败必须与取流失败区分开 ---
def test_douyin_main_reports_transcription_exit_code(monkeypatch, capsys):
    douyin = load_script("douyin_extract")
    monkeypatch.setattr(
        douyin,
        "extract_douyin",
        lambda *a, **k: (_ for _ in ()).throw(exit_codes.TranscriptionFailedError("no CUDA device")),
    )

    exit_code = douyin.main(["--json", "7654321098765432100"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == exit_codes.EXIT_TRANSCRIPTION_FAILED  # 取流已成功，问题在本机运行时。
    assert payload["exit_code"] == exit_codes.EXIT_TRANSCRIPTION_FAILED


# --- 抖音转写步骤：底层异常必须被包装成"本地转写失败" ---
def test_douyin_transcribe_wraps_engine_failure(monkeypatch):
    douyin = load_script("douyin_extract")
    monkeypatch.setattr(
        douyin,
        "transcribe_audio_file",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model load failed")),
    )

    with pytest.raises(exit_codes.TranscriptionFailedError, match="model load failed"):
        douyin.transcribe("audio.wav")


# --- B站链路：分类成功用精确码，未分类保留既有 2 号语义 ---
def test_bilibili_failure_exit_code_keeps_legacy_fallback():
    fetch = load_script("fetch_bilibili")

    assert fetch.resolve_failure_exit_code(TimeoutError("timed out")) == exit_codes.EXIT_NETWORK_TIMEOUT  # 网络可重试
    assert fetch.resolve_failure_exit_code(exit_codes.TranscriptionFailedError("no CUDA")) == \
        exit_codes.EXIT_TRANSCRIPTION_FAILED  # 本地 ASR 失败
    assert fetch.resolve_failure_exit_code(RuntimeError("video is gone")) == 2  # 未分类异常保持历史行为


# --- CLI 与后端脚本必须引用同一份授权码定义 ---
def test_cli_and_backend_share_one_exit_code_contract():
    from cli_anything.video_learning import video_learning_cli  # CLI 是 Agent 最终看到的退出码出口。

    assert video_learning_cli.COOKIE_PERMISSION_EXIT_CODE == exit_codes.EXIT_COOKIE_PERMISSION_REQUIRED  # 不允许两处各写一份。


# --- B站取流失败必须返回"取流不可用"，而不是 0 ---
def test_bilibili_download_failure_sets_share_page_code(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    monkeypatch.setattr(transcribe, "download_audio", lambda bvid, output_dir, cookies=None: (None, "fixture: no audio"))

    result = transcribe.bilibili_transcribe("BV1111111111", output_dir=str(tmp_path))

    assert result["status"] == "error"
    assert result["exit_code"] == exit_codes.EXIT_SHARE_PAGE_UNAVAILABLE  # 过去这里恒为 0，Agent 看不出失败


# --- B站本机 ASR 失败必须与取流失败区分 ---
def test_bilibili_asr_failure_sets_transcription_code(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake")
    monkeypatch.setattr(transcribe, "download_audio", lambda bvid, output_dir, cookies=None: (str(audio), None))
    monkeypatch.setattr(
        transcribe, "transcribe_audio_file",
        lambda *a, **k: (_ for _ in ()).throw(exit_codes.TranscriptionFailedError("no CUDA device")),
    )

    result = transcribe.bilibili_transcribe("BV1ntah6TEe9", output_dir=str(tmp_path))

    assert result["status"] == "error"
    assert result["exit_code"] == exit_codes.EXIT_TRANSCRIPTION_FAILED  # 换机器能解决，不是平台问题


# --- 成功时退出码必须是 0 ---
def test_bilibili_success_keeps_exit_code_zero(monkeypatch, tmp_path):
    transcribe = load_script("transcribe_bilibili")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake")
    monkeypatch.setattr(transcribe, "download_audio", lambda bvid, output_dir, cookies=None: (str(audio), None))
    monkeypatch.setattr(transcribe, "transcribe_audio_file", lambda *a, **k: {
        "engine": "faster-whisper", "device": "cpu", "compute_type": "int8",
        "segments": [{"from": 0.0, "to": 1.0, "content": "正文"}], "diagnostics": [],
    })

    result = transcribe.bilibili_transcribe("BV1ntah6TEe9", output_dir=str(tmp_path))

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
