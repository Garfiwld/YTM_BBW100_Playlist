"""Self-check for the pure helpers. Run: python test_sync.py"""
import sync
from sync import norm, ratio

sync._token["value"] = "x"  # skip token refresh

assert norm("Sailor Song (Sped Up)") == "sailor song"
assert ratio("Die With A Smile", "Die With a Smile") > 0.95
assert ratio("APT.", "Some Unrelated Song") < 0.8

# well_matched: song name as substring of a real video title counts; junk doesn't
assert sync.well_matched("Billie Eilish - BIRDS OF A FEATHER (Official Audio)", "Birds of a Feather")
assert sync.well_matched("Die With a Smile", "Die With A Smile")
assert not sync.well_matched("totally unrelated reaction video", "Some Song")

# match(): keep both audio + mv ids; videoId picks audio by default, mv with PREFER_MV
sync.yt_search = lambda q: [
    {"id": {"videoId": "mv"}, "snippet": {"title": "Billie Eilish - BIRDS OF A FEATHER (Official Music Video)", "channelTitle": "BillieEilishVEVO"}},
    {"id": {"videoId": "aud"}, "snippet": {"title": "BIRDS OF A FEATHER", "channelTitle": "Billie Eilish - Topic"}},
]
entry, well = sync.match("Birds of a Feather", "Billie Eilish")
assert well and entry == {"videoId": "aud", "audio": "aud", "mv": "mv"}, entry

import os as _o
_o.environ["PREFER_MV"] = "1"
assert sync.match("Birds of a Feather", "Billie Eilish")[0] == {"videoId": "mv", "audio": "aud", "mv": "mv"}
del _o.environ["PREFER_MV"]

# no result -> skip
sync.yt_search = lambda q: []
assert sync.match("x", "y") == (None, False)

# only a weak result -> still returned, flagged weak, no audio/mv signal
sync.yt_search = lambda q: [{"id": {"videoId": "vW"}, "snippet": {"title": "totally different clip", "channelTitle": "rando"}}]
assert sync.match("Some Song", "Some Artist") == ({"videoId": "vW", "audio": None, "mv": None}, False)

# cache_entry back-compat: bare string still works
assert sync.cache_entry("abc") == {"videoId": "abc", "audio": None, "mv": None}

# write_matches_csv: flat table, sorted by artist then song
import tempfile as _tf, os as _os2
sync.MATCHES_CSV = _os2.path.join(_tf.mkdtemp(), "m.csv")
sync.write_matches_csv({
    "Song Z|Artist B": {"videoId": "z", "audio": "z", "mv": None},
    "Song A|Artist A": "a",
})
lines = open(sync.MATCHES_CSV).read().splitlines()
assert lines[0] == "song,artist,videoId,audio,mv"
assert lines[1] == "Song A,Artist A,a,," and lines[2] == "Song Z,Artist B,z,z,"

# write_matches_db: same rows queryable via SQL
import sqlite3 as _sq
sync.MATCHES_DB = _os2.path.join(_tf.mkdtemp(), "m.db")
sync.write_matches_db({"S|A": {"videoId": "v", "audio": "v", "mv": "w"}})
con = _sq.connect(sync.MATCHES_DB)
assert con.execute("SELECT song,artist,mv FROM matches").fetchall() == [("S", "A", "w")]
con.close()

sync.yt_search = lambda q: []
assert sync.match("x", "y") == (None, False)

# resolve(): a cached song must NOT trigger a search (the resume guarantee)
def boom(*a, **k):
    raise AssertionError("search called for a cached song")


sync.yt_search = boom
cache = {"Song A|Artist": "vidA", "Weak B|Artist": "vidB"}
prev_unmatched = {"Weak B|Artist"}

def rv(*a):
    e, w = sync.resolve(*a)
    return e["videoId"], w


assert rv("Song A|Artist", "Song A", "Artist", cache, prev_unmatched, False) == ("vidA", True)
assert rv("Weak B|Artist", "Weak B", "Artist", cache, prev_unmatched, False) == ("vidB", False)
assert rv("Song A|Artist", "Song A", "Artist", cache, prev_unmatched, True) == ("vidA", True)
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
