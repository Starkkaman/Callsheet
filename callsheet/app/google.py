"""The only code in Callsheet that talks to Google.

Read-only by construction: every request goes through `request()`, which checks it against an
explicit allowlist of (method, URL prefix) pairs. Gmail and Calendar are GET only; the only POSTs
go to the OAuth token and revoke endpoints. Anything else raises NotAllowed before any network I/O.
Redirects are not followed.

Base URLs can be overridden by env for tests only: CALLSHEET_GOOGLE_AUTH_BASE,
CALLSHEET_GOOGLE_TOKEN_URL, CALLSHEET_GOOGLE_API_BASE.

Errors carry plain sentences and Google's short error code, never tokens or request bodies.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

SCOPES = ("openid email https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/calendar.readonly")
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
REDIRECT_URI = "http://127.0.0.1:8765/"
METADATA_HEADERS = ("From", "To", "Cc", "Subject", "Date", "List-Id", "List-Unsubscribe", "Precedence",
                    "Auto-Submitted", "Reply-To")
TIMEOUT = 15


def auth_base():
    return os.environ.get("CALLSHEET_GOOGLE_AUTH_BASE", "https://accounts.google.com").rstrip("/")


def token_url():
    return os.environ.get("CALLSHEET_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")


def revoke_url():
    # https://oauth2.googleapis.com/token -> https://oauth2.googleapis.com/revoke (same for test overrides)
    return token_url().rsplit("/", 1)[0] + "/revoke"


def api_base():
    return os.environ.get("CALLSHEET_GOOGLE_API_BASE", "https://www.googleapis.com").rstrip("/")


def auth_url():
    return auth_base() + "/o/oauth2/v2/auth"


def allowlist():
    """(method, exact URL or prefix ending in '/') pairs. Recomputed per call so test env overrides apply."""
    api = api_base()
    return (
        ("POST", token_url()),
        ("POST", revoke_url()),
        ("GET", api + "/gmail/v1/users/me/"),
        ("GET", api + "/calendar/v3/"),
    )


class NotAllowed(Exception):
    """A request outside the allowlist. A programming error, never a user error."""


class GoogleError(Exception):
    """Google answered with an error, or could not be reached.
    .status: HTTP status (0 for network errors); .code: Google's error code (e.g. invalid_grant)."""

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
    if parts.fragment or "/../" in parts.path or parts.path.endswith("/.."):
        raise NotAllowed(f"{method} to this URL is not allowed")
    plain = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    for m, prefix in allowlist():
        if m != method:
            continue
        if plain == prefix or (prefix.endswith("/") and plain.startswith(prefix)):
            return
    raise NotAllowed(f"{method} {parts.path} is not allowed")


def request(method, url, params=None, form=None, token=None):
    """Make one allowlisted request; returns parsed JSON (or {} for an empty body)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    check_allowed(method, url)
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except (ValueError, OSError):
            payload = {}
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, str):
            code = err
        elif isinstance(err, dict):
            code = str(err.get("status", ""))
        else:
            code = ""
        raise GoogleError(_plain(e.code, code), e.code, code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise GoogleError("Google didn't answer in time.") from None
        raise GoogleError("Couldn't reach Google. Check the internet connection.") from None
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        raise GoogleError("Google sent an answer Callsheet can't read.", 200) from None


def _plain(status, code):
    if code == "invalid_grant":
        return "Google asked to sign in again."
    if code in ("invalid_client", "unauthorized_client"):
        return "Google doesn't accept the client ID and secret. Check them in Settings."
    if status == 401:
        return "Google refused access. Reconnect the account."
    if status == 403:
        return "Google said no access. Check that the Gmail API and Google Calendar API are enabled."
    if status == 404:
        return "Google couldn't find it."
    if status == 429:
        return "Google asked Callsheet to slow down. It will try again at the next check-in."
    if 300 <= status < 400:
        return "Google sent an unexpected redirect."
    if status >= 500:
        return "Google had a problem. It will try again at the next check-in."
    return f"Google said no (HTTP {status})."


# --- OAuth -----------------------------------------------------------------
def build_auth_url(client_id, state, code_challenge):
    q = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return auth_url() + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def exchange_code(client, code, verifier):
    return request("POST", token_url(), form={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code", "code_verifier": verifier})


def refresh(client, refresh_token):
    return request("POST", token_url(), form={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": refresh_token, "grant_type": "refresh_token"})


def revoke(token):
    """Best effort; returns True when Google confirmed."""
    if not token:
        return False
    try:
        request("POST", revoke_url(), form={"token": token})
        return True
    except GoogleError:
        return False


# --- read-only APIs --------------------------------------------------------
def _q(s):
    return urllib.parse.quote(s, safe="")


def gmail_profile(token):
    return request("GET", api_base() + "/gmail/v1/users/me/profile", token=token)


def gmail_threads(token, q, max_results=50):
    return request("GET", api_base() + "/gmail/v1/users/me/threads", token=token,
                   params={"q": q, "maxResults": max_results})


def gmail_thread(token, thread_id):
    return request("GET", api_base() + "/gmail/v1/users/me/threads/" + _q(thread_id), token=token,
                   params=[("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS])


def calendar_list(token):
    items, page = [], None
    for _ in range(10):
        params = {"maxResults": 250}
        if page:
            params["pageToken"] = page
        res = request("GET", api_base() + "/calendar/v3/users/me/calendarList", token=token, params=params)
        items += res.get("items", [])
        page = res.get("nextPageToken")
        if not page:
            break
    return items


def calendar_events(token, calendar_id, time_min, time_max, tz):
    items, page = [], None
    for _ in range(5):
        params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime",
                  "maxResults": 250, "timeZone": tz}
        if page:
            params["pageToken"] = page
        res = request("GET", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events",
                      token=token, params=params)
        items += res.get("items", [])
        page = res.get("nextPageToken")
        if not page:
            break
    return items
