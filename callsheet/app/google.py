"""The only code in Callsheet that talks to Google (writer.py talks to Claude and the local model).

Read-only by construction: every request goes through `request()`, which checks it against an
explicit allowlist of (method, URL prefix) pairs. Gmail and Calendar are GET only; the only POSTs
go to the OAuth token and revoke endpoints. 0.6.0 (Know me index): GET only to Drive's files.list
(`/drive/v3/files`), files.get (`/drive/v3/files/<id>`, metadata or alt=media for plain text) and
files.export (`/drive/v3/files/<id>/export`), with the drive.readonly scope the owner grants separately (0.12.1: the
Reports Drive folders read through the same three). 0.15.0 (live reading into the message store): Gmail's
users.history.list (`GET /gmail/v1/users/me/history`) for incremental reads, under the same gmail.readonly scope.
0.21.0 Add to calendar (approved 2026-11-11): one of the two writes. `POST /calendar/v3/calendars/<calendarId>/events`
(events.insert) with exactly `sendUpdates=none`, a JSON body of only summary, start, end and the private
extendedProperty `callsheet_item` (never attendees: nobody is invited or emailed), under the calendar.events scope the
owner grants per account ("Allow adding events"). No update, patch, delete, move, import, quickAdd, watch, calendar,
ACL or settings call.
0.21.0 (Put in Gmail Drafts, approved 2026-11-12): the other write, `POST /gmail/v1/users/me/drafts`
(drafts.create) with a body of only message.raw and message.threadId, under the gmail.drafts.create scope the owner
grants per account; no send, update, modify or delete.
0.21.0 (Google Contacts, read-only, approved 2026-11-12): exactly `GET /v1/people/me/connections` on
people.googleapis.com (People API connections.list), under the contacts.readonly scope the owner grants per account
("Allow contacts", contacts.py). No other People path (no otherContacts, searchContacts, people/<id>, batch or any
create, update or delete).
0.21.0 Answer invites (approved 2026-11-13): `PATCH /calendar/v3/calendars/<id>/events/
<eventId>` with exactly `sendUpdates=all` (Google tells the organizer: the only thing Callsheet ever sends, on the
owner's press) and a body of only `attendees`, equal to the event's attendee list fetched just before except the
owner's own `self: true` entry's responseStatus (check_rsvp_body). No other field, attendee, comment or event change.
Anything else raises NotAllowed before any network I/O. Redirects are not followed.

Base URLs can be overridden by env for tests only: CALLSHEET_GOOGLE_AUTH_BASE,
CALLSHEET_GOOGLE_TOKEN_URL, CALLSHEET_GOOGLE_API_BASE, CALLSHEET_GOOGLE_PEOPLE_BASE.

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
# 0.21.0: only asked for by "Allow adding events" (events only: no calendar settings, sharing or calendar creation)
CAL_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# 0.21.0 Put in Gmail Drafts (approved 2026-11-12): only asked for by "Allow drafts in Gmail" (gdrafts.py)
DRAFTS_SCOPE = "https://www.googleapis.com/auth/gmail.drafts.create"
# 0.21.0 Google Contacts (approved 2026-11-12): only asked for by "Allow contacts" (contacts.py)
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,birthdays,organizations,nicknames"
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


def people_base():
    """0.21.0: the People API host (contacts.py reads the owner's connections there, read-only)."""
    return os.environ.get("CALLSHEET_GOOGLE_PEOPLE_BASE", "https://people.googleapis.com").rstrip("/")


def auth_url():
    return auth_base() + "/o/oauth2/v2/auth"


def allowlist():
    """(method, exact URL or prefix ending in '/') pairs. Recomputed per call so test env overrides apply."""
    api = api_base()
    return (
        ("POST", token_url()),
        ("POST", revoke_url()),
        ("GET", api + "/gmail/v1/users/me/"),
        ("GET", api + "/gmail/v1/users/me/history"),   # 0.15.0 live reading: users.history.list (exact; a GET)
        ("GET", api + "/calendar/v3/"),
        ("GET", api + "/drive/v3/files"),     # 0.6.0: files.list (exact)
        ("GET", api + "/drive/v3/files/"),    # 0.6.0: files.get / files.export, checked by _DRIVE_PATH below
        ("POST", api + "/calendar/v3/calendars/"),  # 0.21.0: events.insert only, checked by _EVENTS_INSERT below
        # 0.21.0 Answer invites (approved 2026-11-13): events.patch of attendees only, checked by _EVENT_PATCH
        ("PATCH", api + "/calendar/v3/calendars/"),
        # 0.21.0 (approved 2026-11-12): drafts.create, the other write (exact: not drafts/send, drafts/<id>,
        # messages/send or any other path; PUT/DELETE never)
        ("POST", api + "/gmail/v1/users/me/drafts"),
        # 0.21.0 Google Contacts (approved 2026-11-12): connections.list only (exact; read-only)
        ("GET", people_base() + "/v1/people/me/connections"),
    )


# 0.6.0: under /drive/v3/files/ only "<file id>" and "<file id>/export" (files.get and files.export); nothing else,
# e.g. not files/generateIds or any comments/permissions/revisions sub-resource.
_DRIVE_PATH = re.compile(r"[A-Za-z0-9_-]{1,200}(?:/export)?")
_DRIVE_NOT_IDS = ("generateIds", "trash")
# 0.21.0: under POST /calendar/v3/calendars/ only "<calendarId>/events" (events.insert; the id is one URL-quoted path
# segment), never events/<id>, events/import, events/quickAdd, events/watch, acl, clear or a new calendar.
_EVENTS_INSERT = re.compile(r"[A-Za-z0-9._%~+-]{1,300}/events")
INSERT_QUERY = {"sendUpdates": ["none"]}      # the only query an insert may carry: nobody is ever notified
EVENT_KEYS = ("summary", "start", "end", "extendedProperties")   # the only fields an inserted event may have
# 0.21.0: under PATCH /calendar/v3/calendars/ only "<calendarId>/events/<eventId>" with exactly sendUpdates=all
_EVENT_PATCH = re.compile(r"([A-Za-z0-9._%~+-]{1,300})/events/([A-Za-z0-9_%-]{1,1024})")
RSVP_QUERY = {"sendUpdates": ["all"]}
RSVP_STATUSES = ("accepted", "declined", "tentative")     # Yes, No, Maybe


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
            if m == "POST" and prefix.endswith("/calendar/v3/calendars/"):
                rest = plain[len(prefix):]
                if not _EVENTS_INSERT.fullmatch(rest) or urllib.parse.unquote(rest[:-len("/events")]) in ("", ".", "..") \
                        or urllib.parse.parse_qs(parts.query, keep_blank_values=True) != INSERT_QUERY:
                    continue
            if m == "PATCH":
                rest = plain[len(prefix):]
                mt = _EVENT_PATCH.fullmatch(rest)
                if not mt or not prefix.endswith("/calendar/v3/calendars/") \
                        or urllib.parse.unquote(mt.group(1)) in ("", ".", "..") \
                        or urllib.parse.unquote(mt.group(2)) in ("", ".", "..", "import", "quickAdd", "watch") \
                        or urllib.parse.parse_qs(parts.query, keep_blank_values=True) != RSVP_QUERY:
                    continue
            if prefix.endswith("/drive/v3/files/"):
                rest = plain[len(prefix):]
                if not _DRIVE_PATH.fullmatch(rest) or rest.split("/")[0] in _DRIVE_NOT_IDS:
                    continue
            return
    raise NotAllowed(f"{method} {parts.path} is not allowed")


def request(method, url, params=None, form=None, token=None, body=None, json_body=None, rsvp=None):
    """Make one allowlisted request; returns parsed JSON (or {} for an empty body). body (0.21.0): a JSON body, only
    for events.insert (checked by check_event_body); json_body (0.21.0): only for drafts.create (check_draft_body);
    rsvp (0.21.0): (body, the attendees fetched just before), only for events.patch (check_rsvp_body)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    check_allowed(method, url)
    if sum(x is not None for x in (body, json_body, rsvp)) > 1:
        raise NotAllowed("One body at a time")
    if (method.upper() == "PATCH") != (rsvp is not None):
        raise NotAllowed("A PATCH carries only an RSVP")     # an RSVP body goes only to events.patch, and only it
    if rsvp is not None:
        if not isinstance(rsvp, (tuple, list)) or len(rsvp) != 2:
            raise NotAllowed("This RSVP body is not allowed")
        check_rsvp_body(rsvp[0], rsvp[1])
        data = json.dumps(rsvp[0]).encode()
    elif body is not None:
        path = urllib.parse.urlsplit(url).path
        if not (method.upper() == "POST" and path.startswith(urllib.parse.urlsplit(api_base()).path.rstrip("/")
                                                              + "/calendar/v3/calendars/") and path.endswith("/events")):
            raise NotAllowed("This body is not allowed here")   # an event body goes only to events.insert
        check_event_body(body)
        data = json.dumps(body).encode()
    elif json_body is not None:
        check_draft_body(url, json_body)
        data = json.dumps(json_body).encode()
    else:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/json")
    if body is not None or json_body is not None or rsvp is not None:
        req.add_header("Content-Type", "application/json")
    elif data is not None:
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


def check_event_body(ev):
    """0.21.0: an inserted event holds only EVENT_KEYS: no attendees (nobody is invited or emailed), no conference,
    attachments, guests' permissions, recurrence or source; extendedProperties only private.callsheet_item."""
    if not isinstance(ev, dict) or set(ev) - set(EVENT_KEYS) or not {"summary", "start", "end"} <= set(ev):
        raise NotAllowed("This event body is not allowed")
    xp = ev.get("extendedProperties")
    if xp is not None and (not isinstance(xp, dict) or set(xp) != {"private"} or not isinstance(xp["private"], dict)
                           or set(xp["private"]) != {"callsheet_item"}):
        raise NotAllowed("This event body is not allowed")
    for k in ("start", "end"):
        t = ev[k]
        if not isinstance(t, dict) or not (set(t) in ({"date"}, {"dateTime", "timeZone"})):
            raise NotAllowed("This event body is not allowed")
    if not isinstance(ev["summary"], str):
        raise NotAllowed("This event body is not allowed")


def check_rsvp_body(body, current):
    """0.21.0 Answer invites: the body is {"attendees": [...]} and nothing else; the list equals `current` (the event's
    attendees as Google gave them just before) entry for entry, except the one `self: true` entry's responseStatus,
    which becomes accepted, declined or tentative. No attendee added, removed, reordered or otherwise changed (no
    comment, no additionalGuests), no other event field."""
    if not isinstance(body, dict) or set(body) != {"attendees"} or not isinstance(body["attendees"], list) \
            or not isinstance(current, list) or not current or len(body["attendees"]) != len(current):
        raise NotAllowed("This RSVP body is not allowed")
    selves = [i for i, a in enumerate(current) if isinstance(a, dict) and a.get("self") is True]
    if len(selves) != 1:
        raise NotAllowed("This RSVP body is not allowed")
    for i, (new, old) in enumerate(zip(body["attendees"], current)):
        if not isinstance(new, dict) or not isinstance(old, dict):
            raise NotAllowed("This RSVP body is not allowed")
        if i != selves[0]:
            if new != old:
                raise NotAllowed("This RSVP body is not allowed")
            continue
        if set(new) != set(old) | {"responseStatus"} or new.get("responseStatus") not in RSVP_STATUSES \
                or any(new[k] != old[k] for k in old if k != "responseStatus"):
            raise NotAllowed("This RSVP body is not allowed")


def check_draft_body(url, body):
    """0.21.0: a JSON body goes only to drafts.create and holds only message.raw (+ message.threadId)."""
    if urllib.parse.urlsplit(url).path != urllib.parse.urlsplit(api_base()).path.rstrip("/") + "/gmail/v1/users/me/drafts":
        raise NotAllowed("This body is not allowed here")
    msg = body.get("message") if isinstance(body, dict) and set(body) == {"message"} else None
    if not isinstance(msg, dict) or not isinstance(msg.get("raw"), str) or set(msg) - {"raw", "threadId"} \
            or not isinstance(msg.get("threadId", ""), str):
        raise NotAllowed("This draft body is not allowed")


def calendar_insert_event(token, calendar_id, event):
    """0.21.0 Add to calendar: events.insert, sendUpdates=none (one of the two writes; the other is drafts.create)."""
    return request("POST", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events", token=token,
                   params={"sendUpdates": "none"}, body=event)


def calendar_event_get(token, calendar_id, event_id):
    """0.21.0: one event as Google has it now (events.get, a GET under /calendar/v3/), read just before an RSVP."""
    return request("GET", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events/" + _q(event_id),
                   token=token)


def calendar_rsvp(token, calendar_id, event_id, attendees, current):
    """0.21.0 Answer invites (approved 2026-11-13): events.patch with sendUpdates=all and a body of only the
    attendee list, where only the owner's own responseStatus differs from `current` (check_rsvp_body)."""
    out = request("PATCH", f"{api_base()}/calendar/v3/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
                  token=token, params={"sendUpdates": "all"}, rsvp=({"attendees": attendees}, current))
    return out


def calendar_event_by_item(token, calendar_id, item_id):
    """0.21.0: the event Callsheet added for this ledger item, if any (a GET by its private extendedProperty), so a
    retry after a lost answer never adds it twice."""
    res = request("GET", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events", token=token,
                  params={"privateExtendedProperty": f"callsheet_item={int(item_id)}", "maxResults": 5,
                          "showDeleted": "false"})
    return [e for e in res.get("items") or [] if isinstance(e, dict) and e.get("status") != "cancelled"]


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


# --- 0.15.0 live reading (live.py): incremental Gmail and Calendar reads, GET only --------------------------------
def gmail_history(token, start_history_id, page_token=None, max_results=500):
    """users.history.list since a historyId (messages added or deleted, labels changed). A 404 means the id expired."""
    params = [("startHistoryId", str(start_history_id)), ("maxResults", str(max_results))] + \
        [("historyTypes", t) for t in ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")]
    if page_token:
        params.append(("pageToken", page_token))
    return request("GET", api_base() + "/gmail/v1/users/me/history", token=token, params=params)


def calendar_events_changes(token, calendar_id, time_min, time_max, updated_min=None, page_token=None,
                            max_results=250):
    """events.list for the live reader: expanded instances, cancelled ones included (showDeleted), optionally only
    those changed since updated_min."""
    params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "maxResults": max_results,
              "showDeleted": "true"}
    if updated_min:
        params["updatedMin"] = updated_min
    if page_token:
        params["pageToken"] = page_token
    return request("GET", api_base() + "/calendar/v3/calendars/" + _q(calendar_id) + "/events", token=token,
                   params=params)


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


# --- 0.21.0 Put in Gmail Drafts (approved 2026-11-12; gdrafts.py) ------------------------------------------
# The reply's threading headers are read with gmail.readonly (a GET under /gmail/v1/users/me/); the draft is made with
# drafts.create, the one allowlisted write. Nothing here sends, updates, modifies or deletes anything.
REPLY_HEADERS = ("From", "Reply-To", "To", "Cc", "Subject", "Message-ID", "References", "In-Reply-To", "Date")


def gmail_message_meta(token, message_id):
    """One message's headers for a reply (messages.get format=metadata)."""
    return request("GET", api_base() + "/gmail/v1/users/me/messages/" + _q(message_id), token=token,
                   params=[("format", "metadata")] + [("metadataHeaders", h) for h in REPLY_HEADERS])


def gmail_thread_meta(token, thread_id):
    """A thread's messages with the reply headers (threads.get format=metadata)."""
    return request("GET", api_base() + "/gmail/v1/users/me/threads/" + _q(thread_id), token=token,
                   params=[("format", "metadata")] + [("metadataHeaders", h) for h in REPLY_HEADERS])


def gmail_draft_create(token, raw, thread_id=None):
    """users.drafts.create: one draft (base64url RFC 2822 `raw`), in `thread_id` when it is a reply. Returns
    {id, message: {id, threadId}}. Never sends."""
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    return request("POST", api_base() + "/gmail/v1/users/me/drafts", token=token, json_body={"message": message})


# --- 0.21.0 Google Contacts, read-only (approved 2026-11-12; contacts.py) ------------------------------------
def people_connections(token, page_token=None, sync_token=None, page_size=1000):
    """people.connections.list: one page of the owner's contacts (names, emails, phones, birthdays, organisations,
    nicknames). sync_token: only the changes since (deleted ones carry metadata.deleted); requestSyncToken asks for
    the next one on the last page. The only People API call (a GET)."""
    params = {"personFields": PERSON_FIELDS, "pageSize": int(page_size), "requestSyncToken": "true"}
    if page_token:
        params["pageToken"] = page_token
    if sync_token:
        params["syncToken"] = sync_token
    return request("GET", people_base() + "/v1/people/me/connections", params=params, token=token)
