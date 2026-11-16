#!/usr/bin/env python3
"""Light-mode, map-style time-lapses on an OSM basemap, built on ebola_osm_timelapse.py's cache.

* Zone dossiers: for each zone with the most added buildings, a ~5.5 km window centred on the densest
  additions, drawing real building footprints (grey = mapped before the start date, light orange = added
  since, dark orange = added in that frame's week, blue outline = removed that week) over current OSM
  roads, water and place names (Overpass API).
* Overview: the whole study area with zone boundaries, main roads, rivers, towns; building density
  (grey) with additions since the start date (orange); boxes mark the dossier windows.

usage: python zone_timelapses.py --zones data/zones_build.gpkg --zone-col zonesante --end 2026-11-15 [--top 7]
Writes out/<layer>/zones/<slug>.gif|_last.png, out/<layer>/overview.gif|.mp4|_last.png, web_summary.json.
"""
import argparse, glob, json, math, re, time
from pathlib import Path
import numpy as np, pandas as pd

import ebola_osm_timelapse as m

# ----------------------------------------------------------------------------------------- style
THEMES = {
    "light": dict(fig="#ffffff", halo="#ffffff", land="#f3f1ec", water="#a9d1e0", river="#8fc2d6", road="#ffffff",
                  road_case="#b9b2a6", major="#fbe3a6", major_case="#c9a35a", track="#a8957a", bldg="#c7bdb3",
                  bldg_edge="#9c9086", added="#f4b27a", added_edge="#d98a45", new="#d4490f", new_edge="#9c3006",
                  gone="#2f6fb5", ink="#1f2624", muted="#5d6865", zone="#6e5a8a",
                  dens=("#dcd6cf", "#6f665d"), dens_add=("#f7c79c", "#c2410c"), suffix=""),
    "dark": dict(fig="#121517", halo="#121517", land="#1b2023", water="#1f3d52", river="#2d5f7a", road="#2c3337",
                 road_case="#252b2f", major="#6b5a33", major_case="#4a3f24", track="#4a443b", bldg="#8f8980",
                 bldg_edge="#aaa398", added="#b4652b", added_edge="#d9863f", new="#ff7a2e", new_edge="#ffc08f",
                 gone="#78b0ff", ink="#e7ecee", muted="#a3afb3", zone="#b9a3dc",
                 dens=("#3a3f42", "#c9c2b9"), dens_add=("#8a4318", "#ffa257"), suffix="_dark"),
}
PAL = dict(THEMES["light"])
FONTS = [Path(__file__).with_name("data") / "fonts" / f"Roboto-{w}.ttf" for w in ("Regular", "Bold")]
OVERPASS = "https://overpass-api.de/api/interpreter"
DPI = 192  # 2x screen density so frames stay sharp when zoomed / on high-DPI displays (set by --dpi)


def setup_mpl():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, rcParams
    for fp in FONTS:  # static instances cut from Google Fonts' Roboto variable font (see README)
        if fp.exists():
            font_manager.fontManager.addfont(str(fp))
            rcParams["font.family"] = "Roboto"
    import matplotlib.pyplot as plt
    return plt


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ----------------------------------------------------------------------------------------- basemap
def overpass(query, cache_file):
    import httpx
    cache_file = Path(cache_file)
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    for n in range(4):
        r = httpx.post(OVERPASS, data={"data": query}, timeout=400,
                       headers={"User-Agent": "osm-change-timelapse/0.2 (research script)"})
        if r.status_code == 200:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(r.text, encoding="utf-8")
            return r.json()
        print("  overpass", r.status_code, "retrying"); time.sleep(30 * (n + 1))
    raise RuntimeError("Overpass failed")


