"""B站 素材实测：① 长视频分块标定 ② 音频前端在音乐/口播上的行为 ③ 覆盖率兜底的真实触发情况。

全部用仓库自己的函数（不另写一套），结果写到 docs/experiments/results.json + SUMMARY.md。
"""
import difflib, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path("/home/a/deepseek_project/Project_7 学习视频/work/bilibili-douyin-video-learning")
SKILL = ROOT / ".agents/skills/bilibili-video-learning"
WORK = ROOT / "outputs/experiments"
sys.path.insert(0, str(SKILL / "scripts"))
os.environ.setdefault("HF_HOME", "/home/a/.cache/hf")

import asr_coverage                      # 读音频时长（仓库自己的实现）
import media_tools                      # 找 ffmpeg
import speech_to_text as A               # 仓库自己的转写入口

FREE = media_tools.find_ffmpeg()
LONG_BV, MUSIC_BV = "BV154hD61Ez8", "BV1zKZrYAEi8"   # 46 分钟数学课 / 1 分钟民谣


# --- 用 yt-dlp 取音频轨（与 transcribe_bilibili 同一路径），再统一成 16k 单声道 ---
def fetch_audio(bv: str) -> Path:
    raw = WORK / f"{bv}.m4a"
    if not raw.exists():
        subprocess.run([sys.executable, "-m", "yt_dlp", "-f", "bestaudio", "-o", str(raw),
                        f"https://www.bilibili.com/video/{bv}"], check=True, capture_output=True)
    wav = WORK / f"{bv}.wav"
    if not wav.exists():
        subprocess.run([FREE, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                        "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    return wav


def slice_audio(source: Path, seconds: float, target: Path) -> Path:
    if not target.exists():
        subprocess.run([FREE, "-hide_banner", "-loglevel", "error", "-y", "-t", f"{seconds}",
                        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(target)], check=True)
    return target


def run(wav: Path, **settings) -> dict:
    started = time.time()
    result = A.transcribe_audio_file(wav, model_size="small", settings=A.TranscriptionSettings(**settings))
    elapsed = round(time.time() - started, 1)
    text = "".join(str(item.get("content") or "") for item in result["segments"])
    return {"elapsed": elapsed, "segments": len(result["segments"]), "chars": len(text), "text": text,
            "chunked": result.get("chunked"), "chunks": len(result.get("chunks") or []),
            "diagnostics": [item.get("step") for item in result.get("diagnostics") or []],
            "device": result.get("device"), "raw": result}


results = {}
long_wav = fetch_audio(LONG_BV)
duration = round(asr_coverage.audio_duration_seconds(long_wav) or 0, 1)
# ① 长视频：整段 vs 10 分钟块
plain = run(long_wav, chunk_length=0.0)
chunked = run(long_wav, chunk_length=600.0, chunk_overlap=2.0)
ratio = difflib.SequenceMatcher(None, plain["text"], chunked["text"], autojunk=False).ratio()
results["long_video"] = {"bv": LONG_BV, "duration_s": duration,
                         "plain": {k: v for k, v in plain.items() if k != "raw"},
                         "chunked": {k: v for k, v in chunked.items() if k != "raw"},
                         "similarity": round(ratio, 4),
                         "chars_delta": chunked["chars"] - plain["chars"]}

# ② 音频前端：音乐 1 分钟 + 口播前 3 分钟，各跑开/关
music_wav = fetch_audio(MUSIC_BV)
speech_slice = slice_audio(long_wav, 180.0, WORK / "speech_180s.wav")
for label, wav in (("music", music_wav), ("speech", speech_slice)):
    on = run(wav, normalize_audio=True)
    off = run(wav, normalize_audio=False)
    results[f"frontend_{label}"] = {
        "with_normalize": {k: v for k, v in on.items() if k != "raw"},
        "without_normalize": {k: v for k, v in off.items() if k != "raw"},
        "similarity": round(difflib.SequenceMatcher(None, on["text"], off["text"], autojunk=False).ratio(), 4)}

(WORK / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({"long_similarity": results["long_video"]["similarity"],
                  "long_plain": {k: results["long_video"]["plain"][k] for k in ("elapsed", "segments", "chars")},
                  "long_chunked": {k: results["long_video"]["chunked"][k] for k in ("elapsed", "segments", "chars", "chunks")},
                  "music": {k: results["frontend_music"][k][k2] for k in ("with_normalize", "without_normalize") for k2 in ("chars",)},
                  "speech": {k: results["frontend_speech"][k][k2] for k in ("with_normalize", "without_normalize") for k2 in ("chars",)}},
                 ensure_ascii=False, indent=1))
