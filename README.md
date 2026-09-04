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

### 1. Google OAuth client

1. [Google Cloud Console](https://console.cloud.google.com/) → new project.
2. APIs & Services → Enable **YouTube Data API v3**.
3. OAuth consent screen → External → add the Google account that will own the
   playlists as a **Test user**.
4. Credentials → Create credentials → OAuth client ID → type **TVs and Limited Input devices**.
5. Note the **Client ID** and **Client secret**.

### 2. Generate the token

```bash
pip install -r requirements.txt
ytmusicapi oauth --client-id YOUR_ID --client-secret YOUR_SECRET
```

Follow the device-code prompt while logged into the account that should own the playlists.
Produces `oauth.json`.

### 3. Run once locally to bootstrap

```bash
cp .env.example .env      # then edit .env with your client id/secret
python sync.py            # sync.py auto-loads .env
```

First run creates the rolling playlist and writes its id into `config.json`. Commit
`config.json`, `cache.json`, `unmatched.txt`.

### 4. GitHub Actions

Repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `YTM_OAUTH_JSON` | full contents of `oauth.json` |
| `YTM_CLIENT_ID` | OAuth client id |
| `YTM_CLIENT_SECRET` | OAuth client secret |

The workflow runs every Wednesday 20:00 Asia/Bangkok (`0 13 * * 3` UTC) and on manual
dispatch. It commits the updated state files back to the repo. If the chart date in
`recent.json` hasn't changed since `last_synced_date`, the run is a no-op.

## Tests

```bash
python test_sync.py
```
