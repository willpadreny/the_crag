#!/usr/bin/env python3
"""
extract_cache_gpx.py — build a GPX purely from already-cached theCrag route pages.

No network. Reads the .cache_thecrag/ folder the scraper wrote, finds every cached
*route* page, pulls its coordinate + name out of the saved HTML, and writes one GPX.

Usage:
    python extract_cache_gpx.py                      # cache=.cache_thecrag  out=./out/cache-boulders.gpx
    python extract_cache_gpx.py --cache .cache_thecrag --out ./out/cache-boulders.gpx

Then load the GPX into Organic Maps / Gaia, same as before. Each waypoint is named
after the boulder problem (with grade), and its crag + link go in the description.
"""

import argparse
import html
import os
import re
import sys

AUS_BBOX = (-45.0, -9.0, 112.0, 155.0)

ROUTE_ID_RE = re.compile(r"route_(\d+)")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
CANON_RE = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', re.I)

# coordinate sources, in priority order
LOCATION_RE = re.compile(r"Location:\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")
PAIR_RES = [
    re.compile(r'"lat(?:itude)?"\s*:\s*(-?\d+\.\d+)\s*,\s*"l(?:ng|on|ongitude)"\s*:\s*(-?\d+\.\d+)', re.I),
    re.compile(r'"latlng"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)', re.I),
    re.compile(r'data-lat[^0-9\-]*(-?\d+\.\d+)[^0-9\-]+data-l(?:ng|on)[^0-9\-]*(-?\d+\.\d+)', re.I),
    re.compile(r"(-\d{2}\.\d{3,})\s*,?\s+(1[0-5]\d\.\d{3,})"),
]
LAT_RE = re.compile(r'(?:data-lat(?:itude)?|["\']?lat(?:itude)?["\']?)\s*[:=]\s*["\']?(-?\d+\.\d+)', re.I)
LNG_RE = re.compile(r'(?:data-l(?:ng|on|ongitude)|["\']?l(?:ng|on|ongitude)["\']?)\s*[:=]\s*["\']?(-?\d+\.\d+)', re.I)


def in_bbox(lat, lng):
    lo_lat, hi_lat, lo_lng, hi_lng = AUS_BBOX
    return lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng


def extract_coords(text):
    m = LOCATION_RE.search(text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if in_bbox(lat, lng):
            return (lat, lng)
    for pat in PAIR_RES:
        for m in pat.finditer(text):
            try:
                lat, lng = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            if in_bbox(lat, lng):
                return (lat, lng)
    lat = next((v for v in (float(x) for x in LAT_RE.findall(text))
                if AUS_BBOX[0] <= v <= AUS_BBOX[1]), None)
    lng = next((v for v in (float(x) for x in LNG_RE.findall(text))
                if AUS_BBOX[2] <= v <= AUS_BBOX[3]), None)
    if lat is not None and lng is not None:
        return (lat, lng)
    return None


def get_title(text):
    m = OG_TITLE_RE.search(text) or TITLE_RE.search(text)
    return html.unescape(m.group(1).strip()) if m else ""


def parse_title(t):
    """'Name, V3 at Corin Road Bouldering | theCrag' -> ('Name', 'V3', 'Corin Road Bouldering')."""
    t = re.sub(r"\s*\|\s*theCrag.*$", "", t, flags=re.I).strip()
    m = re.match(r"^(?P<name>.*?)(?:,\s*(?P<grade>[^,]+?))?\s+at\s+(?P<crag>.+)$", t)
    if m:
        return m.group("name").strip(), (m.group("grade") or "").strip(), m.group("crag").strip()
    parts = [p.strip() for p in t.split(",")]
    return parts[0], (parts[1] if len(parts) > 1 else ""), ""


def main():
    ap = argparse.ArgumentParser(description="Build a GPX from cached theCrag route pages (offline).")
    ap.add_argument("--cache", default=".cache_thecrag", help="cache directory")
    ap.add_argument("--out", default="./out/cache-boulders.gpx", help="output GPX path")
    args = ap.parse_args()

    if not os.path.isdir(args.cache):
        sys.exit(f"cache dir not found: {args.cache}")

    files = [f for f in os.listdir(args.cache) if f.endswith(".html") and ROUTE_ID_RE.search(f)]
    print(f"Scanning {len(files)} cached route pages in {args.cache}/ …")

    routes = {}
    located = unlocated = 0
    for fn in files:
        rid = ROUTE_ID_RE.search(fn).group(1)
        if rid in routes:
            continue
        try:
            with open(os.path.join(args.cache, fn), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        coords = extract_coords(text)
        name, grade, crag = parse_title(get_title(text))
        cm = CANON_RE.search(text)
        url = html.unescape(cm.group(1)) if cm else f"route {rid}"
        if coords:
            located += 1
        else:
            unlocated += 1
        routes[rid] = {"name": name or f"Route {rid}", "grade": grade,
                       "crag": crag, "url": url, "coords": coords}

    pts = []
    for r in sorted(routes.values(), key=lambda r: (r["crag"].lower(), r["name"].lower())):
        if not r["coords"]:
            continue
        lat, lng = r["coords"]
        label = f'{r["name"]} ({r["grade"]})' if r["grade"] else r["name"]
        desc = " — ".join(x for x in (r["crag"], r["url"]) if x)
        pts.append(f'  <wpt lat="{lat}" lon="{lng}">\n'
                   f'    <name>{html.escape(label)}</name>\n'
                   f'    <desc>{html.escape(desc)}</desc>\n'
                   f'    <sym>Pin</sym>\n  </wpt>')

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    gpx = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<gpx version="1.1" creator="extract_cache_gpx" '
           'xmlns="http://www.topografix.com/GPX/1/1">\n'
           + "\n".join(pts) + "\n</gpx>\n")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(gpx)

    print(f"Route pages parsed : {len(routes)}")
    print(f"  located          : {located}")
    print(f"  no coords found  : {unlocated}")
    print(f"Wrote {len(pts)} waypoints -> {args.out}")


if __name__ == "__main__":
    main()
