"""The only code in Callsheet that talks to Google (writer.py talks to Claude and the local model).

Read-only by construction: every request goes through `request()`, which checks it against an
explicit allowlist of (method, URL prefix) pairs. Gmail and Calendar are GET only; the only POSTs
go to the OAuth token and revoke endpoints. 0.6.0 (Know me index): GET only to Drive's files.list
(`/drive/v3/files`), files.get (`/drive/v3/files/<id>`, metadata or alt=media for plain text) and
files.export (`/drive/v3/files/<id>/export`), with the drive.readonly scope the owner grants separately (0.12.1: the
Reports Drive folders read through the same three). Anything else raises NotAllowed before any network I/O.
Redirects are not followed.

Base URLs can be overridden by env for tests only: CALLSHEET_GOOGLE_AUTH_BASE,
CALLSHEET_GOOGLE_TOKEN_URL, CALLSHEET_GOOGLE_API_BASE.

Errors carry plain sentences and Google's short error code, never tokens or request bodies.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

SCOPES = ("openid email https://www.googleapis.com/auth/gmail.readonly "
          "https://www.googleapis.com/auth/calendar.readonly")
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"  # 0.6.0: only asked for by "Grant Drive access"
REDIRECT_URI = "http://127.0.0.1:8765/"
METADATA_HEADERS = ("From", "To", "Cc", "Subject", "Date", "List-Id", "List-Unsubscribe", "Precedence",
                    "Auto-Submitted", "Reply-To", "Authentication-Results",  # 0.8.0: Gmail's sender check
                    # 0.9.0: bulk-sender anchors for the sorter's promo rule (triage.bulk_markers; codes only)
                    "Feedback-ID", "X-Mailgun-Sid", "X-SG-EID", "X-Campaign", "X-CampaignID", "X-MC-User",
                    "X-Mandrill-User")
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
        ("GET", api + "/drive/v3/files"),     # 0.6.0: files.list (exact)
        ("GET", api + "/drive/v3/files/"),    # 0.6.0: files.get / files.export, checked by _DRIVE_PATH below
    )


# 0.6.0: under /drive/v3/files/ only "<file id>" and "<file id>/export" (files.get and files.export); nothing else,
# e.g. not files/generateIds or any comments/permissions/revisions sub-resource.
_DRIVE_PATH = re.compile(r"[A-Za-z0-9_-]{1,200}(?:/export)?")
_DRIVE_NOT_IDS = ("generateIds", "trash")


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
            if prefix.endswith("/drive/v3/files/"):
                rest = plain[len(prefix):]
                if not _DRIVE_PATH.fullmatch(rest) or rest.split("/")[0] in _DRIVE_NOT_IDS:
                    continue
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
            # 0.6.0: Gmail/Drive quota answers (403/429 with a reason) are told apart from "no access"
            reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)]
            for r in reasons:
                if r in RATE_REASONS or r in ("accessNotConfigured", "insufficientPermissions"):
                    code = r
                    break
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


RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded")


def _plain(status, code):
    if code in RATE_REASONS:
        return "Google asked Callsheet to slow down (usage limit reached). It will try again later."
    if code == "accessNotConfigured":
        return "This Google API isn't turned on in your Google Cloud project. Turn it on, then try again."
    if code == "insufficientPermissions":
        return "Google said this account hasn't given Callsheet that permission."
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
def build_auth_url(client_id, state, code_challenge, extra_scopes=(), login_hint=None):
    """extra_scopes (0.6.0): added to the usual scopes for an incremental grant (include_granted_scopes keeps the
    scopes the account already gave); login_hint picks the account on Google's page."""
    q = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join([SCOPES] + [s for s in extra_scopes if s not in SCOPES.split()]),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        q["login_hint"] = login_hint
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


def send_as(token):
    """0.8.2 People: the account's Gmail send-as addresses (users.settings.sendAs.list). A GET under the allowed
    /gmail/v1/users/me/ prefix; gmail.readonly is enough. Read only when the owner presses the button."""
    return request("GET", api_base() + "/gmail/v1/users/me/settings/sendAs", token=token)


def gmail_threads(token, q, max_results=50):
    return request("GET", api_base() + "/gmail/v1/users/me/threads", token=token,
                   params={"q": q, "maxResults": max_results})


