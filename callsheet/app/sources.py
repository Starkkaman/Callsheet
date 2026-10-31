"""0.6.0 "Know me": the sources the local index reads, all read-only.

A source is one small class (see `Source`): `targets(ctx)` lists what can be read (one per Google account, Slack
workspace or vault), `estimate(ctx, target, since)` counts cheaply (or returns None), and `step(ctx, target, since,
until, cursor)` reads one bounded batch and returns `(items, cursor)`; cursor None means this target is finished.
The scanner (scan.py) stores each step's items and cursor in one transaction, so a restart resumes where it was.
Signal (another branch: a local message store) plugs in as one more class with the same three methods.

Network access goes only through google.py and slack.py and their allowlists. Bodies are stripped of quoted
history and signatures with writer.py's helpers and capped by index.cap_body. Errors are plain sentences:
`Retry` (Google or Slack asked to slow down, or a passing network problem: the scanner waits and tries the same
step again) and `SourceError` (this target can't be read now; the others go on).
"""
import datetime as dt
import email.utils
import html
import os
import pathlib
import re
import time

import google
import index as ix
import rules
import slack
import writer
from store import InputError

GMAIL_PAGE = 100       # ids per messages.list page
GMAIL_CHUNK = 20       # messages fetched per step (the resume granularity)
GMAIL_PACE_S = 0.25    # between message requests: ~4 per second, far below Gmail's per-user quota
CAL_PAGE = 250
DRIVE_PAGE = 100
DRIVE_CHUNK = 5
DRIVE_PACE_S = 0.5
DRIVE_MAX_MEDIA = 1_000_000   # plain-text files larger than this are indexed by name only
SLACK_PAGE = 200
SLACK_PACE_S = 1.2     # conversations.history is Slack tier 3 (~50 per minute)
SLACK_MAX_THREADS = 2000
NOTE_CHUNK = 200
NOTE_MAX_BYTES = 1_000_000
NOTE_MAX_FILES = 20000
ALL_TIME = dt.datetime(1990, 1, 1, tzinfo=dt.timezone.utc).timestamp()
SCOPE_CHECK_S = 3600
VAULT_ROOTS_ENV = "CALLSHEET_VAULT_ROOTS"   # tests only; Home Assistant maps /share and /media read-only

DRIVE_TYPES = {
    "application/vnd.google-apps.document": ("doc", "text/plain"),
    "application/vnd.google-apps.spreadsheet": ("sheet", "text/csv"),       # Drive exports the first sheet
    "application/vnd.google-apps.presentation": ("slides", "text/plain"),
    "text/plain": ("file", None),
    "text/markdown": ("file", None),
    "text/csv": ("file", None),
}


class Retry(Exception):
    """Wait, then try the same step again. .wait: the shortest wait in seconds."""

    def __init__(self, message, wait=60):
        super().__init__(message)
        self.wait = wait