def basemap(bbox, cache_file, detail):
    """Return dict of shapely geometries + labels for bbox=(xmin,ymin,xmax,ymax)."""
    import shapely
    from shapely.geometry import LineString, Polygon
    from shapely.ops import polygonize
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    b = f"({s},{w},{n},{e})"
    if detail:
        q = f"""[out:json][timeout:300];
        (way["highway"]{b}; way["waterway"~"river|stream|canal"]{b}; way["natural"="water"]{b};
         relation["natural"="water"]{b}; node["place"~"town|city|village|suburb|neighbourhood|quarter"]{b};);
        out geom;"""
    else:
        q = f"""[out:json][timeout:400];
        (way["highway"~"^(trunk|primary|secondary|tertiary)$"]{b}; way["waterway"="river"]{b};
         relation["natural"="water"]["water"~"lake|reservoir"]{b}; way["natural"="water"]["water"="lake"]{b};
         node["place"~"town|city"]{b};);
        out geom;"""
    d = overpass(q, cache_file)
    roads, water, rivers, places = [], [], [], []
    for el in d["elements"]:
        t = el.get("tags", {})
        if el["type"] == "node":
            if "name" in t:
                places.append((el["lon"], el["lat"], t["name"], t.get("place")))
        elif el["type"] == "way" and "geometry" in el:
            xy = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if len(xy) < 2:
                continue
            if "highway" in t:
                roads.append((LineString(xy), t["highway"]))
            elif "waterway" in t:
                rivers.append((LineString(xy), t["waterway"]))
            elif t.get("natural") == "water" and len(xy) >= 4 and xy[0] == xy[-1]:
                water.append(Polygon(xy))
        elif el["type"] == "relation":
            lines = [LineString([(p["lon"], p["lat"]) for p in mb["geometry"]])
                     for mb in el.get("members", []) if mb.get("role") == "outer" and "geometry" in mb]
            water += list(polygonize(lines))
    return dict(roads=roads, water=water, rivers=rivers, places=places)


MAJOR = {"trunk", "primary", "secondary", "trunk_link", "primary_link", "secondary_link"}
MID = {"tertiary", "tertiary_link", "unclassified"}
TRACK = {"track", "path", "footway", "bridleway", "cycleway", "steps"}


def draw_basemap(ax, bm, detail):
    import shapely
    from matplotlib.collections import LineCollection, PatchCollection
    from matplotlib.patches import Polygon as MPoly

    def lc(geoms):
        segs = []
        for g in geoms:
            for part in getattr(g, "geoms", [g]):
                segs.append(np.asarray(part.coords))
        return segs
    for g in bm["water"]:
        for part in getattr(g, "geoms", [g]):
            ax.add_patch(MPoly(np.asarray(part.exterior.coords), closed=True, fc=PAL["water"], ec="none", zorder=1))
    rv = [g for g, k in bm["rivers"] if k == "river"]; st = [g for g, k in bm["rivers"] if k != "river"]
    ax.add_collection(LineCollection(lc(st), colors=PAL["river"], linewidths=0.6, zorder=1))
    ax.add_collection(LineCollection(lc(rv), colors=PAL["river"], linewidths=1.8 if detail else 1.2, zorder=1))
    tr = [g for g, k in bm["roads"] if k in TRACK]
    mn = [g for g, k in bm["roads"] if k not in TRACK | MAJOR | MID]
    md = [g for g, k in bm["roads"] if k in MID]
    mj = [g for g, k in bm["roads"] if k in MAJOR]
    if detail:
        ax.add_collection(LineCollection(lc(tr), colors=PAL["track"], linewidths=0.5, linestyles=(0, (2, 1.5)), zorder=2))
        ax.add_collection(LineCollection(lc(mn), colors=PAL["road_case"], linewidths=2.2, zorder=2))
        ax.add_collection(LineCollection(lc(mn), colors=PAL["road"], linewidths=1.5, zorder=2))
        ax.add_collection(LineCollection(lc(md), colors=PAL["road_case"], linewidths=3.0, zorder=2))
        ax.add_collection(LineCollection(lc(md), colors=PAL["road"], linewidths=2.2, zorder=2))
        ax.add_collection(LineCollection(lc(mj), colors=PAL["major_case"], linewidths=4.2, zorder=2))
        ax.add_collection(LineCollection(lc(mj), colors=PAL["major"], linewidths=3.2, zorder=2))
    else:
        ax.add_collection(LineCollection(lc(md), colors=PAL["road_case"], linewidths=0.8, zorder=2))
        ax.add_collection(LineCollection(lc(mj), colors=PAL["major_case"], linewidths=1.6, zorder=2))
        ax.add_collection(LineCollection(lc(mj), colors=PAL["major"], linewidths=1.0, zorder=2))


