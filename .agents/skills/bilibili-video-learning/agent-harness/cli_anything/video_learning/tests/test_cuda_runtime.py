"""
CUDA 运行时库发现与预加载的离线测试。
这些测试守住一件事：**设备可见不等于 GPU 可用**——缺 cuBLAS/cuDNN 时必须报出可用性并给出可执行建议，
而不是等到第一次推理才抛 "Library libcublas.so.12 is not found"。
运行示例：python -m pytest cli_anything/video_learning/tests/test_cuda_runtime.py -v
"""
from __future__ import annotations  # 测试使用现代类型标注，同时保持 Python 3.10+ 兼容。

import importlib.util  # 按真实脚本路径加载 live Skill，而不是复制业务实现。
import os  # 验证 LD_LIBRARY_PATH 只被追加一次。
import sys  # 把 scripts 目录加入模块搜索路径，保持脚本原有绝对导入可用。
from pathlib import Path  # 从测试文件稳定定位 Skill 根目录。
from types import SimpleNamespace  # 构造假的 runtime 与假引擎模块。

import pytest  # 提供 monkeypatch 与临时目录。


SKILL_ROOT = Path(__file__).resolve().parents[4]  # tests -> video_learning -> cli_anything -> agent-harness -> Skill。
SCRIPTS_DIR = SKILL_ROOT / "scripts"  # live 后端脚本的唯一来源目录。
if str(SCRIPTS_DIR) not in sys.path:  # 动态加载脚本前先满足它们的同目录导入。
    sys.path.insert(0, str(SCRIPTS_DIR))

import asr_coverage  # noqa: E402 与 speech_to_text 共用同一实例，便于注入时长探测。
import cuda_runtime  # noqa: E402 与被测的生产模块共用同一实例。


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


# --- 造一棵假的 site-packages/nvidia 目录树 ---
def fake_nvidia_tree(root: Path, packages=("cublas", "cudnn", "cuda_runtime")) -> Path:
    site_packages = root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    names = {"cublas": ("libcublas.so.12", "libcublasLt.so.12"),
             "cudnn": ("libcudnn.so.9",),
             "cuda_runtime": ("libcudart.so.12",)}
    for package in packages:
        for name in names.get(package, ()):
            target = site_packages / "nvidia" / package / "lib" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"not-a-real-library")  # 只验证发现逻辑，不真的 dlopen
    return site_packages


# --- 目录发现：能从 sys.prefix 推导出 nvidia/*/lib ---
def test_candidate_directories_are_discovered_under_the_interpreter_prefix(monkeypatch, tmp_path):
    fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))

    directories = cuda_runtime.candidate_library_dirs()

    assert any(path.endswith(os.path.join("nvidia", "cublas", "lib")) for path in directories)
    assert any(path.endswith(os.path.join("nvidia", "cudnn", "lib")) for path in directories)


# --- 库发现：四个运行时都会被认出来（含 cublasLt） ---
def test_discover_libraries_finds_every_runtime(monkeypatch, tmp_path):
    fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))

    found = cuda_runtime.discover_libraries()

    assert set(found) == {"cublas", "cublasLt", "cudnn", "cudart"}


# --- 没有库时要给出可执行的安装建议，而不是静默返回 ---
def test_missing_libraries_return_actionable_guidance(tmp_path):
    report = cuda_runtime.prepare_cuda_libraries(directories=[str(tmp_path / "empty")])

    assert report["found"] == {}
    assert report["preloaded"] == []
    assert "gpu-cuda12" in report["guidance"]  # 建议里必须带上确切的 pip extra


# --- 找到但加载失败（假文件）要报错但不断链 ---
def test_unloadable_libraries_are_reported_without_raising(monkeypatch, tmp_path):
    fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))

    report = cuda_runtime.prepare_cuda_libraries()

    assert report["found"]  # 文件确实被发现了
    assert report["preloaded"] == []  # 假文件当然加载不了
    assert report["errors"]  # 但要明确报错，而不是假装成功
    assert "gpu-cuda12" in (report.get("guidance") or "")


