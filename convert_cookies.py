#!/usr/bin/env python3
"""Convert Netscape cookies.txt → Cobalt cookies.json"""
import json, os

src = os.path.join(os.path.dirname(__file__), "cookies.txt")
dst = os.path.join(os.path.dirname(__file__), "cookies.json")

cookies = []
with open(src) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and ".youtube.com" in parts[0]:
            cookies.append(f"{parts[5]}={parts[6]}")

with open(dst, "w") as f:
    json.dump({"youtube": ["; ".join(cookies)]}, f, indent=2)

print(f"✓ Converted {len(cookies)} cookies → {dst}")