def draw_labels(ax, bm, extent, detail):
    import matplotlib.patheffects as pe
    xmin, xmax, ymin, ymax = extent
    rank = {"city": 0, "town": 1, "suburb": 2, "village": 3, "quarter": 4, "neighbourhood": 5}
    pl = sorted([p for p in bm["places"] if xmin < p[0] < xmax and ymin < p[1] < ymax],
                key=lambda p: rank.get(p[3], 9))
    used = []
    dx, dy = (xmax - xmin) * 0.09, (ymax - ymin) * 0.035
    for x, y, name, kind in pl[: 22 if detail else 30]:
        if any(abs(x - u) < dx and abs(y - v) < dy for u, v in used):
            continue
        used.append((x, y))
        big = kind in ("city", "town")
        ax.text(x, y, name, fontsize=(11 if big else 8.5) if detail else (9 if big else 7.5),
                color=PAL["ink"] if big else PAL["muted"], ha="center", va="center", zorder=8,
                weight="bold" if big else "normal",
                path_effects=[pe.withStroke(linewidth=2.6, foreground=PAL["halo"])])


def scalebar(ax, extent, km):
    xmin, xmax, ymin, ymax = extent
    lat = (ymin + ymax) / 2
    deg = km / (111.32 * math.cos(math.radians(lat)))
    x0, y0 = xmin + (xmax - xmin) * 0.04, ymin + (ymax - ymin) * 0.04
    ax.plot([x0, x0 + deg], [y0, y0], color=PAL["ink"], lw=2.5, zorder=9, solid_capstyle="butt")
    ax.text(x0 + deg / 2, y0 + (ymax - ymin) * 0.012, f"{km:g} km", ha="center", va="bottom", fontsize=8.5,
            color=PAL["ink"], zorder=9)


# ----------------------------------------------------------------------------------------- footprints
def window_geoms(cache, tiles, window):
    """All versions intersecting window with geometry -> DataFrame(key, edit_timestamp, valid_to, geom)."""
    import pyarrow.parquet as pq, shapely
    from shapely.geometry import box
    wb = box(*window)
    fr = []
    for i, t in enumerate(tiles):
        if not box(*t).intersects(wb):
            continue
        fp = cache / f"features_{i:04d}.parquet"
        if not fp.exists():
            continue
        f = pq.read_table(fp, columns=["osm_type", "osm_id", "edit_timestamp", "valid_to_timestamp", "geom", "bbox"]).to_pandas()
        bb = pd.DataFrame(list(f.pop("bbox")))
        keep = ((bb.xmax >= window[0]) & (bb.xmin <= window[2]) & (bb.ymax >= window[1]) & (bb.ymin <= window[3])).to_numpy()
        fr.append(f[keep])
    f = pd.concat(fr, ignore_index=True)
    f["edit_timestamp"] = m.to_naive_ns(f["edit_timestamp"])
    f["valid_to"] = m.to_naive_ns(f.pop("valid_to_timestamp"))
    f["key"] = m.make_key(f)
    f = f.drop_duplicates(["key", "edit_timestamp"]).reset_index(drop=True)
    f["geom"] = shapely.from_wkb(f["geom"].to_numpy())
    return f


def paths_for(geoms):
    from matplotlib.path import Path as MPath
    out = []
    for g in geoms:
        parts = getattr(g, "geoms", [g])
        verts, codes = [], []
        for p in parts:
            ring = np.asarray(p.exterior.coords)
            verts.append(ring); codes += [MPath.MOVETO] + [MPath.LINETO] * (len(ring) - 2) + [MPath.CLOSEPOLY]
        out.append(MPath(np.vstack(verts), codes))
    return out


