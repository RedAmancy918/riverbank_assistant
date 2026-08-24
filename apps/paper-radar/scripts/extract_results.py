#!/usr/bin/env python3
"""Extract result-bearing sentences from Experiments sections."""
import sys, json, re
from pathlib import Path

ids = json.loads(sys.argv[1])
outdir = Path("/tmp/paperradar")

for aid in ids:
    p = outdir / f"{aid}.json"
    if not p.exists():
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"\n{'='*90}\n### {aid}\n{'='*90}")
    for sec in d["sections"]:
        if "experiment" not in sec["heading"].lower() and "conclusion" not in sec["heading"].lower():
            continue
        body = sec["body"]
        # split into sentences
        sents = re.split(r"(?<=[.;]) ", body)
        for s in sents:
            if re.search(r"\d+(\.\d+)?\s*%|\d+/\d+|[0-9]+\.[0-9]+", s) and len(s) < 500:
                print(f"  [{sec['heading'][:20]}] {s.strip()}")
