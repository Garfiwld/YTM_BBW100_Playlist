#!/usr/bin/env python3
"""Scrape videoIds from YouTube Music search via Playwright.

Opens https://music.youtube.com/search?q=<query>, clicks the "Songs" (and/or
"Videos") filter chip, captures the /youtubei/v1/search XHR that fires, and pulls
every `...musicResponsiveListItemRenderer.playlistItemData.videoId` out of the
response. Results go into a SQLite db.

    pip install playwright && playwright install chromium

    python3 ytm_search.py "Birds of a Feather Billie Eilish" "APT. ROSE Bruno Mars"
    python3 ytm_search.py --from-chart charts/2026-09-05.json --unmatched-only
    python3 ytm_search.py --videos "some query"          # also do the Videos chip

Table  ytm_hit(query, filter, rank, videoId, title, subtitle, ts)  -- UNIQUE(query,filter,videoId)
"""

import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ytm_search.db")
CSV = os.path.join(HERE, "ytm_search.csv")
SEARCH_XHR = "/youtubei/v1/search"
# YT Music blocks the stock Playwright UA ("browser not supported"); pose as desktop Chrome.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")


def find_all(node, key):
    """Yield every value stored under `key` anywhere in a nested dict/list."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            else:
                yield from find_all(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from find_all(v, key)


def runs_text(node):
    """First text.runs[*].text found under node (used for title / subtitle)."""
    for runs in find_all(node, "runs"):
        if isinstance(runs, list) and runs and isinstance(runs[0], dict) and "text" in runs[0]:
            return "".join(r.get("text", "") for r in runs)
    return ""


def hits_from_response(data):
    """[(videoId, title, subtitle), ...] in result order, de-duped."""
    out, seen = [], set()
    for item in find_all(data, "musicResponsiveListItemRenderer"):
        vid = next(find_all(item.get("playlistItemData", {}), "videoId"), None) \
            or next(find_all(item.get("navigationEndpoint", {}), "videoId"), None)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        cols = item.get("flexColumns", [])
        title = runs_text(cols[0]) if cols else ""
        subtitle = runs_text(cols[1]) if len(cols) > 1 else ""
        out.append((vid, title, subtitle))
    return out


def scrape(page, query, filt):
    page.goto("https://music.youtube.com/search?q=" + urllib.parse.quote(query),
              wait_until="domcontentloaded")
    # cookie consent (region dependent)
    for sel in ('button:has-text("Reject all")', 'button:has-text("Accept all")',
                'tp-yt-paper-button:has-text("Reject all")'):
        try:
            page.locator(sel).first.click(timeout=2000)
            break
        except Exception:
            pass
    page.wait_for_selector("ytmusic-chip-cloud-chip-renderer", timeout=15000)
    chip = page.locator(f'ytmusic-chip-cloud-chip-renderer:has-text("{filt}")').first
    with page.expect_response(
        lambda r: SEARCH_XHR in r.url and r.request.method == "POST", timeout=15000
    ) as ri:
        chip.click()
    return hits_from_response(ri.value.json())


COLS = ["query", "filter", "rank", "videoId", "title", "subtitle", "ts"]


def dump_csv():
    con = sqlite3.connect(DB)
    rows = con.execute(f"SELECT {','.join(COLS)} FROM ytm_hit "
                       "ORDER BY query, filter, rank").fetchall()
    con.close()
    with open(CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
    print(f"wrote {CSV} ({len(rows)} rows)")


def merge_cache(chart_path, force):
    """Fold the rank-0 Songs hit for each chart row into ../cache.json (+ unmatched.txt,
    matches.csv, matches.db). No browser -- reads the existing ytm_search.db."""
    import sync

    chart = json.load(open(chart_path))
    date = chart["date"]
    con = sqlite3.connect(DB)
    top = {q: (v, t) for q, v, t in con.execute(
        "SELECT query, videoId, title FROM ytm_hit WHERE filter='Songs' AND rank=0")}
    con.close()

    cache = sync.load_json(sync.CACHE, {})
    weak_lines, added, weak_n, skipped = [], [], 0, 0
    try:
        weak_lines = [l for l in open(sync.UNMATCHED).read().splitlines() if l.strip()]
    except FileNotFoundError:
        pass
    weak_keys = {l.split("\t")[1] for l in weak_lines}

    for row in chart["data"]:
        key = f"{row['song']}|{row['artist']}"
        if key in cache and not force:
            continue
        hit = top.get(f"{row['song']} {row['artist']}")
        if not hit:
            skipped += 1
            continue
        vid, title = hit
        cache[key] = {"videoId": vid, "audio": vid, "mv": None}
        if sync.well_matched(title, row["song"]):
            weak_keys.discard(key)
            weak_lines = [l for l in weak_lines if l.split("\t")[1] != key]
        elif key not in weak_keys:
            weak_lines.append(f"{date}\t{key}\t{vid}")
            weak_n += 1
        added.append(key)

    sync.save_json(sync.CACHE, cache)
    with open(sync.UNMATCHED, "w") as f:
        f.write("\n".join(weak_lines) + ("\n" if weak_lines else ""))
    sync.write_matches_csv(cache)
    sync.write_matches_db(cache)
    print(f"merged {len(added)} into cache.json ({weak_n} flagged weak), {skipped} had no hit")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--from-chart", help="pull 'song artist' queries from a charts/*.json")
    ap.add_argument("--unmatched-only", action="store_true", help="with --from-chart: only rows without a videoId")
    ap.add_argument("--videos", action="store_true", help="also click the Videos chip")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--limit", type=int, default=3, help="store top N hits per (query,filter)")
    ap.add_argument("--merge-cache", action="store_true",
                    help="no scraping: fold rank-0 Songs hits from ytm_search.db into cache.json (needs --from-chart)")
    ap.add_argument("--force", action="store_true", help="with --merge-cache: overwrite existing cache entries")
    ap.add_argument("--dump-csv", action="store_true", help="no scraping: (re)write ytm_search.csv from ytm_search.db")
    args = ap.parse_args()

    if args.dump_csv:
        return dump_csv()
    if args.merge_cache:
        if not args.from_chart:
            sys.exit("--merge-cache needs --from-chart")
        return merge_cache(args.from_chart, args.force)

    queries = list(args.queries)
    if args.from_chart:
        chart = json.load(open(args.from_chart))
        for row in chart["data"]:
            if args.unmatched_only and row.get("videoId"):
                continue
            queries.append(f"{row['song']} {row['artist']}")
    if not queries:
        sys.exit("no queries (pass some, or --from-chart)")

    filters = ["Songs"] + (["Videos"] if args.videos else [])

    from playwright.sync_api import sync_playwright  # lazy: only main() needs a browser

    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS ytm_hit(
        query TEXT, filter TEXT, rank INT, videoId TEXT, title TEXT, subtitle TEXT, ts TEXT,
        UNIQUE(query, filter, videoId))""")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headful,
            args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_context(user_agent=UA, locale="en-US",
                                   viewport={"width": 1280, "height": 900}).new_page()
        for q in queries:
            for filt in filters:
                try:
                    hits = scrape(page, q, filt)[:args.limit]
                except Exception as e:
                    print(f"FAIL  [{filt}] {q}: {e}")
                    continue
                now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                con.executemany(
                    "INSERT OR REPLACE INTO ytm_hit VALUES (?,?,?,?,?,?,?)",
                    [(q, filt, i, v, t, s, now) for i, (v, t, s) in enumerate(hits)])
                con.commit()
                top = hits[0] if hits else ("-", "", "")
                print(f"OK    [{filt}] {q} -> {top[0]} {top[1]!r} ({len(hits)} hits)")
        browser.close()

    con.close()
    print(f"\nwrote {DB}")
    dump_csv()


if __name__ == "__main__":
    main()