def densest_window(feats, zone, start, size):
    sub = feats[(feats["zone"] == zone) & (feats["edit_timestamp"] > pd.Timestamp(start))]
    if sub.empty:
        sub = feats[feats["zone"] == zone]
    c = 0.005
    x0, y0 = sub["x"].min(), sub["y"].min()
    ix = ((sub["x"] - x0) / c).astype(int); iy = ((sub["y"] - y0) / c).astype(int)
    H = np.zeros((iy.max() + 1, ix.max() + 1)); np.add.at(H, (iy, ix), 1)
    k = int(round(size / c))
    from numpy.lib.stride_tricks import sliding_window_view
    if H.shape[0] >= k and H.shape[1] >= k:
        S = sliding_window_view(H, (k, k)).sum(axis=(2, 3))
        j, i = np.unravel_index(S.argmax(), S.shape)
        cx, cy = x0 + (i + k / 2) * c, y0 + (j + k / 2) * c
    else:
        cx, cy = sub["x"].median(), sub["y"].median()
    hw = size / 2
    return (round(cx - hw, 5), round(cy - hw, 5), round(cx + hw, 5), round(cy + hw, 5))


def render_window(plt, f, bm, zone, zgeom, window, times, outdir, fps):
    from matplotlib.collections import PathCollection
    import imageio.v3 as iio
    import geopandas as gpd
    paths = paths_for(f["geom"])
    keys = f["key"].to_numpy()
    t_start = times[0]
    base_keys = set(keys[((f["edit_timestamp"] <= t_start) & (f["valid_to"] > t_start)).to_numpy()])
    is_base = np.isin(keys, list(base_keys))
    extent = (window[0], window[2], window[1], window[3])
    imgs = []
    prev_alive = None
    for T in times:
        alive = ((f["edit_timestamp"] <= T) & (f["valid_to"] > T)).to_numpy()
        if prev_alive is None:
            new = np.zeros_like(alive); gone = np.zeros_like(alive)
        else:
            prev_keys = keys[prev_alive]; cur_keys = keys[alive]
            new = alive & ~np.isin(keys, prev_keys)
            gone = prev_alive & ~np.isin(keys, cur_keys)
        fig = plt.figure(figsize=(7.6, 8.4), dpi=DPI, facecolor=PAL["fig"])
        ax = fig.add_axes([0.0, 0.085, 1.0, 0.835]); ax.set_facecolor(PAL["land"])
        draw_basemap(ax, bm, True)
        for part in getattr(zgeom, "geoms", [zgeom]):
            xy = np.asarray(part.exterior.coords)
            ax.plot(xy[:, 0], xy[:, 1], color=PAL["zone"], lw=1.4, ls=(0, (6, 3)), zorder=3)
        sel = lambda mk: [paths[i] for i in np.flatnonzero(mk)]
        ax.add_collection(PathCollection(sel(alive & is_base), fc=PAL["bldg"], ec=PAL["bldg_edge"], lw=0.25, zorder=4))
        ax.add_collection(PathCollection(sel(alive & ~is_base & ~new), fc=PAL["added"], ec=PAL["added_edge"], lw=0.25, zorder=5))
        ax.add_collection(PathCollection(sel(new), fc=PAL["new"], ec=PAL["new_edge"], lw=0.3, zorder=6))
        ax.add_collection(PathCollection(sel(gone), fc="none", ec=PAL["gone"], lw=0.7, zorder=7))
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3]); ax.set_aspect("equal"); ax.axis("off")
        draw_labels(ax, bm, extent, True)
        scalebar(ax, extent, 1)
        n_alive = int(alive.sum()); n_added = int((alive & ~is_base).sum())
        fig.text(0.025, 0.975, zone, fontsize=19, weight="bold", color=PAL["ink"], va="top")
        fig.text(0.975, 0.975, f"{T:%d %b %Y}", fontsize=17, color=PAL["ink"], va="top", ha="right")
        fig.text(0.025, 0.935, f"{n_alive:,} buildings in OSM in this view  ·  {n_added:,} added since {t_start:%d %b}",
                 fontsize=10.5, color=PAL["muted"], va="top")
        lx = 0.025
        for col, ec, lab in [(PAL["bldg"], PAL["bldg_edge"], f"before {t_start:%d %b}"),
                             (PAL["added"], PAL["added_edge"], "added since"),
                             (PAL["new"], PAL["new_edge"], f"added this week ({int(new.sum()):,})"),
                             ("none", PAL["gone"], f"removed this week ({int(gone.sum()):,})")]:
            fig.patches.append(plt.Rectangle((lx, 0.035), 0.022, 0.02, transform=fig.transFigure, fc=col, ec=ec, lw=1))
            t = fig.text(lx + 0.03, 0.045, lab, fontsize=9.5, color=PAL["ink"], va="center")
            fig.canvas.draw(); bb = t.get_window_extent().transformed(fig.transFigure.inverted())
            lx = bb.x1 + 0.025
        fig.text(0.975, 0.008, "© OpenStreetMap contributors · roads/water: current OSM", fontsize=7.5,
                 color=PAL["muted"], ha="right", va="bottom")
        fig.canvas.draw()
        imgs.append(np.asarray(fig.canvas.buffer_rgba())[..., :3].copy())
        plt.close(fig)
        prev_alive = alive
    return save_anim(imgs, outdir, slug(zone) + PAL["suffix"], fps)


