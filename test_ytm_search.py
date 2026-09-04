"""Pure-function checks for ytm_search (no browser). Run: python test_ytm_search.py"""
from ytm_search import find_all, runs_text, hits_from_response

nested = {"a": {"videoId": "x"}, "b": [{"videoId": "y"}, {"c": {"videoId": "z"}}]}
assert sorted(find_all(nested, "videoId")) == ["x", "y", "z"]

assert runs_text({"text": {"runs": [{"text": "Foo"}, {"text": " Bar"}]}}) == "Foo Bar"


def col(*texts):
    return {"x": {"text": {"runs": [{"text": t} for t in texts]}}}


items = [
    {"musicResponsiveListItemRenderer": {
        "playlistItemData": {"videoId": "ABC123"},
        "flexColumns": [col("Birds Of A Feather"), col("Billie Eilish", " • Song")],
    }},
    {"musicResponsiveListItemRenderer": {
        "navigationEndpoint": {"watchEndpoint": {"videoId": "DEF456"}},
        "flexColumns": [col("Live version")],
    }},
    {"musicResponsiveListItemRenderer": {  # duplicate videoId -> dropped
        "playlistItemData": {"videoId": "ABC123"}, "flexColumns": []}},
]
# bury it the way the real payload nests it
resp = {"contents": {"tabbedSearchResultsRenderer": {"tabs": [
    {"tabRenderer": {"content": {"sectionListRenderer": {"contents": [
        {"musicShelfRenderer": {"contents": items}}]}}}}]}}}

assert hits_from_response(resp) == [
    ("ABC123", "Birds Of A Feather", "Billie Eilish • Song"),
    ("DEF456", "Live version", ""),
], hits_from_response(resp)

print("ok")
