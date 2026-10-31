"""The only code in Callsheet that talks to Home Assistant (0.5.1: phone notifications).

Notify only, by construction: every request goes through `request()`, which checks it against an explicit
allowlist of exactly two (method, URL) forms on the Supervisor's Core API proxy:
  GET  <core>/services                        (to list the notify.mobile_app_* targets)
  POST <core>/services/notify/mobile_app_<x>  (to send one notification to one phone)
Anything else (other services, states, events, templates, query strings, other hosts) raises NotAllowed before
any network I/O. Redirects are not followed.

The Supervisor gives the app SUPERVISOR_TOKEN when config.yaml has `homeassistant_api: true`. The token is read
from the environment per request and only ever put in the Authorization header: it is never logged, stored,
returned or put in an error message.

CALLSHEET_HA_API_BASE (tests only) points the client at a fake Core API.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

CORE_API = "http://supervisor/core/api"
TIMEOUT = 10
SERVICE = re.compile(r"mobile_app_[a-z0-9_]{1,64}")
NOT_IN_HA = "Phone notifications work when Callsheet runs as a Home Assistant app."


def api_base():
    return os.environ.get("CALLSHEET_HA_API_BASE", CORE_API).rstrip("/")


def _token():
    return os.environ.get("SUPERVISOR_TOKEN", "").strip()


def available():
    """True when the Supervisor gave Callsheet its Core API token."""
    return bool(_token())


class NotAllowed(Exception):
    """A request outside the allowlist. A programming error, never a user error."""


class HAError(Exception):
    """Home Assistant said no or couldn't be reached. .status: HTTP status (0 for network errors);
    .code: "gone" (the phone's notify service isn't there), "auth", "unreachable", "server", "unavailable"."""

    def __init__(self, message, status=0, code=""):
        super().__init__(message)
        self.status = status
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx becomes an HTTPError; never follow to another URL


_opener = urllib.request.build_opener(_NoRedirect)


def check_allowed(method, url):
    method = method.upper()
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(api_base())
    if (parts.scheme, parts.netloc) != (base.scheme, base.netloc) or parts.query or parts.fragment \
            or "%" in parts.path or ".." in parts.path or parts.username or parts.password:
        raise NotAllowed(f"{method} to this URL is not allowed")
    path, root = parts.path, base.path.rstrip("/")
    if method == "GET" and path == root + "/services":
        return
    prefix = root + "/services/notify/"
    if method == "POST" and path.startswith(prefix) and SERVICE.fullmatch(path[len(prefix):]):
        return
    raise NotAllowed(f"{method} {path} is not allowed")


def request(method, url, body=None):
    """Make one allowlisted request; returns parsed JSON (or None for an empty body)."""
    check_allowed(method, url)
    token = _token()
    if not token:
        raise HAError(NOT_IN_HA, 0, "unavailable")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise HAError(_plain(e.code, url), e.code, _code(e.code, url)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise HAError("Home Assistant didn't answer in time.", 0, "unreachable") from None
        raise HAError("Couldn't reach Home Assistant.", 0, "unreachable") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise HAError("Home Assistant sent an answer Callsheet can't read.", 200, "server") from None


def _is_notify(url):
    return "/services/notify/" in urllib.parse.urlsplit(url).path


def _code(status, url):
    if status in (400, 404) and _is_notify(url):
        return "gone"
    if status in (401, 403):
        return "auth"
    return "server"


def _plain(status, url):
    if status in (400, 404) and _is_notify(url):
        name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        return (f"Home Assistant doesn't know {friendly(name)} (notify.{name}) any more. Open the Home Assistant app "
                "on that phone, or pick another phone.")
    if status in (401, 403):
        return ("Home Assistant didn't let Callsheet in. Restart the Callsheet app in Home Assistant; if that doesn't "
                "help, reinstall this version (it needs Home Assistant API access).")
    if 300 <= status < 400:
        return "Home Assistant sent an unexpected redirect."
    if status >= 500:
        return "Home Assistant had a problem. Try again in a minute."
    return f"Home Assistant said no (HTTP {status})."


# --- the two calls ----------------------------------------------------------------
def notify_targets():
    """The notify.mobile_app_* services Home Assistant lists: [{id, name}] sorted by name."""
    res = request("GET", api_base() + "/services")
    ids = set()
    for d in res if isinstance(res, list) else []:
        if isinstance(d, dict) and d.get("domain") == "notify" and isinstance(d.get("services"), dict):
            ids.update(s for s in d["services"] if isinstance(s, str) and SERVICE.fullmatch(s))
    return sorted(({"id": s, "name": friendly(s)} for s in ids), key=lambda t: (t["name"].lower(), t["id"]))


def notify(service, payload):
    """POST one notification ({message, title, data}) to notify.<service>."""
    if not isinstance(service, str) or not SERVICE.fullmatch(service):
        raise NotAllowed("not a mobile_app notify service")
    request("POST", api_base() + "/services/notify/" + service, payload)


_WORDS = {"iphone": "iPhone", "ipad": "iPad", "ipod": "iPod", "imac": "iMac", "macbook": "MacBook",
          "watch": "Watch", "pixel": "Pixel", "galaxy": "Galaxy", "oneplus": "OnePlus"}


def friendly(service):
    """A name for a phone from its service id: mobile_app_isaac_ipad -> "Isaac iPad", mobile_app_pixel_9a ->
    "Pixel 9a"."""
    core = str(service or "")
    core = core[len("mobile_app_"):] if core.startswith("mobile_app_") else core
    words = []
    for w in (x for x in core.split("_") if x):
        if w in _WORDS:
            words.append(_WORDS[w])
        elif any(ch.isdigit() for ch in w):
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words) or "Phone"
