#!/usr/bin/env python3
"""
OSM change time-lapse for Ebola-affected areas of DRC (ohsome API v2).

Subcommands
  inspect-zones  print columns of a health-zone/area file so you can pick --zone-col
  fetch          download OSM history (features + contributions) per tile into a cache
  validate       compare reconstructed snapshot counts with /stats/features/count
  build          reconstruct weekly snapshots, write per-zone CSVs, render frames + GIF/MP4

Requires env var OHSOME_API_KEY for fetch/validate (free key: https://account.heigit.org/signup).
The ohsome API v1 is scheduled to shut down on 30 Nov 2026; this script targets v2 only.
"""
import argparse, glob, json, math, os, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

API = os.environ.get("OHSOME_API_URL", "https://api.heigit.org/ohsome-api/v2").rstrip("/")
LAYERS = {
    "buildings": "building=* and geometry:polygon",
    "residential": "landuse=residential and geometry:polygon",
    "places": "place=*",
}
TYPE_CODE = {"node": 1, "way": 2, "relation": 3}
PLACEHOLDER = pd.Timestamp("2200-01-01")  # API uses 2222-01-01 for "still current or deleted"
ZONE_NAME_CANDIDATES = ["zone_name", "zs_name", "hz_name", "zone_sante", "name", "admin2name", "nom", "zs"]


# --------------------------------------------------------------------------- helpers
def load_zones(path, zone_col=None):
    import geopandas as gpd
    z = gpd.read_file(path).to_crs(4326)
    if zone_col is None:
        lower = {c.lower(): c for c in z.columns}
        zone_col = next((lower[c] for c in ZONE_NAME_CANDIDATES if c in lower), None)
        if zone_col is None:
            zone_col = next((c for c in z.columns if z[c].dtype == object), None)
    if zone_col is None:
        z["zone"] = [f"zone_{i}" for i in range(len(z))]
    else:
        z["zone"] = z[zone_col].astype(str)
    return z[["zone", "geometry"]]


def tile_grid(bounds, step):
    xmin, ymin, xmax, ymax = bounds
    xs = np.arange(xmin, xmax, step)
    ys = np.arange(ymin, ymax, step)
    return [(round(float(x), 6), round(float(y), 6), round(float(min(x + step, xmax)), 6),
             round(float(min(y + step, ymax)), 6)) for y in ys for x in xs]


