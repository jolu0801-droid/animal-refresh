"""Push current Shelterluv animals into Cloudflare KV. Safe to run on a schedule.

    python tools/push-animals-kv.py

No Node, no npm, no Wrangler, no interactive login — this talks to Cloudflare's
API directly using only Python's standard library.

WHAT THIS DOES (and deliberately does NOT do)
---------------------------------------------
It writes ONE key of animal data to Cloudflare KV. It does not deploy the site,
does not touch a single site file, and cannot publish work in progress — which
is exactly why this exists instead of a scheduled full deploy.

Once it runs daily:
  • /animals-sitemap.xml is generated live from this data, so Google sees
    animals listed since the last site deploy.
  • A newly listed animal gets a proper Facebook/Google link preview (name +
    photo) within a day instead of waiting for the next deploy.
  • The site itself is unaffected either way — visitors' browsers already read
    Shelterluv live, so animals appear there within minutes regardless.

SETUP (all in the Cloudflare dashboard, then one environment variable)
----------------------------------------------------------------------
1. Create the storage:
   Cloudflare → Storage & Databases → KV → Create a namespace
   Name it exactly:  ANIMALS_KV

2. Connect it to the site:
   Workers & Pages → rescuenetwork → Settings → Bindings → Add → KV namespace
   Variable name:  ANIMALS_KV        (must match exactly)
   Namespace:      the one from step 1
   Add it for Production and Preview, then deploy the site once — bindings
   only take effect on new deployments.

3. Create an API token:
   My Profile → API Tokens → Create Token → Custom token
   Permissions:  Account → Workers KV Storage → Edit
   Copy the token, then in a terminal:
       setx CLOUDFLARE_API_TOKEN "paste-the-token-here"
   Close and reopen the terminal afterwards.

That's it. This script finds your account and the ANIMALS_KV namespace on its
own. (If you'd rather pin them, set CLOUDFLARE_ACCOUNT_ID and/or
RN_ANIMALS_KV_ID and it will use those instead of looking them up.)

SCHEDULE IT (Windows Task Scheduler)
------------------------------------
Create Basic Task → Daily → Start a program:
    Program:   python
    Arguments: "E:\\SHIFT Design\\Claude\\Rescue Network\\tools\\push-animals-kv.py"
    Start in:  E:\\SHIFT Design\\Claude\\Rescue Network
Tick "Run whether user is logged on or not". Daily is plenty; hourly is fine
too — the payload is small and KV's free tier allows 1,000 writes a day.

If this ever fails, nothing breaks: the worker falls back to the snapshot
bundled with the last deploy.
"""
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # find the _ helpers
import _secrets
import _shelterluv
from datetime import datetime, timezone

FEED = "https://new.shelterluv.com/api/v3/available-animals/34981"
SAVED_QUERIES = ["13320"]          # keep in sync with build-animals-snapshot.py
KV_KEY = "animals"                 # keep in sync with _worker.js
NAMESPACE_TITLE = "ANIMALS_KV"     # the namespace name to look for
CF = "https://api.cloudflare.com/client/v4"

# Run this as often as you like — every minute is fine — because of two guards:
#
#  1. It only WRITES to Cloudflare when the data actually changed. Cloudflare's
#     free tier allows 1,000 KV writes a day; a blind write every minute would
#     be 1,440 and blow through it. Nothing changes most minutes, so real
#     writes land in the dozens.
#  2. Bios/fees come from one PUBLIC page per animal, so reading all 216 every
#     minute would be ~310,000 requests a day against someone else's server.
#     Instead new animals are read at once (usually none) and the rest rotate
#     slowly, which is a couple of dozen requests an hour.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".animals-push-state.json")   # inside tools/, never deployed
# Bio/fee/weight live only on each animal's individual embed page, so catching
# an EDIT to one means re-reading that page. Shelterluv sends no caching headers,
# so every check is a full ~13 KB fetch. We therefore re-read a SLICE each run
# rather than everything: at ~40 animals/run once a minute, a full pass takes
# ~6 min (so any bio/fee edit shows within ~6 min) at ~57k requests/day — brisk
# but far below the volume that got Cloudflare's IPs blocked. New animals are
# always read immediately regardless of the rotation; only edits wait for it.
# Everything ELSE (photos/social image, age, breed, location, size) comes from
# the cheap one-request list feed and refreshes every single run.
RICH_ROTATE_EVERY = 4 * 60         # rotate on essentially every run (job runs every 5 min)
RICH_ROTATE_SIZE = 60              # animals re-read per run (~4 runs = ~20 min full pass)
MAX_SCRAPE_PER_RUN = 80            # hard ceiling on embed-page reads per run (burst guard)

