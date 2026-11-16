#!/usr/bin/env python3
"""Validate specific cached tiles (chosen by lon,lat points) against /stats/features/count.
Same logic as `ebola_osm_timelapse.py validate`, but lets you pick high-churn tiles instead of the first N.

usage: python validate_tiles.py --points 30.25,1.57 30.03,1.95 ... [--start 2026-04-01 --end 2026-11-15 --step-days 7]
Needs OHSOME_API_KEY. One stats request per tile.
"""
import argparse, json
from pathlib import Path
import pandas as pd
import ebola_osm_timelapse as m


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out"); p.add_argument("--layer", default="buildings")
    p.add_argument("--points", nargs="+", required=True)
    p.add_argument("--start", default="2026-04-01"); p.add_argument("--end", required=True)
    p.add_argument("--step-days", type=int, default=7)
    a = p.parse_args()
    import httpx
    key = m.get_key()
    cache = Path(a.out) / "cache" / a.layer
    tiles = json.loads((cache / "tiles.json").read_text())
    ids = []
    for pt in a.points:
        x, y = map(float, pt.split(","))
        ids += [i for i, t in enumerate(tiles) if t[0] <= x < t[2] and t[1] <= y < t[3] and i not in ids]
    times = m.frame_times(a.start, a.end, a.step_days)
    worst = 0.0
    with httpx.Client(timeout=200) as c:
        for i in ids:
            feats, _ = m.load_cache(cache, tile_ids={i})
            ours = [int(((feats["edit_timestamp"] <= T) & (feats["valid_to"] > T)).sum()) for T in times]
            body = {"aoi": list(tiles[i]), "filter": m.LAYERS[a.layer],
                    "time": {"start": a.start, "end": a.end, "interval": f"P{a.step_days}D"}}
            res = json.loads(m.post(c, key, "/stats/features/count.json", body))["result"]
            ref = dict(zip(pd.to_datetime(res["timestamp"]).tz_localize(None).date, res["value"]))
            print(f"\ntile {i} {tiles[i]}")
            print(f"{'date':12}{'reconstructed':>15}{'ohsome stats':>15}{'diff %':>9}")
            for T, n in zip(times, ours):
                r = ref.get(T.date())
                if r is None:
                    continue
                d = 100 * (n - r) / r if r else (0 if n == 0 else 100)
                worst = max(worst, abs(d))
                print(f"{T.date()!s:12}{n:>15,}{r:>15,}{d:>9.2f}")
    print(f"\nworst deviation: {worst:.2f}%  ->", "OK" if worst < 2 else "INVESTIGATE (see CLAUDE.md)")


if __name__ == "__main__":
    main()
