#!/usr/bin/env python3
"""Dump key sections of fetched papers for reading."""
import sys, json
from pathlib import Path

ids = json.loads(sys.argv[1])
outdir = Path("/tmp/paperradar")
wanted = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
maxlen = int(sys.argv[3]) if len(sys.argv) > 3 else 6000

for aid in ids:
    p = outdir / f"{aid}.json"
    if not p.exists():
        print(f"### {aid} MISSING")
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"\n{'='*100}\n### {aid} — {d['title']}\n{'='*100}")
    print(f"\n[ABSTRACT] {d['abstract'][:1500]}\n")
    for sec in d["sections"]:
        h = sec["heading"].lower()
        if not any(k in h for k in ["method", "experiment", "conclusion", "training", "model"]):
            continue
        body = sec["body"]
        if len(body) > maxlen:
            body = body[:maxlen] + f" ...<truncated, total {len(sec['body'])} chars>"
        print(f"\n--- {sec['heading']} ---\n{body}\n")
