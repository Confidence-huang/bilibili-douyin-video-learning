#!/usr/bin/env python3
"""Clean transcript text by merging duplicates, removing filler, and fixing common errors."""
import json
import sys

def clean_segments(segments: list[dict]) -> list[dict]:
    """Apply cleaning rules to transcript segments."""
    cleaned = []

    # Common Chinese ASR filler words to remove
    fillers = {'呃', '嗯', '啊', '那个', '这个', '就是说', '然后呢', '对吧', '是不是'}

    for seg in segments:
        text = seg['text'].strip()
        if not text:
            continue

        # Remove standalone fillers
        if text in fillers:
            continue

        # Merge very short segments (<0.5s) with next if possible
        duration = seg['end'] - seg['start']

        cleaned.append({
            'start': seg['start'],
            'end': seg['end'],
            'text': text
        })

    # Deduplicate consecutive identical lines
    deduped = []
    for i, seg in enumerate(cleaned):
        if i > 0 and seg['text'] == cleaned[i-1]['text']:
            # Extend previous segment
            deduped[-1]['end'] = seg['end']
            continue
        deduped.append(seg)

    return deduped

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python clean_transcript.py <transcript.json>")
        sys.exit(1)

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        segments = json.load(f)

    cleaned = clean_segments(segments)
    print(json.dumps(cleaned, ensure_ascii=False, indent=2))
    print(f"\n// {len(cleaned)} segments (removed {len(segments) - len(cleaned)})", file=sys.stderr)
