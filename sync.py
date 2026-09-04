#!/usr/bin/env python3
"""Sync the Billboard Hot 100 into a rolling YouTube Music playlist + a weekly archive.

Data source: https://github.com/mhollingshead/billboard-hot-100 (recent.json)
Auth: OAuth 2.0 refresh-token flow against the YouTube Data API v3 (no ytmusicapi --
      its InnerTube endpoints reject OAuth Bearer tokens with HTTP 400).

Needs a Google OAuth client (type "TVs and Limited Input devices") and a token
minted with `ytmusicapi oauth` (or any device flow for scope
https://www.googleapis.com/auth/youtube).

The rolling playlist is created up front and every match is inserted into it
immediately, at its chart position -- so a run that dies partway still leaves a
real (partial) playlist, and the next run tops it up. Off-chart tracks are pruned
only after a full clean pass; the monthly archive snapshot likewise only fires on
a complete run. Holdovers are not reordered unless FULL_REORDER=1 (wipe + refill).

Matching: one search.list call per uncached song, restricted to the Music category
(videoCategoryId=10). Among the results, pick the best title match, tie-breaking
toward Audio (a "- Topic" channel / "Official Audio" title) rather than the Music
Video -- set PREFER_MV=1 to flip that. A pick whose title contains the song name
or clears FUZZY_THRESHOLD is clean; otherwise it's still added but logged to
unmatched.txt. No music result at all -> the song is skipped.

Quota: two separate daily caps -- 10k units/day AND a hard 100 search.list
calls/day. One call per uncached song, so a cold first run gets through ~100
songs/day. On QuotaExceeded the script saves all state (config.json minus
last_synced_date) and exits; re-run the next day to resume.

Env / .env:
  YTM_CLIENT_ID, YTM_CLIENT_SECRET   the OAuth client
  YTM_OAUTH_FILE                     token json (default ./oauth.json), needs refresh_token

State files (committed back by CI):
  charts/<date>.json -> the week's Billboard chart; each entry gets a "videoId"
                        added once matched (backfilled weeks have chart data only).
  PLAYLISTS.md   -> human-readable list of every playlist URL, regenerated each run.
  config.json    -> {"rolling_playlist_id": str, "last_synced_date": "YYYY-MM-DD",
                     "last_archived_month": "YYYY-MM",
                     "archives": {"YYYY-MM-DD": playlist_id}}
  cache.json     -> {"song|artist": {"videoId": chosen, "audio": id|null, "mv": id|null}}
                    the working match store, reused across weeks (a bare id string
                    is still accepted for back-compat)
  matches.csv    -> song,artist,videoId,audio,mv  -- the same data as a flat table,
                    regenerated from cache.json each run
  unmatched.txt  -> "DATE\tsong|artist\tvideoId"  weak matches (top hit's title
                    didn't fuzzy-match). Kept from cache like anything else;
                    RETRY_WEAK=1 re-searches them (costs search calls).
"""

import csv
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
FUZZY_THRESHOLD = 0.8

HERE = os.path.dirname(os.path.abspath(__file__))
CHARTS_DIR = os.path.join(HERE, "charts")
MATCHES_CSV = os.path.join(HERE, "matches.csv")
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


