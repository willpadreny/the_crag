#!/usr/bin/env python3
import argparse
import html
import os
import re
import sys
import time
import random
from collections import OrderedDict
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlsplit, urlunsplit

IMPERSONATE = "chrome"
try:
    from curl_cffi import requests as _http
    USING_CFFI = True
except ImportError:
    import requests as _http
    USING_CFFI = False

from bs4 import BeautifulSoup

BASE = "https://www.thecrag.com"
USER_AGENT = "PersonalCragBackup/1.1 (personal offline use; contact: you@example.com)"
REQUEST_DELAY = random.uniform(1.0, 5.0) 
MAX_PAGES_DEFAULT = 2000
CACHE_DIR = ".cache_thecrag"
AUS_BBOX = (-45.0, -9.0, 112.0, 155.0)


def make_session():
    if USING_CFFI:
        return _http.Session(impersonate=IMPERSONATE)
    s = _http.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


session = make_session()


def cache_path(url: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", url)[-180:]
    return os.path.join(CACHE_DIR, safe + ".html")


def fetch(url: str):
    """Return (text, final_url, from_cache) or (None, None, False)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = cache_path(url)
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            return f.read(), url, True
    max_attempts = 6
    for attempt in range(max_attempts):
        try:
            time.sleep(REQUEST_DELAY)
            r = session.get(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                with open(cp, "w", encoding="utf-8") as f:
                    f.write(r.text)
                return r.text, r.url, False
            if r.status_code in (429, 503):
                ra = r.headers.get("Retry-After")
                try:
                    wait = min(int(ra), 120) if ra else min(10 * (2 ** attempt), 120)
                except ValueError:
                    wait = min(10 * (2 ** attempt), 120)
                print(f"  [{r.status_code}] rate-limited; waiting {wait}s "
                      f"(attempt {attempt + 1}/{max_attempts})")
                time.sleep(wait)
                continue
            print(f"  [{r.status_code}] skipping {url}")
            return None, None, False
        except Exception as e:
            print(f"  [err] {e} - retry {attempt + 1}")
            time.sleep(3 * (attempt + 1))
    return None, None, False


def is_gated(requested: str, final_url: str | None) -> bool:
    """True if we asked for a content page but got redirected to the homepage."""
    if not final_url:
        return False
    fp = urlparse(final_url).path.rstrip("/")
    return fp.endswith("/home") and not requested.rstrip("/").endswith("/home")


def path_of(url: str) -> str:
    p = urlparse(url).path
    return re.sub(r"^/[a-z]{2}/", "/", p)


def is_route(url: str) -> bool:
    return "/route/" in path_of(url)


def in_scope(url: str, scope_path: str) -> bool:
    return path_of(url).startswith(scope_path)


# grade token (case preserved so we can tell Font "6B" from French "6b")
GRADE_RE = re.compile(r"\b(V\d+|VB|VW|E\d+|5\.\d+[a-d]?|\d{1,2}[A-Za-z+/]{0,3}|WI\d+|M\d+)\b")


def parse_grade(text: str) -> str:
    m = GRADE_RE.search(text)
    return m.group(0) if m else ""


def looks_boulder(grade: str, info: str, area: str) -> bool:
    g = grade.strip()
    if re.match(r"^V\d", g) or g in ("VB", "VW"):
        return True
    if re.match(r"^\d{1,2}[A-C][+/-]?$", g):
        return True
    blob = f"{info} {area}".lower()
    return "boulder" in blob


def extract_routes(soup: BeautifulSoup, page_url: str) -> list[dict]:
    out = OrderedDict()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("#")[0]
        if not is_route(href):
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        row = a.find_parent(["tr", "li"]) or a.parent
        row_text = " ".join(row.get_text(" ", strip=True).split()) if row else name
        leftover = row_text.replace(name, " ", 1).strip()
        out.setdefault(href, {"name": name, "grade": parse_grade(leftover),
                              "info": leftover, "url": href})
    return list(out.values())


def extract_child_areas(soup: BeautifulSoup, page_url: str, scope_path: str) -> list[str]:
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("#")[0].split("?")[0]
        if is_route(href) or not in_scope(href, scope_path):
            continue
        if "/climbing/" not in href and "/area/" not in href:
            continue
        if href == page_url or href in seen:
            continue
        seen.add(href)
        out.append(href)
    return out


COORD_PATTERNS = [
    re.compile(r'data-lat[^0-9\-]*(-?\d+\.\d+)[^0-9\-]+data-l(?:ng|on)[^0-9\-]*(-?\d+\.\d+)', re.I),
    re.compile(r'"lat(?:itude)?"\s*:\s*(-?\d+\.\d+)\s*,\s*"l(?:ng|on|ongitude)"\s*:\s*(-?\d+\.\d+)', re.I),
    re.compile(r'"latlng"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)', re.I),
    re.compile(r'latitude["\s:=]+(-?\d+\.\d+).{0,40}?longitude["\s:=]+(-?\d+\.\d+)', re.I | re.S),
]

LAT_RE = re.compile(r'(?:data-lat(?:itude)?|["\']?lat(?:itude)?["\']?)\s*[:=]\s*["\']?(-?\d+\.\d+)', re.I)
LNG_RE = re.compile(r'(?:data-l(?:ng|on|ongitude)|["\']?l(?:ng|on|ongitude)["\']?)\s*[:=]\s*["\']?(-?\d+\.\d+)', re.I)


def extract_coords(page_html: str):
    lo_lat, hi_lat, lo_lng, hi_lng = AUS_BBOX
    for pat in COORD_PATTERNS:
        for m in pat.finditer(page_html):
            try:
                lat, lng = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            if lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng:
                return (lat, lng)
    lat = next((v for v in (float(x) for x in LAT_RE.findall(page_html))
                if lo_lat <= v <= hi_lat), None)
    lng = next((v for v in (float(x) for x in LNG_RE.findall(page_html))
                if lo_lng <= v <= hi_lng), None)
    if lat is not None and lng is not None:
        return (lat, lng)
    return None


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.split("|")[0].strip()
    return "Unknown"


def with_page(url: str, page: int) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    q["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def area_path_from_route(url: str):
    """.../climbing/australia/corin-road-bouldering/route/123 -> 'australia/corin-road-bouldering'."""
    m = re.search(r"/climbing/(.+?)/route/", path_of(url))
    return m.group(1) if m else None


def area_url_from_path(apath):
    return f"{BASE}/en/climbing/{apath}" if apath else None


def prettify_area(apath):
    if not apath:
        return "Ungrouped boulders"
    slug = apath.rstrip("/").split("/")[-1]
    name = slug.replace("-", " ").title()
    return re.sub(r"\b(Act|Nsw|Wa|Nt|Sa)\b",
                  lambda m: m.group(1).upper(), name)


def clean_title(raw: str, fallback: str) -> str:
    """theCrag h1 mashes 'Name' + discipline + 'N routes in crag' together — cut it."""
    if not raw:
        return fallback
    name = re.split(
        r"(?:All\b|Rock\b|Trad\b|Sport\b|Mixed\b|Aid\b|Ice\b|Alpine\b|Mostly\b|[Bb]ouldering|climbing)",
        raw, 1,
    )[0].strip(" ,|·🚫")
    return name if name and not name[0].isdigit() else fallback


# bare "-35.4776, 148.9288" pairs sometimes appear in route descriptions
TEXT_COORD_RE = re.compile(r"(-\d{2}\.\d{3,})\s*,?\s+(1[0-5]\d\.\d{3,})")


def extract_coords_from_text(text: str):
    lo_lat, hi_lat, lo_lng, hi_lng = AUS_BBOX
    m = TEXT_COORD_RE.search(text or "")
    if not m:
        return None
    try:
        lat, lng = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng:
        return (lat, lng)
    return None


def crawl(start_url: str, max_pages: int, boulder_only: bool) -> "OrderedDict[str, dict]":
    scope_path = path_of(start_url).rstrip("/")
    areas: "OrderedDict[str, dict]" = OrderedDict()
    queue, visited, pages = [start_url], set(), 0
    while queue and pages < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        text, final, cached = fetch(url)
        pages += 1
        if not text:
            continue
        if is_gated(url, final):
            print(f"[{pages}] GATED -> homepage ({url}); skipping")
            continue
        soup = BeautifulSoup(text, "lxml")
        title = page_title(soup)
        routes = extract_routes(soup, url)
        if boulder_only:
            routes = [r for r in routes if looks_boulder(r["grade"], r["info"], title)]
        coords = extract_coords(text)
        if routes or coords:
            node = areas.setdefault(url, {"title": title, "url": url,
                                          "coords": coords, "routes": []})
            if coords and not node["coords"]:
                node["coords"] = coords
            have = {r["url"] for r in node["routes"]}
            node["routes"].extend(r for r in routes if r["url"] not in have)
            print(f"[{pages}] {title}: +{len(routes)} boulders"
                  f"{' (located)' if coords else ''}")
        else:
            print(f"[{pages}] {title}: (index)")
        for child in extract_child_areas(soup, url, scope_path):
            if child not in visited:
                queue.append(child)
    print(f"\nCrawled {pages} pages; {len(areas)} areas with content.")
    return areas


def crawl_routes_search(routes_url: str, max_pages: int, boulder_only: bool,
                        resolve_coords: bool, boulder_coords: bool = False
                        ) -> "OrderedDict[str, dict]":
    """Collect routes from a faceted list, then group by parent area (fetched for coords)."""
    collected: "OrderedDict[str, dict]" = OrderedDict()
    page, empties = 1, 0
    while page <= max_pages:
        url = routes_url if page == 1 else with_page(routes_url, page)
        text, final, cached = fetch(url)
        if not text:
            break
        if is_gated(url, final):
            print("GATED: the faceted search redirected to the homepage.\n"
                  "Pass your browser Cookie header via --cookie (see script header).")
            break
        soup = BeautifulSoup(text, "lxml")
        rows = extract_routes(soup, url)
        new = [r for r in rows if r["url"] not in collected]
        for r in new:
            collected[r["url"]] = r
        print(f"[page {page}] +{len(new)} routes (total {len(collected)})")
        if not new:
            empties += 1
            if empties >= 2:
                break
        else:
            empties = 0
        page += 1

    if boulder_only:
        collected = OrderedDict(
            (k, v) for k, v in collected.items()
            if looks_boulder(v["grade"], v["info"], "")
        )

    areas: "OrderedDict[str, dict]" = OrderedDict()
    for r in collected.values():
        apath = area_path_from_route(r["url"])
        key = apath or "ungrouped"
        node = areas.setdefault(
            key,
            {"title": prettify_area(apath), "url": area_url_from_path(apath),
             "coords": None, "routes": []},
        )
        r["coords"] = extract_coords_from_text(r["info"])
        node["routes"].append(r)

    if resolve_coords:
        print(f"\nResolving coordinates for {len(areas)} crags "
              f"(one request each, cached)…")
        for i, (key, node) in enumerate(areas.items(), 1):
            if not node["url"]:
                continue
            text, final, cached = fetch(node["url"])
            if not text or is_gated(node["url"], final):
                print(f"  [{i}/{len(areas)}] {node['title']}: no page")
                continue
            csoup = BeautifulSoup(text, "lxml")
            node["coords"] = extract_coords(text)
            print(f"  [{i}/{len(areas)}] {node['title']}: "
                  f"{'located' if node['coords'] else 'no coords'}")

    if boulder_coords:
        all_routes = [r for n in areas.values() for r in n["routes"]]
        total = len(all_routes)
        print(f"\nResolving PER-BOULDER coordinates for {total} boulders "
              f"(one request each — this is the slow, heavy path; cached + resumable)…")
        located = 0
        for i, r in enumerate(all_routes, 1):
            if r.get("coords"):          # already had GPS in its description
                located += 1
                continue
            text, final, cached = fetch(r["url"])
            if text and not is_gated(r["url"], final):
                r["coords"] = extract_coords(text)
                if r["coords"]:
                    located += 1
            if i % 25 == 0 or i == total:
                print(f"  [{i}/{total}] located {located}")
    return areas


def assign_tags(areas: dict) -> list[dict]:
    """Return a sorted list of area nodes, each given a stable Txx tag."""
    nodes = [n for n in areas.values() if n["routes"]]
    nodes.sort(key=lambda n: n["title"].lower())
    for i, n in enumerate(nodes, 1):
        n["tag"] = f"T{i:02d}"
    return nodes


def write_gpx(nodes: list[dict], out_path: str) -> int:
    pts = []
    for n in nodes:
        if n["coords"]:
            lat, lng = n["coords"]
            name = html.escape(f'{n["tag"]} · {n["title"]}')
            desc = html.escape(f'{len(n["routes"])} problems — {n["url"]}')
            pts.append(f'  <wpt lat="{lat}" lon="{lng}">\n'
                       f'    <name>{name}</name>\n'
                       f'    <desc>{desc}</desc>\n'
                       f'    <sym>Pin</sym>\n  </wpt>')
        # precise pins for individual boulders that had GPS in their description
        for r in n["routes"]:
            if not r.get("coords"):
                continue
            lat, lng = r["coords"]
            nm = html.escape(f'{n["tag"]} · {r["name"]} ({r["grade"]})')
            pts.append(f'  <wpt lat="{lat}" lon="{lng}">\n'
                       f'    <name>{nm}</name>\n'
                       f'    <desc>{html.escape(r["url"])}</desc>\n'
                       f'    <sym>Flag</sym>\n  </wpt>')
    gpx = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<gpx version="1.1" creator="thecrag_scraper" '
           'xmlns="http://www.topografix.com/GPX/1/1">\n'
           + "\n".join(pts) + "\n</gpx>\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(gpx)
    return len(pts)


def write_html(nodes: list[dict], out_path: str, source_url: str) -> None:
    total = sum(len(n["routes"]) for n in nodes)
    generated = time.strftime("%Y-%m-%d %H:%M")
    sections = []
    for n in nodes:
        loc = ""
        if n["coords"]:
            lat, lng = n["coords"]
            loc = (f' <a class="geo" target="_blank" rel="noopener" '
                   f'href="https://www.google.com/maps?q={lat},{lng}">📍</a>')
        rows = "".join(
            "<tr>"
            f'<td class="g">{html.escape(r["grade"])}</td>'
            f'<td><a href="{html.escape(r["url"])}" target="_blank" '
            f'rel="noopener">{html.escape(r["name"])}</a>'
            + (f' <a class="geo" target="_blank" rel="noopener" '
               f'href="https://www.google.com/maps?q={r["coords"][0]},{r["coords"][1]}">📍</a>'
               if r.get("coords") else "")
            + f'</td><td class="i">{html.escape(r["info"])}</td></tr>'
            for r in n["routes"]
        )
        sections.append(
            f'<section class="crag" id="{n["tag"]}">'
            f'<h2><span class="tag">{n["tag"]}</span> {html.escape(n["title"])} '
            f'<span class="ct">({len(n["routes"])})</span>{loc}</h2>'
            f'<table>{rows}</table></section>'
        )
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bouldering guide - offline</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 16px/1.4 -apple-system, system-ui, sans-serif; margin: 0 auto;
          max-width: 900px; padding: 1rem; }}
  header {{ position: sticky; top: 0; background: Canvas; padding: .6rem 0;
            border-bottom: 1px solid #8884; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .4rem; }}
  .meta {{ font-size: .8rem; opacity: .7; margin: 0 0 .5rem; }}
  #q {{ width: 100%; padding: .6rem .8rem; font-size: 1rem; border: 1px solid #8886;
        border-radius: 10px; background: Field; color: FieldText; }}
  section.crag {{ margin: 1.2rem 0; scroll-margin-top: 6rem; }}
  h2 {{ font-size: 1.05rem; border-bottom: 2px solid #8883; padding-bottom: .2rem; }}
  .tag {{ font: 600 .75rem monospace; background: #8883; padding: .1rem .35rem;
          border-radius: 5px; }}
  .ct {{ opacity: .5; font-weight: normal; font-size: .85rem; }}
  .geo {{ text-decoration: none; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: .35rem .4rem; border-bottom: 1px solid #8882; vertical-align: top; }}
  td.g {{ white-space: nowrap; font-weight: 600; width: 3.5rem; }}
  td.i {{ font-size: .82rem; opacity: .7; }}
  a {{ color: LinkText; }}
  .hidden {{ display: none; }}
</style></head><body>
<header>
  <h1>Bouldering guide (offline)</h1>
  <p class="meta">{len(nodes)} boulders/areas · {total} problems · generated {generated}<br>
     Tip: tap a map pin's tag (e.g. T07), then search it here. ·
     Data © theCrag contributors, CC BY-NC-SA · <a href="{html.escape(source_url)}">source</a></p>
  <input id="q" type="search" placeholder="Search problem, grade, boulder, or tag…" autocomplete="off">
</header>
{''.join(sections)}
<script>
const q = document.getElementById('q');
const crags = [...document.querySelectorAll('section.crag')].map(c => ({{
  el: c, head: c.querySelector('h2').textContent.toLowerCase(),
  rows: [...c.querySelectorAll('tr')]
}}));
q.addEventListener('input', () => {{
  const t = q.value.trim().toLowerCase();
  for (const c of crags) {{
    const headMatch = c.head.includes(t);
    let any = false;
    for (const r of c.rows) {{
      const show = !t || headMatch || r.textContent.toLowerCase().includes(t);
      r.classList.toggle('hidden', !show);
      any = any || show;
    }}
    c.el.classList.toggle('hidden', !(any || headMatch));
  }}
}});
</script>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)


def main() -> None:
    global IMPERSONATE, session, REQUEST_DELAY
    ap = argparse.ArgumentParser(description="Personal offline boulder backup from theCrag.")
    ap.add_argument("--url", help="area/region/crag URL to crawl (Mode B)")
    ap.add_argument("--routes-url", help="faceted routes-search URL, e.g. .../with-gear-style/boulder/ (Mode A)")
    ap.add_argument("--cookie", help="raw Cookie header from your browser (makes Mode A work)")
    ap.add_argument("--user-agent", help="your browser's User-Agent (must match the "
                    "one that earned cf_clearance, or Cloudflare will block the script)")
    ap.add_argument("--impersonate", default=IMPERSONATE,
                    help="curl_cffi browser to impersonate: chrome | firefox | safari | edge "
                    "(match the browser your cookie came from)")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY,
                    help="seconds between requests (raise to 3-5 if you hit 429s)")
    ap.add_argument("--all-styles", action="store_true", help="don't filter to boulders")
    ap.add_argument("--no-route-coords", action="store_true",
                    help="Mode A: skip the per-crag coord fetch (fastest; no pins)")
    ap.add_argument("--boulder-coords", action="store_true",
                    help="Mode A: fetch EVERY boulder's page for its own coordinates "
                    "(per-boulder pins; ~1 request per boulder, slow but cached/resumable)")
    ap.add_argument("--robots", action="store_true", help="print robots.txt and exit")
    args = ap.parse_args()
    REQUEST_DELAY = args.delay

    if args.impersonate != IMPERSONATE:
        IMPERSONATE = args.impersonate
        session = make_session()
    print(f"HTTP backend: {'curl_cffi (impersonate=' + IMPERSONATE + ')' if USING_CFFI else 'requests (no TLS impersonation — Cloudflare may 403)'}")

    if args.user_agent:
        session.headers.update({"User-Agent": args.user_agent})
    if args.cookie:
        session.headers.update({"Cookie": args.cookie})

    if args.robots:
        text, _, _ = fetch(BASE + "/robots.txt")
        print(text or "(could not fetch)")
        return

    if not (args.url or args.routes_url):
        ap.error("provide --routes-url (Mode A) or --url (Mode B), or --robots")

    boulder_only = not args.all_styles
    if args.routes_url:
        areas = crawl_routes_search(args.routes_url, args.max_pages, boulder_only,
                                    resolve_coords=not args.no_route_coords,
                                    boulder_coords=args.boulder_coords)
    else:
        areas = crawl(args.url, args.max_pages, boulder_only)

    nodes = assign_tags(areas)
    if not nodes:
        print("No matching climbs found. If you used --routes-url and saw 'GATED', "
              "pass --cookie. Otherwise inspect cached HTML in "
              f"{CACHE_DIR}/ to check what came back.")
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)
    html_path = os.path.join(args.out, "climbs.html")
    gpx_path = os.path.join(args.out, "crags.gpx")
    write_html(nodes, html_path, args.url or args.routes_url)
    n_wpt = write_gpx(nodes, gpx_path)
    located = sum(1 for n in nodes if n["coords"])
    print(f"\nWrote {html_path}")
    print(f"Wrote {gpx_path}  ({n_wpt} located waypoints; {located}/{len(nodes)} areas located)")


if __name__ == "__main__":
    main()
