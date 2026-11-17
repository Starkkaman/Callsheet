"""Tests only: a library (library.py) over a temp messages.db that still takes 0.6.0 index-style items of any source,
so the tests written against the old index (index.py, retired in 0.21.0) feed the same data through the new store:
mail, Slack and Signal items become messages, calendar items events, everything else documents ("obsidian" ->
"notes", a connector kind -> "feed"/"report")."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))
import library  # noqa: E402
import msgstore  # noqa: E402
from msgstore import MAX_BODY, MAX_LIMIT, SHORTENED, cap_body, fts_query  # noqa: E402,F401

MESSAGES = ("gmail", "slack", "signal", "sms", "sms_live")


def _people(p):
    if isinstance(p, (list, tuple)):
        return [str(x) for x in p if x]
    return [x.strip() for x in str(p or "").split(";") if x.strip()]


def _addr(p):
    if "<" in p and p.endswith(">"):
        return p[:p.index("<")].strip(), p[p.index("<") + 1:-1].strip()
    return ("", p) if "@" in p else (p, "")


def as_message(it):
    ppl = _people(it.get("people"))
    name, addr = _addr(ppl[0]) if ppl else ("", "")
    out = it.get("kind") == "sent" or it.get("from_owner") is True
    return {"source": it["source"], "account": it.get("account") or "", "msg_id": it["source_id"],
            "thread": it.get("thread") or "", "direction": "out" if out else "in", "sender": addr or name,
            "sender_name": name, "recipients": [{"name": n, "address": a} for n, a in map(_addr, ppl[1:])],
            "ts": float(it.get("ts") or 0), "subject": it.get("title") or "", "text": it.get("body") or "",
            "channel": it.get("channel") or "", "link": it.get("url") or ""}


def as_event(it):
    cal, _, eid = str(it["source_id"]).rpartition(":")
    eid = eid if not it.get("thread") else f"{it['thread']}_{eid}"
    ppl = _people(it.get("people"))
    return {"account": it.get("account") or "", "calendar": cal or "primary", "event_id": eid,
            "title": it.get("title") or "", "start_ts": float(it.get("ts") or 0), "end_ts": float(it.get("ts") or 0),
            "location": it.get("body") or "", "link": it.get("url") or "",
            "attendees": [{"name": n, "email": a} for n, a in map(_addr, ppl)]}


def as_doc(it):
    s = it.get("source")
    if s not in msgstore.DOC_SOURCES:
        s = {"obsidian": "notes"}.get(s, s if s in ("drive", "github") else library.doc_source(s))
    return dict(it, source=s)


def feed(lib, items, source=None, account=None, key="docs", cursor=None, clear_cursor=False):
    """Old-style items into a library: messages, events and documents. Returns the library's counts."""
    items = [it for it in items or [] if isinstance(it, dict)]
    msgs = [as_message(it) for it in items if it.get("source") in MESSAGES]
    evs = [as_event(it) for it in items if it.get("source") == "calendar"]
    docs = [as_doc(it) for it in items if it.get("source") not in MESSAGES + ("calendar",)]
    n = library.Library.commit_step(lib, docs, source, account, key, cursor, clear_cursor)
    if isinstance(getattr(lib, "test_areas", None), dict):
        for it in items:
            if it.get("source") in MESSAGES + ("calendar",) and it.get("area"):
                lib.test_areas[it.get("account") or ""] = it["area"]
    if msgs:
        lib.ms.upsert_messages(msgs)
        n["added"] += len(msgs)
    if evs:
        lib.ms.upsert_events(evs)
        n["added"] += len(evs)
    return n


class Index(library.Library):
    """A store-less library; a message's or an event's area is its account's, taken from the items fed (the old index
    kept an area on each item; the library maps an account to its area through the store)."""

    def __init__(self, data_dir, clock=time.time):
        super().__init__(msgstore.MessageStore(data_dir, clock=clock), clock=clock)
        self.test_areas = {}

    def _areas(self):
        return {**super()._areas(), **self.test_areas}

    def close(self):
        self.ms.close()

    def commit_step(self, items, source=None, account=None, key="docs", cursor=None, clear_cursor=False):
        return feed(self, items, source, account, key, cursor, clear_cursor)


# --- the old index tests' environment (0.6.0 test_index_060, retired with the index) ----------------------------------
import datetime as dt  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 28, 10, 0, tzinfo=UTC)   # a Wednesday, away from the call times (UTC)
DAY = 86400


def item(sid, title="", body="", people="", ts=None, source="gmail", account="acc1", area="personal", **kw):
    d = {"source": source, "account": account, "source_id": sid, "kind": "email", "title": title, "body": body,
         "people": people, "area": area, "ts": ts if ts is not None else T0.timestamp(), "url": "https://x.example/" + sid}
    d.update(kw)
    return d


class Env(unittest.TestCase):
    """An App on a temp dir with a fixed clock, the fake Google and a notes root."""

    def setUp(self):
        import api
        import sources
        from fake_google_index import FakeGoogleIndex
        self.api = api
        self.now = [T0.timestamp()]
        self.fake = FakeGoogleIndex()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vroot = pathlib.Path(self.tmp.name) / "share"
        self.vroot.mkdir()
        env = mock.patch.dict(os.environ, {**self.fake.env(), sources.VAULT_ROOTS_ENV: str(self.vroot)})
        env.start()
        self.addCleanup(env.stop)
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.app = self.new_app()

    def new_app(self):
        app = self.api.App(str(self.data), clock=lambda: self.now[0])
        app.store.update_settings({"timezone": "UTC"})
        self.addCleanup(app.close)
        return app

    def connect(self, email, events=None):
        from fake_google import CLIENT_ID, CLIENT_SECRET
        if not self.app.store.google_client():
            self.app.handle("POST", "google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.fake.add_user(email, events=events)
        flow = self.app.handle("POST", "accounts/start", {"area_id": "personal"})
        return self.app.handle("POST", "accounts/finish", {"flow_id": flow["flow_id"],
                                                           "pasted": self.fake.approve(flow["auth_url"], email)})