TOKEN = _secrets.get("CLOUDFLARE_API_TOKEN")
ACCOUNT_ID = _secrets.get("CLOUDFLARE_ACCOUNT_ID")
NAMESPACE_ID = _secrets.get("RN_ANIMALS_KV_ID")


def shelterluv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "RescueNetworkSite/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))


def cf_api(path, method="GET", body=None, content_type="application/json"):
    req = urllib.request.Request(CF + path, method=method,
                                 data=body if isinstance(body, bytes) else
                                 (json.dumps(body).encode("utf-8") if body is not None else None))
    req.add_header("Authorization", "Bearer " + TOKEN)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit("\nCloudflare API error %d on %s\n%s\n\n%s"
                         % (e.code, path, detail, _hint(e.code, detail)))


def _hint(code, detail=""):
    # Cloudflare returns 400 (not 401) for a malformed token, so match on the
    # body too — otherwise the commonest setup mistake gets a useless hint.
    looks_like_auth = any(s in detail.lower() for s in
                          ("authorization", "authentication", "invalid request headers", "6003", "9109"))
    if code in (401, 403) or looks_like_auth:
        return ("The API token was rejected. Check that:\n"
                "  • CLOUDFLARE_API_TOKEN is set correctly — run:  echo %CLOUDFLARE_API_TOKEN%\n"
                "  • you reopened the terminal after 'setx' (it doesn't affect the current one)\n"
                "  • the token has permission: Account -> Workers KV Storage -> Edit")
    if code == 404:
        return ("Not found — usually the namespace doesn't exist yet. Create a KV namespace\n"
                "named %s in the Cloudflare dashboard (see the notes at the top of this file)."
                % NAMESPACE_TITLE)
    return "See the setup notes at the top of this file."


def resolve_account():
    if ACCOUNT_ID:
        return ACCOUNT_ID
    res = cf_api("/accounts")
    accounts = res.get("result") or []
    if not accounts:
        raise SystemExit("No Cloudflare accounts visible to this token. Set CLOUDFLARE_ACCOUNT_ID.")
    if len(accounts) > 1:
        names = ", ".join("%s (%s)" % (a["name"], a["id"]) for a in accounts)
        raise SystemExit("Several accounts found — set CLOUDFLARE_ACCOUNT_ID to the right one:\n  " + names)
    print("  account: %s" % accounts[0]["name"])
    return accounts[0]["id"]


def resolve_namespace(account):
    if NAMESPACE_ID:
        return NAMESPACE_ID
    res = cf_api("/accounts/%s/storage/kv/namespaces?per_page=100" % account)
    for ns in res.get("result") or []:
        if ns.get("title", "").strip().lower() == NAMESPACE_TITLE.lower():
            print("  namespace: %s" % ns["title"])
            return ns["id"]
    raise SystemExit(
        "\nNo KV namespace named %r found.\n"
        "Create one in the Cloudflare dashboard (Storage & Databases → KV → Create a\n"
        "namespace), or set RN_ANIMALS_KV_ID to an existing namespace id." % NAMESPACE_TITLE)


