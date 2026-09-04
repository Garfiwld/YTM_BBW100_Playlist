# Billboard Hot 100 → YouTube Music

Weekly job that mirrors the [Billboard Hot 100](https://www.billboard.com/charts/hot-100/)
into YouTube Music:

- **Rolling playlist** `Billboard Hot 100` — public, created once then topped up toward
  the current chart every run (matches are inserted live, so a partial run still leaves
  a usable playlist).
- **Monthly archive** `Billboard Hot 100 - YYYY-MM-DD` — public, one snapshot per calendar
  month, only on a run that finishes cleanly.

Chart data comes from [`mhollingshead/billboard-hot-100`](https://github.com/mhollingshead/billboard-hot-100)
(`recent.json`), not scraped. Each run saves that week's chart to
`charts/YYYY-MM-DD.json` (with the resolved `videoId` added to each entry once
matched) and commits it, so the repo keeps its own chart + match history.

`sync.py` talks to the **YouTube Data API v3** directly over OAuth — no `ytmusicapi`
at runtime (its InnerTube endpoints reject OAuth Bearer tokens with HTTP 400).
Runtime is Python stdlib only.

## How matching works

For each uncached song — **one** `search.list` call, `q="song artist"`, restricted
to the **Music category** (`videoCategoryId=10`) — take the top result:

1. Its title contains the song name, or fuzzy-matches it (ratio ≥ 0.8) → clean match.
2. Neither → still added to the playlist, but logged to `unmatched.txt` for a
   human to check and fix later.
3. No music result at all → skipped and logged; the playlist is just shorter that week.

`cache.json` remembers resolved `song|artist → videoId`; a cached song is **never
re-searched**, including weak matches in `unmatched.txt`. To force weak entries to
be looked up again, run `RETRY_WEAK=1 python3 sync.py` (spends search calls) or just
delete their lines from `unmatched.txt` and their keys from `cache.json`.

## Quota

YouTube Data API v3 has **two** daily caps: 10,000 units/day *and* a hard
**100 `search.list` calls/day**. The search cap is what bites — one call per
uncached song, so a cold first run gets through ~100 songs (≈ the whole chart).
On `quotaExceeded` the script saves all state (`config.json` without
`last_synced_date`) and exits; re-run the next day to resume — cached songs are
skipped, so it only spends search calls on ones still missing.

- **Weekly**: only the churn — insert chart entrants live, prune chart drops after
  a clean pass, plus searches for genuinely new titles ≈ 4,000 units.
- **Monthly**: the first clean sync of a new calendar month also snapshots an
  archive playlist (`last_archived_month` in `config.json` gates it) ≈ 5,000 units.
- **`FULL_REORDER=1 python3 sync.py`**: clears the rolling playlist and refills it
  in exact rank order (~10,000 units). Run by hand on a quiet day.
- **First run**: ~100 uncached songs = ~100 search calls, right at the daily cap.
  If it stops a little short, re-run the next day; the playlist fills in as it goes.

## One-time setup

### 1. Google OAuth client

1. [Google Cloud Console](https://console.cloud.google.com/) → project.
2. APIs & Services → Enable **YouTube Data API v3**.
3. OAuth consent screen → External → complete the Branding page → add the Google
   account that will own the playlists as a **Test user**.
4. Credentials → Create credentials → OAuth client ID → **TVs and Limited Input devices**.
5. Note the **Client ID** and **Client secret**. (If you ever delete this client,
   every token minted from it dies with `deleted_client` — make a new one and
   re-mint.)

### 2. Mint the token

```bash
pip install -r requirements.txt          # only needed for this step
cp .env.example .env                      # fill in client id/secret
ytmusicapi oauth --client-id "$(grep '^YTM_CLIENT_ID=' .env | cut -d= -f2)" \
                 --client-secret "$(grep '^YTM_CLIENT_SECRET=' .env | cut -d= -f2)"
```

Approve on the device-code page while logged into the playlist-owner account.
Produces `oauth.json` (has the `refresh_token` `sync.py` needs).

### 3. Check the token

```bash
python3 diag_oauth.py
```

Tests A (Data API playlists) and B (Data API search) must return HTTP 200. Test C
(InnerTube) is expected to 400 — `sync.py` does not use that path.

### 4. Bootstrap

```bash
python3 sync.py
```

Creates both playlists and writes `rolling_playlist_id` into `config.json`. Commit
`config.json`, `cache.json`, `unmatched.txt`.

### 5. GitHub Actions

Repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `YTM_OAUTH_JSON` | full contents of `oauth.json` |
| `YTM_CLIENT_ID` | OAuth client id |
| `YTM_CLIENT_SECRET` | OAuth client secret |

Runs every Wednesday 20:00 Asia/Bangkok (`0 13 * * 3` UTC) and on manual dispatch.
Commits the updated state files back. If `recent.json`'s `date` hasn't changed since
`last_synced_date`, the run is a no-op.

## Review dashboard

`index.html` is a static, dependency-free page: pick any chart week (1958→now),
see the full 100 in rank order with movement, match status, thumbnails, and
YT Music links. `weak only` filters to the picks that need a look.

Enable it: repo → Settings → Pages → Source **Deploy from a branch**, branch
`master` / folder `/ (root)` — must be root so the page can `fetch`
`charts/*.json` and `charts/index.json`. Then open
`https://garfiwld.github.io/YTM_BBW100_Playlist/`.

**Fixing a pick from the dashboard:** paste a fine-grained GitHub token
(*Contents: read+write* on this repo) into the ⚙ box — it stays in that browser's
localStorage. Edit the `videoId` field on any row, hit **Save to GitHub**: it
commits `cache.json`, `unmatched.txt`, and that week's `charts/…json` in one go.
Pages redeploys in ~a minute; the next sync uses the corrected id.

## Tests

```bash
python3 test_sync.py
```

## Backfilling history

`charts/` was seeded with every weekly chart back to 1958 via:

```bash
python3 backfill_charts.py            # all of it
python3 backfill_charts.py --since 2000
```

It downloads `all.json` and splits it; no API quota. Re-run any time to pick up
weeks added since (existing files are skipped).

## Later

- **Backfill playlists** — only the chart *JSON* is backfilled. Historical archive
  *playlists* (one per past week/month) are not built yet: ~5,000 quota units each,
  so it has to be paced across days.
