#!/usr/bin/env python3
"""Create a synthetic cache (2 tiles) + zones file that mimics the ohsome v2 extraction schema,
so `build` can be exercised offline. Usage: python tests/make_synthetic_cache.py out_test"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd, pyarrow as pa, pyarrow.parquet as pq
import shapely, geopandas as gpd
from shapely.geometry import box

out = Path(sys.argv[1] if len(sys.argv) > 1 else "out_test")
cache = out / "cache" / "buildings"
cache.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(1)
FAR = pd.Timestamp("2222-01-01", tz="UTC")
tiles = [(30.0, 1.4, 30.25, 1.65), (30.25, 1.4, 30.5, 1.65)]
(cache / "tiles.json").write_text(json.dumps(tiles))

zones = gpd.GeoDataFrame({"zs_name": ["Bunia", "Rwampara"]},
                         geometry=[box(30.0, 1.4, 30.25, 1.65), box(30.25, 1.4, 30.5, 1.65)], crs=4326)
zones.to_file(out / "zones.geojson", driver="GeoJSON")

def make(tile, n_pre, n_new, campaign_ids, oid0):
    xmin, ymin, xmax, ymax = tile
    rows, crows = [], []
    for k in range(n_pre + n_new):
        oid = oid0 + k
        x, y = rng.uniform(xmin, xmax - .001), rng.uniform(ymin, ymax - .001)
        g = shapely.to_wkb(box(x, y, x + .0001, y + .0001))
        if k < n_pre:
            t0 = pd.Timestamp("2024-01-01", tz="UTC"); cs = 1
        else:
            t0 = pd.Timestamp("2026-05-22", tz="UTC") + pd.Timedelta(days=int(rng.integers(0, 150)))
            cs = int(rng.choice(campaign_ids + [2]))
        deleted = rng.random() < 0.05 and k >= n_pre
        t_del = t0 + pd.Timedelta(days=20)
        rows.append(dict(osm_type="way", osm_id=oid, edit_timestamp=t0, valid_to_timestamp=FAR,
                         changeset_id=cs, geom=g))
        crows.append(dict(osm_type="way", osm_id=oid, edit_timestamp=t0, contribution_type="CREATION",
                          changeset_id=cs, changeset_tags=[("hashtags", "#eboladrc2026" if cs in campaign_ids else "#other")]))
        if deleted:  # API quirk: deleted features keep the far-future valid_to
            crows.append(dict(osm_type="way", osm_id=oid, edit_timestamp=t_del, contribution_type="DELETION",
                              changeset_id=99, changeset_tags=[("comment", "validation")]))
    f = pd.DataFrame(rows)
    c = pd.DataFrame(crows)
    return f, c

for i, t in enumerate(tiles):
    f, c = make(t, 200 + 100 * i, 300 * (i + 1) ** 2, [10, 11], 1_000_000 * (i + 1))
    ts = pa.timestamp("us", tz="UTC")
    ft = pa.Table.from_pandas(f, preserve_index=False).cast(pa.schema([
        ("osm_type", pa.string()), ("osm_id", pa.int64()), ("edit_timestamp", ts),
        ("valid_to_timestamp", ts), ("changeset_id", pa.int64()), ("geom", pa.binary())]))
    pq.write_table(ft, cache / f"features_{i:04d}.parquet")
    ct = pa.table({
        "osm_type": c["osm_type"], "osm_id": c["osm_id"], "edit_timestamp": pa.array(c["edit_timestamp"], ts),
        "contribution_type": c["contribution_type"], "changeset_id": c["changeset_id"],
        "changeset_tags": pa.array(list(c["changeset_tags"]), pa.map_(pa.string(), pa.string()))})
    pq.write_table(ct, cache / f"contrib_{i:04d}.parquet")
print("synthetic cache written to", cache)
