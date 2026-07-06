#!/usr/bin/env python3
"""
Parse a "collection" markdown file (like OpenSwarm_100_video_texts_v3.md) into
individual video scripts.

Each script looks like:

    ### 1 — Full-stack SaaS (A)

    ```
    <the on-screen text block>
    ```
    CAPTION: <post caption>
    HASHTAGS: <#a #b #c>

parse_collection(path) -> list of dicts:
    { "n": 1, "title": "Full-stack SaaS (A)",
      "text": "<block>", "caption": "...", "hashtags": "..." }
"""

import re

HEADER = re.compile(r"^#{2,4}\s*(\d+)\s*[—\-:]\s*(.+?)\s*$")


def parse_collection(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    scripts = []
    i = 0
    n = len(lines)
    while i < n:
        m = HEADER.match(lines[i])
        if not m:
            i += 1
            continue
        num, title = int(m.group(1)), m.group(2).strip()
        i += 1

        # find the opening code fence
        while i < n and not lines[i].strip().startswith("```"):
            # stop if we hit the next header (malformed section)
            if HEADER.match(lines[i]):
                break
            i += 1
        if i >= n or not lines[i].strip().startswith("```"):
            continue
        i += 1  # past opening fence

        block = []
        while i < n and not lines[i].strip().startswith("```"):
            block.append(lines[i])
            i += 1
        i += 1  # past closing fence

        # trim blank edges of the block
        while block and not block[0].strip():
            block.pop(0)
        while block and not block[-1].strip():
            block.pop()

        caption, hashtags = "", ""
        # scan a few lines after the block for CAPTION / HASHTAGS
        look = 0
        while i < n and look < 6 and not HEADER.match(lines[i]) \
                and not lines[i].strip().startswith("---"):
            s = lines[i].strip()
            if s.upper().startswith("CAPTION:"):
                caption = s.split(":", 1)[1].strip()
            elif s.upper().startswith("HASHTAGS:"):
                hashtags = s.split(":", 1)[1].strip()
            i += 1
            look += 1

        if block:
            scripts.append({
                "n": num, "title": title,
                "text": "\n".join(block),
                "caption": caption, "hashtags": hashtags,
            })

    scripts.sort(key=lambda s: s["n"])
    return scripts


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "texts.md"
    scripts = parse_collection(path)
    print(f"parsed {len(scripts)} scripts")
    for s in scripts[:2]:
        print(f"\n#{s['n']} — {s['title']}")
        print(s["text"])
        print("CAPTION:", s["caption"])
        print("HASHTAGS:", s["hashtags"])