def save_anim(imgs, outdir, name, fps, gif=False):
    """MP4 (H.264, small) + JPEG first/last posters; optional GIF (large for dense footprints)."""
    import imageio.v3 as iio, imageio
    outdir.mkdir(parents=True, exist_ok=True)
    H = max(i.shape[0] for i in imgs); W = max(i.shape[1] for i in imgs)
    H += H % 2; W += W % 2  # yuv420p needs even dimensions
    imgs = [np.pad(i, ((0, H - i.shape[0]), (0, W - i.shape[1]), (0, 0)), mode="edge") for i in imgs]
    # encode at 15 fps output (each map frame repeated) so all browsers/players handle the low frame rate
    rep = max(1, int(round(15 / fps)))
    seq = [im for im in imgs for _ in range(rep)] + [imgs[-1]] * 45
    imageio.mimsave(outdir / f"{name}.mp4", seq, fps=15, macro_block_size=2, pixelformat="yuv420p",
                    ffmpeg_params=["-crf", "20", "-preset", "slow", "-tune", "stillimage"])
    iio.imwrite(outdir / f"{name}_last.jpg", imgs[-1], quality=90)
    iio.imwrite(outdir / f"{name}_first.jpg", imgs[0], quality=90)
    if gif:
        durs = [int(1000 / fps)] * (len(imgs) - 1) + [2500]
        iio.imwrite(outdir / f"{name}.gif", np.stack(imgs), duration=durs, loop=0)
    return name


