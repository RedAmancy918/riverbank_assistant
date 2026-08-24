#!/usr/bin/env python3
"""Fetch arXiv HTML pages and extract clean text for full reading."""
import sys, re, json, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup


def fetch(url):
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
    r.raise_for_status()
    return r.text


def clean_text(s):
    s = s.replace("\u2009", " ").replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract(html):
    out = {"title": "", "abstract": "", "sections": []}
    soup = BeautifulSoup(html, "lxml")
    t = soup.select_one("h1.ltx_title")
    if t:
        out["title"] = clean_text(t.get_text(" ", strip=True))
    abs_el = soup.select_one(".ltx_abstract")
    if abs_el:
        out["abstract"] = clean_text(abs_el.get_text(" ", strip=True))
    article = soup.select_one("article") or soup
    for sec in article.find_all("section", class_="ltx_section"):
        h = sec.find(["h2", "h3"])
        heading = clean_text(h.get_text()) if h else ""
        body = clean_text(sec.get_text(" ", strip=True))
        if heading:
            # remove duplicated heading text (incl. roman numeral) at start of body
            body = re.sub(r"^[IVXLC]+\.?\s*" + re.escape(heading) + r"\s*", "", body)
        out["sections"].append({"heading": heading, "body": body})
    return out


def main():
    ids = json.loads(sys.argv[1])
    outdir = Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    for aid in ids:
        url = f"https://arxiv.org/html/{aid}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"ERROR {aid}: {e}", file=sys.stderr)
            continue
        data = extract(html)
        data["url"] = url
        (outdir / f"{aid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        heads = [s["heading"] for s in data["sections"]]
        print(f"OK {aid} | title={data['title'][:50]!r} | abslen={len(data['abstract'])} | sections={heads}")
        time.sleep(1)


if __name__ == "__main__":
    main()
