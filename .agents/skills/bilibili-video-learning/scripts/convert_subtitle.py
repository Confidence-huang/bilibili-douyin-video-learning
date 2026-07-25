#!/usr/bin/env python3
"""Convert various subtitle formats to a common JSON timeline format."""
import json
import re
import sys
from pathlib import Path


# --- 把 SRT/VTT 时间戳转换为秒 ---
def parse_timestamp(value: str) -> float:
    """Parse HH:MM:SS.mmm or MM:SS.mmm into seconds."""
    clean_value = value.strip().replace(",", ".")             # SRT 用逗号，VTT 用小数点。
    parts = clean_value.split(":")                              # VTT 允许省略小时字段。
    if len(parts) == 3:
        hours, minutes, seconds = parts                          # 完整时间戳包含小时。
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]       # 短 VTT cue 默认零小时。
    else:
        raise ValueError(f"Unsupported subtitle timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --- 解析 SRT/VTT 共用的 cue 块 ---
def parse_timed_text(text: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", text.strip())                # 空行是两种格式的 cue 分隔符。
    segments = []                                                # 每个结果统一为 start/end/text。
    for block in blocks:
        lines = [line.rstrip("\r") for line in block.splitlines()]
        timestamp_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timestamp_index is None:                              # WEBVTT 头、NOTE 和样式块没有时间线。
            continue
        time_parts = lines[timestamp_index].split("-->", 1)
        start_text = time_parts[0].strip()                       # 左侧只有开始时间。
        end_text = time_parts[1].strip().split()[0]              # 右侧 cue settings 不属于时间戳。
        try:
            start = parse_timestamp(start_text)
            end = parse_timestamp(end_text)
        except (TypeError, ValueError):                          # 单个坏 cue 不应丢掉整个字幕文件。
            continue
        cue_text = "\n".join(lines[timestamp_index + 1:]).strip()
        if cue_text:                                             # 空 cue 没有可学习正文。
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": cue_text})
    return segments

def parse_srt(text: str) -> list[dict]:
    """Parse .srt subtitle format."""
    return parse_timed_text(text)                                # SRT 索引行会自然落在时间戳之前并被忽略。

def parse_vtt(text: str) -> list[dict]:
    """Parse WebVTT, including MM:SS.mmm and cue settings."""
    clean_text = text.lstrip("\ufeff")                          # 浏览器导出的 VTT 偶尔带 UTF-8 BOM。
    return parse_timed_text(clean_text)                          # 无时间戳的 WEBVTT 头会被统一跳过。

def parse_bilibili_json(text: str) -> list[dict]:
    """Parse Bilibili's native subtitle JSON format."""
    data = json.loads(text)
    segments = []
    body = data.get('body', data) if isinstance(data, dict) else data  # API 可能返回 {body: []} 或直接列表。
    if isinstance(body, list):
        for item in body:
            segments.append({
                "start": round(item.get('from', 0), 3),
                "end": round(item.get('to', 0), 3),
                "text": item.get('content', '')
            })
    return segments


# --- 解析 yt-dlp/YouTube 风格 JSON3 字幕 ---
def parse_ytdlp_json(text: str) -> list[dict]:
    data = json.loads(text)                                      # JSON 语法错误交给调用方明确报告。
    segments = []                                                # events 统一转成秒和正文。
    for event in data.get("events", []):
        start_ms = event.get("tStartMs", 0) or 0                # JSON3 使用毫秒整数。
        duration_ms = event.get("dDurationMs", 0) or 0
        cue_text = "".join(segment.get("utf8", "") for segment in event.get("segs", []))
        cue_text = cue_text.strip()
        if cue_text:                                             # 样式或空事件不进入学习正文。
            segments.append({
                "start": round(start_ms / 1000, 3),
                "end": round((start_ms + duration_ms) / 1000, 3),
                "text": cue_text,
            })
    return segments

def detect_and_parse(filepath: str) -> list[dict]:
    """Auto-detect format and parse."""
    path = Path(filepath)
    ext = path.suffix.lower()
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()

    if ext == '.srt':
        return parse_srt(text)
    elif ext == '.vtt':
        return parse_vtt(text)
    elif ext == '.json':
        # 先识别 Bilibili body，再识别 yt-dlp JSON3 events。
        try:
            data = json.loads(text)
            if 'body' in data or (isinstance(data, list) and data and 'from' in data[0]):
                return parse_bilibili_json(text)
            if isinstance(data, dict) and "events" in data:
                return parse_ytdlp_json(text)
        except json.JSONDecodeError:
            pass
        return []
    elif ext == '.ass':
        # Basic ASS parsing — extract dialogue lines
        segments = []
        for line in text.split('\n'):
            if line.startswith('Dialogue:'):
                parts = line.split(',', 9)
                if len(parts) >= 10:
                    # ASS timestamps: H:MM:SS.cc
                    start_str = parts[1].strip()
                    end_str = parts[2].strip()
                    text_content = parts[9].strip()
                    # Remove ASS override tags
                    text_content = re.sub(r'\{[^}]*\}', '', text_content)
                    # Simple timestamp parse
                    try:
                        h1, m1, s1 = start_str.split(':')
                        start = int(h1)*3600 + int(m1)*60 + float(s1)
                        h2, m2, s2 = end_str.split(':')
                        end = int(h2)*3600 + int(m2)*60 + float(s2)
                        if text_content.strip():
                            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text_content.strip()})
                    except ValueError:
                        continue
        return segments
    else:
        # Try SRT format as default
        return parse_srt(text)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python convert_subtitle.py <subtitle_file>")
        sys.exit(1)

    segments = detect_and_parse(sys.argv[1])
    print(json.dumps(segments, ensure_ascii=False, indent=2))
    print(f"\n// {len(segments)} segments", file=sys.stderr)