def render_overview(plt, feats, zones, bm, windows, times, outdir, fps, cell=0.0025):
    from matplotlib.colors import LinearSegmentedColormap, LogNorm
    import matplotlib.patheffects as pe
    xmin, ymin, xmax, ymax = zones.total_bounds
    pad = 0.02; xmin, ymin, xmax, ymax = xmin - pad, ymin - pad, xmax + pad, ymax + pad
    nx, ny = int(math.ceil((xmax - xmin) / cell)), int(math.ceil((ymax - ymin) / cell))
    rng = [[xmin, xmin + nx * cell], [ymin, ymin + ny * cell]]
    x, y = feats["x"].to_numpy(), feats["y"].to_numpy()
    hist = lambda mk: np.histogram2d(x[mk], y[mk], bins=[nx, ny], range=rng)[0].T
    t0 = times[0]
    base = ((feats["edit_timestamp"] <= t0) & (feats["valid_to"] > t0)).to_numpy()
    keys = feats["key"].to_numpy()
    is_base = np.isin(keys, keys[base])
    greys = LinearSegmentedColormap.from_list("g", list(PAL["dens"]))
    oranges = LinearSegmentedColormap.from_list("o", list(PAL["dens_add"]))
    extent = [rng[0][0], rng[0][1], rng[1][0], rng[1][1]]
    vmax = max(2.0, hist(((feats["edit_timestamp"] <= times[-1]) & (feats["valid_to"] > times[-1])).to_numpy()).max())
    imgs = []
    for T in times:
        alive = ((feats["edit_timestamp"] <= T) & (feats["valid_to"] > T)).to_numpy()
        fig = plt.figure(figsize=(9.6, 9.6 * (ymax - ymin) / (xmax - xmin) + 1.1), dpi=DPI, facecolor=PAL["fig"])
        top = 1 - 0.75 / fig.get_figheight(); bot = 0.45 / fig.get_figheight()
        ax = fig.add_axes([0, bot, 1, top - bot]); ax.set_facecolor(PAL["land"])
        draw_basemap(ax, bm, False)
        ax.imshow(np.ma.masked_less(hist(alive & is_base), 1), extent=extent, origin="lower", cmap=greys,
                  norm=LogNorm(1, vmax), interpolation="nearest", zorder=3, alpha=0.9)
        ad = np.ma.masked_less(hist(alive & ~is_base), 1)
        if ad.count():
            ax.imshow(ad, extent=extent, origin="lower", cmap=oranges, norm=LogNorm(1, max(2, vmax / 3)),
                      interpolation="nearest", zorder=4)
        zones.boundary.plot(ax=ax, color=PAL["zone"], lw=0.7, zorder=5)
        for z, r in zones.iterrows():
            c = r.geometry.representative_point()
            ax.text(c.x, c.y, r["zone"], fontsize=7.5, color=PAL["zone"], ha="center", va="center", zorder=6,
                    style="italic", path_effects=[pe.withStroke(linewidth=2, foreground=PAL["halo"])])
        for zname, w in windows.items():
            ax.add_patch(plt.Rectangle((w[0], w[1]), w[2] - w[0], w[3] - w[1], fill=False, ec=PAL["ink"], lw=1.1, zorder=7))
        draw_labels(ax, bm, (extent[0], extent[1], extent[2], extent[3]), False)
        scalebar(ax, (extent[0], extent[1], extent[2], extent[3]), 25)
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3]); ax.set_aspect("equal"); ax.axis("off")
        n_add = int((alive & ~is_base).sum())
        fig.text(0.02, 1 - 0.18 / fig.get_figheight(), "Buildings in OpenStreetMap, outbreak area of Ituri", fontsize=16,
                 weight="bold", color=PAL["ink"], va="top")
        fig.text(0.98, 1 - 0.18 / fig.get_figheight(), f"{T:%d %b %Y}", fontsize=16, color=PAL["ink"], va="top", ha="right")
        fig.text(0.02, 1 - 0.5 / fig.get_figheight(),
                 f"{int(alive.sum()):,} buildings  ·  {n_add:,} added since {t0:%d %b} (orange)  ·  boxes = zoomed views below",
                 fontsize=10.5, color=PAL["muted"], va="top")
        fig.text(0.98, 0.1 / fig.get_figheight(), f"density per ~{cell * 111000:.0f} m cell · © OpenStreetMap contributors", fontsize=7.5,
                 color=PAL["muted"], ha="right", va="bottom")
        fig.canvas.draw()
        imgs.append(np.asarray(fig.canvas.buffer_rgba())[..., :3].copy())
        plt.close(fig)
    return save_anim(imgs, outdir, "overview" + PAL["suffix"], fps)


