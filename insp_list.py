"""List INSP SitRep PDF links from the category pages. Output JSON lines to argv[1]."""
import re, sys, json, httpx
out = sys.argv[1]
links = {}
with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 research-script"}) as c:
    for n in range(1, 40):
        url = "https://insp.cd/category/sitrep/" + (f"page/{n}/" if n > 1 else "")
        r = c.get(url)
        if r.status_code != 200:
            print("stop", n, r.status_code); break
        found = set(re.findall(r'https://insp\.cd/wp-content/uploads/[^"\'\s<>]+?\.pdf', r.text))
        posts = set(re.findall(r'https://insp\.cd/[a-z0-9\-]*sitrep[a-z0-9\-]*/', r.text, re.I))
        for f in found: links[f] = url
        for p in posts: links.setdefault(p, url)
        print(n, len(found), len(posts))
        if not found and not posts: break
json.dump(sorted(links), open(out, "w"), indent=0)
print(len(links))
