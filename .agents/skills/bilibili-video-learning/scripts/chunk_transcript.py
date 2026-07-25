#!/usr/bin/env python3
"""Chunk a transcript by time intervals or semantic topic boundaries."""
import json
import sys

def chunk_by_time(segments: list[dict], interval_seconds: int = 300) -> list[dict]:
    """Split transcript into fixed-duration chunks."""
    if not segments:
        return []

    chunks = []
    chunk_start = segments[0]['start']
    chunk_text = []
    chunk_start_time = segments[0]['start']

    for seg in segments:
        if seg['start'] - chunk_start > interval_seconds and chunk_text:
            chunks.append({
                'start': chunk_start_time,
                'end': seg['start'],
                'text': '\n'.join(chunk_text)
            })
            chunk_start = seg['start']
            chunk_start_time = seg['start']
            chunk_text = []

        chunk_text.append(f"[{seg['start']:.1f}s] {seg['text']}")

    # Don't forget the last chunk
    if chunk_text:
        chunks.append({
            'start': chunk_start_time,
            'end': segments[-1]['end'],
            'text': '\n'.join(chunk_text)
        })

    return chunks

def format_chunks_markdown(chunks: list[dict]) -> str:
    """Format chunks as Markdown sections."""
    output = []
    for i, chunk in enumerate(chunks):
        start_m = int(chunk['start'] // 60)
        start_s = int(chunk['start'] % 60)
        end_m = int(chunk['end'] // 60)
        end_s = int(chunk['end'] % 60)
        header = f"### {start_m:02d}:{start_s:02d}-{end_m:02d}:{end_s:02d}"
        output.append(header)
        output.append(chunk['text'])
        output.append('')
    return '\n'.join(output)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python chunk_transcript.py <transcript.json> [interval_seconds=300]")
        sys.exit(1)

    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 300

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        segments = json.load(f)

    chunks = chunk_by_time(segments, interval)
    print(format_chunks_markdown(chunks))
    print(f"\n// {len(chunks)} chunks at {interval}s intervals", file=sys.stderr)