# ----------------------------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out"); p.add_argument("--layer", default="buildings")
    p.add_argument("--zones", required=True); p.add_argument("--zone-col")
    p.add_argument("--start", default="2026-04-01"); p.add_argument("--end", required=True)
    p.add_argument("--step-days", type=int, default=7); p.add_argument("--top", type=int, default=7)
    p.add_argument("--window-deg", type=float, default=0.05); p.add_argument("--fps", type=float, default=1.5)
    p.add_argument("--campaign", default="eboladrc2026")
    p.add_argument("--only", nargs="*", help="render only these zones (debug)")
    p.add_argument("--skip-overview", action="store_true")
    p.add_argument("--dpi", type=int, default=DPI)
    p.add_argument("--themes", nargs="+", default=["light", "dark"], choices=list(THEMES),
                   help="render one MP4/JPEG set per theme (dark files get a _dark suffix)")
    a = p.parse_args()
    globals()["DPI"] = a.dpi
    plt = setup_mpl()

    cache = Path(a.out) / "cache" / a.layer
    tiles = json.loads((cache / "tiles.json").read_text())
    feats, contribs = m.load_cache(cache)
    zones = m.load_zones(a.zones, a.zone_col)
    feats["zone"] = m.assign_zones(feats, zones)
    feats["camp"] = m.campaign_flags(feats, contribs, a.campaign).to_numpy()
    times = m.frame_times(a.start, a.end, a.step_days)
    df = m.zone_series(feats, times)
    df = df[df["zone"] != "outside"]
    base = Path(a.out) / a.layer
    gain = pd.read_csv(base / "zone_gain.csv", index_col=0)
    gain = gain[gain.index != "outside"]
    picks = list(gain.sort_values("gross_new", ascending=False).head(a.top).index)
    zg = zones.set_index("zone").geometry
    windows, info = {}, {}
    bmdir = Path("data") / "basemap"
    for z in picks:
        w = densest_window(feats, z, a.start, a.window_deg)
        windows[z] = w
        if a.only and z not in a.only:
            continue
        print("window", z, w)
        bm = basemap(w, bmdir / f"win_{slug(z)}.json", detail=True)
        f = window_geoms(cache, tiles, w)
        for th in a.themes:
            PAL.clear(); PAL.update(THEMES[th])
            render_window(plt, f, bm, z, zg[z], w, times, base / "zones", a.fps)
        # stats inside the window, for captions
        t0, t1 = times[0], times[-1]
        a0 = int(((f.edit_timestamp <= t0) & (f.valid_to > t0)).sum()); a1 = int(((f.edit_timestamp <= t1) & (f.valid_to > t1)).sum())
        near = sorted(bm["places"], key=lambda p: ({"city": 0, "town": 1, "suburb": 2, "village": 3}.get(p[3], 9),
                      (p[0] - (w[0] + w[2]) / 2) ** 2 + (p[1] - (w[1] + w[3]) / 2) ** 2))
        info[z] = dict(window=w, win_start=a0, win_end=a1, near=near[0][2] if near else None)
    if not a.skip_overview:
        bb = tuple(zones.total_bounds)
        bm = basemap(bb, bmdir / "overview.json", detail=False)
        for th in a.themes:
            PAL.clear(); PAL.update(THEMES[th])
            render_overview(plt, feats, zones, bm, windows, times, base, a.fps)
    area = zones.to_crs(32635).set_index("zone").area / 1e6
    prev = {}
    sp = base / "web_summary.json"
    if a.only and sp.exists():
        prev = json.loads(sp.read_text()).get("windows", {})
    out = {
        "start": a.start, "end": a.end, "step_days": a.step_days,
        "zones": {z: {"slug": slug(z), "area_km2": round(float(area[z]), 1),
                      **{k: (None if pd.isna(v) else (float(v) if isinstance(v, float) else int(v)))
                         for k, v in gain.loc[z].items()}}
                  for z in gain.index},
        "series": {z: g[["date", "alive", "new", "new_campaign", "removed"]].astype({"date": str}).to_dict("list")
                   for z, g in df.groupby("zone")},
        "total": df.groupby("date")[["alive", "new", "new_campaign", "removed"]].sum().reset_index()
                   .astype({"date": str}).to_dict("list"),
        "picks": picks, "windows": {**prev, **info},
    }
    sp.write_text(json.dumps(out))
    print("wrote", sp)


if __name__ == "__main__":
    main()
