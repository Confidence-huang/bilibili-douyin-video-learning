"""
B站深度归档引擎：BV/av/b23 链接 → yt-dlp 取视频 → 场景抽帧 → 本地 GPU 转写 → 图文对照 → 写回笔记。
与 douyin_deep_archive.py 共享同一套算法与图文对照契约（场景解析/峰值选帧/对齐/图注直接 import 复用），
仅下载与元数据层换成 B 站（公开视频匿名可用；会员视频经 BILIBILI_COOKIE_FILE 提供 cookies）。
笔记契约键为 bvid；收件箱缺省 = $BILIBILI_OBSIDIAN_VAULT/$BILIBILI_OBSIDIAN_FOLDER（00-原始笔记/B站归档）。
调用示例：python bilibili_deep_archive.py --url "https://b23.tv/xxxx" --vault "D:/NOTE" --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from douyin_deep_archive import (  # 同目录复用：算法与契约层完全一致
    DEFAULT_MERGE,
    DEFAULT_MIN_GAP,
    DEFAULT_REL_FACTOR,
    DEFAULT_SCENE_FLOOR,
    adaptive_peaks,
    align_segments,
    build_section,
    parse_scene_scores,
    vision_caption,
)
from file_output import write_text_atomically
from runtime_output import log
from speech_to_text import transcribe_audio_file

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", "Referer": "https://www.bilibili.com/"}


# --- 链接/ID 归一：b23.tv 短链跟随跳转，av 号走 view API 换 bvid ---
def resolve_bvid(url_or_id: str) -> str:
    s = url_or_id.strip()
    m = re.search(r"(BV[0-9A-Za-z]{8,12})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"av\d+", s, re.I):
        req = urllib.request.Request(f"{VIEW_API}?aid={s[2:]}", headers=HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        return d["data"]["bvid"]
    if "b23.tv" in s or "bilibili.com" in s:
        req = urllib.request.Request(s if s.startswith("http") else "https://" + s, method="GET",
                                     headers=HEADERS)
        final = urllib.request.urlopen(req, timeout=30).geturl()
        m = re.search(r"(BV[0-9A-Za-z]{8,12})", final)
        if m:
            return m.group(1)
    raise ValueError(f"cannot resolve bvid from: {url_or_id}")


def fetch_view(bvid: str) -> dict:
    import time as _t
    last = None
    for attempt in range(4):  # 412/超时退避重试
        try:
            req = urllib.request.Request(f"{VIEW_API}?bvid={bvid}", headers=HEADERS)
            d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
            if d.get("code") != 0:
                raise RuntimeError(f"view API code={d.get('code')}: {d.get('message')}")
            return d["data"]
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (412, 429):
                _t.sleep(10 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last = e
            _t.sleep(5)
    raise last


def download_cover(pic_url: str, cover_dir: Path, media_rel: str, bvid: str) -> str:
    """下载视频封面到 <media>/cover/<bvid>.<ext>，返回库内相对路径；失败返回空串。"""
    m = re.search(r"\.(jpe?g|png|webp|gif)(?:[?#]|$)", pic_url, re.I)
    ext = "." + (m.group(1).lower().replace("jpeg", "jpg") if m else "jpg")
    cover_dir.mkdir(parents=True, exist_ok=True)
    out = cover_dir / f"{bvid}{ext}"
    if not out.exists():
        req = urllib.request.Request(pic_url, headers=HEADERS)
        data = urllib.request.urlopen(req, timeout=30).read()
        if not data:
            return ""
        out.write_bytes(data)
    return f"{media_rel}/cover/{bvid}{ext}"


def ensure_frontmatter_fields(path: Path, fields: list[str]) -> int:
    """仅在 frontmatter 缺失对应键时插入（不覆盖已有值，保护已晋升状态等）。"""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return 0
    end = text.find("\n---", 3)
    if end < 0:
        return 0
    added = [line for line in fields
             if not re.search(rf"^{re.escape(line.split(':', 1)[0])}:", text[:end], re.M)]
    if not added:
        return 0
    text = text[:end] + "\n" + "\n".join(added) + text[end:]
    write_text_atomically(path, text)
    return len(added)


def ydl_download(bvid: str, out_mp4: Path, cookies_file: str | None) -> None:
    exe = Path(sys.executable).parent / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    exe = str(exe) if exe.exists() else "yt-dlp"
    fmt = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720][ext=mp4]/b"
    cmd = [exe, "-f", fmt, "--merge-output-format", "mp4", "--no-playlist", "--no-progress",
           "-o", str(out_mp4), f"https://www.bilibili.com/video/{bvid}"]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out_mp4.exists():
        raise RuntimeError(f"yt-dlp failed: {(proc.stderr or proc.stdout)[-200:]}")


def run_ffmpeg(args: list[str], exe: str = "ffmpeg") -> str:
    proc = subprocess.run([exe, "-hide_banner", "-loglevel", "info", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-200:]}")
    return proc.stdout + "\n" + proc.stderr  # Duration 在 stderr，场景打分在 stdout，合流供解析。


def classify_text(url: str, model: str, categories: list[str], title: str, snippet: str) -> str:
    prompt = ("从以下分类列表中选择最匹配的一个，只输出分类名本身，不要输出任何其他文字：\n"
              + "\n".join(categories) + f"\n\n标题：{title}\n内容摘要：{snippet}")
    payload = {"model": model, "max_tokens": 40, "temperature": 0,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    reply = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    if reply in categories:
        return reply
    for c in categories:
        if c and c in reply:
            return c
    return ""


def find_note(inbox: Path, bvid: str) -> Path | None:
    for p in inbox.iterdir():
        if p.is_file() and p.suffix == ".md" and bvid in p.name:
            return p
    return None


def update_note(path: Path, section: str, frames_count: int, model: str, status: str = "success", category: str = "") -> None:
    text = path.read_text(encoding="utf-8")
    heading = "## \u56fe\u6587\u5bf9\u7167"
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
    if category:
        fields["category"] = f'"{category}"'
    for key, value in fields.items():
        pattern = re.compile(rf"^{key}:.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(f"{key}: {value}", text, count=1)
        else:
            anchor = re.search(r"^transcript_status:.*$", text, re.M)
            insert_at = anchor.end() if anchor else re.search(r"^---$", text, re.M).end()
            text = text[:insert_at] + f"\n{key}: {value}" + text[insert_at:]
    write_text_atomically(path, text)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bilibili deep archive: yt-dlp + scene frames + local ASR + aligned note.")
    ap.add_argument("--bvid", dest="bvid")
    ap.add_argument("--url", help="B站网页/分享链接，自动解析 BV 号")
    ap.add_argument("--note", default=None, help="目标笔记绝对路径；给定则直接更新该笔记（薄客户端入口）")
    ap.add_argument("--vault", default=os.environ.get("BILIBILI_OBSIDIAN_VAULT"))
    ap.add_argument("--inbox-dir", default=None,
                    help="收件箱相对路径；缺省 = $BILIBILI_OBSIDIAN_FOLDER 或 00-原始笔记/B站归档")
    ap.add_argument("--media-dir", default=None, help="媒体目录相对路径；缺省 = 附件/bili-media")
    ap.add_argument("--max-frames", type=int, default=24)
    ap.add_argument("--model", default="small")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--device", default=None, choices=[None, "auto", "cuda", "cpu"])
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--workdir", default=None, help="视频缓存目录；缺省 = <TEMP>/bilibili-vault-link/<bvid>")
    ap.add_argument("--cookies-file", default=os.environ.get("BILIBILI_COOKIE_FILE"),
                    help="B站 cookies 文件（会员/高清视频需要）；缺省读 BILIBILI_COOKIE_FILE")
    ap.add_argument("--no-transcribe", action="store_true", help="只抽帧不转写")
    ap.add_argument("--metadata-only", action="store_true",
                    help="收藏同步轻量模式：只取元数据+AI分类建笔记，不下载视频不抽帧不转写")
    ap.add_argument("--vision", action="store_true", help="逐帧生成视觉图注（本地 Ollama 等）")
    ap.add_argument("--vision-url", default=os.environ.get("DOUYIN_VIDEO_VISION_URL"))
    ap.add_argument("--vision-model", default=os.environ.get("DOUYIN_VIDEO_VISION_MODEL") or "qwen2.5vl:3b")
    ap.add_argument("--ai-url", default=os.environ.get("DOUYIN_VIDEO_VISION_URL"),
                    help="文本 AI 端点（自动分类用）；缺省同视觉端点环境变量")
    ap.add_argument("--ai-model", default="qwen2.5:3b")
    ap.add_argument("--categories", default=os.environ.get("BILI_AI_CATEGORIES", ""),
                    help='逗号或换行分隔的分类列表；非空时为笔记写入 category 字段（环境变量 BILI_AI_CATEGORIES）')
    args = ap.parse_args()

    bvid = args.bvid or (resolve_bvid(args.url) if args.url else None)
    if not bvid:
        print(json.dumps({"error": "--bvid 或 --url 必填其一"}, ensure_ascii=False))
        return 1
    if not args.vault:
        print(json.dumps({"error": "vault required: --vault or BILIBILI_OBSIDIAN_VAULT env"}, ensure_ascii=False))
        return 1
    vault = Path(args.vault).expanduser()
    inbox_rel = args.inbox_dir or os.environ.get("BILIBILI_OBSIDIAN_FOLDER") or "00-\u539f\u59cb\u7b14\u8bb0/B\u7ad9\u5f52\u6863"
    inbox = vault / inbox_rel
    media_rel = args.media_dir or "\u9644\u4ef6/bili-media"  # 附件/bili-media
    media_root = vault / media_rel
    workdir = Path(args.workdir or (Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")) / "bilibili-vault-link" / bvid)
    workdir.mkdir(parents=True, exist_ok=True)
    video_path = workdir / "video.mp4"

    if args.metadata_only:
        log("[archive] metadata-only mode ...")
        view = fetch_view(bvid)
        title = str(view.get("title") or bvid)
        author = str((view.get("owner") or {}).get("name") or "")
        duration = int(view.get("duration") or 0)
        desc = str(view.get("desc") or "")
        url = f"https://www.bilibili.com/video/{bvid}"
        category = ""
        cats = [x.strip() for x in re.split("[" + chr(10) + ",，;；]", args.categories or "") if x.strip()]
        if cats:
            ai_url = (args.ai_url or "").rstrip("/")
            if ai_url:
                try:
                    category = classify_text(ai_url, args.ai_model, cats, title, desc[:220])
                    log(f"[archive] category: {category or '(未匹配)'}")
                except Exception as e:
                    log(f"[archive] classify fail: {str(e)[:80]}")
        cover_rel = ""
        pic = str(view.get("pic") or "")
        if pic:
            try:
                cover_rel = download_cover(pic, media_root / "cover", media_rel, bvid)
                log(f"[archive] cover: {cover_rel or 'empty'}")
            except Exception as e:
                log(f"[archive] cover fail: {str(e)[:80]}")
        contract = ['vault_status: "待整合"', 'promoted_to: ""']
        if cover_rel:
            contract.insert(0, f'cover: "{cover_rel}"')
        inbox.mkdir(parents=True, exist_ok=True)  # 新夹目录首次创建
        note = Path(args.note) if args.note else find_note(inbox, bvid)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if note and note.is_file():
            ensure_frontmatter_fields(note, contract)  # 已有笔记只补契约字段，不覆盖状态
            action = "updated"
        else:
            safe_title = re.sub(r'[\/:*?"<>|#^\[\]%]+', " ", title).strip()[:60] or f"B站视频_{bvid}"
            note = inbox / f"{safe_title} [{bvid}].md"
            dur_line = f'duration: "{duration // 60}分{duration % 60}秒"' if duration else 'duration: ""'
            lines = ["---", f'bvid: "{bvid}"', f'title: "{title}"', 'type: "视频"',
                     'source: "B站收藏"', f'author: "{author}"', f'url: "{url}"', dur_line]
            if category:
                lines.append(f'category: "{category}"')
            lines += contract
            lines += ["transcript_status: not_requested", "tags:", "  - B站", "  - 收藏",
                      "---", "", f"# {title}", "",
                      f"作者：**{author}** ｜ [原视频链接]({url})", ""]
            if desc:
                lines += ["> [!info]- 简介", f"> {desc}", ""]
            write_text_atomically(note, "\n".join(lines))
            action = "created"
        print(json.dumps({"ok": True, "bvid": bvid, "action": action, "note": str(note),
                          "frames": 0, "segments": 0, "transcript_status": "not_requested",
                          "category": category, "duration": duration}, ensure_ascii=False), flush=True)
        return 0

    view: dict = {}
    if video_path.exists() and video_path.stat().st_size > 0:
        log(f"[archive] reuse cached video: {video_path}")
    else:
        log("[archive] fetch view metadata ...")
        view = fetch_view(bvid)
        log("[archive] yt-dlp download (<=720p) ...")
        ydl_download(bvid, video_path, args.cookies_file)
        log(f"[archive] video saved {video_path.stat().st_size} bytes")

    title = str(view.get("title") or bvid)
    author = str((view.get("owner") or {}).get("name") or "")
    duration = int(view.get("duration") or 0)
    if duration <= 0:
        probe = run_ffmpeg(["-i", str(video_path), "-f", "null", "-"], exe=args.ffmpeg)
        m = re.search(r"Duration: (\d+):(\d+):(\d+)", probe)
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else 0

    log("[archive] scene detection ...")
    detect = run_ffmpeg(["-i", str(video_path), "-vf", "select='gt(scene,0)',metadata=print:file=-", "-f", "null", "-"], exe=args.ffmpeg)
    peaks = adaptive_peaks(parse_scene_scores(detect), max_frames=args.max_frames)
    times = [0.0] + peaks
    frames_dir = media_root / "frames" / bvid
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t1 in enumerate(times):
        t2 = times[i + 1] if i + 1 < len(times) else (duration or t1 + 1.0)
        idx = i + 1
        name = f"\u56fe{idx:02d}_{t1:.1f}s-{t2:.1f}s.jpg"
        out = frames_dir / name
        if not out.exists():
            run_ffmpeg(["-ss", f"{t1:.3f}", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(out)], exe=args.ffmpeg)
        frames.append({"idx": idx, "t1": round(t1, 2), "t2": round(t2, 2),
                       "vault_path": f"{media_rel}/frames/{bvid}/{name}", "path": str(out)})
    log(f"[archive] frames: {len(frames)}")

    segments: list[dict] = []
    transcript_status = "skipped" if args.no_transcribe else "success"
    if not args.no_transcribe:
        log(f"[archive] local transcribe ({args.model}) ...")
        asr = transcribe_audio_file(video_path, model_size=args.model, language=args.language,
                                    device=None if args.device in (None, "auto") else args.device, log_prefix="archive")
        segments = [{"from": s["from"], "to": s["to"], "content": s["content"]} for s in asr["segments"]]
        log(f"[archive] segments: {len(segments)} ({asr['device']}/{asr['compute_type']})")

    if args.vision:
        vurl = (args.vision_url or "").rstrip("/")
        if not vurl:
            log("[archive] vision requested but no endpoint; skipped")
        else:
            log(f"[archive] vision captions via {args.vision_model} ...")
            okc = 0
            for fr in frames:
                try:
                    fr["caption"] = vision_caption(vurl, args.vision_model, Path(fr["path"]), title)
                    okc += 1
                except Exception as e:
                    log(f"[archive] vision fail f{fr['idx']:02d}: {str(e)[:90]}")
            log(f"[archive] captions: {okc}/{len(frames)}")

    category = ""
    cats = [x.strip() for x in re.split(r"[\n,，;；]", args.categories or "") if x.strip()]
    if cats:
        ai_url = (args.ai_url or args.vision_url or "").rstrip("/")
        if ai_url:
            try:
                snippet = re.sub(r"\s+", " ", " ".join(s["content"] for s in segments))[:180] or str(view.get("desc") or "")[:180]
                category = classify_text(ai_url, args.ai_model, cats, title, snippet)
                log(f"[archive] category: {category or '(未匹配)'}")
            except Exception as e:
                log(f"[archive] classify fail: {str(e)[:80]}")

    section = build_section(frames, segments, duration)
    inbox.mkdir(parents=True, exist_ok=True)
    note = Path(args.note) if args.note else find_note(inbox, bvid)
    url = f"https://www.bilibili.com/video/{bvid}"
    cover_rel = ""
    pic = str(view.get("pic") or "")
    if pic:
        try:
            cover_rel = download_cover(pic, media_root / "cover", media_rel, bvid)
            log(f"[archive] cover: {cover_rel or 'empty'}")
        except Exception as e:
            log(f"[archive] cover fail: {str(e)[:80]}")
    contract = ['vault_status: "待整合"', 'promoted_to: ""']
    if cover_rel:
        contract.insert(0, f'cover: "{cover_rel}"')
    if note and note.is_file():
        ensure_frontmatter_fields(note, contract)  # 只补缺失的契约/封面字段，不覆盖已有值
        update_note(note, section, len(frames), f"faster-whisper:{args.model}", transcript_status, category)
        action = "updated"
    else:
        safe_title = re.sub(r'[\\/:*?"<>|#^[\]%]+', " ", title).strip()[:60] or f"B\u7ad9\u89c6\u9891_{bvid}"
        note = inbox / f"{safe_title} [{bvid}].md"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        dur_line = f'duration: "{duration // 60}\u5206{duration % 60}\u79d2"' if duration else 'duration: ""'
        lines = ["---", f'bvid: "{bvid}"', f'title: "{title}"', 'type: "\u89c6\u9891"',
                 'source: "B\u7ad9\u5f52\u6863"', f'author: "{author}"', f'url: "{url}"', dur_line,
                 "transcript_status: " + transcript_status, f"frames_extracted: {len(frames)}",
                 *( [f'category: "{category}"'] if category else [] ),
                 *contract,
                 f'transcript_provider: "faster-whisper:{args.model}"', f'deep_archived_at: "{stamp}"',
                 "tags:", "  - B\u7ad9", "  - \u6280\u80fd\u5f52\u6863", "---", "", f"# {title}", "", section]
        write_text_atomically(note, "\n".join(lines))
        action = "created"

    print(json.dumps({"ok": True, "bvid": bvid, "action": action, "note": str(note), "frames": len(frames),
                      "segments": len(segments), "transcript_status": transcript_status,
                      "duration": duration}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
