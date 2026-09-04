#!/usr/bin/env python3
"""One-off: fill charts/ with every historical Billboard Hot 100.

Downloads all.json (~44 MB, 3553 weekly charts back to 1958) from
mhollingshead/billboard-hot-100 and splits it into charts/<date>.json, the same
per-week shape sync.py writes. No API, no quota. Skips weeks already on disk.

    python3 backfill_charts.py [--since YYYY] [--force]
"""

import argparse
import json
import os
import urllib.request

from sync import CHARTS_DIR, save_json

ALL_URL = "https://raw.githubusercontent.com/mhollingshead/billboard-hot-100/main/all.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0, help="only years >= this")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()

    print(f"downloading {ALL_URL} ...")
    with urllib.request.urlopen(ALL_URL) as r:
        charts = json.load(r)
    print(f"{len(charts)} charts")

    os.makedirs(CHARTS_DIR, exist_ok=True)
    written = skipped = 0
    for chart in charts:
        date = chart["date"]
        if int(date[:4]) < args.since:
            continue
        path = os.path.join(CHARTS_DIR, f"{date}.json")
        if os.path.exists(path) and not args.force:
            skipped += 1
            continue
        save_json(path, chart)
        written += 1

    print(f"wrote {written}, skipped {skipped} existing")


if __name__ == "__main__":
    main()
