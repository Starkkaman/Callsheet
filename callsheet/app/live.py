"""Rework stage 1: live readers into the message store (msgstore.py, docs/messages.md).

Background threads, one per source kind, never on the web server's threads:
- Gmail, per account: a first-run backfill (msgstore.backfill_days, newest first, resumable, paced), then every
  2 minutes users.history.list from the saved historyId (a time-window list when the id has expired). Sent mail is
  stored with direction "out". Bodies from messages.get format=full, text/plain preferred.
- Calendar, per account: every 15 minutes, the selected calendars from 30 days ago on (cancellations kept).
- Slack, per workspace: every 3 minutes, conversations.history for the conversations Callsheet already reads (not the
  ignored ones), plus replies of active threads; the owner's own messages are direction "out". A request cap per
  pass keeps it under Slack's limits; what is left is read next time.
- Signal: signal-cli already receives continuously (signalcli.py); `SignalIngest.on_envelope` stores each message
  (incoming, the owner's own from his phone as "out", group id and name, attachments as name and type only).

Every pass records freshness (last attempt, last success, newest message, consecutive errors in plain words,
backlog); `Live.status` turns it into the Sources panel's rows, with a warning when a source stops advancing.
Read-only: only the GETs in google.py's and slack.py's allowlists, and signal-cli's receive-only methods.
"""
import datetime as dt
import email.utils
import html
import json
import sys
import threading
import time
import urllib.error
import zoneinfo

import google
import htmltext
import msgstore
import rules
import signalcli
import slack
import sources
import writer

INTERVAL = {"gmail": 120, "slack": 180, "calendar": 900}
NAMES = {"gmail": "Gmail", "calendar": "Calendar", "slack": "Slack", "signal": "Signal"}
BACKLOG_TICK_S = {"gmail": 15, "calendar": 60, "slack": 60}   # the next chunk of a backfill or a capped pass
BACKOFF_MAX_S = 3600
ERRORS_WARN = 3                # consecutive failures before the panel warns
LATE_FACTOR = 3                # "behind" after this many intervals without a successful check
TICK_S = 5.0
GMAIL_PAGE = 100
GMAIL_PACE_S = 0.1             # between messages.get calls (5 quota units each; far under 250 units/s)
GMAIL_HISTORY_PAGES = 20
GMAIL_FETCH_MAX = 300          # messages fetched per pass (the rest next pass)
GMAIL_SKIP = {"DRAFT", "SPAM"}
CAL_PAST_DAYS = 30
CAL_FUTURE_DAYS = 400
CAL_FULL_EVERY_S = 86400
CAL_PAGES = 10
SLACK_FIRST_DAYS = 14          # how far back Slack is read the first time
SLACK_CALLS = 60               # requests per workspace per pass
SLACK_CONVS_EVERY_S = 3600
SLACK_USERS_EVERY_S = 86400
SLACK_THREAD_DAYS = 3          # a thread stays "active" this long after its newest reply
SLACK_MAX_THREADS = 20
INDEX_BEHIND_S = 6 * 3600      # Chat's search (the old index) is "behind" after this
SIGNAL_TOUCH_S = 60


def log(msg):
    sys.stderr.write(f"Callsheet live: {msg}\n")


def plain_error(e):
    """An exception as one plain sentence (never a token or a request body)."""
    if isinstance(e, (google.GoogleError, sources.SourceError, sources.Retry, slack.SlackError)):
        return str(e)[:300] or "The service answered with an error."
    if isinstance(e, (urllib.error.URLError, OSError, TimeoutError)):
        return "Couldn't reach the service (network)."
    return f"Something went wrong ({type(e).__name__})."