class SourceError(Exception):
    """This target can't be read now (a plain sentence); the scan goes on with the others. .status: Google's HTTP
    status when there was one."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def target(source, account, label, area="", unit="items", blocked=None):
    return {"source": source, "account": account, "label": label, "area": area or "", "unit": unit,
            "blocked": blocked}


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Google access: tokens, refresh on 401, granted scopes ----------------------------------------------------------
class GoogleAccess:
    """Access tokens for the index (saved back to the store like a sync does) and the granted scopes, read from the
    token endpoint's answer (`scope`), so no extra Google endpoint is needed."""

    def __init__(self, store, index, clock=time.time):
        self.store, self.index, self.clock = store, index, clock

    def _refresh(self, acc_id):
        client = self.store.google_client()
        if not client:
            raise SourceError("The Google client ID and secret are missing. Add them in Setup.")
        acc = self.store.account_secret(acc_id)
        if not acc.get("refresh_token"):
            raise SourceError("Google asked to sign in again. Reconnect the account in Setup.")
        try:
            res = google.refresh(client, acc["refresh_token"])
        except google.GoogleError as e:
            if e.code == "invalid_grant":
                raise SourceError("Google asked to sign in again. Reconnect the account in Setup.") from None
            raise _mapped(e) from None
        tok = res.get("access_token")
        if not tok:
            raise SourceError("Google didn't send an access token.")
        exp = self.clock() + float(res.get("expires_in", 3600))
        self.store.set_tokens(acc_id, tok, exp, res.get("refresh_token"))
        if isinstance(res.get("scope"), str):
            self._save_scopes(acc_id, res["scope"])
        return tok

    def _save_scopes(self, acc_id, scope):
        known = self.index.meta("scopes", {})
        known[acc_id] = {"scope": scope, "at": self.clock()}
        self.index.set_meta("scopes", known)

    def token(self, acc_id, force=False):
        acc = self.store.account_secret(acc_id)
        if not force and acc.get("access_token") and (acc.get("expires_at") or 0) - 60 > self.clock():
            return acc["access_token"]
        return self._refresh(acc_id)

    def call(self, acc_id, fn, *args, **kw):
        """fn(token, *args): refresh once on 401; Google's errors become Retry or SourceError."""
        try:
            try:
                return fn(self.token(acc_id), *args, **kw)
            except google.GoogleError as e:
                if e.status != 401:
                    raise
            return fn(self.token(acc_id, force=True), *args, **kw)
        except google.GoogleError as e:
            raise _mapped(e) from None

    def scopes(self, acc_id, force=False):
        """The scopes this account granted (a set), or None when Google didn't say. Cached for an hour."""
        known = self.index.meta("scopes", {}).get(acc_id)
        if force or not known or self.clock() - float(known.get("at") or 0) > SCOPE_CHECK_S:
            try:
                self._refresh(acc_id)
            except (SourceError, Retry):
                pass
            known = self.index.meta("scopes", {}).get(acc_id)
        return set(known["scope"].split()) if known else None

    def has_drive(self, acc_id, force=False):
        s = self.scopes(acc_id, force)
        return None if s is None else google.DRIVE_SCOPE in s

    def cached_drive(self, acc_id):
        known = self.index.meta("scopes", {}).get(acc_id)
        return None if not known else google.DRIVE_SCOPE in known["scope"].split()


def _mapped(e):
    if e.status == 429 or e.code in google.RATE_REASONS or e.code == "RESOURCE_EXHAUSTED":
        wait = 3600 if e.code in ("dailyLimitExceeded", "quotaExceeded") else 60
        return Retry("Google asked Callsheet to slow down. The scan waits and tries again.", wait)
    if e.status >= 500 or e.status == 0:
        return Retry(str(e), 60)
    return SourceError(str(e), e.status)


class Ctx:
    """What a source step needs: the store, the index, Google access, the clock, a pacing sleep and the settings."""

    def __init__(self, store, index, clock=time.time, sleep=time.sleep, settings=None, stop=None):
        self.store, self.index, self.clock, self.sleep = store, index, clock, sleep
        self.google = GoogleAccess(store, index, clock)
        self.settings = settings or {}
        self.stop = stop   # callable: True when the scanner should stop early (pause, cancel, call time)

    def pace(self, seconds):
        if seconds > 0:
            self.sleep(seconds)

    def stopping(self):
        return bool(self.stop and self.stop())


class Source:
    key = ""
    label = ""
    unit = "items"

    def targets(self, ctx):
        return []

    def estimate(self, ctx, tgt, since):
        return None

    def step(self, ctx, tgt, since, until, cursor):
        return [], None


def _google_accounts(ctx, service):
    out = []
    for a in ctx.store.accounts():
        if a["services"].get(service, True):
            out.append(a)
    return out


# --- Gmail ---------------------------------------------------------------------------------------------------------
def _headers(payload):
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers") or []
            if isinstance(h, dict)}


