"""
抖音深度归档（agent 侧入口）：链接 → 本地桥接取视频 → 场景抽帧 → 本地 GPU 转写 → 图文对照 → 写回收件箱笔记。
与 Obsidian 插件(douyin-vault-link deep-archive)共用同一份笔记契约：douyin_id 定位笔记、图文对照节、
frames_extracted/transcript_provider/deep_archived_at 三个 YAML 字段、帧落在 附件/douyin-media/frames/<id>/。
转写走本地 faster-whisper，不需要任何云端 Key；视频经登录态桥接获取（端点由 --bridge 或
DOUYIN_VIDEO_BRIDGE 提供，仓库不内置主机地址），本机不落库。
调用示例：python douyin_deep_archive.py --id 6639535529765375240 --json
"""
from __future__ import annotations  # 保持现代类型标注兼容 Python 3.10+。

import argparse  # 稳定命令行契约：链接或 ID、收件箱与媒体目录均可显式覆盖。
import base64  # 桥接 /download 返回 base64 视频字节。
import json  # stdout 契约 = 一份 JSON 结果文档。
import os  # 读取 BILIBILI_OBSIDIAN_VAULT 与 LOCALAPPDATA。
import re  # 解析 ffmpeg 场景打分行与定位笔记 frontmatter。
import subprocess  # 调 ffmpeg 完成场景检测与抽帧。
import sys  # 退出码区分成功与失败。
import urllib.request  # 与本地桥接通信（仅 127.0.0.1）。
from datetime import datetime, timezone  # deep_archived_at 用 UTC ISO 时间戳。
from pathlib import Path  # 统一路径处理。

from file_output import write_text_atomically  # 笔记写入与插件同款：临时文件 + 原子替换。
from runtime_output import log  # 进度走 stderr，stdout 只留 JSON。
from speech_to_text import transcribe_audio_file  # 复用 faster-whisper 优先的统一转写入口。

DEFAULT_SCENE_FLOOR = 0.025  # 与插件设置同名同值：场景分数硬下限。
DEFAULT_REL_FACTOR = 0.08  # 相对最高分的比例下限。
DEFAULT_MIN_GAP = 1.0  # 相邻帧最小间隔秒。
DEFAULT_MERGE = 0.6  # 局部极大值合并窗口秒。
BRIDGE_TIMEOUT = 300  # 桥接下载大视频的宽裕超时。


# --- 解析 ffmpeg select=scene 的打分行 ---
def parse_scene_scores(text: str) -> list[tuple[float, float]]:
    frames: list[tuple[float, float]] = []
    pts: float | None = None
    for line in text.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            pts = float(m.group(1))
        m = re.search(r"lavfi\.scene_score=([\d.eE+-]+)", line)
        if m and pts is not None:
            frames.append((pts, float(m.group(1))))
    return frames


# --- 自适应峰值选帧：与插件同一算法（局部极大值 ≥ max(硬下限, 最高分×相对系数)，贪心保间隔） ---
def adaptive_peaks(scores: list[tuple[float, float]], max_frames: int = 24, floor_abs: float = DEFAULT_SCENE_FLOOR,
                   rel_factor: float = DEFAULT_REL_FACTOR, min_gap: float = DEFAULT_MIN_GAP,
                   merge: float = DEFAULT_MERGE) -> list[float]:
    if not scores:
        return []
    top1 = max(s for _, s in scores)
    floor = max(floor_abs, top1 * rel_factor)
    candidates: list[tuple[float, float]] = []
    for i, (t, s) in enumerate(scores):
        if s < floor:
            continue
        prev_s = scores[i - 1][1] if i > 0 else -1.0
        next_s = scores[i + 1][1] if i + 1 < len(scores) else -1.0
        if s >= prev_s and s >= next_s:  # 局部极大值（允许平台段取首个）。
            if candidates and t - candidates[-1][0] <= merge:
                if s > candidates[-1][1]:
                    candidates[-1] = (t, s)
                continue
            candidates.append((t, s))
    picked: list[float] = []
    for t, _ in sorted(candidates, key=lambda x: -x[1]):  # 分数降序贪心，保证最小间隔。
        if all(abs(t - p) >= min_gap for p in picked):
            picked.append(t)
        if len(picked) >= max_frames - 1:
            break
    return sorted(picked)


