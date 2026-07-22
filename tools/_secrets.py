"""Where the build/push scripts look for API keys.

Checked in this order:

  1. An environment variable (e.g. SHELTERLUV_API_KEY)
  2. tools/secrets.json  —  {"SHELTERLUV_API_KEY": "...", "CLOUDFLARE_API_TOKEN": "..."}

The file is usually the easier option: `setx` only affects terminals opened
*after* you run it, and Windows Task Scheduler doesn't always pass user
environment variables to a task, so a scheduled job can silently see no key
while your own terminal sees it fine. A file behaves the same everywhere.

tools/secrets.json is never uploaded to the website — the deploy zip excludes
the whole tools/ folder (there's a test asserting that).
"""
import io
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.json")
_cache = None


def _file_secrets():
    global _cache
    if _cache is None:
        try:
            data = json.load(io.open(_PATH, encoding="utf-8"))
            _cache = {str(k): str(v).strip() for k, v in data.items() if v}
        except FileNotFoundError:
            _cache = {}
        except Exception as e:
            print("  WARNING: tools/secrets.json could not be read (%s).\n"
                  "           Check it is valid JSON — every line needs quotes and commas." % e)
            _cache = {}
    return _cache


# Accept the names people actually use. The Shelterluv key is stored in
# Cloudflare under "shelterlov", so that spelling is just as likely to be typed
# here — matching the worker, which accepts the same variants.
ALIASES = {
    "SHELTERLUV_API_KEY": ["SHELTERLUV_API_KEY", "shelterlov", "SHELTERLOV",
                           "shelterluv", "SHELTERLUV", "SHELTERLUV_KEY"],
    "CLOUDFLARE_API_TOKEN": ["CLOUDFLARE_API_TOKEN", "CF_API_TOKEN", "cloudflare_api_token"],
}


def _names(name):
    return ALIASES.get(name, [name])


def get(name, default=""):
    """Return a secret from the environment, else tools/secrets.json.

    Any of the accepted aliases for `name` will do — see ALIASES.
    """
    for n in _names(name):
        v = os.environ.get(n, "").strip()
        if v:
            return v
    f = _file_secrets()
    for n in _names(name):
        if f.get(n):
            return f[n]
    return default


def source_of(name):
    """Where a secret came from — used to make setup problems obvious."""
    for n in _names(name):
        if os.environ.get(n, "").strip():
            return "environment variable %s" % n
    f = _file_secrets()
    for n in _names(name):
        if f.get(n):
            return 'tools/secrets.json ("%s")' % n
    return None
