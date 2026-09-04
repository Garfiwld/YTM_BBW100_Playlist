#!/usr/bin/env python3
"""Sync the Billboard Hot 100 into a rolling YouTube Music playlist + a weekly archive.

Data source: https://github.com/mhollingshead/billboard-hot-100 (recent.json)
Auth: OAuth 2.0 refresh-token flow against the YouTube Data API v3 (no ytmusicapi --
      its InnerTube endpoints reject OAuth Bearer tokens with HTTP 400).

Needs a Google OAuth client (type "TVs and Limited Input devices") and a token
minted with `ytmusicapi oauth` (or any device flow for scope
https://www.googleapis.com/auth/youtube).

Quota: two separate daily caps -- 10k units/day AND a hard 100 search.list
calls/day. Each uncached song costs 1-2 search calls, so a cold first run only
gets through ~60-90 songs/day. On QuotaExceeded the script saves cache.json /
unmatched.txt / config.json (minus last_synced_date) and exits; re-running the
next day resumes from the cache.
  - weekly: diff the rolling playlist (remove drops, insert new entries at their
    rank position). Holdovers are NOT reordered.
  - monthly: also snapshot an archive playlist.
  - FULL_REORDER=1 forces a wipe+refill of the rolling playlist to re-sort
    holdovers exactly; run it by hand on a quiet day.

Env / .env:
  YTM_CLIENT_ID, YTM_CLIENT_SECRET   the OAuth client
  YTM_OAUTH_FILE                     token json (default ./oauth.json), needs refresh_token

State files (committed back by CI):
  charts/<date>.json -> the raw Billboard chart for that week, as fetched.
  config.json    -> {"rolling_playlist_id": str, "last_synced_date": "YYYY-MM-DD",
                     "last_archived_month": "YYYY-MM"}
  cache.json     -> {"song|artist": "videoId"}   resolved matches, reused across weeks
  unmatched.txt  -> "DATE\tsong|artist\tvideoId"  weak matches (top hit's title
                    didn't fuzzy-match). Kept from cache like anything else;
                    RETRY_WEAK=1 re-searches them (costs search calls).
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RECENT_URL = "https://raw.githubusercontent.com/mhollingshead/billboard-hot-100/main/recent.json"
API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ROLLING_NAME = "Billboard Hot 100"
FUZZY_THRESHOLD = 0.6

HERE = os.path.dirname(os.path.abspath(__file__))
CHARTS_DIR = os.path.join(HERE, "charts")
CONFIG = os.path.join(HERE, "config.json")
CACHE = os.path.join(HERE, "cache.json")
UNMATCHED = os.path.join(HERE, "unmatched.txt")


def _load_dotenv():
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


# --- pure helpers (unit-tested, no network) ---------------------------------

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
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9฀-๿]+", " ", s)
    return s.strip()


def primary_artist(artist):
    a = re.split(r"\bfeat\.?\b|\bfeaturing\b|\bwith\b|&|,|/| x ", artist, flags=re.I)[0]
    return a.strip()


def ratio(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def pick(items, want_title, check_title):
    """First search result with a videoId (and, if check_title, a fuzzy title match)."""
    for it in items or []:
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        if check_title and ratio(it.get("snippet", {}).get("title", ""), want_title) < FUZZY_THRESHOLD:
            continue
        return vid
    return None


# --- YouTube Data API v3 ----------------------------------------------------

_token = {"value": None}


def access_token():
    if _token["value"]:
        return _token["value"]
    oauth = load_json(os.environ.get("YTM_OAUTH_FILE", os.path.join(HERE, "oauth.json")), None)
    if not oauth or "refresh_token" not in oauth:
        sys.exit("oauth.json missing or has no refresh_token - run `ytmusicapi oauth` first")
    data = urllib.parse.urlencode({
        "client_id": os.environ["YTM_CLIENT_ID"],
        "client_secret": os.environ["YTM_CLIENT_SECRET"],
        "refresh_token": oauth["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data)) as r:
            _token["value"] = json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"token refresh failed: HTTP {e.code}: {e.read().decode()}")
    return _token["value"]


class QuotaExceeded(Exception):
    pass


def api(method, path, params=None, body=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {access_token()}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            if e.code == 429 or "quotaExceeded" in payload:
                raise QuotaExceeded(f"{method} {path}: {payload[:200]}")
            if e.code in (403, 500, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            sys.exit(f"{method} {path} -> HTTP {e.code}: {payload}")


def yt_search(query):
    return api("GET", "search", {
        "part": "snippet", "type": "video", "maxResults": "5", "q": query,
    }).get("items", [])


def resolve(key, song, artist, cache, prev_unmatched, retry_weak):
    """(videoId, well_matched). Returns straight from cache without any network
    call unless the song is uncached (or it's a weak match and retry_weak is set)."""
    weak_cached = key in prev_unmatched
    if key in cache and not (weak_cached and retry_weak):
        return cache[key], not weak_cached
    return match(song, artist)


def match(song, artist):
    """(videoId, well_matched). well_matched=False => top hit didn't fuzzy-match the
    title; logged to unmatched.txt. At most 2 search calls (search.list caps at 100/day)."""
    vid = pick(yt_search(f"{song} {artist}"), song, check_title=True)
    if vid:
        return vid, True
    items = yt_search(f"{norm(song)} {primary_artist(artist)}")
    vid = pick(items, song, check_title=True)
    if vid:
        return vid, True
    vid = pick(items, song, check_title=False)
    return (vid, False) if vid else (None, False)


def playlist_items(pid):
    """[(playlistItemId, videoId), ...] in playlist order."""
    out, page = [], None
    while True:
        params = {"part": "snippet", "playlistId": pid, "maxResults": "50"}
        if page:
            params["pageToken"] = page
        resp = api("GET", "playlistItems", params)
        for it in resp.get("items", []):
            out.append((it["id"], it["snippet"]["resourceId"].get("videoId")))
        page = resp.get("nextPageToken")
        if not page:
            return out


def diff_rolling(pid, desired):
    """Remove items not in `desired` (and duplicates); insert missing ones at their
    rank position. Does not reorder holdovers."""
    current = playlist_items(pid)
    seen, removed = set(), 0
    for item_id, vid in current:
        if vid not in desired or vid in seen:
            api("DELETE", "playlistItems", {"id": item_id})
            removed += 1
        else:
            seen.add(vid)
    added = 0
    for i, vid in enumerate(desired):
        if vid not in seen:
            api("POST", "playlistItems", {"part": "snippet"}, {
                "snippet": {"playlistId": pid, "position": i,
                            "resourceId": {"kind": "youtube#video", "videoId": vid}},
            })
            added += 1
    return added, removed


def add_items(pid, video_ids):
    for vid in video_ids:
        api("POST", "playlistItems", {"part": "snippet"}, {
            "snippet": {"playlistId": pid,
                        "resourceId": {"kind": "youtube#video", "videoId": vid}},
        })


def create_playlist(title, description, video_ids):
    pid = api("POST", "playlists", {"part": "snippet,status"}, {
        "snippet": {"title": title, "description": description},
        "status": {"privacyStatus": "public"},
    })["id"]
    add_items(pid, video_ids)
    return pid


# --- main -----------------------------------------------------------------

def main():
    with urllib.request.urlopen(RECENT_URL) as resp:
        chart = json.load(resp)
    date = chart["date"]
    entries = sorted(chart["data"], key=lambda d: d["this_week"])[:100]

    os.makedirs(CHARTS_DIR, exist_ok=True)
    save_json(os.path.join(CHARTS_DIR, f"{date}.json"), chart)

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

    def persist():
        save_json(CONFIG, config)
        save_json(CACHE, cache)
        with open(UNMATCHED, "w") as f:
            f.write("\n".join(still_unmatched) + ("\n" if still_unmatched else ""))

    def quota_stop(where):
        persist()
        sys.exit(f"\nquota exhausted during {where}; state saved. Re-run tomorrow "
                 f"-- it resumes from cache.json. (matched so far: {len(ordered)})")

    retry_weak = bool(os.environ.get("RETRY_WEAK"))
    ordered, still_unmatched, skipped, seen = [], [], [], set()
    for e in entries:
        song, artist = e["song"], e["artist"]
        key = f"{song}|{artist}"
        try:
            vid, well = resolve(key, song, artist, cache, prev_unmatched, retry_weak)
        except QuotaExceeded:
            quota_stop("search")

        if not vid:
            skipped.append(key)
            print(f"SKIP  #{e['this_week']:>3} {key}")
            continue

        cache[key] = vid
        if not well:
            still_unmatched.append(f"{date}\t{key}\t{vid}")
            print(f"WEAK  #{e['this_week']:>3} {key} -> {vid} (weak title match)")
        if vid not in seen:
            seen.add(vid)
            ordered.append(vid)

    if len(ordered) < 50:
        sys.exit(f"Only {len(ordered)} matches - aborting, looks broken.")

    desc = f"Billboard Hot 100 - auto-updated weekly. Chart week: {date}"
    try:
        pid = config.get("rolling_playlist_id")
        if not pid:
            pid = create_playlist(ROLLING_NAME, desc, ordered)
            config["rolling_playlist_id"] = pid
            print(f"Created rolling playlist {pid}")
        elif os.environ.get("FULL_REORDER"):
            for item_id, _ in playlist_items(pid):
                api("DELETE", "playlistItems", {"id": item_id})
            add_items(pid, ordered)
            print(f"Rebuilt rolling playlist {pid} ({len(ordered)} tracks, full reorder)")
        else:
            added, removed = diff_rolling(pid, ordered)
            print(f"Diffed rolling playlist {pid}: +{added} -{removed}")
        api("PUT", "playlists", {"part": "snippet"},
            {"id": pid, "snippet": {"title": ROLLING_NAME, "description": desc}})

        month = date[:7]
        if config.get("last_archived_month") != month:
            archive = create_playlist(f"{ROLLING_NAME} - {date}", desc, ordered)
            config["last_archived_month"] = month
            print(f"Created archive playlist {archive}")
        else:
            print(f"Archive for {month} already done; skipping.")
    except QuotaExceeded:
        quota_stop("playlist write")

    config["last_synced_date"] = date
    persist()
    print(f"\nDone. matched={len(ordered)} weak={len(still_unmatched)} skipped={len(skipped)}")


if __name__ == "__main__":
    main()