def ratio(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


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
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            if e.code == 429 or "quotaExceeded" in payload:
                raise QuotaExceeded(f"{method} {path}: {payload[:200]}")
            if e.code in (409, 500, 503) or (e.code == 403 and "SERVICE_UNAVAILABLE" in payload):
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
            sys.exit(f"{method} {path} -> HTTP {e.code}: {payload}")


def yt_search(query):
    # videoCategoryId 10 = Music -> only song/music results
    return api("GET", "search", {
        "part": "snippet", "type": "video", "videoCategoryId": "10",
        "regionCode": "US", "maxResults": "5", "q": query,
    }).get("items", [])


def cache_entry(v):
    """Normalise a cache value to {"videoId","audio","mv"}. Old caches stored a
    bare id string."""
    if isinstance(v, str):
        return {"videoId": v, "audio": None, "mv": None}
    return {"videoId": v.get("videoId"), "audio": v.get("audio"), "mv": v.get("mv")}


def resolve(key, song, artist, cache, prev_unmatched, retry_weak):
    """(entry, well_matched) where entry is {"videoId","audio","mv"}. Returns
    straight from cache without any network call unless the song is uncached (or
    it's a weak match and retry_weak is set)."""
    weak_cached = key in prev_unmatched
    if key in cache and not (weak_cached and retry_weak):
        return cache_entry(cache[key]), not weak_cached
    return match(song, artist)


def well_matched(title, song):
    """The result's title should contain the song name outright, or be a close
    fuzzy match. Video titles carry the artist + 'Official Audio' etc., so a plain
    ratio is too harsh -- the substring check catches normal official uploads."""
    nt, ns = norm(title), norm(song)
    return (ns and ns in nt) or ratio(title, song) >= FUZZY_THRESHOLD


_MV_RE = re.compile(r"official (music )?video|\bm/?v\b|music video", re.I)
_AUDIO_RE = re.compile(r"official audio|\baudio\b|lyric|visuali[sz]er", re.I)


def raw_kind(item):
    """> 0 => looks like Audio (a track), < 0 => looks like a Music Video."""
    sn = item.get("snippet", {})
    title, ch = sn.get("title", ""), sn.get("channelTitle", "")
    s = 0
    if ch.strip().lower().endswith("- topic"):
        s += 2
    if _AUDIO_RE.search(title):
        s += 1
    if _MV_RE.search(title):
        s -= 1
    return s


def match(song, artist):
    """One search.list call. Return ({"videoId","audio","mv"}, well_matched) --
    keeps both the best Audio and the best Music Video result when present.
    videoId is the chosen one: Audio by default, the MV if PREFER_MV=1. Weak picks
    (not well_matched) are still used but logged to unmatched.txt for review."""
    cands = [it for it in yt_search(f"{song} {artist}") if it.get("id", {}).get("videoId")]
    if not cands:
        return None, False

    def wm(it):
        return well_matched(it.get("snippet", {}).get("title", ""), song)

    pool = [it for it in cands if wm(it)] or cands
    audio = max((it for it in pool if raw_kind(it) > 0),
               key=lambda it: (raw_kind(it), -cands.index(it)), default=None)
    mv = max((it for it in pool if raw_kind(it) < 0),
             key=lambda it: (-raw_kind(it), -cands.index(it)), default=None)
    order = [mv, audio] if os.environ.get("PREFER_MV") else [audio, mv]
    chosen = next((x for x in order if x), pool[0])
    vid = lambda it: it["id"]["videoId"] if it else None
    return {"videoId": vid(chosen), "audio": vid(audio), "mv": vid(mv)}, wm(chosen)


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


def write_matches_csv(cache):
    """cache.json as a flat table: song,artist,videoId,audio,mv (sorted)."""
    rows = []
    for key, v in cache.items():
        song, _, artist = key.partition("|")
        e = cache_entry(v)
        rows.append((song, artist, e["videoId"] or "", e["audio"] or "", e["mv"] or ""))
    rows.sort(key=lambda r: (r[1].lower(), r[0].lower()))
    with open(MATCHES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["song", "artist", "videoId", "audio", "mv"])
        w.writerows(rows)


def write_charts_index():
    """charts/index.json -- sorted list of every chart date on disk, for the dashboard."""
    dates = sorted(f[:-5] for f in os.listdir(CHARTS_DIR) if f.endswith(".json") and f != "index.json")
    save_json(os.path.join(CHARTS_DIR, "index.json"), dates)


def playlist_url(pid):
    return f"https://music.youtube.com/playlist?list={pid}"


def write_playlists_md(config):
    lines = ["# Playlists", ""]
    rolling = config.get("rolling_playlist_id")
    if rolling:
        lines += [f"**Rolling — [{ROLLING_NAME}]({playlist_url(rolling)})**",
                  f"<br>updated through chart week {config.get('last_synced_date', '?')}", ""]
    archives = config.get("archives", {})
    if archives:
        lines += ["## Monthly archives", "", "| Chart week | Playlist |", "|---|---|"]
        for d in sorted(archives, reverse=True):
            lines.append(f"| {d} | [{ROLLING_NAME} - {d}]({playlist_url(archives[d])}) |")
        lines.append("")
    with open(os.path.join(HERE, "PLAYLISTS.md"), "w") as f:
        f.write("\n".join(lines))


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
        write_matches_csv(cache)
        with open(UNMATCHED, "w") as f:
            f.write("\n".join(still_unmatched) + ("\n" if still_unmatched else ""))
        weak = {ln.split("\t")[1] for ln in still_unmatched}
        for row in chart["data"]:
            k = f"{row['song']}|{row['artist']}"
            if cache.get(k):
                e = cache_entry(cache[k])
                row["videoId"] = e["videoId"]
                row["audio"] = e["audio"]
                row["mv"] = e["mv"]
            row["weak"] = k in weak
        save_json(os.path.join(CHARTS_DIR, f"{date}.json"), chart)
        write_charts_index()
        write_playlists_md(config)

    def quota_stop(where):
        persist()
        sys.exit(f"\nquota hit during {where}; playlist has {len(ordered)} tracks so far, "
                 f"state saved. Re-run tomorrow -- it resumes from cache.json.")

    retry_weak = bool(os.environ.get("RETRY_WEAK"))
    full_reorder = bool(os.environ.get("FULL_REORDER"))
    desc = f"Billboard Hot 100 - auto-updated weekly. Chart week: {date}"
    ordered, still_unmatched, skipped, seen = [], [], [], set()

    pid = config.get("rolling_playlist_id")
    if not pid:
        pid = api("POST", "playlists", {"part": "snippet,status"}, {
            "snippet": {"title": ROLLING_NAME, "description": desc},
            "status": {"privacyStatus": "public"},
        })["id"]
        config["rolling_playlist_id"] = pid
        persist()
        print(f"Created rolling playlist {pid}")

    if full_reorder:
        for item_id, _ in playlist_items(pid):
            api("DELETE", "playlistItems", {"id": item_id})
        existing = set()
        print("FULL_REORDER: cleared rolling playlist")
    else:
        existing = {vid for _, vid in playlist_items(pid) if vid}

    for i, e in enumerate(entries):
        song, artist = e["song"], e["artist"]
        key = f"{song}|{artist}"
        try:
            entry, well = resolve(key, song, artist, cache, prev_unmatched, retry_weak)
            vid = entry["videoId"] if entry else None
            if not vid:
                skipped.append(key)
                print(f"SKIP  #{e['this_week']:>3} {key}")
                continue
            cache[key] = entry
            if not well:
                still_unmatched.append(f"{date}\t{key}\t{vid}")
                print(f"WEAK  #{e['this_week']:>3} {key} -> {vid} (weak title match)")
            if vid in seen:
                continue
            seen.add(vid)
            ordered.append(vid)
            if vid not in existing:
                api("POST", "playlistItems", {"part": "snippet"}, {
                    "snippet": {"playlistId": pid, "position": i,
                                "resourceId": {"kind": "youtube#video", "videoId": vid}},
                })
                print(f"ADD   #{e['this_week']:>3} {key} -> {vid}")
        except QuotaExceeded:
            quota_stop("sync")
        except SystemExit:
            persist()  # keep the matches we already resolved before bailing
            raise

    if len(ordered) < 50:
        sys.exit(f"Only {len(ordered)} matches - aborting, looks broken.")

    # loop finished cleanly: drop tracks no longer on the chart, then archive
    try:
        keep = set(ordered)
        removed = 0
        for item_id, vid in playlist_items(pid):
            if vid not in keep:
                api("DELETE", "playlistItems", {"id": item_id})
                removed += 1
        api("PUT", "playlists", {"part": "snippet"},
            {"id": pid, "snippet": {"title": ROLLING_NAME, "description": desc}})
        print(f"Rolling playlist {pid}: {len(ordered)} tracks, -{removed} off-chart")

        month = date[:7]
        if config.get("last_archived_month") != month:
            archive = create_playlist(f"{ROLLING_NAME} - {date}", desc, ordered)
            config["last_archived_month"] = month
            config.setdefault("archives", {})[date] = archive
            print(f"Created archive playlist {archive}")
        else:
            print(f"Archive for {month} already done; skipping.")
    except QuotaExceeded:
        quota_stop("cleanup")

    config["last_synced_date"] = date
    persist()
    print(f"\nDone. matched={len(ordered)} weak={len(still_unmatched)} skipped={len(skipped)}")


if __name__ == "__main__":
    main()
