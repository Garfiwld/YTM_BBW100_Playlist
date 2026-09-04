# Billboard Hot 100 → YouTube Music

Weekly job that mirrors the [Billboard Hot 100](https://www.billboard.com/charts/hot-100/)
into YouTube Music:

- **Rolling playlist** `Billboard Hot 100` — public, diffed each week toward the current chart.
- **Monthly archive** `Billboard Hot 100 - YYYY-MM-DD` — public, one snapshot per calendar month.

Chart data comes from [`mhollingshead/billboard-hot-100`](https://github.com/mhollingshead/billboard-hot-100)
(`recent.json`), not scraped. Each run also saves that week's raw chart to
`charts/YYYY-MM-DD.json` and commits it, so the repo keeps its own chart history.

`sync.py` talks to the **YouTube Data API v3** directly over OAuth — no `ytmusicapi`
at runtime (its InnerTube endpoints reject OAuth Bearer tokens with HTTP 400).
Runtime is Python stdlib only.

## How matching works

For each of the 100 songs (at most **2** `search.list` calls — the daily cap is 100):

1. `search.list q="song artist"`; take the top hit whose title fuzzy-matches (ratio ≥ 0.6).
2. Retry once with parentheticals and featured artists stripped.
3. Still no fuzzy match → take that retry's top result anyway, log it to `unmatched.txt`.
4. Nothing at all → skipped and logged; the playlist is just shorter that week.

`cache.json` remembers resolved `song|artist → videoId`; a cached song is **never
re-searched**, including weak matches in `unmatched.txt`. To force weak entries to
be looked up again, run `RETRY_WEAK=1 python3 sync.py` (spends search calls) or just
delete their lines from `unmatched.txt` and their keys from `cache.json`.

## Quota

YouTube Data API v3 has **two** daily caps: 10,000 units/day *and* a hard
**100 `search.list` calls/day**. The search cap is what bites — an uncached song
costs 1–2 calls, so a cold first run only gets through ~60–90 songs per day.
On `quotaExceeded` the script saves `cache.json` / `unmatched.txt` / `config.json`
(without `last_synced_date`) and exits; re-run the next day to resume. To stay inside:

- **Weekly**: the rolling playlist is *diffed* — remove chart drops, insert new
  entries at their rank position, holdovers left where they are. Only the churn
  (~15 songs each way) plus searches for genuinely new titles ≈ 4,000 units.
- **Monthly**: the first sync of a new calendar month also snapshots an archive
  playlist (`last_archived_month` in `config.json` gates it) ≈ 5,000 units.
- **`FULL_REORDER=1 python3 sync.py`**: wipes and refills the rolling playlist so
  holdovers are re-sorted into exact rank order (~10,000 units). Run by hand on a
  day the scheduled job isn't also archiving.
- **First run** ≈ 15,000–20,000 units. If you hit `quotaExceeded`, just run it
  again the next day — it resumes from `cache.json`.

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

## Tests

```bash
python3 test_sync.py
```

## Later

- **Backfill** — `sync.py` only handles the current chart. Historical archive
  playlists (iterate `date/YYYY-MM-DD.json` in `mhollingshead/billboard-hot-100`,
  newest → oldest) are not built yet. ~5,000 quota units per archive, so any
  backfill has to be paced across days.