def people_of(*fields):
    out = []
    for name, addr in email.utils.getaddresses([f for f in fields if f]):
        addr = addr.strip()
        if not addr and not name:
            continue
        out.append(f"{name.strip()} <{addr}>" if name.strip() and addr else (addr or name.strip()))
    return out


def mail_text(msg):
    """The text of a messages.get(format=full) message: text/plain, else the HTML as text, else the snippet;
    quoted history and signatures removed (writer.strip_quoted). Attachments are never read."""
    payload = msg.get("payload") or {}
    found = {"text/plain": None, "text/html": None}
    for p in writer._parts(payload):
        mime = str(p.get("mimeType", "")).lower()
        data = (p.get("body") or {}).get("data")
        if mime in found and found[mime] is None and data and not p.get("filename"):
            found[mime] = writer._decode(data)
    text = found["text/plain"]
    if text is None and found["text/html"] is not None:
        text = writer.html_to_text(found["text/html"])
    if text is None:
        text = html.unescape(msg.get("snippet") or "")
    return writer.strip_quoted(text.replace("\r\n", "\n"), _headers(payload).get("subject")) or ""


def mail_item(msg, acc, area):
    payload = msg.get("payload") or {}
    hd = _headers(payload)
    try:
        ts = int(msg.get("internalDate") or 0) / 1000.0
    except (TypeError, ValueError):
        ts = 0.0
    labels = msg.get("labelIds") or []
    return {"source": "gmail", "account": acc["id"], "source_id": str(msg.get("id")),
            "kind": "email" if "SENT" not in labels else "sent", "thread": str(msg.get("threadId") or ""),
            "title": hd.get("subject") or "(no subject)", "people": people_of(hd.get("from"), hd.get("to"), hd.get("cc")),
            "area": area, "ts": ts, "url": rules.gmail_link(acc["email"], msg.get("threadId") or "", msg.get("id") or ""),
            "snippet": html.unescape(msg.get("snippet") or ""), "body": ix.cap_body(mail_text(msg))}


class GmailSource(Source):
    key, label, unit = "gmail", "Mail", "emails"

    def targets(self, ctx):
        return [target("gmail", a["id"], "Mail · " + a["email"], a["area_id"], self.unit)
                for a in _google_accounts(ctx, "mail")]

    @staticmethod
    def query(since, until):
        q = "-in:chats -in:drafts"
        if since:
            q = f"after:{int(since)} " + q
        if until:
            q = f"before:{int(until)} " + q
        return q

    def estimate(self, ctx, tgt, since):
        prof = ctx.google.call(tgt["account"], google.gmail_profile)
        total = int(prof.get("messagesTotal") or 0) or None
        if not since:
            return total
        res = ctx.google.call(tgt["account"], google.gmail_messages, self.query(since, None), None, 1)
        est = int(res.get("resultSizeEstimate") or 0)
        return min(est, total) if total else est

    def step(self, ctx, tgt, since, until, cursor):
        cur = dict(cursor or {"page": None, "pending": [], "listed": 0, "last": False})
        acc = ctx.store.account(tgt["account"])
        if cur["pending"]:
            items = []
            batch, rest = cur["pending"][:GMAIL_CHUNK], cur["pending"][GMAIL_CHUNK:]
            for mid in batch:
                try:
                    msg = ctx.google.call(tgt["account"], google.gmail_message_full, mid)
                except SourceError as e:
                    if e.status == 404:   # deleted since it was listed
                        continue
                    raise
                items.append(mail_item(msg, acc, tgt["area"]))
                ctx.pace(GMAIL_PACE_S)
            cur["pending"] = rest
            return items, cur
        if cur["last"]:
            return [], None
        res = ctx.google.call(tgt["account"], google.gmail_messages, self.query(since, until), cur["page"], GMAIL_PAGE)
        ids = [str(m["id"]) for m in res.get("messages") or [] if isinstance(m, dict) and m.get("id")]
        cur["pending"] = ids
        cur["listed"] += len(ids)
        cur["page"] = res.get("nextPageToken")
        cur["last"] = not cur["page"]
        return [], cur if (ids or not cur["last"]) else None


