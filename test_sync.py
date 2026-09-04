"""Self-check for the pure helpers. Run: python test_sync.py"""
from sync import norm, primary_artist, ratio, pick

assert norm("Sailor Song (Sped Up)") == "sailor song"
assert primary_artist("Post Malone Featuring Doja Cat") == "Post Malone"
assert primary_artist("Lady Gaga & Bruno Mars") == "Lady Gaga"
assert ratio("Die With A Smile", "Die With a Smile") > 0.95
assert ratio("APT.", "Some Unrelated Song") < 0.6

items = [
    {"id": {"videoId": "a"}, "snippet": {"title": "Wrong Song"}},
    {"id": {"videoId": "b"}, "snippet": {"title": "Birds Of A Feather"}},
]
assert pick(items, "Birds of a Feather", check_title=True) == "b"
assert pick(items, "Birds of a Feather", check_title=False) == "a"
assert pick([{"id": {}, "snippet": {"title": "x"}}], "x", check_title=False) is None

# diff_rolling: remove drops + dupes, insert missing at rank position
import sync

calls = []
sync._token["value"] = "x"  # skip token refresh


def fake_api(method, path, params=None, body=None):
    calls.append((method, path, params, body))
    if method == "GET":  # current playlist: A, B, B(dup), OLD
        return {"items": [
            {"id": "i1", "snippet": {"resourceId": {"videoId": "A"}}},
            {"id": "i2", "snippet": {"resourceId": {"videoId": "B"}}},
            {"id": "i3", "snippet": {"resourceId": {"videoId": "B"}}},
            {"id": "i4", "snippet": {"resourceId": {"videoId": "OLD"}}},
        ]}
    return {}


sync.api = fake_api
added, removed = sync.diff_rolling("pl", ["NEW", "A", "B"])
assert (added, removed) == (1, 2), (added, removed)
deletes = {c[2]["id"] for c in calls if c[0] == "DELETE"}
assert deletes == {"i3", "i4"}, deletes
ins = [c for c in calls if c[0] == "POST"]
assert len(ins) == 1 and ins[0][3]["snippet"]["position"] == 0  # NEW at rank 0

# resolve(): a cached song must NOT trigger a search (this is the resume guarantee)
def boom(*a, **k):
    raise AssertionError("search called for a cached song")


sync.yt_search = boom
cache = {"Song A|Artist": "vidA", "Weak B|Artist": "vidB"}
prev_unmatched = {"Weak B|Artist"}

assert sync.resolve("Song A|Artist", "Song A", "Artist", cache, prev_unmatched, False) == ("vidA", True)
# weak match: still cache-only, still flagged as not-well-matched
assert sync.resolve("Weak B|Artist", "Weak B", "Artist", cache, prev_unmatched, False) == ("vidB", False)
# RETRY_WEAK on -> weak match falls through to search (boom); well-matched cached still safe
assert sync.resolve("Song A|Artist", "Song A", "Artist", cache, prev_unmatched, True) == ("vidA", True)
try:
    sync.resolve("Weak B|Artist", "Weak B", "Artist", cache, prev_unmatched, True)
    raise SystemExit("expected search for weak match under RETRY_WEAK")
except AssertionError:
    pass

# write_playlists_md: rolling link + archives newest-first
import tempfile, os as _os
sync.HERE = tempfile.mkdtemp()
sync.write_playlists_md({"rolling_playlist_id": "PLr", "last_synced_date": "2026-09-05",
                         "archives": {"2026-07-04": "PLa", "2026-09-05": "PLb"}})
md = open(_os.path.join(sync.HERE, "PLAYLISTS.md")).read()
assert "playlist?list=PLr" in md
assert md.index("PLb") < md.index("PLa"), "archives not newest-first"

print("ok")
