"""Reading bios, adoption fees and weights from Shelterluv WITHOUT an API key.

Why this exists
---------------
Bios and fees are not in Shelterluv's public JSON feed, and their keyed API is
unreachable from Cloudflare (their firewall stalls it). For a while the plan was
to call the keyed API from a local machine — but that needs the API key value,
which neither Cloudflare (encrypted) nor Shelterluv (shown once) will hand back.

It turns out none of that is necessary. Each animal's PUBLIC embed page —
https://new.shelterluv.com/embed/animal/{nid} — carries the whole animal record
as JSON inside an HTML attribute, including:

    kennel_description   the bio (HTML: <br /> and &#039; entities)
    adoptionFee          e.g. "$325.00"
    weight/weight_units  e.g. 35 "lbs"
    videos

Same data Shelterluv's own widget renders, same URL anyone can open. No key, no
authentication, no firewall problem.

Cost: one small page (~13 KB) per animal. All 216 take about 13 seconds at the
concurrency set here, which is fine for a build. The every-minute push job uses
the incremental helper instead so it costs almost nothing per run.
"""
import concurrent.futures
import gzip
import html as _html
import json
import re
import urllib.request

EMBED = "https://new.shelterluv.com/embed/animal/%s"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
WORKERS = 6            # polite concurrency against someone else's server


def _fetch(url, timeout=45):
    r = urllib.request.urlopen(urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html"}), timeout=timeout)
    raw = r.read()
    if r.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def _clean_bio(raw):
    """Shelterluv stores the bio as HTML. Turn it into plain text with blank
    lines between paragraphs, which is what the site's renderer expects."""
    s = str(raw or "")
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*p\s*>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    # The bio often repeats the location line the site already shows separately.
    s = re.sub(r"^\s*Location:[^\n]*\n+", "", s)
    return s.strip()


def _money(raw):
    try:
        v = float(re.sub(r"[^\d.]", "", str(raw or "")))
        return v if v > 0 else 0
    except ValueError:
        return 0


def _parse(page):
    """Pull the animal JSON out of the HTML attribute it's embedded in."""
    best = None
    for m in re.finditer(r'"(\{&quot;.*?)"', page, re.S):
        cand = _html.unescape(m.group(1))
        if "kennel_description" in cand and (best is None or len(cand) > len(best)):
            best = cand
    if not best:
        return None
    try:
        return json.loads(best)
    except ValueError:
        return None


def rich_for(nid):
    """{description, fee, videos, weight} for one animal, or {} on any failure."""
    d = None
    for attempt in range(2):                 # one retry — rate-limit hiccups are transient
        try:
            d = _parse(_fetch(EMBED % nid))
            if d:
                break
        except Exception:
            d = None
        if attempt == 0:
            import time as _t
            _t.sleep(1.5)
    if not d:
        return {}
    out = {}
    bio = _clean_bio(d.get("kennel_description"))
    if bio:
        out["description"] = bio
    price = _money(d.get("adoptionFee"))
    if price:
        out["fee"] = {"price": price}
    w = str(d.get("weight") or "").strip()
    if w and w not in ("0",):
        out["weight"] = (w + " " + str(d.get("weight_units") or "lbs")).strip()
    vids = []
    for v in (d.get("videos") or []):
        s = v if isinstance(v, str) else json.dumps(v)
        m = re.search(r"https?://[^\"'\s\\]*(?:youtube\.com|youtu\.be|vimeo\.com)[^\"'\s\\]*", s)
        if m:
            vids.append(m.group(0).replace("\\/", "/"))
    if vids:
        out["videos"] = vids
    return out


def fetch_many(nids, label="animals", progress=True):
    """Scrape a list of animals concurrently. Returns {nid: rich}."""
    nids = [str(n) for n in nids]
    if not nids:
        return {}
    out, done = {}, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(rich_for, n): n for n in nids}
        for f in concurrent.futures.as_completed(futures):
            n = futures[f]
            try:
                r = f.result()
            except Exception:
                r = {}
            if r:
                out[n] = r
            done += 1
            if progress and done % 50 == 0:
                print("    %d/%d %s..." % (done, len(nids), label))
    return out
