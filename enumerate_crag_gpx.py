#!/usr/bin/env python3
"""
enumerate_crag_gpx.py — list every crag GPX URL to download, from the cache (offline).

theCrag's per-CRAG gpx export includes individual boulders; the region export does not.
So we download at crag level. This script finds the distinct parent crag of every boulder
route you've already cached and emits one gpx URL per crag.

    python enumerate_crag_gpx.py --cache .cache_thecrag --out crag_gpx_urls.txt

Then feed crag_gpx_urls.txt to fetch_merge_gpx.py.
"""

import argparse
import os
import re
import sys

BASE = "https://www.thecrag.com"

ROUTE_HREF_RE = re.compile(r'(?:https?://[^"\'\s]*?)?/(?:[a-z]{2}/)?climbing/([^"\'\s]+?)/route/\d+', re.I)


def main():
    ap = argparse.ArgumentParser(description="Enumerate crag gpx URLs from cached pages.")
    ap.add_argument("--cache", default=".cache_thecrag")
    ap.add_argument("--out", default="crag_gpx_urls.txt")
    args = ap.parse_args()

    if not os.path.isdir(args.cache):
        sys.exit(f"cache dir not found: {args.cache}")

    crag_paths = set()
    files = [f for f in os.listdir(args.cache) if f.endswith(".html")]
    for fn in files:
        try:
            with open(os.path.join(args.cache, fn), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in ROUTE_HREF_RE.finditer(text):
            crag_paths.add(m.group(1).strip("/"))

    urls = sorted(f"{BASE}/en/climbing/{p}/gpx" for p in crag_paths)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + "\n")

    print(f"Scanned {len(files)} cached pages")
    print(f"Found {len(urls)} distinct crags -> {args.out}")
    for u in urls:
        print("  " + u)


if __name__ == "__main__":
    main()