# --- 转写句对齐到帧区间（判定式与插件逐字符一致） ---
def align_segments(segments: list[dict], frames: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    hits_per_frame: list[list[dict]] = []
    covered: set[int] = set()
    for fr in frames:
        hits: list[dict] = []
        for idx, seg in enumerate(segments):
            if seg["to"] > fr["t1"] - 0.01 and seg["from"] < fr["t2"] - 0.01:
                hits.append(seg)
                covered.add(idx)
        hits_per_frame.append(hits)
    rest = [seg for idx, seg in enumerate(segments) if idx not in covered]
    return hits_per_frame, rest


def mmss(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


# --- 单帧视觉图注：OpenAI 兼容多模态接口（如本地 Ollama），失败返回空串不中断 ---
def vision_caption(base: str, model: str, jpg: Path, title: str, timeout: int = 120) -> str:
    b64 = base64.b64encode(jpg.read_bytes()).decode()
    payload = {
        "model": model,
        "max_tokens": 120,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            {"type": "text", "text": ("\u8BC6\u522B\u753B\u9762\u4E2D\u7684\u6240\u6709\u6587\u5B57\uFF08\u5B57\u5E55/\u6807\u9898/\u8868\u683C\uFF09\uFF0C"
                                      "\u518D\u7528\u4E0D\u8D85\u8FC7 30 \u5B57\u6982\u8FF0\u753B\u9762\u5185\u5BB9\uFF0C"
                                      f"\u683C\u5F0F\uFF1A\u6587\u5B57\uFF1A<\u539F\u6587>>\uFF5C\u6982\u8FF0\uFF1A<\u4E00\u53E5\u8BDD>\u3002\u4E0A\u4E0B\u6587\uFF1A{title[:40]}")},
        ]}],
    }
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()


def _frame_name(idx: int, t1: float, t2: float) -> str:
    return f"\u56fe{idx:02d}_{t1:.1f}s-{t2:.1f}s.jpg"  # 图NN_0.0s-3.2s.jpg，与插件同名规则。


# --- 组装图文对照节（与 buildComparisonSection 同构） ---
def build_section(frames: list[dict], segments: list[dict], duration: float) -> str:
    lines: list[str] = ["", "## \u56fe\u6587\u5bf9\u7167\uFF08\u9010\u5E27 \u00D7 \u8F6C\u5199\uFF09", ""]
    lines.append("> [!tip]- \u5BF9\u7167\u8BF4\u660E")
    lines.append(f"> {len(frames)} \u4E2A\u5173\u952E\u5E27\uFF08\u573A\u666F\u5207\u6362\u68C0\u6D4B\uFF09\u00D7 {len(segments)} \u53E5\u9010\u5B57\u7A3F\uFF0C\u6309\u65F6\u95F4\u5BF9\u9F50\u3002\u5E27\u533A\u95F4 = \u672C\u5E27\u51FA\u73B0\u5230\u4E0B\u4E00\u5E27\u51FA\u73B0\u3002")
    lines.append("")
    hits_per_frame, rest = align_segments(segments, frames)
    for fr, hits in zip(frames, hits_per_frame):
        lines.append(f"### \u56fe {fr['idx']:02d} \uFF5C {mmss(fr['t1'])}\u2013{mmss(fr['t2'])}")
        lines.append("")
        lines.append(f"![[{fr['vault_path']}]]")
        cap = str(fr.get("caption") or "").strip()
        lines.append(f"\uFF08\u56fe {fr['idx']}\uFF1A{cap or '\u5F85\u8865\u89C6\u89C9\u8BF4\u660E'}\uff09")
        lines.append("")
        if hits:
            lines.append("> [!quote]- \u672C\u6BB5\u89E3\u8BF4")
            for h in hits:
                lines.append(f"> `{mmss(h['from'])}` {h['content']}")
        else:
            lines.append("> [!note]- \u672C\u6BB5\u89E3\u8BF4")
            lines.append("> \uFF08\u8BE5\u5E27\u533A\u95F4\u5185\u65E0\u4EBA\u58F0\u89E3\u8BF4\uFF09")
        lines.append("")
    if rest:
        lines.append("### \u65F6\u95F4\u8F74\u8865\u9057\uFF08\u672A\u843D\u5165\u5E27\u533A\u95F4\u7684\u89E3\u8BF4\uFF09")
        lines.append("")
        for h in rest:
            lines.append(f"> `{mmss(h['from'])}` {h['content']}")
        lines.append("")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"*\u56FE\u6587\u5BF9\u7167\u751F\u6210\u4E8E {stamp} \uFF5C bilibili-video-learning douyin-deep-archive*")
    lines.append("")
    return "\n".join(lines)


