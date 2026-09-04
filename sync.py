#!/usr/bin/env python3
"""Sync the Billboard Hot 100 into a rolling YouTube Music playlist + a weekly archive.

Data source: https://github.com/mhollingshead/billboard-hot-100 (recent.json)
Auth: OAuth 2.0 refresh-token flow against the YouTube Data API v3 (no ytmusicapi --
      its InnerTube endpoints reject OAuth Bearer tokens with HTTP 400).

Needs a Google OAuth client (type "TVs and Limited Input devices") and a token
minted with `ytmusicapi oauth` (or any device flow for scope
https://www.googleapis.com/auth/youtube). Quota: this burns well past the default
10k units/day on the first run -- request an increase first.

Env / .env:
  YTM_CLIENT_ID, YTM_CLIENT_SECRET   the OAuth client
  YTM_OAUTH_FILE                     token json (default ./oauth.json), needs refresh_token

State files (committed back by CI):
  config.json    -> {"rolling_playlist_id": str, "last_synced_date": "YYYY-MM-DD"}
  cache.json     -> {"song|artist": "videoId"}   resolved matches, reused across weeks
  unmatched.txt  -> "DATE\tsong|artist\tvideoId"  weak matches (not Music category);
                    retried every run.
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
MUSIC_CATEGORY = "10"

HERE = os.path.dirname(os.path.abspath(__file__))
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
            if e.code in (403, 500, 503) and attempt < 3 and "quotaExceeded" not in payload:
                time.sleep(2 ** attempt)
                continue
            sys.exit(f"{method} {path} -> HTTP {e.code}: {payload}")


def yt_search(query, music_only):
    params = {"part": "snippet", "type": "video", "maxResults": "5", "q": query}
    if music_only:
        params["videoCategoryId"] = MUSIC_CATEGORY
        params["regionCode"] = "US"
    return api("GET", "search", params).get("items", [])


def match(song, artist):
    """(videoId, well_matched). well_matched=False => not confirmed as a Music-category
    result; logged to unmatched.txt and retried next week."""
    q = f"{song} {artist}"
    vid = pick(yt_search(q, music_only=True), song, check_title=True)
    if vid:
        return vid, True
    q2 = f"{norm(song)} {primary_artist(artist)}"
    vid = pick(yt_search(q2, music_only=True), song, check_title=True)
    if vid:
        return vid, True
    vid = pick(yt_search(q, music_only=False), song, check_title=False)
    return (vid, False) if vid else (None, False)


def playlist_item_ids(pid):
    ids, page = [], None
    while True:
        params = {"part": "id", "playlistId": pid, "maxResults": "50"}
        if page:
            params["pageToken"] = page
        resp = api("GET", "playlistItems", params)
        ids += [it["id"] for it in resp.get("items", [])]
        page = resp.get("nextPageToken")
        if not page:
            return ids


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

    ordered, still_unmatched, skipped, seen = [], [], [], set()
    for e in entries:
        song, artist = e["song"], e["artist"]
        key = f"{song}|{artist}"
        if key in cache and key not in prev_unmatched:
            vid, well = cache[key], True
        else:
            vid, well = match(song, artist)

        if not vid:
            skipped.append(key)
            print(f"SKIP  #{e['this_week']:>3} {key}")
            continue

        cache[key] = vid
        if not well:
            still_unmatched.append(f"{date}\t{key}\t{vid}")
            print(f"VIDEO #{e['this_week']:>3} {key} -> {vid} (non-music fallback)")
        if vid not in seen:
            seen.add(vid)
            ordered.append(vid)

    if len(ordered) < 50:
        sys.exit(f"Only {len(ordered)} matches - aborting, looks broken.")

    desc = f"Billboard Hot 100 - auto-updated weekly. Chart week: {date}"

    pid = config.get("rolling_playlist_id")
    if not pid:
        pid = create_playlist(ROLLING_NAME, desc, ordered)
        config["rolling_playlist_id"] = pid
        print(f"Created rolling playlist {pid}")
    else:
        old = playlist_item_ids(pid)
        add_items(pid, ordered)
        for item_id in old:
            api("DELETE", "playlistItems", {"id": item_id})
        api("PUT", "playlists", {"part": "snippet"},
            {"id": pid, "snippet": {"title": ROLLING_NAME, "description": desc}})
        print(f"Updated rolling playlist {pid} ({len(ordered)} tracks)")

    archive = create_playlist(f"{ROLLING_NAME} - {date}", desc, ordered)
    print(f"Created archive playlist {archive}")

    config["last_synced_date"] = date
    save_json(CONFIG, config)
    save_json(CACHE, cache)
    with open(UNMATCHED, "w") as f:
        f.write("\n".join(still_unmatched) + ("\n" if still_unmatched else ""))

    print(f"\nDone. matched={len(ordered)} non_music_fallback={len(still_unmatched)} skipped={len(skipped)}")


if __name__ == "__main__":
    main()