def load_state():
    try:
        return json.load(io.open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    try:
        io.open(STATE_FILE, "w", encoding="utf-8", newline="\n").write(
            json.dumps(st, separators=(",", ":")))
    except Exception as e:
        print("  (could not save state: %s — next run will just do more work)" % e)


def seed_from_snapshot():
    """The deployed animals-snapshot.json already carries a bio/fee for every
    animal (baked in by build-animals-snapshot.py). Seed the cache from it so a
    cold start begins with 224 good bios instead of scraping all 224 embed pages
    at once — that burst tripped Shelterluv's rate limit and left gaps."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "animals-snapshot.json")
    seed = {}
    try:
        snap = json.load(io.open(path, encoding="utf-8"))
        for a in snap.get("animals", []):
            r = a.get("rn_rich")
            if r:
                seed[str(a.get("nid"))] = r
    except Exception as e:
        print("  (couldn't seed bios from the deployed snapshot: %s)" % e)
    if seed:
        print("  seeded %d bios from the deployed snapshot (no scrape needed)" % len(seed))
    return seed


def refresh_rich(animals, state):
    """Bios/fees/weights from the PUBLIC embed pages — no API key.

    One page per animal, so this is deliberately incremental: the cache is
    seeded from the deployed snapshot, new animals are read immediately (usually
    none), and a small slice of the rest is refreshed on a slow rotation. A
    once-a-minute schedule therefore reads ~40 pages, never all 224 at once.
    """
    cached = dict(state.get("rich") or {})
    if not cached:                                   # cold start
        cached = seed_from_snapshot()
    nids = [str(a.get("nid")) for a in animals]
    missing = [n for n in nids if n not in cached]

    due = []
    if time.time() - float(state.get("richRotatedAt") or 0) > RICH_ROTATE_EVERY:
        start_i = int(state.get("richCursor") or 0) % max(1, len(nids))
        due = (nids + nids)[start_i:start_i + RICH_ROTATE_SIZE]
        state["richCursor"] = (start_i + RICH_ROTATE_SIZE) % max(1, len(nids))
        state["richRotatedAt"] = time.time()

    # Never scrape more than this in one run, even after a big intake — the rest
    # stay "missing" and get picked up over the next few runs. Prevents a burst.
    todo = list(dict.fromkeys(missing + due))[:MAX_SCRAPE_PER_RUN]
    if todo:
        print("  reading %d animal page(s) (%d new, %d on rotation)"
              % (len(todo), len([n for n in todo if n in missing]),
                 len([n for n in todo if n not in missing])))
        got = _shelterluv.fetch_many(todo, "pages", progress=False)
        cached.update(got)     # only successful scrapes overwrite — a failure keeps the old bio
    # drop animals that are no longer listed so the cache can't grow forever
    cached = {k: v for k, v in cached.items() if k in set(nids)}
    state["rich"] = cached
    return cached


def main():
    if not TOKEN:
        sys.exit("No Cloudflare API token found.\n"
                 "Put it in tools/secrets.json (copy tools/secrets.example.json),\n"
                 "or set the CLOUDFLARE_API_TOKEN environment variable.")

    print("fetching animals from Shelterluv...")
    animals = (shelterluv(FEED).get("animals")) or []
    if not animals:
        sys.exit("ERROR: Shelterluv returned no animals — refusing to overwrite KV with an\n"
                 "empty list. Nothing was changed.")

    # A saved query that comes back empty or errors is NOT written as "no
    # animals". Publishing that would delete a list the deployed snapshot still
    # has, and the worker would then treat a live foster-to-adopt animal as
    # adopted. Better to abort and leave yesterday's good data in place.
    saved = {}
    for q in SAVED_QUERIES:
        try:
            rows = shelterluv(FEED + "?saved_query=" + q).get("animals") or []
        except Exception as e:
            sys.exit("ERROR: saved query %s could not be fetched (%s).\n"
                     "Nothing was written — KV still holds the previous good data." % (q, e))
        if not rows:
            sys.exit("ERROR: saved query %s returned no animals.\n"
                     "That is usually a Shelterluv hiccup rather than a real empty list, so\n"
                     "nothing was written — KV still holds the previous good data." % q)
        saved[q] = rows

    # Full UTC timestamp, not just a date: at a 30-minute cadence several
    # pushes share a day, and the worker decides between this and the deployed
    # snapshot by comparing these strings. ISO-8601 sorts correctly as text,
    # and a timestamp always sorts after the bare date a deploy writes.
    # ---- bios / fees / videos, from the keyed api, only when needed ----
    state = load_state()
    membership = hashlib.sha256(
        json.dumps(sorted(str(a.get("nid")) for a in animals)).encode("utf-8")).hexdigest()
    rich = refresh_rich(animals, state)
    hits = 0
    for a in animals:
        r = rich.get(str(a.get("nid")))
        if r:
            a["rn_rich"] = r
            hits += 1
    if rich:
        print("  bios/fees attached to %d of %d animals" % (hits, len(animals)))
    state["membership"] = membership

    # ---- only write when something actually changed ----
    body = {"count": len(animals), "savedQueries": saved, "animals": animals}
    digest = hashlib.sha256(
        json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        .encode("utf-8")).hexdigest()
    if digest == state.get("digest"):
        save_state(state)
        print("\nNo change since the last push — nothing written. (%d animals)" % len(animals))
        return

    payload = dict(body)
    payload["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    print("connecting to Cloudflare...")
    account = resolve_account()
    namespace = resolve_namespace(account)

    # Expire after 14 days. If this job ever stops running unnoticed, the key
    # eventually clears itself and the site falls back to the snapshot shipped
    # with the last deploy, rather than serving months-old animals forever.
    print("pushing %d animals (%.0f KB)..." % (len(animals), len(blob) / 1024))
    res = cf_api("/accounts/%s/storage/kv/namespaces/%s/values/%s?expiration_ttl=%d"
                 % (account, namespace, KV_KEY, 14 * 24 * 3600),
                 method="PUT", body=blob, content_type="text/plain")
    if not res.get("success"):
        sys.exit("Cloudflare reported failure: %s" % json.dumps(res.get("errors"))[:300])

    state["digest"] = digest
    save_state(state)
    print("\nPushed. %d animals live in KV (%d in saved queries, %d with bios)."
          % (len(animals), sum(len(v) for v in saved.values()), hits))
    print('Verify: open https://rescuenetworkmn.org/api/animals — it should say "source":"kv"')


if __name__ == "__main__":
    main()
