#!/usr/bin/env python3
"""Extend the cached OSM history to a later end date with one request per tile.

For tiles already cached up to --since, fetch only /extraction/contributions.parquet for (since, end] and merge:
  * every non-DELETION contribution becomes a new feature version (geometry from the contribution),
    valid until the element's next contribution (or the far-future placeholder);
  * cached versions that were still open (valid_to = 2222) are closed at the element's first later contribution;
  * the new contributions are appended to the tile's contrib file (used for campaign hashtags).
Originals are backed up to cache/<layer>_bak_<since>/ before being rewritten. Tiles with no cached files are
skipped (fetch them with `ebola_osm_timelapse.py fetch`). Validate afterwards with validate_tiles.py.

usage: python update_increment.py --since 2026-11-15 --end 2026-11-21 [--layer buildings]
"""
import argparse, json, shutil, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow as pa, pyarrow.parquet as pq
import ebola_osm_timelapse as m

FCOLS = ["osm_type", "osm_id", "edit_timestamp", "valid_to_timestamp", "changeset_id", "geom", "bbox"]
CCOLS = ["osm_type", "osm_id", "edit_timestamp", "contribution_type", "changeset_id", "changeset_tags"]
FAR = pd.Timestamp("2222-01-01", tz="UTC")


def merge_tile(fp, cp, inc_bytes, bak):
    inc_path = bak / ("inc_" + cp.name)
    inc_path.write_bytes(inc_bytes)
    inc = pq.read_table(inc_path)
    if inc.num_rows == 0:
        return 0, 0
    for p in (fp, cp):
        if not (bak / p.name).exists():
            shutil.copy2(p, bak / p.name)
    ft = pq.read_table(bak / fp.name, columns=FCOLS)
    ct = pq.read_table(bak / cp.name, columns=CCOLS)
    ic = inc.select(list(dict.fromkeys(FCOLS[:2] + ["edit_timestamp", "contribution_type", "changeset_id", "changeset_tags", "geom", "bbox"])))
    c = ic.to_pandas()
    c["key"] = m.make_key(c)
    c = c.sort_values(["key", "edit_timestamp"]).reset_index(drop=True)
    # next contribution time per element (closes the version each contribution creates)
    c["next_ts"] = c.groupby("key")["edit_timestamp"].shift(-1)
    first_new = c.groupby("key")["edit_timestamp"].min()

    f = ft.to_pandas()
    f["key"] = m.make_key(f)
    open_ = f["valid_to_timestamp"] >= FAR - pd.Timedelta(days=1)
    hit = open_ & f["key"].isin(first_new.index)
    # close the open version at the first new contribution after it
    f.loc[hit, "valid_to_timestamp"] = f.loc[hit, "key"].map(first_new).to_numpy()
    bad = hit & (f["valid_to_timestamp"] <= f["edit_timestamp"])
    if bad.any():
        print(f"  warning: {int(bad.sum())} cached versions edited after --since; left as-is", file=sys.stderr)

    nv = c[c["contribution_type"] != "DELETION"].copy()
    nv["valid_to_timestamp"] = nv["next_ts"].fillna(FAR)
    nv = nv[FCOLS + ["key"]]
    merged = pd.concat([f, nv], ignore_index=True).drop_duplicates(["key", "edit_timestamp"], keep="last")
    merged = merged.drop(columns="key")
    out_f = pa.Table.from_pandas(merged, preserve_index=False).cast(ft.schema)
    pq.write_table(out_f, fp)

    new_c = pa.concat_tables([ct, ic.select(CCOLS).cast(ct.schema)])
    pq.write_table(new_c, cp)
    return len(nv), int((c["contribution_type"] == "DELETION").sum())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out"); p.add_argument("--layer", default="buildings")
    p.add_argument("--since", required=True); p.add_argument("--end", required=True)
    p.add_argument("--only", nargs="*", type=int, help="tile indices to process (default: all cached)")
    a = p.parse_args()
    import httpx
    key = m.get_key()
    cache = Path(a.out) / "cache" / a.layer
    bak = Path(a.out) / "cache" / f"{a.layer}_bak_{a.since}"
    bak.mkdir(parents=True, exist_ok=True)
    tiles = json.loads((cache / "tiles.json").read_text())
    done_file = bak / f"merged_to_{a.end}.json"
    done = set(json.loads(done_file.read_text())) if done_file.exists() else set()
    filt = m.LAYERS[a.layer]
    tot_v = tot_d = 0
    with httpx.Client(timeout=300) as cl:
        for i, t in enumerate(tiles):
            fp, cp = cache / f"features_{i:04d}.parquet", cache / f"contrib_{i:04d}.parquet"
            if i in done or not (fp.exists() and cp.exists()) or (a.only and i not in a.only):
                continue
            body = {"aoi": list(t), "filter": filt, "time": {"start": a.since, "end": a.end}}
            nv, nd = merge_tile(fp, cp, m.post(cl, key, "/extraction/contributions.parquet", body), bak)
            tot_v += nv; tot_d += nd
            done.add(i); done_file.write_text(json.dumps(sorted(done)))
            if nv or nd:
                print(f"tile {i}: +{nv} versions, {nd} deletions")
    print(f"merged {len(done)} tiles: {tot_v} new versions, {tot_d} deletions (backup: {bak})")


if __name__ == "__main__":
    main()
