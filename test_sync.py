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

print("ok")