# --- 预加载必须幂等，且两个平台各自用对的机制 ---
def test_prepare_is_idempotent_for_the_library_path(monkeypatch, tmp_path):
    site_packages = fake_nvidia_tree(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/existing/path")

    first_report = cuda_runtime.prepare_cuda_libraries()
    second_report = cuda_runtime.prepare_cuda_libraries()

    if os.name == "nt":
        # Windows 用 os.add_dll_directory 注册 DLL 目录，不应改动 LD_LIBRARY_PATH
        assert os.environ["LD_LIBRARY_PATH"] == "/existing/path"
        assert first_report["platform"] == "windows"
        assert second_report["platform"] == "windows"  # 第二次调用同样不抛错
    else:
        first = os.environ["LD_LIBRARY_PATH"]
        assert os.environ["LD_LIBRARY_PATH"] == first  # 第二次不重复追加
        assert first.split(os.pathsep).count(str(site_packages / "nvidia" / "cublas" / "lib")) == 1
        assert first.endswith("/existing/path")  # 既有条目被保留在后面
        assert first_report["path_updated"] is True


# --- 可用性判断：三种情形必须给出不同结论 ---
def test_usability_distinguishes_device_from_runtime_libraries(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(get_cuda_device_count=lambda: 1))
    monkeypatch.setattr(cuda_runtime, "prepare_cuda_libraries",
                        lambda *a, **k: {"found": {}, "preloaded": [], "guidance": "install it"})
    no_libraries = cuda_runtime.describe_gpu_usability()
    assert no_libraries["usable"] is False
    assert "缺少 CUDA 运行时库" in no_libraries["guidance"]

    monkeypatch.setattr(cuda_runtime, "prepare_cuda_libraries",
                        lambda *a, **k: {"found": {"cublas": "x", "cudnn": "y"}, "preloaded": ["x", "y"]})
    usable = cuda_runtime.describe_gpu_usability()
    assert usable["usable"] is True
    assert usable["guidance"] is None

    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(get_cuda_device_count=lambda: 0))
    no_device = cuda_runtime.describe_gpu_usability()
    assert no_device["usable"] is False
    assert "cpu/int8" in no_device["guidance"]  # 没有设备时明确说明会退回 CPU


# --- ASR 入口必须在导入 ctranslate2 之前预加载（真实故障的修复点） ---
def test_asr_preloads_cuda_libraries_before_choosing_a_device(monkeypatch, tmp_path):
    speech_to_text = load_script("speech_to_text")
    calls: list[str] = []  # 记录执行顺序

    def fake_prepare(*args, **kwargs):
        calls.append("prepare")
        return {"found": {"cublas": "x"}, "preloaded": ["x"], "guidance": None,
                "searched_directories": [], "errors": [], "path_updated": False, "platform": "posix"}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            calls.append("model")

        def transcribe(self, audio, **kwargs):
            calls.append("transcribe")
            return iter([SimpleNamespace(text="正文", start=0.0, end=1.0, avg_logprob=-0.1,
                                          no_speech_prob=0.0, compression_ratio=1.0)]), SimpleNamespace()

    monkeypatch.setattr(cuda_runtime, "prepare_cuda_libraries", fake_prepare)
    monkeypatch.setattr(speech_to_text, "_choose_ctranslate2_device",
                        lambda device: (calls.append("choose_device"), ("cpu", "int8", 0))[1])
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(asr_coverage, "audio_duration_seconds", lambda path: None)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = speech_to_text.transcribe_audio_file(audio,
                                                  settings=speech_to_text.TranscriptionSettings(coverage_check=False))

    assert calls[:3] == ["prepare", "choose_device", "model"]  # 顺序就是修复点
    assert result["cuda_runtime"]["preloaded"] == ["x"]  # 诊断里能看到预加载了什么


# --- doctor 必须报告可用性，并在不可用时给出建议 ---
def test_doctor_reports_usability_and_guidance():
    doctor = load_script("doctor") if False else None  # doctor 位于 harness 包内，按包导入
    from cli_anything.video_learning.core import doctor as doctor_module

    fake_cuda = SimpleNamespace(describe_gpu_usability=lambda: {
        "device_count": 1, "runtime_libraries": [], "preloaded": [],
        "usable": False, "guidance": "install the gpu-cuda12 extra", "error": None,
    })
    runtime = SimpleNamespace(load_script=lambda name: fake_cuda)

    gpu = doctor_module._inspect_gpu(runtime)

    assert gpu["available"] is True  # 设备可见
    assert gpu["usable"] is False  # 但运行时库缺失
    assert "gpu-cuda12" in gpu["guidance"]  # 建议直达可执行命令
    assert gpu["runtime_libraries"] == []


# --- 没有 runtime 时保持历史行为（只报可见性） ---
def test_doctor_without_runtime_keeps_the_legacy_shape():
    from cli_anything.video_learning.core import doctor as doctor_module

    gpu = doctor_module._inspect_gpu()

    assert {"available", "devices", "error"} <= set(gpu)
