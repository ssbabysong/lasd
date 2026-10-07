"""Download a Wikimedia Commons photo for every place in index.html into images/<key>.jpg.

Order per place: Wikipedia lead image (only if it lives on Commons) -> Commons search -> neighbourhood
article image (marked approx). Photos you add yourself (images/<key>.jpg not listed in credits.json)
are never touched. Writes images/credits.json with author/license for attribution.
"""
import html, io, json, os, re, sys, time, urllib.parse, urllib.request

from PIL import Image, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "images")
UA = "lasd-trip-page/1.0 (https://github.com/ssbabysong/lasd; personal travel page)"
WIDTH = 1280  # a standard Wikimedia thumbnail step
FORCE = os.environ.get("FORCE") == "1"


def get(url, raw=False):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data if raw else json.loads(data)
        except Exception as e:  # retry on transient errors / rate limits
            print("  retry", attempt + 1, url[:90], e, file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return None


def api(host, **params):
    params.update(format="json", formatversion="2")
    return get(f"https://{host}/w/api.php?" + urllib.parse.urlencode(params)) or {}


def parse_places():
    src = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    block = src[src.index("const P={"):]
    block = block[: block.index("\n};")]
    places = {}
    for m in re.finditer(r"^\s*(\w+):\{(.*)\},?\s*$", block, re.M):
        body = m.group(2)
        f = lambda k: (re.search(k + r":'([^']+)'", body) or [None, None])[1]
        places[m.group(1)] = {"w": f("w"), "cs": f("cs"), "zone": f("zone"), "n": f("n"),
                              "ax": bool(re.search(r"\bax:1\b", body))}
    zw = dict(re.findall(r"'([^']+)':'([^']+)'", re.search(r"const ZW=\{(.*?)\};", src).group(1)))
    return places, zw


def commons_info(titles):
    """titles: list of 'File:..' -> {title: info} for files that exist on Commons."""
    out = {}
    for i in range(0, len(titles), 40):
        q = api("commons.wikimedia.org", action="query", titles="|".join(titles[i:i + 40]),
                prop="imageinfo", iiprop="url|extmetadata|mime|size", iiurlwidth=WIDTH)
        for p in q.get("query", {}).get("pages", []):
            ii = (p.get("imageinfo") or [None])[0]
            if ii and not p.get("missing") and ii.get("mime", "").startswith("image/"):
                out[p["title"]] = ii
    return out


def wiki_files(titles):
    out = {}
    for i in range(0, len(titles), 40):
        part = titles[i:i + 40]
        q = api("en.wikipedia.org", action="query", titles="|".join(part), prop="pageimages",
                piprop="name", pilicense="free", redirects="1").get("query", {})
        fwd = {n["from"]: n["to"] for n in q.get("normalized", []) + q.get("redirects", [])}
        pages = {p["title"]: p for p in q.get("pages", [])}
        for t in part:
            x = t
            for _ in range(4):
                if x in fwd:
                    x = fwd[x]
            p = pages.get(x) or pages.get(x.replace("_", " "))
            if p and p.get("pageimage"):
                out[t] = "File:" + p["pageimage"].replace("_", " ")
    return out


def search(q):
    r = api("commons.wikimedia.org", action="query", generator="search", gsrnamespace="6",
            gsrsearch=q + " filetype:bitmap", gsrlimit="5", prop="imageinfo",
            iiprop="url|extmetadata|mime|size", iiurlwidth=WIDTH)
    pages = sorted(r.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
    for p in pages:
        ii = (p.get("imageinfo") or [None])[0]
        if ii and usable(p["title"], ii):
            return p["title"], ii
    return None


def shrink(data, max_w=1000):
    """Re-encode as a progressive JPEG no wider than max_w so the page stays light on phones."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80, optimize=True, progressive=True)
    return out.getvalue()


def usable(title, ii):
    """Skip logos and tiny graphics so places get real photos."""
    if re.search(r"logo|icon|map|seal|flag|wordmark", title, re.I):
        return False
    return ii.get("mime") in ("image/jpeg", "image/png", "image/webp") and (ii.get("width") or 9999) >= 600


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def main():
    places, zw = parse_places()
    credits_path = os.path.join(IMG, "credits.json")
    credits = json.load(open(credits_path, encoding="utf-8")) if os.path.exists(credits_path) else {}
    todo = []
    for k in places:
        path = os.path.join(IMG, k + ".jpg")
        if os.path.exists(path) and k not in credits:
            print(k, "own photo, skip")
            continue
        if os.path.exists(path) and not FORCE:
            continue
        todo.append(k)
    print(len(places), "places,", len(todo), "to fetch")
    if not todo:
        return

    wtitles = sorted({t for k in todo for t in (places[k]["w"], zw.get(places[k]["zone"] or "")) if t})
    wfile = wiki_files(wtitles)
    info = commons_info(sorted(set(wfile.values())))  # drops en-wiki-only (non-Commons) files

    for k in todo:
        p, hit, approx = places[k], None, False
        f = wfile.get(p["w"] or "")
        if f in info and usable(f, info[f]):
            hit = (f, info[f])
        if not hit and p["cs"]:
            hit = search(p["cs"])
            approx = bool(hit) and p["ax"]
        if not hit and p["zone"]:
            f = wfile.get(zw.get(p["zone"], ""))
            if f in info and usable(f, info[f]):
                hit, approx = (f, info[f]), True
        if not hit:
            print(k, "-> nothing found")
            continue
        title, ii = hit
        data = get(ii.get("thumburl") or ii["url"], raw=True)
        if not data:
            print(k, "-> download failed")
            continue
        try:
            data = shrink(data)
        except Exception as e:
            print(k, "-> not an image", e)
            continue
        with open(os.path.join(IMG, k + ".jpg"), "wb") as fh:
            fh.write(data)
        meta = ii.get("extmetadata", {})
        credits[k] = {
            "file": title,
            "page": ii.get("descriptionurl"),
            "artist": clean(meta.get("Artist", {}).get("value"))[:120],
            "license": clean(meta.get("LicenseShortName", {}).get("value")),
            "approx": approx,
        }
        print(k, "->", title, "(approx)" if approx else "", len(data) // 1024, "KB")
        time.sleep(0.5)

    with open(credits_path, "w", encoding="utf-8") as fh:
        json.dump(credits, fh, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
