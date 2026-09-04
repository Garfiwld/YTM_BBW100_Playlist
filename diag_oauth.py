#!/usr/bin/env python3
"""Diagnose the OAuth token WITHOUT ytmusicapi, using only urllib.

It isolates where the HTTP 400 comes from:

  Test A  official YouTube Data API v3  (playlists?mine=true)   -> should be 200
  Test B  official Data API v3 search                           -> should be 200
  Test C  InnerTube youtubei/v1/search  (what ytmusicapi uses)  -> the 400 suspect

If A+B pass and C fails, the token/scope/consent-screen are all fine and the
problem is purely InnerTube refusing an OAuth Bearer context.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"


def _load_dotenv(path=".env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


def load_oauth():
    with open(os.environ.get("YTM_OAUTH_FILE", "oauth.json")) as f:
        return json.load(f)


def post(url, data, headers, is_json):
    body = json.dumps(data).encode() if is_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def refresh(oauth):
    cid = os.environ.get("YTM_CLIENT_ID") or oauth.get("client_id")
    csecret = os.environ.get("YTM_CLIENT_SECRET") or oauth.get("client_secret")
    if not cid or not csecret:
        sys.exit("need YTM_CLIENT_ID / YTM_CLIENT_SECRET (env or .env) to refresh the token")
    status, text = post(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": csecret,
            "refresh_token": oauth["refresh_token"],
            "grant_type": "refresh_token",
        },
        {"Content-Type": "application/x-www-form-urlencoded"},
        is_json=False,
    )
    print(f"token refresh: HTTP {status}")
    if status != 200:
        print(text)
        sys.exit(1)
    tok = json.loads(text)
    print(f"  scope = {tok.get('scope')}")
    return tok["access_token"]


def show(label, status, text):
    print(f"\n=== {label}: HTTP {status} ===")
    print(text[:800])


def main():
    oauth = load_oauth()
    print("oauth.json keys:", sorted(oauth))
    at = refresh(oauth)
    bearer = {"Authorization": f"Bearer {at}"}

    s, t = get("https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=1", bearer)
    show("A  Data API v3  playlists?mine=true", s, t)

    q = urllib.parse.urlencode({"part": "snippet", "q": "Oasis Wonderwall", "type": "video", "maxResults": "1"})
    s, t = get(f"https://www.googleapis.com/youtube/v3/search?{q}", bearer)
    show("B  Data API v3  search", s, t)

    s, t = post(
        "https://music.youtube.com/youtubei/v1/search?prettyPrint=false",
        {"query": "Oasis Wonderwall",
         "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20240101.01.00"}}},
        {**bearer, "Content-Type": "application/json"},
        is_json=True,
    )
    show("C  InnerTube  youtubei/v1/search  (ytmusicapi path)", s, t)


if __name__ == "__main__":
    main()
