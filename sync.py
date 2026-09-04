#!/usr/bin/env python3
"""Sync the Billboard Hot 100 into a rolling YouTube Music playlist + a weekly archive.

Data source: https://github.com/mhollingshead/billboard-hot-100 (recent.json)
Auth: ytmusicapi OAuth (oauth.json + Google OAuth client id/secret).

State files (committed back by CI):
  config.json    -> {"rolling_playlist_id": str, "last_synced_date": "YYYY-MM-DD"}
  cache.json     -> {"song|artist": "videoId"}   resolved matches, reused across weeks
  unmatched.txt  -> "DATE\tsong|artist\tvideoId"  songs that only matched a plain
                    YouTube video (not a real YT Music song); retried every run.
"""

import difflib
import json
import os
import re
import sys
import urllib.request

RECENT_URL = "https://raw.githubusercontent.com/mhollingshead/billboard-hot-100/main/recent.json"
ROLLING_NAME = "Billboard Hot 100"
FUZZY_THRESHOLD = 0.6

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()

CONFIG = os.path.join(HERE, "config.json")
CACHE = os.path.join(HERE, "cache.json")
UNMATCHED = os.path.join(HERE, "unmatched.txt")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def norm(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # drop parentheticals
    s = re.sub(r"[^a-z0-9฀-๿]+", " ", s)  # keep latin+digits+thai
    return s.strip()


def primary_artist(artist):
    # "Post Malone Featuring Doja Cat" / "A & B" / "A, B" -> "A"
    a = re.split(r"\bfeat\.?\b|\bfeaturing\b|\bwith\b|&|,|/| x ", artist, flags=re.I)[0]
    return a.strip()


def ratio(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def yt():
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.oauth import OAuthCredentials

    oauth_file = os.environ.get("YTM_OAUTH_FILE", os.path.join(HERE, "oauth.json"))
    cid = os.environ["YTM_CLIENT_ID"]
    csecret = os.environ["YTM_CLIENT_SECRET"]
    return YTMusic(oauth_file, oauth_credentials=OAuthCredentials(client_id=cid, client_secret=csecret))


def first_id(results, want_title, check_title):
    for r in results or []:
        vid = r.get("videoId")
        if not vid:
            continue
        if check_title and ratio(r.get("title", ""), want_title) < FUZZY_THRESHOLD:
            continue
        return vid
    return None


def match(ym, song, artist):
    """Return (videoId, well_matched). well_matched=False means it's a plain YouTube
    video fallback, not a confirmed YT Music song."""
    q = f"{song} {artist}"
    vid = first_id(ym.search(q, filter="songs"), song, check_title=True)
    if vid:
        return vid, True
    # retry with parentheticals + featured artists stripped
    q2 = f"{norm(song)} {primary_artist(artist)}"
    vid = first_id(ym.search(q2, filter="songs"), song, check_title=True)
    if vid:
        return vid, True
    # fallback: any YouTube video
    vid = first_id(ym.search(q, filter="videos"), song, check_title=False)
    if vid:
        return vid, False
    return None, False


def wipe_and_fill(ym, pid, video_ids):
    old = ym.get_playlist(pid, limit=None).get("tracks", [])
    ym.add_playlist_items(pid, video_ids, duplicates=True)
    if old:
        ym.remove_playlist_items(pid, old)


def main():
    with urllib.request.urlopen(RECENT_URL) as resp:
        chart = json.load(resp)
    date = chart["date"]
    entries = sorted(chart["data"], key=lambda d: d["this_week"])[:100]

    config = load_json(CONFIG, {})
    if config.get("last_synced_date") == date:
        print(f"Chart {date} already synced; nothing to do.")
        return

    cache = load_json(CACHE, {})
    prev_unmatched = set()
    try:
        with open(UNMATCHED) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    prev_unmatched.add(parts[1])
    except FileNotFoundError:
        pass

    ym = yt()
    ordered, still_unmatched, skipped = [], [], []
    seen = set()

    for e in entries:
        song, artist = e["song"], e["artist"]
        key = f"{song}|{artist}"
        force = key in prev_unmatched
        if key in cache and not force:
            vid, well = cache[key], True
        else:
            vid, well = match(ym, song, artist)

        if not vid:
            skipped.append(key)
            print(f"SKIP  #{e['this_week']:>3} {key}")
            continue

        cache[key] = vid
        if not well:
            still_unmatched.append(f"{date}\t{key}\t{vid}")
            print(f"VIDEO #{e['this_week']:>3} {key} -> {vid} (youtube fallback)")
        if vid not in seen:
            seen.add(vid)
            ordered.append(vid)

    if len(ordered) < 50:
        sys.exit(f"Only {len(ordered)} matches - aborting, looks broken.")

    # rolling playlist
    pid = config.get("rolling_playlist_id")
    desc = f"Billboard Hot 100 - auto-updated weekly. Chart week: {date}"
    if not pid:
        pid = ym.create_playlist(ROLLING_NAME, desc, "PUBLIC", video_ids=ordered)
        if not isinstance(pid, str):
            sys.exit(f"create_playlist failed: {pid}")
        config["rolling_playlist_id"] = pid
        print(f"Created rolling playlist {pid}")
    else:
        wipe_and_fill(ym, pid, ordered)
        ym.edit_playlist(pid, description=desc)
        print(f"Updated rolling playlist {pid} ({len(ordered)} tracks)")

    # weekly archive
    archive = ym.create_playlist(f"{ROLLING_NAME} - {date}", desc, "PUBLIC", video_ids=ordered)
    print(f"Created archive playlist {archive}")

    config["last_synced_date"] = date
    save_json(CONFIG, config)
    save_json(CACHE, cache)
    with open(UNMATCHED, "w") as f:
        f.write("\n".join(still_unmatched) + ("\n" if still_unmatched else ""))

    print(f"\nDone. matched={len(ordered)} youtube_fallback={len(still_unmatched)} skipped={len(skipped)}")


if __name__ == "__main__":
    main()