def gmail_thread(token, thread_id):
    return request("GET", api_base() + "/gmail/v1/users/me/threads/" + _q(thread_id), token=token,
                   params=[("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS])


def gmail_thread_full(token, thread_id):
    """The full thread (bodies) for the AI writer's suggested replies. Still a GET under gmail.readonly."""
    return request("GET", api_base() + "/gmail/v1/users/me/threads/" + _q(thread_id), token=token,
                   params={"format": "full"})


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


def calendar_event_by_uid(token, calendar_id, ical_uid):
    """0.8.0: is an invite found in mail on this calendar? events.list with iCalUID (a GET under /calendar/v3/)."""
    res = request("GET", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events", token=token,
                  params={"iCalUID": ical_uid, "showDeleted": "false", "maxResults": 5})
    return res.get("items", [])


def gmail_attachment(token, message_id, attachment_id):
    """0.8.0: one attachment of a message (a calendar file, at most gather.MAX_ATTACHMENT bytes; the caller checks
    the part's size first). users.messages.attachments.get: a GET under /gmail/v1/users/me/, read-only."""
    return request("GET", api_base() + "/gmail/v1/users/me/messages/" + _q(message_id) + "/attachments/" +
                   _q(attachment_id), token=token)


# --- 0.6.0: Know me index reads (GET only; paged by the caller so a scan can resume) ----------------------
def gmail_messages(token, q, page_token=None, max_results=100):
    params = {"q": q, "maxResults": max_results, "includeSpamTrash": "false"}
    if page_token:
        params["pageToken"] = page_token
    return request("GET", api_base() + "/gmail/v1/users/me/messages", token=token, params=params)


def gmail_message_full(token, message_id):
    """One message with its body parts (format=full); the index keeps only the text parts."""
    return request("GET", api_base() + "/gmail/v1/users/me/messages/" + _q(message_id), token=token,
                   params={"format": "full"})


def calendar_events_page(token, calendar_id, time_min, time_max, page_token=None, max_results=250, fields=None):
    params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "maxResults": max_results,
              "showDeleted": "false"}
    if page_token:
        params["pageToken"] = page_token
    if fields:
        params["fields"] = fields
    return request("GET", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events", token=token,
                   params=params)


def drive_files(token, q, page_token=None, page_size=100, fields=None, order_by="modifiedTime desc"):
    params = {"q": q, "pageSize": page_size, "orderBy": order_by, "spaces": "drive",
              "includeItemsFromAllDrives": "true", "supportsAllDrives": "true", "corpora": "allDrives",
              "fields": fields or "nextPageToken,files(id,name,mimeType,modifiedTime,owners(displayName,emailAddress),"
                                  "webViewLink,size)"}
    if page_token:
        params["pageToken"] = page_token
    return request("GET", api_base() + "/drive/v3/files", token=token, params=params)


def drive_export(token, file_id, mime):
    """Text of a Google Docs/Sheets/Slides file (files.export). Returns str."""
    return request_text("GET", api_base() + "/drive/v3/files/" + _q(file_id) + "/export", token=token,
                        params={"mimeType": mime})


def drive_file(token, file_id, fields):
    """0.12.1 Reports from Drive: one file's or folder's metadata (files.get, JSON). A GET under /drive/v3/files/."""
    return request("GET", api_base() + "/drive/v3/files/" + _q(file_id), token=token,
                   params={"fields": fields, "supportsAllDrives": "true"})


def drive_media_bytes(token, file_id, limit):
    """0.12.1 Reports from Drive: a file's bytes (files.get alt=media), at most limit + 1 read (the caller refuses a
    longer one). The same endpoint and allowlist as drive_media; bytes so the caller decides what is text."""
    return _request_body("GET", api_base() + "/drive/v3/files/" + _q(file_id), token=token,
                         params={"alt": "media", "supportsAllDrives": "true"}, limit=limit + 1)


def drive_media(token, file_id):
    """Content of a small plain-text file (files.get alt=media). Returns str."""
    return request_text("GET", api_base() + "/drive/v3/files/" + _q(file_id), token=token,
                        params={"alt": "media", "supportsAllDrives": "true"})


MAX_TEXT_BYTES = 2_000_000


def request_text(method, url, params=None, token=None):
    """Like request() for a text body (Drive export/media): the same allowlist and errors; at most MAX_TEXT_BYTES
    are read."""
    return _request_body(method, url, params, token).decode("utf-8", "replace")


def _request_body(method, url, params=None, token=None, limit=MAX_TEXT_BYTES):
    """The raw body of one allowlisted request (at most `limit` bytes are read)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    check_allowed(method, url)
    req = urllib.request.Request(url, method=method.upper())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            body = resp.read(limit)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except (ValueError, OSError):
            payload = {}
        err = payload.get("error") if isinstance(payload, dict) else None
        code = ""
        if isinstance(err, dict):
            code = str(err.get("status", ""))
            for r in [x.get("reason") for x in err.get("errors") or [] if isinstance(x, dict)]:
                if r in RATE_REASONS or r in ("accessNotConfigured", "insufficientPermissions", "exportSizeLimitExceeded"):
                    code = r
                    break
        raise GoogleError(_plain(e.code, code), e.code, code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise GoogleError("Google didn't answer in time.") from None
        raise GoogleError("Couldn't reach Google. Check the internet connection.") from None
    return body
