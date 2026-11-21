"""Download a sample of INSP SitReps and parse cumulative confirmed cases/deaths per Ituri health zone.
usage: insp_parse.py links.json pdf_dir out.csv"""
import re, sys, json, os, unicodedata, httpx, pdfplumber, pandas as pd
links, pdfdir, outcsv = sys.argv[1:4]
os.makedirs(pdfdir, exist_ok=True)
posts = [u for u in json.load(open(links)) if re.search(r"sitrep-(mve-)?n-?0*(\d+)", u)]
def num(u): return int(re.search(r"sitrep-(?:mve-)?n-?0*(\d+)", u).group(1))
bynum = {}
for u in posts: bynum.setdefault(num(u), u)
want = sorted(n for n in bynum if n % 7 == 0 or n == max(bynum) or n in (21, 59, 60, 85, 86))
H = {"User-Agent": "Mozilla/5.0 research-script"}

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())

ROW = re.compile(r"^([A-Za-zÀ-ÿ'\- ]+?)\s+(\d[\d ]*?)\s+(\d[\d ]*?)\s+([\d,]+\s?%|NA)")
rows, meta = [], []
with httpx.Client(timeout=120, follow_redirects=True, headers=H) as c:
    for n in want:
        pdf = os.path.join(pdfdir, f"sitrep_{n:03d}.pdf")
        if not os.path.exists(pdf):
            page = c.get(bynum[n]).text
            m = re.findall(r'https://insp\.cd/wp-content/uploads/[^"\'\s<>]+?\.pdf', page)
            if not m:
                import base64
                for b in re.findall(r'pdfemb-data=([A-Za-z0-9_\-+/=]+)', page):
                    try:
                        j = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
                        m.append(j["url"])
                    except Exception:
                        pass
            if not m:
                print(n, "no pdf link"); continue
            open(pdf, "wb").write(c.get(m[0]).content)
        try:
            with pdfplumber.open(pdf) as p:
                text = "\n".join((pg.extract_text() or "") for pg in p.pages[:6])
        except Exception as e:
            print(n, "pdf error", e); continue
        d = re.search(r"Date de rapportage\s*:?\s*(\d{1,2})\s+(\w+)\s+(\d{4})", text)
        dm = re.search(r"SitRep\s*N\S*?(\d+)\S*?/(\d{2})/(\d{2})/(\d{4})", text)
        date = f"{dm.group(4)}-{dm.group(3)}-{dm.group(2)}" if dm else None
        # Ituri block: from the 'Ituri <cases> <deaths>' line in the zone table to the next province line
        lines = text.splitlines()
        start = next((i for i, l in enumerate(lines[:-1]) if re.match(r"^Ituri\s+\d", l)
                      and not re.match(r"^(Nord|Haut|Tshopo|Sud|Bas)", lines[i + 1])
                      and ROW.match(lines[i + 1].strip())), None)
        if start is None:
            print(n, date, "Ituri table not found"); meta.append((n, date, bynum[n], 0)); continue
        k = 0
        for l in lines[start + 1:]:
            if re.match(r"^(Nord-Kivu|Haut-U|Tshopo|Total|Bas|Sud)", l): break
            m = ROW.match(l.strip())
            if m:
                rows.append(dict(sitrep=n, date=date, zone=m.group(1).strip(),
                                 cases=int(m.group(2).replace(" ", "")), deaths=int(m.group(3).replace(" ", ""))))
                k += 1
        meta.append((n, date, bynum[n], k))
        print(n, date, k, "zones")
df = pd.DataFrame(rows)
df["zone_key"] = df["zone"].map(norm)
df.to_csv(outcsv, index=False)
pd.DataFrame(meta, columns=["sitrep", "date", "url", "n_zones"]).to_csv(outcsv.replace(".csv", "_sources.csv"), index=False)
print(df.pivot_table(index="zone", columns="date", values="cases").to_string())
