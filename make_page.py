#!/usr/bin/env python3
"""Assemble site/index.html (+ media) from out/buildings/web_summary.json and INSP case tables.

usage: python make_page.py [--out out] [--cases data/ituri_cases_by_zone.csv] [--site site]
"""
import argparse, json, shutil
from pathlib import Path
import pandas as pd

NAME_FIX = {"Gethy": "Gety", "Nia-Nia": "Nia Nia"}
SITREP0 = pd.Timestamp("2026-05-14")  # INSP SitRep N reports the situation on 14 May 2026 + N days

# WHO Disease Outbreak News: zone-level confirmed cases before the INSP series we parsed
WHO_DON = [
    ("2026-06-06", "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON606",
     {"Bunia": 142, "Rwampara": 98, "Mongbwalu": 92, "Nyankunde": 24}),
    ("2026-06-17", "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON608",
     {"Bunia": 247, "Rwampara": 195, "Mongbwalu": 189, "Nyankunde": 68}),
    ("2026-07-30", "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON614",
     {"Bunia": 880, "Rwampara": 627, "Mongbwalu": 541, "Nyankunde": 114}),
]


def load_cases(path, zones):
    c = pd.read_csv(path)
    c["zone"] = c["zone"].replace(NAME_FIX)
    c = c[c["zone"].isin(zones)]
    c["date"] = (SITREP0 + pd.to_timedelta(c["sitrep"], unit="D")).dt.strftime("%Y-%m-%d")
    c = c.drop_duplicates(["zone", "sitrep"])
    src = pd.read_csv(str(path).replace(".csv", "_sources.csv"))
    urls = dict(zip(src["sitrep"], src["url"]))
    out = {}
    for z, g in c.sort_values("sitrep").groupby("zone"):
        pts = [dict(d=r.date, c=int(r.cases), k=int(r.deaths), s=f"INSP SitRep N°{r.sitrep}", u=urls.get(r.sitrep))
               for r in g.itertuples()]
        out[z] = pts
    for d, u, vals in WHO_DON:
        for z, v in vals.items():
            out.setdefault(z, []).append(dict(d=d, c=v, k=None, s="WHO DON", u=u))
    for z in out:
        out[z].sort(key=lambda p: p["d"])
    latest = int(c["sitrep"].max())
    return out, latest, urls.get(latest)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="out"); p.add_argument("--layer", default="buildings")
    p.add_argument("--cases", default="data/ituri_cases_by_zone.csv"); p.add_argument("--site", default="site")
    p.add_argument("--meta", default="data/page_meta.json")
    a = p.parse_args()
    base = Path(a.out) / a.layer
    summ = json.loads((base / "web_summary.json").read_text())
    cases, latest_sr, latest_url = load_cases(a.cases, set(summ["zones"]))
    meta = json.loads(Path(a.meta).read_text(encoding="utf-8"))
    site = Path(a.site); (site / "media").mkdir(parents=True, exist_ok=True)
    for theme in ("", "_dark"):  # light and dark renderings (page swaps them with its mode toggle)
        for z in summ["picks"]:
            s = summ["zones"][z]["slug"] + theme
            for suf in (".mp4", "_first.jpg", "_last.jpg"):
                shutil.copy(base / "zones" / f"{s}{suf}", site / "media" / f"{s}{suf}")
        for suf in (".mp4", "_last.jpg"):
            shutil.copy(base / f"overview{theme}{suf}", site / "media" / f"overview{theme}{suf}")
    data = dict(summary=summ, cases=cases, latest_sitrep=latest_sr, latest_sitrep_url=latest_url, meta=meta)
    tpl = Path(__file__).with_name("page_template.html").read_text(encoding="utf-8")
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (site / "index.html").write_text(html, encoding="utf-8")
    print("wrote", site / "index.html", f"({len(html)/1e3:.0f} kB)")


if __name__ == "__main__":
    main()