def find_note(inbox: Path, aweme_id: str) -> Path | None:
    for p in inbox.iterdir():  # 不能用 glob：文件名里的 [id] 会被 glob 当字符类解析。
        if p.is_file() and p.suffix == ".md" and aweme_id in p.name:
            return p
    return None


# --- 已有笔记：替换或插入图文对照节 + 更新 frontmatter 字段（幂等，重跑不重复堆节） ---
def update_note(path: Path, section: str, frames_count: int, model: str, status: str = "success") -> None:
    text = path.read_text(encoding="utf-8")
    heading = "## \u56FE\u6587\u5BF9\u7167"
    start = text.find(heading)
    if start > 0:  # 已有旧节：整节替换，重跑幂等。
        tail_idx = text.find("\n---\n", start)
        end = tail_idx + 1 if tail_idx > 0 else len(text)
        text = text[:start] + section.lstrip("\n") + text[end:]
    else:
        idx = text.rfind("\n---\n")
        if idx > 0:
            text = text[:idx + 1] + section + text[idx + 1:]
        else:
            text = text.rstrip("\n") + "\n" + section
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {
        "transcript_status": status,
        "frames_extracted": str(frames_count),
        "transcript_provider": f'"{model}"',
        "deep_archived_at": f'"{stamp}"',
    }
    for key, value in fields.items():
        pattern = re.compile(rf"^{key}:.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(f"{key}: {value}", text, count=1)
        else:  # 插到 transcript_status 行后，保持 frontmatter 聚拢。
            anchor = re.search(r"^transcript_status:.*$", text, re.M)
            insert_at = anchor.end() if anchor else re.search(r"^---$", text, re.M).end()
            text = text[:insert_at] + f"\n{key}: {value}" + text[insert_at:]
    write_text_atomically(path, text)


def bridge_post(base: str, path: str, payload: dict, timeout: int = BRIDGE_TIMEOUT) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_ffmpeg(args: list[str], exe: str = "ffmpeg") -> str:
    proc = subprocess.run([exe, "-hide_banner", "-loglevel", "info", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-200:]}")
    return proc.stdout + "\n" + proc.stderr  # Duration 在 stderr，场景打分在 stdout，合流供解析。


def check_bridge(base: str) -> None:  # 下载前预检桥接，给出可行动的提示而不是裸连接错误。
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/ping", timeout=5) as resp:
            if json.loads(resp.read().decode("utf-8")).get("ok"):
                return
    except Exception:
        pass
    raise RuntimeError("本地桥接未启动：请打开 Obsidian 执行一次 douyin-sync 同步（会自动拉起桥接），"
                       "或手动运行 node douyin-bridge.js 8765 后重试。")


# --- 主流程 ---
def main() -> int:
    parser = argparse.ArgumentParser(description="Douyin deep archive: bridge video + scene frames + local ASR + aligned note.")
    parser.add_argument("--id", dest="aweme_id", help="抖音视频 ID（与 --url 二选一）")
    parser.add_argument("--url", help="抖音分享/网页链接，自动提取 19 位视频 ID")
    parser.add_argument("--bridge", default=os.environ.get("DOUYIN_VIDEO_BRIDGE"),
                        help="本地桥接端点（登录态下载通道）；缺省读环境变量 DOUYIN_VIDEO_BRIDGE，仓库不内置任何主机地址")
    parser.add_argument("--max-frames", type=int, default=24)
    parser.add_argument("--model", default="small", help="faster-whisper 模型，默认 small")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", default=None, choices=[None, "auto", "cuda", "cpu"])
    parser.add_argument("--no-transcribe", action="store_true", help="只抽帧不转写（转写状态记 skipped）")
    parser.add_argument("--vision", action="store_true", help="逐帧生成视觉图注（需多模态端点，如本地 Ollama）")
    parser.add_argument("--vision-url", default=os.environ.get("DOUYIN_VIDEO_VISION_URL"),
                        help="OpenAI 兼容多模态端点根（自动补 /chat/completions）；缺省读 DOUYIN_VIDEO_VISION_URL")
    parser.add_argument("--vision-model", default=os.environ.get("DOUYIN_VIDEO_VISION_MODEL") or "qwen2.5vl:3b",
                        help="视觉模型名，默认 qwen2.5vl:3b")
    parser.add_argument("--note", default=None, help="目标笔记绝对路径；给定则直接更新该笔记（Obsidian 薄客户端入口）")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--workdir", default=None, help="视频缓存目录；默认 %%LOCALAPPDATA%%/DouyinSyncBridge/media-work")
    parser.add_argument("--vault", default=None, help="Vault 根；默认读 BILIBILI_OBSIDIAN_VAULT")
    args = parser.parse_args()

    aweme_id = args.aweme_id
    if not aweme_id and args.url:
        m = re.search(r"(\d{15,21})", args.url)
        if not m:
            print(json.dumps({"error": "cannot extract video id from url"}, ensure_ascii=False))
            return 1
        aweme_id = m.group(1)
    if not aweme_id:
        parser.error("--id 或 --url 必填其一")

    vault = Path(args.vault or os.environ.get("BILIBILI_OBSIDIAN_VAULT") or "~/Notes").expanduser()
    inbox = vault / "00-\u539f\u59cb\u7b14\u8bb0" / "\u6296\u97f3\u5f52\u6863" / "\u6536\u85cf" / "\u672a\u5206\u7c7b"  # 00-原始笔记/抖音归档/收藏/未分类
    media_root = vault / "\u9644\u4ef6" / "douyin-media"  # 附件/douyin-media
    workdir = Path(args.workdir or (os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) + "/DouyinSyncBridge/media-work") / aweme_id
    workdir.mkdir(parents=True, exist_ok=True)
    video_path = workdir / "video.mp4"

    detail: dict = {}
    title, author, duration = "", "", 0.0
    if not args.bridge:
        print(json.dumps({"error": "bridge endpoint required: pass --bridge or set DOUYIN_VIDEO_BRIDGE env"}, ensure_ascii=False))
        return 1
    if video_path.exists() and video_path.stat().st_size > 0:
        log(f"[archive] reuse cached video: {video_path}")
    else:
        check_bridge(args.bridge)
        log("[archive] bridge detail ...")
        qs = f"aweme_id={aweme_id}&device_platform=webapp&aid=6383&channel=channel_pc_web&version_code=170400"
        outer = bridge_post(args.bridge, "/req", {"url": f"https://www.douyin.com/aweme/v1/web/aweme/detail/?{qs}", "method": "GET"})
        detail = json.loads(outer["text"]).get("aweme_detail") or {}
        if not detail:
            print(json.dumps({"error": "detail empty (bridge down or cookie expired?)"}, ensure_ascii=False))
            return 1
        title = str(detail.get("desc") or "").split("\n")[0]
        author = str((detail.get("author") or {}).get("nickname") or "")
        duration = round(int(detail.get("video", {}).get("duration") or 0) / 1000, 2)
        play_url = ((detail.get("video", {}).get("play_addr") or {}).get("url_list") or [""])[0].replace("playwm", "play")
        log(f"[archive] bridge download ({duration}s) ...")
        dl = bridge_post(args.bridge, "/download", {"url": play_url})
        if dl.get("error") or not dl.get("b64"):
            print(json.dumps({"error": f"download failed: {dl.get('error', 'no b64')}"}, ensure_ascii=False))
            return 1
        video_path.write_bytes(base64.b64decode(dl["b64"]))
        log(f"[archive] video saved {video_path.stat().st_size} bytes")

    if duration <= 0:
        probe = run_ffmpeg(["-i", str(video_path), "-f", "null", "-"], exe=args.ffmpeg)
        m = re.search(r"Duration: (\d+):(\d+):(\d+)", probe)
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else 0.0

    log("[archive] scene detection ...")
    detect = run_ffmpeg(["-i", str(video_path), "-vf", "select='gt(scene,0)',metadata=print:file=-", "-f", "null", "-"], exe=args.ffmpeg)
    scores = parse_scene_scores(detect)
    peaks = adaptive_peaks(scores, max_frames=args.max_frames)
    times = [0.0] + peaks
    frames_dir = media_root / "frames" / aweme_id
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    for i, t1 in enumerate(times):
        t2 = times[i + 1] if i + 1 < len(times) else (duration or t1 + 1.0)
        idx = i + 1
        name = _frame_name(idx, t1, t2)
        out = frames_dir / name
        if not out.exists():
            run_ffmpeg(["-ss", f"{t1:.3f}", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(out)], exe=args.ffmpeg)
        vault_rel = media_root.relative_to(vault).as_posix()
        frames.append({"idx": idx, "t1": round(t1, 2), "t2": round(t2, 2),
                       "vault_path": f"{vault_rel}/frames/{aweme_id}/{name}", "path": str(out)})
    log(f"[archive] frames: {len(frames)}")
    if args.vision:
        vurl = (args.vision_url or "").rstrip("/")
        if not vurl:
            log("[archive] vision requested but no endpoint (--vision-url / DOUYIN_VIDEO_VISION_URL); skipped")
        else:
            log(f"[archive] vision captions via {args.vision_model} ...")
            okc = 0
            for fr in frames:
                try:
                    fr["caption"] = vision_caption(vurl, args.vision_model, Path(fr["path"]), title or aweme_id)
                    okc += 1
                except Exception as e:
                    log(f"[archive] vision fail f{fr['idx']:02d}: {str(e)[:90]}")
            log(f"[archive] captions: {okc}/{len(frames)}")

    segments: list[dict] = []
    transcript_status = "skipped" if args.no_transcribe else "success"
    if not args.no_transcribe:
        log(f"[archive] local transcribe ({args.model}) ...")
        asr = transcribe_audio_file(video_path, model_size=args.model, language=args.language,
                                    device=None if args.device in (None, "auto") else args.device, log_prefix="archive")
        segments = [{"from": s["from"], "to": s["to"], "content": s["content"]} for s in asr["segments"]]
        log(f"[archive] segments: {len(segments)} ({asr['device']}/{asr['compute_type']})")

    section = build_section(frames, segments, duration)
    if args.note:
        note = Path(args.note)
        if not note.is_file():
            print(json.dumps({"error": f"note not found: {note}"}, ensure_ascii=False))
            return 1
    else:
        inbox.mkdir(parents=True, exist_ok=True)
        note = find_note(inbox, aweme_id)
    if note:
        update_note(note, section, len(frames), f"faster-whisper:{args.model}", transcript_status)
        action = "updated"
    else:
        note = inbox / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')} {title[:60]} [{aweme_id}].md"
        stat = detail.get("statistics") or {}
        url = f"https://www.douyin.com/video/{aweme_id}"
        lines = ["---", f'douyin_id: "{aweme_id}"', f'title: "{title}"', 'type: "\u89c6\u9891"',
                 'source: "\u6280\u80fd\u5f52\u6863"', f'author: "{author}"',
                 f'url: "{url}"', f'duration: "{int(duration // 60)}\u5206{int(duration % 60)}\u79d2"' if duration else 'duration: ""',
                 f'likes: {stat.get("digg_count", 0)}', "transcript_status: success",
                 f'frames_extracted: {len(frames)}', 'transcript_provider: "faster-whisper"',
                 f'deep_archived_at: "{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}"',
                 "tags:", "  - \u6296\u97f3", "  - \u6280\u80fd\u5f52\u6863", "---", "", f"# {title}", "",
                 section]
        write_text_atomically(note, "\n".join(lines))
        action = "created"

    result = {"ok": True, "action": action, "note": str(note), "frames": len(frames),
              "segments": len(segments), "transcript_status": transcript_status, "duration": duration}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
