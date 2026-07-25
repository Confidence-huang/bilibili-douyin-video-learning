#!/usr/bin/env python3
"""Export Anki flashcards from learning notes as CSV."""
import csv
import sys
import re

def extract_cards_from_markdown(text: str) -> list[tuple]:
    """Extract Q&A pairs from the Anki 卡片 section of notes."""
    cards = []

    # Try to find the Anki table
    in_anki_section = False
    in_table = False
    for line in text.split('\n'):
        line = line.strip()
        if '## Anki' in line or '## Anki卡片' in line:
            in_anki_section = True
            continue
        if in_anki_section and line.startswith('## '):
            in_anki_section = False
            continue
        if in_anki_section:
            # Parse table row: | front | back |
            match = re.match(r'\|\s*(.+?)\s*\|\s*(.+?)\s*\|', line)
            if match and not match.group(1).strip().startswith('-'):
                front = match.group(1).strip()
                back = match.group(2).strip()
                if front and back and front not in ('正面', 'Front'):
                    cards.append((front, back, 'bilibili-learning', ''))

    return cards

def export_csv(cards: list[tuple], output_path: str):
    """Write cards to Anki-compatible CSV."""
    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Front', 'Back', 'Tags', 'Source'])
        for front, back, tags, source in cards:
            writer.writerow([front, back, tags, source])

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python export_anki.py <notes.md> <output.csv>")
        sys.exit(1)

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        text = f.read()

    cards = extract_cards_from_markdown(text)

    if not cards:
        # Fallback: extract any bold text pairs or numbered Q&A
        qa_pairs = re.findall(r'\*\*(.+?)\*\*[：:]\s*(.+?)(?:\n|$)', text)
        cards = [(q.strip(), a.strip(), 'bilibili-learning', '') for q, a in qa_pairs[:20]]

    export_csv(cards, sys.argv[2])
    print(f"Exported {len(cards)} cards to {sys.argv[2]}")
