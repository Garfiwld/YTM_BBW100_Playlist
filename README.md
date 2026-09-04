# Billboard Hot 100 → YouTube Music

Weekly job that mirrors the [Billboard Hot 100](https://www.billboard.com/charts/hot-100/)
into YouTube Music:

- **Rolling playlist** `Billboard Hot 100` — public, wiped and refilled each week in chart order.
- **Weekly archive** `Billboard Hot 100 - YYYY-MM-DD` — public, one new playlist per chart week.

Chart data comes from [`mhollingshead/billboard-hot-100`](https://github.com/mhollingshead/billboard-hot-100)
(`recent.json`), not scraped.

## How matching works

For each of the 100 songs:

1. `search(filter="songs")`, take the top hit whose title fuzzy-matches (ratio ≥ 0.6).
2. Retry with parentheticals and featured artists stripped.
3. Fallback: `search(filter="videos")` — a plain YouTube video. Logged to `unmatched.txt`
   and re-tried as a proper song every following week.
4. Nothing at all → skipped and logged; the playlist is just shorter that week.

`cache.json` remembers resolved `song|artist → videoId` so repeat entries aren't re-searched.

## One-time setup

### 1. Browser auth

OAuth does not work: YouTube returns HTTP 400 on `search` for OAuth tokens
(ytmusicapi 1.12.x), so this uses browser auth.

```bash
pip install -r requirements.txt
ytmusicapi browser
```

Follow the prompt: open <https://music.youtube.com> logged into the account that
should own the playlists → DevTools → Network → copy the request headers of any
`/youtubei/v1/...` POST → paste. Produces `browser.json`.

Those cookies last for months, but die if you sign that Google session out
elsewhere — re-run `ytmusicapi browser` and refresh the secret if sync starts
failing with 401.

### 2. Run once locally to bootstrap

```bash
python sync.py
```

First run creates the rolling playlist and writes its id into `config.json`. Commit
`config.json`, `cache.json`, `unmatched.txt`.

### 3. GitHub Actions

Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|---|---|
| `YTM_BROWSER_JSON` | full contents of `browser.json` |

The workflow runs every Wednesday 20:00 Asia/Bangkok (`0 13 * * 3` UTC) and on manual
dispatch. It commits the updated state files back to the repo. If the chart date in
`recent.json` hasn't changed since `last_synced_date`, the run is a no-op.

## Tests

```bash
python test_sync.py
```
