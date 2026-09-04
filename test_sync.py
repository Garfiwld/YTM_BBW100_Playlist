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

import sync

sync._token["value"] = "x"  # skip token refresh

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