# --- Calendar ------------------------------------------------------------------------------------------------------
def _event_ts(ev, tz):
    st = ev.get("start") or {}
    if st.get("dateTime"):
        try:
            return dt.datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    if st.get("date"):
        try:
            return dt.datetime.combine(dt.date.fromisoformat(st["date"]), dt.time(0, 0), tzinfo=tz).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def event_item(ev, acc, cal_id, area, tz):
    people = []
    org = ev.get("organizer") or {}
    for p in [org] + [a for a in ev.get("attendees") or [] if isinstance(a, dict)]:
        if p.get("email") or p.get("displayName"):
            people.append(f"{p.get('displayName')} <{p.get('email')}>" if p.get("displayName") and p.get("email")
                          else p.get("email") or p.get("displayName"))
    desc = str(ev.get("description") or "")
    if "<" in desc and ">" in desc:
        desc = writer.html_to_text(desc)
    body = "\n".join(x for x in (("Where: " + ev["location"]) if ev.get("location") else "", desc) if x)
    return {"source": "calendar", "account": acc["id"], "source_id": f"{cal_id}:{ev.get('id')}", "kind": "event",
            "thread": str(ev.get("recurringEventId") or ""), "title": ev.get("summary") or "(no title)",
            "people": people, "area": area, "ts": _event_ts(ev, tz), "url": ev.get("htmlLink") or "", "body": body}


class CalendarSource(Source):
    key, label, unit = "calendar", "Calendars", "events"

    def targets(self, ctx):
        return [target("calendar", a["id"], "Calendars · " + a["email"], a["area_id"], self.unit)
                for a in _google_accounts(ctx, "calendar") if any(c.get("selected") for c in a["calendars"])]

    @staticmethod
    def _cals(ctx, acc_id):
        return [c["id"] for c in ctx.store.account(acc_id)["calendars"] if c.get("selected")]

    def estimate(self, ctx, tgt, since):
        n = 0
        t_min, t_max = iso(since or ALL_TIME), iso(ctx.clock() + 365 * 86400)
        for cal in self._cals(ctx, tgt["account"]):
            page = None
            for _ in range(2):
                res = ctx.google.call(tgt["account"], google.calendar_events_page, cal, t_min, t_max, page, 2500,
                                      "nextPageToken,items(id)")
                n += len(res.get("items") or [])
                page = res.get("nextPageToken")
                if not page:
                    break
        return n

    def step(self, ctx, tgt, since, until, cursor):
        cur = dict(cursor or {"cals": self._cals(ctx, tgt["account"]), "page": None})
        if not cur["cals"]:
            return [], None
        acc = ctx.store.account(tgt["account"])
        cal = cur["cals"][0]
        t_min, t_max = iso(since or ALL_TIME), iso(until or ctx.clock() + 365 * 86400)
        res = ctx.google.call(tgt["account"], google.calendar_events_page, cal, t_min, t_max, cur["page"], CAL_PAGE)
        tz = rules.tzinfo(ctx.store.settings())
        items = [event_item(ev, acc, cal, tgt["area"], tz) for ev in res.get("items") or []
                 if isinstance(ev, dict) and ev.get("id") and ev.get("status") != "cancelled"]
        cur["page"] = res.get("nextPageToken")
        if not cur["page"]:
            cur["cals"] = cur["cals"][1:]
        return items, cur if cur["cals"] else None


