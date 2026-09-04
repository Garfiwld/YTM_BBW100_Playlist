"""Self-check for the pure helpers. Run: python test_sync.py"""
from sync import norm, primary_artist, ratio, first_id

assert norm("Sailor Song (Sped Up)") == "sailor song"
assert primary_artist("Post Malone Featuring Doja Cat") == "Post Malone"
assert primary_artist("Lady Gaga & Bruno Mars") == "Lady Gaga"
assert ratio("Die With A Smile", "Die With a Smile") > 0.95
assert ratio("APT.", "Some Unrelated Song") < 0.6

res = [{"title": "Wrong", "videoId": "a"}, {"title": "Birds Of A Feather", "videoId": "b"}]
assert first_id(res, "Birds of a Feather", check_title=True) == "b"
assert first_id(res, "Birds of a Feather", check_title=False) == "a"
assert first_id([{"title": "x"}], "x", check_title=False) is None  # no videoId

print("ok")