def ago(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 48 * 3600:
        h = s // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    return f"{s // 86400} days ago"


def _tz(store):
    try:
        return zoneinfo.ZoneInfo(store.settings().get("timezone") or "UTC")
    except Exception:  # noqa: BLE001
        return dt.timezone.utc


def when(ts, now, tz):
    """'2 min ago' within a day, else the day ('Tue Nov 3')."""
    if now - ts < 86400:
        return ago(now - ts)
    d = dt.datetime.fromtimestamp(ts, tz)
    return d.strftime("%a %b ") + str(d.day)


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Google access ----------------------------------------------------------------------------------------------------
class Tokens(sources.GoogleAccess):
    """Access tokens like the index's (saved back to the store), without the index's scope cache."""

    def __init__(self, store, clock=time.time):
        super().__init__(store, None, clock)

    def _save_scopes(self, acc_id, scope):
        pass

    def run(self, acc_id, fn, *args, **kw):
        """fn(token, ...), refreshed once on 401. Google's own errors (GoogleError) pass through."""
        try:
            return fn(self.token(acc_id), *args, **kw)
        except google.GoogleError as e:
            if e.status != 401:
                raise
        return fn(self.token(acc_id, force=True), *args, **kw)


class GoogleApi:
    """The reads the live readers use (tests swap in a fake)."""
    profile = staticmethod(google.gmail_profile)
    history = staticmethod(google.gmail_history)
    messages = staticmethod(google.gmail_messages)
    message = staticmethod(google.gmail_message_full)
    events = staticmethod(google.calendar_events_changes)


# --- Gmail -------------------------------------------------------------------------------------------------------
def _addresses(value):
    return [(n.strip(), a.strip()) for n, a in email.utils.getaddresses([value]) if n.strip() or a.strip()] \
        if value else []


def gmail_text(payload, snippet=""):
    """(text, attachments): text/plain preferred, else the HTML as text (htmltext), else the snippet. Quoted history
    is kept. Attachments: name, type and size only (never read)."""
    found, files = {"text/plain": None, "text/html": None}, []
    for p in writer._parts(payload or {}):  # noqa: SLF001
        mime = str(p.get("mimeType", "")).lower()
        body = p.get("body") or {}
        if p.get("filename"):
            files.append({"name": str(p["filename"])[:200], "type": mime[:100], "size": int(body.get("size") or 0)})
            continue
        if mime in found and found[mime] is None and body.get("data"):
            found[mime] = writer._decode(body["data"])  # noqa: SLF001
    text = found["text/plain"]
    if text is None and found["text/html"] is not None:
        text = htmltext.html_text(found["text/html"])[0]
    if text is None:
        text = html.unescape(snippet or "")
    return text.replace("\r\n", "\n").strip(), files


def gmail_record(msg, acc):
    """One messages.get(format=full) message -> a store record. Sent mail (SENT label, or from the account itself)
    is direction "out"."""
    payload = msg.get("payload") or {}
    hd = sources._headers(payload)  # noqa: SLF001
    labels = [str(x) for x in msg.get("labelIds") or []]
    frm = _addresses(hd.get("from", ""))
    sender_name, sender = frm[0] if frm else ("", "")
    out = "SENT" in labels or (sender and sender.lower() == str(acc.get("email") or "").lower())
    recips = [{"name": n, "address": a, "kind": k} for k in ("to", "cc", "bcc") for n, a in _addresses(hd.get(k, ""))]
    text, files = gmail_text(payload, msg.get("snippet"))
    try:
        ts = int(msg.get("internalDate") or 0) / 1000.0
    except (TypeError, ValueError):
        ts = 0.0
    tid, mid = str(msg.get("threadId") or ""), str(msg.get("id"))
    return {"source": "gmail", "account": acc["id"], "msg_id": mid, "thread": tid, "direction": "out" if out else "in",
            "sender": sender, "sender_name": sender_name, "recipients": recips, "ts": ts,
            "subject": hd.get("subject", ""), "text": text, "labels": labels, "channel": "",
            "link": rules.gmail_link(acc.get("email") or "", tid, mid), "attachments": files}


class GmailReader:
    key = "gmail"

    def __init__(self, store, ms, tokens, api=GoogleApi, clock=time.time, sleep=time.sleep):
        self.store, self.ms, self.tokens, self.api, self.clock, self.sleep = store, ms, tokens, api, clock, sleep

    def targets(self):
        return [(a["id"], a.get("email") or a["id"]) for a in self.store.accounts() if a["services"].get("mail", True)]

    def _acc(self, aid):
        return next((a for a in self.store.accounts() if a["id"] == aid), {"id": aid, "email": ""})

    def _call(self, aid, fn, *args, **kw):
        return self.tokens.run(aid, fn, *args, **kw)

    def _fetch(self, acc, ids, limit=GMAIL_FETCH_MAX):
        """Read and store the messages not stored yet. Returns (new, newest_ts, left)."""
        todo = [i for i in dict.fromkeys(ids) if not self.ms.has_message("gmail", acc["id"], i)]
        recs = []
        for i in todo[:limit]:
            try:
                msg = self._call(acc["id"], self.api.message, i)
            except google.GoogleError as e:
                if e.status == 404:   # deleted before it was read
                    continue
                raise
            if not set(msg.get("labelIds") or []) & GMAIL_SKIP:
                recs.append(gmail_record(msg, acc))
            if len(recs) >= 25:
                self._store(recs)
                recs = []
            if GMAIL_PACE_S:
                self.sleep(GMAIL_PACE_S)
        self._store(recs)
        return self._new, self._newest, max(0, len(todo) - limit)

    def _store(self, recs):
        if recs:
            res = self.ms.upsert_messages(recs)
            self._new += res["new"]
            if res["newest"]:
                self._newest = max(self._newest or 0, res["newest"])

    def tick(self, aid):
        """One pass for one account: {"new", "newest", "backlog"}."""
        acc = self._acc(aid)
        self._new, self._newest = 0, None
        hist = self.ms.cursor("gmail", aid, "history")
        bf = self.ms.cursor("gmail", aid, "backfill")
        left = 0
        if hist is None:   # first run: remember where "now" is, then read back
            prof = self._call(aid, self.api.profile)
            self.ms.set_cursor("gmail", aid, "history", {"id": str(prof.get("historyId") or "")})
            days = self.ms.backfill_days()
            bf = {"done": False, "page": None, "days": days, "since": self.clock() - days * 86400, "total": None,
                  "read": 0}
            self.ms.set_cursor("gmail", aid, "backfill", bf)
        else:
            left = self._incremental(acc, hist)
        if bf and not bf.get("done"):
            bf = self._backfill(acc, bf)
        backlog = left + (max(0, (bf.get("total") or 0) - bf.get("read", 0)) if bf and not bf.get("done") else 0)
        if bf and not bf.get("done") and not backlog:
            backlog = 1
        return {"new": self._new, "newest": self._newest, "backlog": backlog}

    def _incremental(self, acc, hist):
        aid, page, start = acc["id"], None, hist.get("id")
        if not start:
            return self._recover(acc)
        added, deleted, labels, latest = [], [], {}, start
        for _ in range(GMAIL_HISTORY_PAGES):
            try:
                res = self._call(aid, self.api.history, start, page)
            except google.GoogleError as e:
                if e.status == 404:   # the history id expired (about a week): read by time instead
                    return self._recover(acc)
                raise
            latest = str(res.get("historyId") or latest)
            for h in res.get("history") or []:
                added += [x["message"]["id"] for x in h.get("messagesAdded") or [] if (x.get("message") or {}).get("id")
                          and not set(x["message"].get("labelIds") or []) & GMAIL_SKIP]
                deleted += [x["message"]["id"] for x in h.get("messagesDeleted") or [] if (x.get("message") or {}).get("id")]
                for k in ("labelsAdded", "labelsRemoved"):
                    for x in h.get(k) or []:
                        m = x.get("message") or {}
                        if m.get("id"):
                            labels[m["id"]] = m.get("labelIds") or []
            page = res.get("nextPageToken")
            if not page:
                break
        _, _, left = self._fetch(acc, added)
        for mid in deleted:
            self.ms.mark_deleted("gmail", aid, mid)
        for mid, lb in labels.items():
            self.ms.set_labels("gmail", aid, mid, lb)
        if not left:   # move on only when everything up to `latest` is stored
            self.ms.set_cursor("gmail", aid, "history", {"id": latest})
        return left

    def _recover(self, acc):
        """The history id expired: list by time since the newest stored mail (a day of overlap), then start history
        again from now."""
        aid = acc["id"]
        prof = self._call(aid, self.api.profile)
        newest = self.ms.newest_ts("gmail", aid) or (self.clock() - 2 * 86400)
        q, page, ids = f"after:{int(newest - 86400)}", None, []
        for _ in range(10):
            res = self._call(aid, self.api.messages, q, page)
            ids += [m["id"] for m in res.get("messages") or [] if m.get("id")]
            page = res.get("nextPageToken")
            if not page:
                break
        _, _, left = self._fetch(acc, ids, limit=len(ids))
        self.ms.set_cursor("gmail", aid, "history", {"id": str(prof.get("historyId") or ""), "recovered": self.clock()})
        log(f"Gmail history expired for one account; read {self._new} new messages by time instead")
        return left

    def _backfill(self, acc, bf):
        """One page of the first-run read-back (newest first); resumable from the saved page token."""
        aid = acc["id"]
        res = self._call(aid, self.api.messages, f"after:{int(bf['since'])}", bf.get("page"))
        ids = [m["id"] for m in res.get("messages") or [] if m.get("id")]
        if bf.get("total") is None:
            bf["total"] = int(res.get("resultSizeEstimate") or len(ids))
        self._fetch(acc, ids, limit=len(ids))
        bf["read"] = bf.get("read", 0) + len(ids)
        bf["page"] = res.get("nextPageToken")
        if not bf["page"]:
            bf.update(done=True, finished=self.clock())
            bf["total"] = bf["read"]
        self.ms.set_cursor("gmail", aid, "backfill", bf)
        return bf


# --- Calendar ----------------------------------------------------------------------------------------------------
def event_record(ev, aid, cal_id, tz, old=None):
    """One events.list item -> a store record. A cancelled instance (Google sends only id and status) keeps the stored
    fields and changes the status."""
    st, en = ev.get("start") or {}, ev.get("end") or {}
    if ev.get("status") == "cancelled" and not st and old:
        rec = {k: old[k] for k in ("ical_uid", "start", "end", "tz", "start_ts", "end_ts", "all_day", "title",
                                   "location", "link")}
        rec["attendees"] = json.loads(old["attendees"] or "[]")
        rec.update(account=aid, calendar=cal_id, event_id=str(ev.get("id")), status="cancelled",
                   updated=str(ev.get("updated") or old["updated"]))
        return rec
    end_ts = sources._event_ts({"start": en}, tz) or None  # noqa: SLF001
    return {"account": aid, "calendar": cal_id, "event_id": str(ev.get("id")), "ical_uid": ev.get("iCalUID") or "",
            "start": st.get("dateTime") or st.get("date") or "", "end": en.get("dateTime") or en.get("date") or "",
            "tz": st.get("timeZone") or "", "start_ts": sources._event_ts(ev, tz) or None, "end_ts": end_ts,  # noqa: SLF001
            "all_day": bool(st.get("date") and not st.get("dateTime")), "title": ev.get("summary") or "",
            "location": ev.get("location") or "",
            "attendees": [{"name": a.get("displayName") or "", "address": a.get("email") or "",
                           "response": a.get("responseStatus") or "", "self": bool(a.get("self"))}
                          for a in ev.get("attendees") or [] if isinstance(a, dict)][:200],
            "status": ev.get("status") or "confirmed", "updated": ev.get("updated") or "",
            "link": ev.get("htmlLink") or ""}


class CalendarReader:
    key = "calendar"

    def __init__(self, store, ms, tokens, api=GoogleApi, clock=time.time):
        self.store, self.ms, self.tokens, self.api, self.clock = store, ms, tokens, api, clock

    def targets(self):
        return [(a["id"], a.get("email") or a["id"]) for a in self.store.accounts()
                if a["services"].get("calendar", True) and any(c.get("selected") for c in a.get("calendars") or [])]

    def tick(self, aid):
        acc = next((a for a in self.store.accounts() if a["id"] == aid), None)
        if acc is None:
            return {"new": 0, "newest": None, "backlog": 0}
        now, tz, new = self.clock(), _tz(self.store), 0
        for cal in [c for c in acc.get("calendars") or [] if c.get("selected")]:
            cid = str(cal["id"])
            cur = self.ms.cursor("calendar", aid, "cal:" + cid) or {}
            full = not cur.get("updated_min") or now - cur.get("full_at", 0) > CAL_FULL_EVERY_S
            page, recs = None, []
            for _ in range(CAL_PAGES):
                res = self.tokens.run(aid, self.api.events, cid, _iso(now - CAL_PAST_DAYS * 86400),
                                      _iso(now + CAL_FUTURE_DAYS * 86400), None if full else cur["updated_min"], page)
                for ev in res.get("items") or []:
                    if isinstance(ev, dict) and ev.get("id"):
                        old = self.ms.event(aid, cid, str(ev["id"])) if ev.get("status") == "cancelled" else None
                        if ev.get("status") == "cancelled" and not ev.get("start") and old is None:
                            continue   # a cancellation of something never stored
                        recs.append(event_record(ev, aid, cid, tz, old))
                page = res.get("nextPageToken")
                if not page:
                    break
            res = self.ms.upsert_events(recs)
            new += res["new"] + res["updated"]
            self.ms.set_cursor("calendar", aid, "cal:" + cid, {"updated_min": _iso(now - 300),
                                                              "full_at": now if full else cur.get("full_at", now)})
        return {"new": new, "newest": now if new else None, "backlog": 0}


# --- Slack -------------------------------------------------------------------------------------------------------
def slack_record(ws, cid, kind, cname, other, m, names, me):
    uid = m.get("user") or ""
    if kind == "dm":
        channel = "DM with " + names.get(other, other or "someone")
        recips = [{"name": names.get(other, ""), "address": other}] if other else []
    elif kind == "group":
        channel, recips = "Group DM " + cname, [{"name": cname, "address": cid}]
    else:
        channel, recips = "#" + cname, [{"name": "#" + cname, "address": cid}]
    root = m.get("thread_ts")
    files = [{"name": str(f.get("name") or f.get("title") or "")[:200], "type": str(f.get("filetype") or "")[:40]}
             for f in m.get("files") or [] if isinstance(f, dict)]
    return {"source": "slack", "account": ws["id"], "msg_id": f"{cid}:{m['ts']}",
            "thread": f"{cid}:{root}" if root else cid, "direction": "out" if uid and uid == me else "in",
            "sender": uid, "sender_name": names.get(uid, ""), "recipients": recips, "ts": slack._ts(m.get("ts")),  # noqa: SLF001
            "subject": "", "text": rules.slack_text(m.get("text") or "", {k: {"name": v} for k, v in names.items()}),
            "labels": [kind], "channel": channel,
            "link": rules.slack_link(ws.get("domain"), cid, m["ts"], root if root and root != m["ts"] else None),
            "attachments": files}


class SlackReader:
    key = "slack"

    def __init__(self, store, ms, client=None, clock=time.time, sleep=time.sleep):
        self.store, self.ms, self.clock, self.sleep = store, ms, clock, sleep
        self.client = client or (lambda token: slack.Client(token, clock=clock, sleep=sleep, max_calls=SLACK_CALLS))

    def targets(self):
        return [(w["id"], "Slack · " + (w.get("team_name") or w.get("team") or w.get("domain") or w["id"]))
                for w in self.store.slack_workspaces()]

    def _names(self, cl, ws):
        names = {k: str(v.get("name") if isinstance(v, dict) else v)[:80]
                 for k, v in ((ws.get("state") or {}).get("users") or {}).items() if v}
        saved = self.ms.meta("slack_users:" + ws["id"]) or {}
        names.update(saved.get("names") or {})
        if self.clock() - (saved.get("at") or 0) > SLACK_USERS_EVERY_S:
            try:
                res = cl.get("users.list", limit=200) or {}
                for u in res.get("members") or []:
                    if isinstance(u, dict) and u.get("id"):
                        prof = u.get("profile") or {}
                        names[u["id"]] = str(prof.get("real_name") or u.get("real_name") or u.get("name") or u["id"])[:80]
                self.ms.set_meta("slack_users:" + ws["id"], {"at": self.clock(), "names": names})
            except (slack.SlackError, slack.Budget):
                pass   # names are a nicety
        return names

    def tick(self, wid):
        ws = self.store.slack_secret(wid)
        cl = self.client(ws["token"])
        me, now = ws.get("user_id") or "", self.clock()
        ignored = set(ws.get("ignored") or [])
        convs = self.ms.cursor("slack", wid, "convs")
        if not convs or now - convs.get("at", 0) > SLACK_CONVS_EVERY_S:
            got = slack.list_conversations(cl)
            convs = {"at": now, "list": [[c["id"], c["kind"], c["name"], c.get("user") or ""] for c in got]}
            self.ms.set_cursor("slack", wid, "convs", convs)
        names = self._names(cl, ws)
        threads = self.ms.cursor("slack", wid, "threads") or {}
        todo = [c for c in convs["list"] if c[0] not in ignored]
        new, newest, left = 0, None, len(todo)
        # the conversation checked longest ago first, so a pass cut short by the request cap is fair
        todo.sort(key=lambda c: (self.ms.cursor("slack", wid, "conv:" + c[0]) or {}).get("checked", 0))
        try:
            for i, (cid, kind, cname, other) in enumerate(todo):
                cc = self.ms.cursor("slack", wid, "conv:" + cid) or {}
                oldest = cc.get("ts") or f"{now - SLACK_FIRST_DAYS * 86400:.6f}"
                recs, top, cursor = [], cc.get("ts"), None
                for _ in range(slack.MAX_PAGES):
                    try:
                        res = cl.get("conversations.history", channel=cid, oldest=oldest, limit=200, cursor=cursor)
                    except slack.SlackError as e:
                        if e.code in ("channel_not_found", "not_in_channel"):
                            res = {}
                        else:
                            raise
                    for m in res.get("messages") or []:
                        if not slack.is_real(m):
                            continue
                        recs.append(slack_record(ws, cid, kind, cname, other, m, names, me))
                        if slack._ts(m["ts"]) > slack._ts(top):  # noqa: SLF001
                            top = m["ts"]
                        if m.get("reply_count") and m.get("thread_ts") == m.get("ts"):
                            threads.setdefault(f"{cid}:{m['ts']}", {"last": m["ts"], "seen": now})
                            threads[f"{cid}:{m['ts']}"]["latest"] = slack._ts(m.get("latest_reply") or m["ts"])  # noqa: SLF001
                    cursor = (res.get("response_metadata") or {}).get("next_cursor") if res.get("has_more") else None
                    if not cursor:
                        break
                n, nt = self._save(recs)
                new, newest = new + n, max(newest or 0, nt or 0) or None
                self.ms.set_cursor("slack", wid, "conv:" + cid, {"ts": top, "checked": now})
                left = len(todo) - i - 1
            n, nt = self._threads(cl, ws, threads, names, me, convs["list"], now)
            new, newest = new + n, max(newest or 0, nt or 0) or None
            left = 0
        except slack.Budget:
            log("Slack request cap reached; the rest is read next pass")
        self.ms.set_cursor("slack", wid, "threads", threads)
        return {"new": new, "newest": newest, "backlog": left}

    def _save(self, recs):
        if not recs:
            return 0, None
        res = self.ms.upsert_messages(recs)
        return res["new"], res["newest"]

    def _threads(self, cl, ws, threads, names, me, convs, now):
        """Replies of active threads (newest reply within SLACK_THREAD_DAYS), newest first, at most SLACK_MAX_THREADS."""
        kinds = {c[0]: c for c in convs}
        for k in [k for k, t in threads.items() if now - max(t.get("latest", 0), slack._ts(t.get("last"))) >  # noqa: SLF001
                  SLACK_THREAD_DAYS * 86400]:
            del threads[k]
        active = sorted(threads.items(), key=lambda kv: -kv[1].get("latest", 0))[:SLACK_MAX_THREADS]
        new, newest = 0, None
        for key, t in active:
            cid, root = key.split(":", 1)
            if cid not in kinds:
                threads.pop(key, None)
                continue
            if t.get("checked") and t.get("latest", 0) <= slack._ts(t.get("last")) and now - t["checked"] < 600:  # noqa: SLF001
                continue   # nothing new seen on its root, and checked within 10 minutes
            _, kind, cname, other = kinds[cid]
            try:
                res = cl.get("conversations.replies", channel=cid, ts=root, oldest=t.get("last") or root, limit=200)
            except slack.SlackError as e:
                if e.code in ("thread_not_found", "channel_not_found", "not_in_channel"):
                    threads.pop(key, None)
                    continue
                raise
            recs = []
            for m in res.get("messages") or []:
                if slack.is_real(m):
                    recs.append(slack_record(ws, cid, kind, cname, other, m, names, me))
                    if slack._ts(m["ts"]) > slack._ts(t.get("last")):  # noqa: SLF001
                        t["last"] = m["ts"]
            t["latest"] = max(t.get("latest", 0), slack._ts(t.get("last")))  # noqa: SLF001
            t["checked"] = now
            n, nt = self._save(recs)
            new, newest = new + n, max(newest or 0, nt or 0) or None
        return new, newest


# --- Signal ------------------------------------------------------------------------------------------------------
def _sig_data(dm, author, author_name, out, peer, peer_name, me):
    gi = dm.get("groupInfo") if isinstance(dm.get("groupInfo"), dict) else None
    if gi and gi.get("groupId"):
        thread, channel, kind = "g:" + str(gi["groupId"]), gi.get("groupName") or "Signal group", "group"
        recips = [{"name": gi.get("groupName") or "", "address": "group:" + str(gi["groupId"])}]
    elif peer:
        thread, channel, kind = "d:" + str(peer), "Signal with " + (peer_name or str(peer)), "dm"
        recips = [{"name": peer_name or "", "address": str(peer)}] if out else \
            [{"name": me.get("name") or "", "address": me.get("number") or "me"}]
    else:
        return None
    rd = dm.get("remoteDelete")
    if isinstance(rd, dict) and isinstance(rd.get("timestamp"), (int, float)):
        return ("delete", f"{author}:{int(rd['timestamp'])}")
    ts = dm.get("timestamp")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    text = signalcli._body(dm, me)[0] if isinstance(dm.get("message"), str) else ""  # noqa: SLF001
    files = [{"name": str(a.get("filename") or "")[:200], "type": str(a.get("contentType") or "")[:100],
              "size": int(a.get("size") or 0)} for a in dm.get("attachments") or [] if isinstance(a, dict)]
    if not text.strip() and not files and not dm.get("sticker"):
        return None   # reactions, receipts, profile keys, group updates: nothing to read
    if dm.get("sticker") and not text:
        text = "[sticker]"
    return ("add", {"source": "signal", "account": "signal", "msg_id": f"{author}:{int(ts)}", "thread": thread,
                    "direction": "out" if out else "in", "sender": "me" if out else str(author),
                    "sender_name": "" if out else (author_name or ""), "recipients": recips, "ts": ts / 1000.0,
                    "subject": "", "text": text, "labels": [kind], "channel": channel, "link": "", "attachments": files})


def signal_action(env, me):
    """One signal-cli envelope -> None | ("add", record) | ("edit", (msg_id, text)) | ("delete", msg_id).
    Handles are the sender's number (else its uuid); the owner's own messages (synced from his phone) are "me"."""
    if not isinstance(env, dict):
        return None
    handle = env.get("sourceNumber") or env.get("sourceUuid") or env.get("source")
    name = env.get("sourceName") if isinstance(env.get("sourceName"), str) else ""
    if isinstance(env.get("dataMessage"), dict):
        if signalcli._is_me(me, env.get("sourceUuid"), env.get("sourceNumber")):  # noqa: SLF001
            return None
        return _sig_data(env["dataMessage"], handle, name, False, handle, name, me)
    if isinstance(env.get("editMessage"), dict):
        e = env["editMessage"]
        act = _sig_data(e.get("dataMessage") or {}, handle, name, False, handle, name, me)
        if act and act[0] == "add" and isinstance(e.get("targetSentTimestamp"), (int, float)):
            return ("edit", (f"{handle}:{int(e['targetSentTimestamp'])}", act[1]["text"]))
        return None
    sync = env.get("syncMessage")
    sent = sync.get("sentMessage") if isinstance(sync, dict) else None
    if isinstance(sent, dict):
        peer = sent.get("destinationNumber") or sent.get("destinationUuid") or sent.get("destination")
        if isinstance(sent.get("editMessage"), dict):
            e = sent["editMessage"]
            act = _sig_data(e.get("dataMessage") or {}, "me", "", True, peer, None, me)
            if act and act[0] == "add" and isinstance(e.get("targetSentTimestamp"), (int, float)):
                return ("edit", (f"me:{int(e['targetSentTimestamp'])}", act[1]["text"]))
            return None
        return _sig_data(sent, "me", "", True, peer, None, me)
    return None


class SignalIngest:
    """Called by signalcli.SignalService for every envelope it receives (its reader thread)."""

    def __init__(self, ms, clock=time.time):
        self.ms, self.clock = ms, clock
        self._touched = 0.0

    def on_envelope(self, env, me):
        now = self.clock()
        act = signal_action(env, me or {})
        new, newest = 0, None
        if act is not None:
            kind, val = act
            if kind == "add":
                res = self.ms.upsert_message(val)
                new, newest = res["new"], res["newest"]
            elif kind == "edit":
                self.ms.edit_message("signal", "signal", val[0], val[1])
            elif kind == "delete":
                self.ms.mark_deleted("signal", "signal", val)
        if new or act is not None or now - self._touched > SIGNAL_TOUCH_S:
            self._touched = now
            self.ms.fresh_ok("signal", "signal", now, new=new, newest_ts=newest)


# --- the manager -------------------------------------------------------------------------------------------------
class Live:
    def __init__(self, store, ms, signal=None, index=None, clock=time.time, sleep=time.sleep, google_api=GoogleApi,
                 slack_client=None, tokens=None):
        self.store, self.ms, self.signal, self.index, self.clock = store, ms, signal, index, clock
        tokens = tokens or Tokens(store, clock)
        self.readers = {"gmail": GmailReader(store, ms, tokens, google_api, clock, sleep),
                        "calendar": CalendarReader(store, ms, tokens, google_api, clock),
                        "slack": SlackReader(store, ms, slack_client, clock, sleep)}
        self.signal_ingest = SignalIngest(ms, clock)
        self._stop = threading.Event()
        self._threads = []
        self.started_at = None

    # --- running ---------------------------------------------------------------------------------------------
    def next_due(self, kind, row):
        if not row or not row.get("last_attempt"):
            return 0.0
        if row.get("errors"):
            return row["last_attempt"] + min(INTERVAL[kind] * 2 ** min(row["errors"], 10), BACKOFF_MAX_S)
        if row.get("backlog"):
            return row["last_attempt"] + BACKLOG_TICK_S[kind]
        return row["last_attempt"] + INTERVAL[kind]

    def run_due(self, kind, force=False):
        """Run every target of this kind whose turn it is. Returns {account: result or error sentence}."""
        reader, out = self.readers[kind], {}
        rows = {r["account"]: r for r in self.ms.freshness(kind)}
        for aid, _label in reader.targets():
            if self._stop.is_set():
                break
            now = self.clock()
            if not force and now < self.next_due(kind, rows.get(aid)):
                continue
            self.ms.fresh_attempt(kind, aid, now)
            try:
                res = reader.tick(aid)
            except Exception as e:  # noqa: BLE001 (recorded in plain words; the next pass backs off)
                msg = plain_error(e)
                self.ms.fresh_error(kind, aid, msg, self.clock())
                log(f"{NAMES[kind]}: {msg}")
                out[aid] = msg
                continue
            self.ms.fresh_ok(kind, aid, self.clock(), new=res["new"], newest_ts=res["newest"], backlog=res["backlog"])
            out[aid] = res
        return out

    def _loop(self, kind):
        while not self._stop.is_set():
            try:
                self.run_due(kind)
            except Exception as e:  # noqa: BLE001 (the loop never dies)
                log(f"{NAMES[kind]} loop: {type(e).__name__}")
            self._stop.wait(TICK_S)

    def start_threads(self):
        if self._threads:
            return
        self.started_at = self.clock()
        for kind in self.readers:
            t = threading.Thread(target=self._loop, args=(kind,), name=f"callsheet-live-{kind}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()

    # --- the Sources panel -------------------------------------------------------------------------------------
    def _row_state(self, kind, row, now):
        """(state, warning or None) for one account's freshness row."""
        iv = INTERVAL[kind]
        if row is None or not row.get("last_attempt"):
            if self.started_at and now - self.started_at > LATE_FACTOR * iv + 60:
                return "behind", "hasn't been read since Callsheet started"
            return "starting", None
        if row.get("errors", 0) >= ERRORS_WARN:
            return "error", f"failed {row['errors']} times in a row: {row.get('last_error') or 'no reason given'}"
        last = row.get("last_success")
        if not last:
            return ("error", f"hasn't worked yet: {row.get('last_error')}") if row.get("errors") else ("starting", None)
        if now - last > LATE_FACTOR * iv + 60:
            return "behind", f"hasn't been checked for {ago(now - last).replace(' ago', '')}"
        if row.get("backlog"):
            return "catching_up", None
        return "current", None

    WORDS = {"live": "Live", "current": "Current", "catching_up": "Catching up", "starting": "Starting",
             "behind": "Behind", "error": "Needs attention", "off": "Off"}
    RANK = ("current", "live", "starting", "catching_up", "behind", "error")

    def _group(self, kind, name, targets, rows, now, tz):
        accs, worst, warns = [], "current", []
        for aid, label in targets:
            st, warn = self._row_state(kind, rows.get(aid), now)
            accs.append({"label": label, "state": st})
            if self.RANK.index(st) > self.RANK.index(worst):
                worst = st
            if warn:
                warns.append(f"{label if len(targets) > 1 else NAMES[kind]} {warn.rstrip('.')}.")
        mine = [rows[a] for a, _ in targets if a in rows]
        checked = min((r["last_success"] for r in mine if r.get("last_success")), default=None) \
            if all(r.get("last_success") for r in mine) and mine else None
        newest = max((r["newest_ts"] for r in mine if r.get("newest_ts")), default=None)
        backlog = sum(r.get("backlog") or 0 for r in mine)
        parts = []
        if checked:
            parts.append("Checked " + ago(now - checked))
        elif worst == "starting":
            parts.append("Not checked yet")
        if newest and kind != "calendar":
            parts.append("read up to " + when(newest, now, tz))
        if backlog and worst == "catching_up":
            parts.append(f"reading back, about {backlog:,} to go")
        line = " · ".join(parts) or "Not checked yet"
        return {"key": kind, "name": name, "state": worst, "word": self.WORDS[worst], "line": line[:1].upper() + line[1:],
                "warn": " ".join(warns) or None, "checked_at": checked, "read_up_to": newest, "backlog": backlog,
                "accounts": accs}

    def _signal_row(self, now, tz):
        if self.signal is None:
            return None
        try:
            st = self.signal.status()
        except Exception:  # noqa: BLE001
            return None
        if not st.get("linked"):
            return None
        row = next(iter(self.ms.freshness("signal")), None) or {}
        newest = row.get("newest_ts") or st.get("last_message_at")
        read = ("last message read " + when(newest, now, tz)) if newest else "no messages yet"
        if st.get("running") and not st.get("error"):
            return {"key": "signal", "name": "Signal", "state": "live", "word": "Live", "line": "Live · " + read,
                    "warn": None, "checked_at": row.get("last_success"), "read_up_to": newest, "backlog": 0}
        why = str(st.get("error") or "signal-cli isn't running").rstrip(".")
        return {"key": "signal", "name": "Signal", "state": "behind", "word": "Not receiving",
                "line": read[:1].upper() + read[1:], "read_up_to": newest, "backlog": 0, "checked_at": row.get("last_success"),
                "warn": f"Signal isn't receiving ({why}). Callsheet restarts it by itself; messages wait on Signal's "
                        "servers meanwhile."}

    def chat_search_row(self, now, tz):
        """The old index (index.db), which Chat still searches in stage 1, and the sorter's cursor into it."""
        if self.index is None:
            return {"key": "chat_search", "name": "Chat’s search", "state": "off", "word": "Off",
                    "line": "The old index isn't available", "warn": None}
        try:
            with self.index.lock:
                r = self.index.db.execute("SELECT max(indexed_at) AS at, max(ts) AS ts, count(*) AS n FROM items").fetchone()
        except Exception:  # noqa: BLE001
            r = None
        if not r or not r["n"]:
            return {"key": "chat_search", "name": "Chat’s search", "state": "off", "word": "Not built",
                    "line": "The old index is empty; Chat searches the message store", "warn": None, "indexed_at": None}
        at = r["at"]
        sorting = (self.store._get_meta("sorting_ix", {}) or {}).get("indexed_at")  # noqa: SLF001
        days = int((now - at) // 86400)
        behind = now - at > INDEX_BEHIND_S
        lag = (f"{days} day{'s' if days != 1 else ''}" if days >= 1 else f"{int((now - at) // 3600)} hours")
        out = {"key": "chat_search", "name": "Chat’s search", "state": "behind" if behind else "current",
               "word": "Behind" if behind else "Current",
               "line": "Old index searchable up to " + when(at, now, tz) + (f" · {lag} behind" if behind else ""),
               "warn": None, "indexed_at": at, "sorting_at": sorting, "behind_days": days if behind else 0}
        if behind:
            have = self.ms.stats()
            out["warn"] = (f"Chat’s search is {lag} behind. Chat also searches the new message store "
                           f"({have['messages']:,} messages), so recent mail, Slack and Signal are still found; Drive "
                           "files, notes and older items are only in the old index.")
            if sorting and now - sorting > INDEX_BEHIND_S:
                out["warn"] += " AI sorting last read the index on " + when(sorting, now, tz) + "."
        return out

    def status(self):
        now, tz = self.clock(), _tz(self.store)
        rows = []
        sig = self._signal_row(now, tz)
        if sig:
            rows.append(sig)
        fresh = {}
        for r in self.ms.freshness():
            fresh.setdefault(r["source"], {})[r["account"]] = r
        for kind in ("gmail", "calendar", "slack"):
            targets = self.readers[kind].targets()
            if not targets:
                continue
            n = len(targets)
            if kind == "slack":
                name = " · ".join(t[1] for t in targets) if n <= 2 else f"Slack · {n} workspaces"
            else:
                name = NAMES[kind] + (f" · {n} accounts" if n > 1 else " · " + targets[0][1])
            rows.append(self._group(kind, name, targets, fresh.get(kind, {}), now, tz))
        st = self.ms.stats()
        return {"now": now, "sources": rows, "chat_search": self.chat_search_row(now, tz),
                "store": {"messages": st["messages"], "events": st["events"], "bytes": st["bytes"],
                          "sent": sum(s.get("out", 0) for s in st["sources"].values())},
                "settings": {"backfill_days": self.ms.backfill_days(), "choices": list(msgstore.BACKFILL_CHOICES)},
                "running": bool(self._threads)}