# --- Slack ---------------------------------------------------------------------------------------------------------
def _slack_call(cl, name, **params):
    try:
        return cl.get(name, **params)
    except slack.Budget:
        raise Retry("Slack asked Callsheet to slow down. The scan waits and tries again.", 60) from None
    except slack.SlackError as e:
        if e.code in slack.RECONNECT_CODES or e.code == "missing_scope":
            raise SourceError(str(e)) from None
        if e.code in ("channel_not_found", "thread_not_found", "not_in_channel"):
            return None
        raise Retry(str(e), 60) from None


class SlackSource(Source):
    key, label, unit = "slack", "Slack", "messages"

    def targets(self, ctx):
        return [target("slack", w["id"], "Slack · " + (w.get("team") or w.get("domain") or w["id"]),
                       w.get("area_id"), self.unit) for w in ctx.store.slack_workspaces()]

    def _client(self, ctx, ws):
        return slack.Client(ws["token"], clock=ctx.clock, sleep=ctx.sleep, max_calls=slack.MAX_CALLS)

    def estimate(self, ctx, tgt, since):
        return None   # Slack has no cheap count; known while reading

    def _users(self, ctx, cl, ws_id):
        names = ctx.index.meta("slack_users:" + ws_id)
        if names is not None:
            return names
        known = (ctx.store.slack_secret(ws_id).get("state") or {}).get("users") or {}
        names = {k: str(v.get("name") if isinstance(v, dict) else v)[:80] for k, v in known.items() if v}
        cursor = None
        for _ in range(slack.MAX_PAGES):
            try:
                res = _slack_call(cl, "users.list", limit=200, cursor=cursor) or {}
            except Retry:   # names are a nicety: the ones the brief already knows are used
                break
            for u in res.get("members") or []:
                if isinstance(u, dict) and u.get("id"):
                    prof = u.get("profile") or {}
                    names[u["id"]] = str(prof.get("real_name") or u.get("real_name") or u.get("name") or u["id"])[:80]
            cursor = (res.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        ctx.index.set_meta("slack_users:" + ws_id, names)
        return names

    def step(self, ctx, tgt, since, until, cursor):
        ws = ctx.store.slack_secret(tgt["account"])
        cl = self._client(ctx, ws)
        if cursor is None:
            convs = [c for c in slack.list_conversations(_Guard(cl)) if c["id"] not in set(ws.get("ignored") or [])]
            names = self._users(ctx, cl, ws["id"])
            return [], {"convs": [[c["id"], c["kind"], c["name"], c.get("user") or ""] for c in convs],
                        "cur": None, "threads": [], "nthreads": 0, "names": len(names)}
        cur = dict(cursor)
        names = ctx.index.meta("slack_users:" + ws["id"], {})
        if cur["convs"]:
            cid, kind, cname, other = cur["convs"][0]
            res = _slack_call(cl, "conversations.history", channel=cid, limit=SLACK_PAGE, cursor=cur["cur"],
                              oldest=f"{since:.6f}" if since else None, latest=f"{until:.6f}" if until else None)
            ctx.pace(SLACK_PACE_S)
            msgs = (res or {}).get("messages") or []
            items = []
            for m in msgs:
                if not slack.is_real(m):
                    continue
                items.append(self._item(ws, tgt, cid, kind, cname, other, m, names))
                if m.get("reply_count") and m.get("thread_ts") == m.get("ts") and \
                        cur["nthreads"] < SLACK_MAX_THREADS:
                    cur["threads"].append([cid, kind, cname, other, m["ts"]])
                    cur["nthreads"] += 1
            nxt = ((res or {}).get("response_metadata") or {}).get("next_cursor") if (res or {}).get("has_more") else None
            cur["cur"] = nxt or None
            if not nxt:
                cur["convs"] = cur["convs"][1:]
            return items, cur
        if cur["threads"]:
            cid, kind, cname, other, root = cur["threads"][0]
            res = _slack_call(cl, "conversations.replies", channel=cid, ts=root, limit=SLACK_PAGE, cursor=cur.get("tcur"))
            ctx.pace(SLACK_PACE_S)
            items = [self._item(ws, tgt, cid, kind, cname, other, m, names) for m in (res or {}).get("messages") or []
                     if slack.is_real(m) and m.get("ts") != root]
            nxt = ((res or {}).get("response_metadata") or {}).get("next_cursor") if (res or {}).get("has_more") else None
            cur["tcur"] = nxt or None
            if not nxt:
                cur["threads"] = cur["threads"][1:]
            return items, cur if (cur["threads"] or cur["convs"]) else None
        return [], None

    @staticmethod
    def _item(ws, tgt, cid, kind, cname, other, m, names):
        who = names.get(m.get("user"), m.get("user") or "")
        if kind == "dm":
            title = "DM with " + names.get(other, other or "someone")
        elif kind == "group":
            title = "Group DM " + cname
        else:
            title = "#" + cname
        people = [who] + ([names.get(other, other)] if kind == "dm" and other and other != m.get("user") else [])
        root = m.get("thread_ts") if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else None
        return {"source": "slack", "account": ws["id"], "source_id": f"{cid}:{m['ts']}", "kind": "message",
                "thread": f"{cid}:{m.get('thread_ts')}" if m.get("thread_ts") else "", "title": title,
                "people": people, "area": tgt["area"], "ts": slack._ts(m.get("ts")),
                "url": rules.slack_link(ws.get("domain"), cid, m["ts"], root),
                "body": rules.slack_text(m.get("text") or "", {k: {"name": v} for k, v in names.items()})}


class _Guard:
    """slack.list_conversations with the scan's error mapping."""

    def __init__(self, cl):
        self.cl = cl

    def get(self, name, **params):
        return _slack_call(self.cl, name, **params) or {}


# --- Drive ---------------------------------------------------------------------------------------------------------
def drive_query(since):
    types = " or ".join(f"mimeType = '{m}'" for m in DRIVE_TYPES)
    q = f"trashed = false and ({types})"
    if since:
        q = f"modifiedTime > '{iso(since)}' and " + q
    return q


def _drive_ts(f):
    try:
        return dt.datetime.fromisoformat(str(f.get("modifiedTime")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class DriveSource(Source):
    key, label, unit = "drive", "Google Drive", "files"

    def targets(self, ctx):
        out = []
        for a in ctx.store.accounts():
            has = ctx.google.cached_drive(a["id"])
            out.append(target("drive", a["id"], "Drive · " + a["email"], a["area_id"], self.unit,
                              None if has else "Grant Drive access first"))
        return out

    def estimate(self, ctx, tgt, since):
        n, page = 0, None
        for _ in range(3):
            res = ctx.google.call(tgt["account"], google.drive_files, drive_query(since), page, 1000,
                                  "nextPageToken,files(id)")
            n += len(res.get("files") or [])
            page = res.get("nextPageToken")
            if not page:
                break
        return n

    def step(self, ctx, tgt, since, until, cursor):
        cur = dict(cursor or {"page": None, "pending": [], "last": False})
        if cur["pending"]:
            acc = ctx.store.account(tgt["account"])
            items = []
            for f in cur["pending"][:DRIVE_CHUNK]:
                items.append(self._item(ctx, acc, tgt, f))
                ctx.pace(DRIVE_PACE_S)
            cur["pending"] = cur["pending"][DRIVE_CHUNK:]
            return items, cur if (cur["pending"] or not cur["last"]) else None
        if cur["last"]:
            return [], None
        res = ctx.google.call(tgt["account"], google.drive_files, drive_query(since), cur["page"], DRIVE_PAGE)
        files = [{k: f.get(k) for k in ("id", "name", "mimeType", "modifiedTime", "owners", "webViewLink", "size")}
                 for f in res.get("files") or [] if isinstance(f, dict) and f.get("id")]
        cur["pending"] = files
        cur["page"] = res.get("nextPageToken")
        cur["last"] = not cur["page"]
        return [], cur if (files or not cur["last"]) else None

    @staticmethod
    def _item(ctx, acc, tgt, f):
        kind, export = DRIVE_TYPES.get(f.get("mimeType"), ("file", None))
        text = ""
        try:
            if export:
                text = ctx.google.call(tgt["account"], google.drive_export, f["id"], export)
            elif int(f.get("size") or 0) <= DRIVE_MAX_MEDIA:
                text = ctx.google.call(tgt["account"], google.drive_media, f["id"])
        except SourceError:
            text = ""   # too large to export, or not readable: indexed by name, owners and date only
        owners = [f"{o.get('displayName')} <{o.get('emailAddress')}>" if o.get("displayName") and o.get("emailAddress")
                  else o.get("emailAddress") or o.get("displayName") for o in f.get("owners") or [] if isinstance(o, dict)]
        return {"source": "drive", "account": acc["id"], "source_id": f["id"], "kind": kind, "thread": "",
                "title": f.get("name") or "(untitled)", "people": [o for o in owners if o], "area": tgt["area"],
                "ts": _drive_ts(f), "url": f.get("webViewLink") or "", "body": text}


# --- Obsidian ------------------------------------------------------------------------------------------------------
def vault_roots():
    raw = os.environ.get(VAULT_ROOTS_ENV) or "/share:/media"
    return [r for r in raw.split(":") if r]


def check_vault(path):
    """The vault folder as a resolved path, or InputError with a plain sentence. It must be under /share or /media
    (mapped read-only into the app), exist, be a folder and be readable."""
    if not isinstance(path, str) or not path.strip():
        raise InputError("Type the vault folder, for example /share/Obsidian/My vault.")
    path = path.strip()
    if len(path) > 500 or "\x00" in path:
        raise InputError("That folder name is too long.")
    if not path.startswith("/"):
        raise InputError("Type the full folder path, starting with /share or /media.")
    real = os.path.realpath(path)
    roots = [os.path.realpath(r) for r in vault_roots()]
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
        raise InputError("The vault folder must be inside /share or /media (Home Assistant's shared folders).")
    if not os.path.exists(real):
        raise InputError("Callsheet can't find that folder. Check the path and that the vault is synced there.")
    if not os.path.isdir(real):
        raise InputError("That's a file, not a folder. Type the vault folder.")
    if not os.access(real, os.R_OK | os.X_OK):
        raise InputError("Callsheet can't read that folder. Check its permissions.")
    return real


def list_notes(root):
    """Relative paths of the vault's .md files, sorted; dot folders (.obsidian, .trash, .git) and other files
    (attachments) are skipped; symlinks are not followed."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            out.append(os.path.relpath(full, root))
            if len(out) >= NOTE_MAX_FILES:
                return out
    return out


_FM_LIST = re.compile(r"^\s*-\s+(.*)$")
_LINK = re.compile(r"(!?)\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_TAG = re.compile(r"(?<![\w/#&])#([A-Za-z][\w/-]{0,60})")
_ATTACH = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".mp3", ".mp4", ".mov", ".wav", ".zip", ".excalidraw")


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def front_matter(text):
    """(fields, rest) for a note starting with a '---' block; a small YAML subset: `key: value`, `key: [a, b]`
    and `key:` followed by `- item` lines. Anything else is ignored."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, min(len(lines), 300)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    fields, key = {}, None
    for ln in lines[1:end]:
        m = _FM_LIST.match(ln)
        if m and key:
            if not isinstance(fields.get(key), list):
                fields[key] = []
            fields[key].append(_unquote(m.group(1)))
            continue
        if ":" in ln and not ln.startswith((" ", "\t")):
            k, v = ln.split(":", 1)
            key = k.strip().lower()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                fields[key] = [_unquote(x) for x in v[1:-1].split(",") if x.strip()]
            elif v:
                fields[key] = _unquote(v)
            else:
                fields[key] = []
    return fields, "\n".join(lines[end + 1:])


def _as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
    return []


def parse_note(text, rel, mtime, areas=None):
    """An index item for one note: title (front matter title, else the first # heading, else the file name), tags
    (front matter and #tags), [[links]] as people/topic hints, the date (front matter date/created, else the file's
    modified time), the area (front matter `area` matching an area's name), the body without front matter."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    fm, body = front_matter(text)
    title = fm.get("title") if isinstance(fm.get("title"), str) else ""
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.M)
        title = m.group(1).strip() if m else pathlib.PurePath(rel).stem
    tags = [t.lstrip("#") for t in _as_list(fm.get("tags") or fm.get("tag"))]
    tags += _TAG.findall(re.sub(r"```.*?```", " ", body, flags=re.S))
    links = []
    for bang, name in _LINK.findall(body):
        name = name.strip()
        if bang and name.lower().endswith(_ATTACH):
            continue
        if name and not name.lower().endswith(_ATTACH):
            links.append(name)
    aliases = fm.get("aliases") if isinstance(fm.get("aliases"), list) else _as_list(fm.get("aliases"))
    hints = links + _as_list(fm.get("people")) + aliases
    hints += ["#" + t for t in dict.fromkeys(tags)]
    ts = mtime
    for k in ("date", "created", "updated"):
        v = fm.get(k)
        if isinstance(v, str) and v:
            try:
                d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                ts = (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
                break
            except ValueError:
                try:
                    ts = dt.datetime.combine(dt.date.fromisoformat(v[:10]), dt.time(12, 0),
                                             tzinfo=dt.timezone.utc).timestamp()
                    break
                except ValueError:
                    pass
    area = ""
    if isinstance(fm.get("area"), str) and areas:
        want = fm["area"].strip().lower()
        area = next((a["id"] for a in areas if a["name"].lower() == want or a["id"] == want), "")
    return {"source": "obsidian", "account": "vault", "source_id": rel, "kind": "note", "thread": "",
            "title": title, "people": hints, "area": area, "ts": ts, "url": rel, "body": body}


class ObsidianSource(Source):
    key, label, unit = "obsidian", "Obsidian vault", "notes"

    def targets(self, ctx):
        path = ctx.settings.get("vault") or ""
        if not path:
            return []
        try:
            check_vault(path)
            blocked = None
        except InputError as e:
            blocked = str(e)
        return [target("obsidian", "vault", "Obsidian · " + os.path.basename(path.rstrip("/")), "", self.unit, blocked)]

    def estimate(self, ctx, tgt, since):
        try:
            return len(list_notes(check_vault(ctx.settings.get("vault") or "")))
        except InputError:
            return None

    def step(self, ctx, tgt, since, until, cursor):
        try:
            root = check_vault(ctx.settings.get("vault") or "")
        except InputError as e:
            raise SourceError(str(e)) from None
        if cursor is None:
            files = list_notes(root)
            return [], {"files": files, "all": files}
        cur = dict(cursor)
        areas = ctx.store.areas()
        items = []
        for rel in cur["files"][:NOTE_CHUNK]:
            full = os.path.join(root, rel)
            try:
                st = os.stat(full, follow_symlinks=False)
                if st.st_size > NOTE_MAX_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(NOTE_MAX_BYTES)
            except OSError:
                continue
            items.append(parse_note(text, rel, st.st_mtime, areas))
        cur["files"] = cur["files"][NOTE_CHUNK:]
        if not cur["files"]:
            # notes deleted from the vault since the last read leave the index too
            ctx.index.delete_missing("obsidian", "vault", set(cur["all"]))
            return items, None
        return items, cur


def all_sources():
    """The sources in scan order. Signal plugs in here as SignalSource()."""
    return [GmailSource(), CalendarSource(), SlackSource(), DriveSource(), ObsidianSource()]