def post(client, key, path, body, retries=8):
    for n in range(retries):
        r = client.post(f"{API}{path}", json=body, headers={"Authorization": key})
        if r.status_code == 200:
            return r.content
        # 500 "problem proxying the request" is transient on the v2 gateway; retry it too
        if r.status_code in (429, 500, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", 15 * (n + 1)))
            print(f"  HTTP {r.status_code}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {r.status_code} on {path}: {r.text[:500]}")
    raise RuntimeError(f"Retries exhausted on {path}. If this was a 504, use a smaller --tile-deg "
                       "(extraction requests time out after 4 min).")


def get_key():
    key = os.environ.get("OHSOME_API_KEY")
    if not key:
        sys.exit("Set OHSOME_API_KEY (free key at https://account.heigit.org/signup).")
    return key


def frame_times(start, end, step_days):
    t = list(pd.date_range(start, end, freq=f"{step_days}D"))
    if t[-1] != pd.Timestamp(end):
        t.append(pd.Timestamp(end))
    return t


def to_naive_ns(s):
    return pd.to_datetime(s, utc=True).dt.tz_convert(None).astype("datetime64[ns]")


def make_key(df):
    return df["osm_id"].astype("int64") * 4 + df["osm_type"].astype(str).map(TYPE_CODE).astype("int64")


# --------------------------------------------------------------------------- loading
def read_tile(ffile, cfile):
    """Read one tile's features + contributions; returns (feats, contribs) with centroid x,y (no geometry)."""
    import pyarrow.parquet as pq
    import shapely
    f = pq.read_table(ffile, columns=["osm_type", "osm_id", "edit_timestamp", "valid_to_timestamp",
                                      "changeset_id", "geom"]).to_pandas()
    geoms = shapely.from_wkb(f["geom"].to_numpy())
    cen = shapely.centroid(geoms)
    f["x"], f["y"] = shapely.get_x(cen), shapely.get_y(cen)
    f = f.drop(columns=["geom"])
    f["edit_timestamp"] = to_naive_ns(f["edit_timestamp"])
    f["valid_to"] = to_naive_ns(f.pop("valid_to_timestamp"))
    f["key"] = make_key(f)

    c = pq.read_table(cfile, columns=["osm_type", "osm_id", "edit_timestamp", "contribution_type",
                                      "changeset_id", "changeset_tags"]).to_pandas()
    c["edit_timestamp"] = to_naive_ns(c["edit_timestamp"])
    c["key"] = make_key(c)
    c["tags_txt"] = c["changeset_tags"].map(lambda t: str(t).lower())
    c = c.drop(columns=["changeset_tags"])
    return f, c


def adjust_deletions(feats, contribs):
    """The API marks both 'still current' and 'deleted' versions with a far-future valid_to.
    Use DELETION contributions to close the last version of deleted elements."""
    dele = (contribs.loc[contribs["contribution_type"] == "DELETION", ["key", "edit_timestamp"]]
            .rename(columns={"edit_timestamp": "del_ts"}).sort_values("del_ts"))
    if dele.empty:
        return feats
    cur = feats.loc[feats["valid_to"] >= PLACEHOLDER, ["key", "edit_timestamp"]].copy()
    cur["_i"] = cur.index
    cur = cur.sort_values("edit_timestamp")
    m = pd.merge_asof(cur, dele, left_on="edit_timestamp", right_on="del_ts", by="key",
                      direction="forward", allow_exact_matches=False)
    hit = m.dropna(subset=["del_ts"])
    feats.loc[hit["_i"].to_numpy(), "valid_to"] = hit["del_ts"].to_numpy()
    return feats


def load_cache(cache_dir, tile_ids=None):
    ffiles = sorted(glob.glob(str(Path(cache_dir) / "features_*.parquet")))
    if tile_ids is not None:
        ffiles = [p for p in ffiles if int(Path(p).stem.split("_")[1]) in tile_ids]
    if not ffiles:
        sys.exit(f"No cached tiles in {cache_dir}. Run `fetch` first.")
    fs, cs = [], []
    for fp in ffiles:
        cp = fp.replace("features_", "contrib_")
        f, c = read_tile(fp, cp)
        fs.append(f)
        cs.append(c)
    feats = pd.concat(fs, ignore_index=True).drop_duplicates(["key", "edit_timestamp"]).reset_index(drop=True)
    contribs = pd.concat(cs, ignore_index=True).drop_duplicates(["key", "edit_timestamp", "contribution_type"])
    return adjust_deletions(feats, contribs), contribs


def campaign_flags(feats, contribs, campaign):
    """True for elements whose first version was created in a changeset mentioning the campaign hashtag."""
    if not campaign:
        return pd.Series(False, index=feats.index)
    tag = campaign.lower().lstrip("#")
    cs = contribs.drop_duplicates("changeset_id")
    ids = set(cs.loc[cs["tags_txt"].str.contains(tag, regex=False), "changeset_id"])
    first = feats.sort_values("edit_timestamp").groupby("key").head(1)
    fk = dict(zip(first["key"], first["changeset_id"].isin(ids)))
    return feats["key"].map(fk).fillna(False).astype(bool)


def assign_zones(feats, zones):
    import geopandas as gpd
    last = feats.sort_values("edit_timestamp").groupby("key").tail(1)
    pts = gpd.GeoDataFrame({"key": last["key"].to_numpy()},
                           geometry=gpd.points_from_xy(last["x"], last["y"]), crs=4326)
    j = gpd.sjoin(pts, zones, how="left", predicate="within").drop_duplicates("key")
    mapping = dict(zip(j["key"], j["zone"].fillna("outside")))
    return feats["key"].map(mapping).fillna("outside")


# --------------------------------------------------------------------------- frames
def iter_frames(feats, times):
    prev_mask, prev_keys = None, None
    for T in times:
        alive = ((feats["edit_timestamp"] <= T) & (feats["valid_to"] > T)).to_numpy()
        keys = feats["key"].to_numpy()[alive]
        if prev_keys is None:
            new = np.zeros(len(feats), bool)
            gone = np.zeros(len(feats), bool)
        else:
            new = alive & ~np.isin(feats["key"].to_numpy(), prev_keys)
            gone = prev_mask & ~np.isin(feats["key"].to_numpy(), keys)
        yield T, alive, new, gone
        prev_mask, prev_keys = alive, keys


def zone_series(feats, times):
    zc, labels = pd.factorize(feats["zone"])
    camp = feats["camp"].to_numpy()
    rows = []
    for T, alive, new, gone in iter_frames(feats, times):
        n = len(labels)
        a = np.bincount(zc[alive], minlength=n)
        nw = np.bincount(zc[new], minlength=n)
        nc = np.bincount(zc[new & camp], minlength=n)
        gn = np.bincount(zc[gone], minlength=n)
        for i, z in enumerate(labels):
            rows.append(dict(date=T.date(), zone=z, alive=int(a[i]), new=int(nw[i]),
                             new_campaign=int(nc[i]), removed=int(gn[i])))
    return pd.DataFrame(rows)


def render(feats, zones, times, outdir, cell_deg, step_days, title, fps):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm
    import imageio.v3 as iio

    outdir = Path(outdir)
    (outdir / "frames").mkdir(parents=True, exist_ok=True)
    if zones is not None:
        xmin, ymin, xmax, ymax = zones.total_bounds
    else:
        xmin, xmax = feats["x"].min(), feats["x"].max()
        ymin, ymax = feats["y"].min(), feats["y"].max()
    nx, ny = int(math.ceil((xmax - xmin) / cell_deg)), int(math.ceil((ymax - ymin) / cell_deg))
    rng = [[xmin, xmin + nx * cell_deg], [ymin, ymin + ny * cell_deg]]
    x, y = feats["x"].to_numpy(), feats["y"].to_numpy()

    def hist(mask):
        h, _, _ = np.histogram2d(x[mask], y[mask], bins=[nx, ny], range=rng)
        return h.T

    frames = list(iter_frames(feats, times))
    vmax = max(1.0, hist(frames[-1][1]).max())
    extent = [rng[0][0], rng[0][1], rng[1][0], rng[1][1]]
    paths = []
    for i, (T, alive, new, gone) in enumerate(frames):
        fig, ax = plt.subplots(figsize=(12.8, 8), dpi=100, facecolor="#0b0f14")
        ax.set_facecolor("#0b0f14")
        h = np.ma.masked_less(hist(alive), 1)
        ax.imshow(h, extent=extent, origin="lower", cmap="cividis",
                  norm=LogNorm(vmin=1, vmax=vmax), interpolation="nearest")
        hn = np.ma.masked_less(hist(new), 1)
        ax.imshow(hn, extent=extent, origin="lower", cmap="autumn_r",
                  norm=LogNorm(vmin=1, vmax=max(2, hn.max() if hn.count() else 2)),
                  interpolation="nearest", alpha=0.95)
        if zones is not None:
            zones.boundary.plot(ax=ax, linewidth=0.4, color="#8a94a0")
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
        ax.set_aspect("equal"); ax.axis("off")
        camp_new = int((new & feats["camp"].to_numpy()).sum())
        ax.set_title(f"{title}\n{T:%d %b %Y}", color="white", fontsize=15, loc="left")
        fig.subplots_adjust(bottom=0.16)
        fig.text(0.02, 0.02,
                f"features in OSM: {int(alive.sum()):,}\nadded since previous frame ({step_days} d): {int(new.sum()):,}"
                f" (campaign hashtag: {camp_new:,})\nremoved: {int(gone.sum()):,}\n"
                f"grey-blue = existing density per {cell_deg*111:.1f} km cell | yellow-to-red = new in interval",
                color="white", fontsize=10, va="bottom")
        p = outdir / "frames" / f"frame_{i:03d}.png"
        fig.savefig(p, facecolor=fig.get_facecolor())
        plt.close(fig)
        paths.append(p)
    imgs = [iio.imread(p) for p in paths]
    iio.imwrite(outdir / "timelapse.gif", np.stack(imgs), duration=int(1000 / fps), loop=0)
    try:
        import imageio
        imageio.mimsave(outdir / "timelapse.mp4", imgs, fps=fps, macro_block_size=1)
    except Exception as e:  # ffmpeg optional
        print(f"MP4 skipped ({e}); `pip install imageio-ffmpeg` to enable.", file=sys.stderr)
    return paths


def zone_gain_outputs(df, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    first, last = df["date"].min(), df["date"].max()
    a0 = df[df["date"] == first].set_index("zone")["alive"]
    a1 = df[df["date"] == last].set_index("zone")["alive"]
    gain = pd.DataFrame({"alive_start": a0, "alive_end": a1}).fillna(0).astype(int)
    gain["net_gain"] = gain["alive_end"] - gain["alive_start"]
    gain["growth_pct"] = (100 * gain["net_gain"] / gain["alive_start"].replace(0, np.nan)).round(1)
    gain["gross_new"] = df.groupby("zone")["new"].sum()
    gain["new_campaign"] = df.groupby("zone")["new_campaign"].sum()
    gain["removed"] = df.groupby("zone")["removed"].sum()
    gain = gain.sort_values("net_gain", ascending=False)
    gain.to_csv(Path(outdir) / "zone_gain.csv")
    top = gain.head(20)[::-1]
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.barh(top.index, top["net_gain"], color="#d95f02")
    ax.set_xlabel(f"Net change in mapped features, {first} to {last}")
    ax.set_title("Largest net OSM increases by zone")
    fig.tight_layout()
    fig.savefig(Path(outdir) / "zone_gain.png", dpi=150)
    plt.close(fig)
    return gain


# --------------------------------------------------------------------------- commands
def cmd_inspect(a):
    import geopandas as gpd
    z = gpd.read_file(a.zones)
    print(z.drop(columns="geometry").head(10).to_string())
    print("\nCRS:", z.crs, "| n =", len(z), "| bounds:", z.total_bounds)


def cmd_fetch(a):
    import httpx
    key = get_key()
    zones = load_zones(a.zones, a.zone_col) if a.zones else None
    bounds = tuple(a.bbox) if a.bbox else (tuple(zones.total_bounds) if zones is not None else None)
    if bounds is None:
        sys.exit("Provide --zones FILE or --bbox xmin ymin xmax ymax.")
    tiles = tile_grid(bounds, a.tile_deg)
    if zones is not None:
        from shapely.geometry import box
        union = zones.union_all() if hasattr(zones, "union_all") else zones.unary_union
        tiles = [t for t in tiles if box(*t).intersects(union)]
    cache = Path(a.out) / "cache" / a.layer
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "tiles.json").write_text(json.dumps(tiles))
    filt = a.filter or LAYERS[a.layer]
    tr = {"start": a.start, "end": a.end}
    print(f"{len(tiles)} tiles | filter: {filt} | {a.start} -> {a.end}")
    with httpx.Client(timeout=300) as c:
        for i, t in enumerate(tiles):
            fp, cp = cache / f"features_{i:04d}.parquet", cache / f"contrib_{i:04d}.parquet"
            if fp.exists() and cp.exists():
                continue
            print(f"tile {i + 1}/{len(tiles)} {t}")
            body = {"aoi": list(t), "filter": filt, "time": tr}
            fp.write_bytes(post(c, key, "/extraction/features.parquet", {**body, "clip": False}))
            cp.write_bytes(post(c, key, "/extraction/contributions.parquet", body))
    print("done ->", cache)


def cmd_validate(a):
    import httpx
    key = get_key()
    cache = Path(a.out) / "cache" / a.layer
    tiles = json.loads((cache / "tiles.json").read_text())
    filt = a.filter or LAYERS[a.layer]
    times = frame_times(a.start, a.end, a.step_days)
    worst = 0.0
    with httpx.Client(timeout=200) as c:
        for i in range(min(a.n_tiles, len(tiles))):
            feats, contribs = load_cache(cache, tile_ids={i})
            ours = [int(((feats["edit_timestamp"] <= T) & (feats["valid_to"] > T)).sum()) for T in times]
            body = {"aoi": list(tiles[i]), "filter": filt,
                    "time": {"start": a.start, "end": a.end, "interval": f"P{a.step_days}D"}}
            res = json.loads(post(c, key, "/stats/features/count.json", body))["result"]
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


def cmd_build(a):
    cache = Path(a.out) / "cache" / a.layer
    feats, contribs = load_cache(cache)
    zones = load_zones(a.zones, a.zone_col) if a.zones else None
    feats["zone"] = assign_zones(feats, zones) if zones is not None else "all"
    feats["camp"] = campaign_flags(feats, contribs, a.campaign).to_numpy()
    times = frame_times(a.start, a.end, a.step_days)
    outdir = Path(a.out) / a.layer
    outdir.mkdir(parents=True, exist_ok=True)
    df = zone_series(feats, times)
    df.to_csv(outdir / "zone_timeseries.csv", index=False)
    gain = zone_gain_outputs(df, outdir)
    print(gain.head(15).to_string())
    if not a.no_render:
        render(feats, zones, times, outdir, a.cell_deg, a.step_days,
               a.title or f"OSM {a.layer} - Ebola-affected Ituri", a.fps)
    print("outputs ->", outdir)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--out", default="out")
        sp.add_argument("--layer", default="buildings", choices=list(LAYERS))
        sp.add_argument("--filter", help="override the ohsome filter for the layer")
        sp.add_argument("--start", default="2026-04-01")
        sp.add_argument("--end", default=str(pd.Timestamp.now("UTC").date()))
        sp.add_argument("--step-days", type=int, default=7)
        sp.add_argument("--zones", help="health zone/area polygons (GeoJSON/GPKG/SHP), e.g. GRID3 Ituri")
        sp.add_argument("--zone-col")

    s = sub.add_parser("inspect-zones"); s.add_argument("zones"); s.set_defaults(f=cmd_inspect)
    s = sub.add_parser("fetch"); common(s)
    s.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    s.add_argument("--tile-deg", type=float, default=0.25)
    s.set_defaults(f=cmd_fetch)
    s = sub.add_parser("validate"); common(s); s.add_argument("--n-tiles", type=int, default=3)
    s.set_defaults(f=cmd_validate)
    s = sub.add_parser("build"); common(s)
    s.add_argument("--campaign", default="eboladrc2026")
    s.add_argument("--cell-deg", type=float, default=0.005, help="density cell size (0.005 deg ~ 550 m)")
    s.add_argument("--fps", type=int, default=2)
    s.add_argument("--title")
    s.add_argument("--no-render", action="store_true")
    s.set_defaults(f=cmd_build)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
