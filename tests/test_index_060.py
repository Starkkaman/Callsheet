"""0.6.0 Know me (stage 3a): the local index (FTS5), the sources against the fakes, the scan job and the API.
Stdlib only, no network: Google is fake_google_index.FakeGoogleIndex, Slack a FakeSlack with users.list and pages."""
import datetime as dt
import http.client
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import api  # noqa: E402
import fake_slack  # noqa: E402
import google  # noqa: E402
import index as ix  # noqa: E402
import scan  # noqa: E402
import server  # noqa: E402
import sources  # noqa: E402
from fake_google import CLIENT_ID, CLIENT_SECRET, FULL_SCOPE, event  # noqa: E402
from fake_google_index import DRIVE_SCOPE, FakeGoogleIndex, message  # noqa: E402
from store import Conflict, InputError, NotFound  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 28, 10, 0, tzinfo=UTC)   # a Wednesday, away from the call times (UTC)
DAY = 86400


def item(sid, title="", body="", people="", ts=None, source="gmail", account="acc1", area="personal", **kw):
    d = {"source": source, "account": account, "source_id": sid, "kind": "email", "title": title, "body": body,
         "people": people, "area": area, "ts": ts if ts is not None else T0.timestamp(), "url": "https://x.example/" + sid}
    d.update(kw)
    return d


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ix = ix.Index(self.tmp.name, clock=lambda: T0.timestamp())
        self.addCleanup(self.ix.close)

    def test_file_is_private_and_separate(self):
        self.ix.upsert([item("m1", "Hello")])
        p = pathlib.Path(self.tmp.name) / "index.db"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertFalse((pathlib.Path(self.tmp.name) / "callsheet.db").exists())

    def test_upsert_is_idempotent(self):
        rows = [item("m1", "Budget sign-off", "Priya needs the Q4 budget"), item("m2", "Plumber", "Home at 4")]
        self.assertEqual(self.ix.upsert(rows), {"added": 2, "updated": 0, "same": 0, "bad": 0})
        first = self.ix.get(self.ix.search("budget")[0]["id"])
        self.assertEqual(self.ix.upsert(rows), {"added": 0, "updated": 0, "same": 2, "bad": 0})
        self.assertEqual(self.ix.get(first["id"]), first)
        self.assertEqual(self.ix.count(), 2)
        changed = [item("m1", "Budget sign-off", "Priya moved it to the roadmap review")]
        self.assertEqual(self.ix.upsert(changed)["updated"], 1)
        self.assertEqual(self.ix.search("Q4"), [])           # the FTS row follows the update
        self.assertEqual(len(self.ix.search("roadmap")), 1)
        self.assertEqual(self.ix.upsert([{"source": "BAD KEY", "source_id": "x"}, "nope"])["bad"], 2)

    def test_ranking_and_highlight(self):
        self.ix.upsert([item("a", "Weekly notes", "we talked about the kitchen renovation and more", ts=T0.timestamp()),
                        item("b", "Kitchen renovation quote", "see attached", ts=T0.timestamp() - DAY),
                        item("c", "Lunch", "nothing here", people="Kitchen Crew <crew@x.example>")])
        res = self.ix.search("kitchen")
        self.assertEqual([r["source_id"] if "source_id" in r else self.ix.get(r["id"])["source_id"] for r in res],
                         ["b", "c", "a"])   # title x8 > people x4 > body
        hl = res[0]["highlight"]
        self.assertTrue(any(t.lower() == "kitchen" and on for t, on in hl), hl)
        self.assertNotIn("\x02", res[0]["snippet"])
        self.assertEqual(len(self.ix.search("kitch")), 3)   # the last word is a prefix
        self.assertEqual(len(self.ix.search('"renovation quote"')), 1)

    def test_filters(self):
        old = (T0 - dt.timedelta(days=400)).timestamp()
        self.ix.upsert([item("m1", "Invoice", "pay", people="Jonas <jonas@mlw.example>"),
                        item("m2", "Invoice", "pay", account="acc2", area="work", ts=old),
                        item("e1", "Invoice review", "", source="calendar", kind="event")])
        self.assertEqual(len(self.ix.search("invoice")), 3)
        self.assertEqual(len(self.ix.search("invoice", source="calendar")), 1)
        self.assertEqual(len(self.ix.search("invoice", source="gmail,calendar")), 3)
        self.assertEqual(len(self.ix.search("invoice", account="acc2")), 1)
        self.assertEqual(len(self.ix.search("invoice", area="work")), 1)
        self.assertEqual(len(self.ix.search("invoice", people="jonas")), 1)
        self.assertEqual(len(self.ix.search("invoice", since=T0.timestamp() - 30 * DAY)), 2)
        self.assertEqual(len(self.ix.search("invoice", until=T0.timestamp() - 30 * DAY)), 1)
        newest = self.ix.search("", source="gmail")
        self.assertEqual([self.ix.get(r["id"])["source_id"] for r in newest], ["m1", "m2"])
        self.assertEqual(len(self.ix.search("invoice", limit=1)), 1)
        self.assertEqual(self.ix.search("invoice", people="100%_"), [])

    def test_odd_queries_never_break(self):
        self.ix.upsert([item("m1", "Hello world", "body")])
        for q in ('"', 'NEAR(a b)', '*', 'a OR', '-x', 'title:hello', "'; DROP TABLE items; --", "()", "^", "x" * 600):
            self.assertIsInstance(self.ix.search(q), list, q)
        self.assertEqual(ix.fts_query('title:hello "big deal" NOT'), '"big deal" "title" "hello"*')

    def test_body_cap_and_get(self):
        self.ix.upsert([item("m1", "Long", "word " * 5000)])
        got = self.ix.get(self.ix.search("long")[0]["id"])
        self.assertLessEqual(len(got["body"]), ix.MAX_BODY + 10)
        self.assertTrue(got["body"].endswith(ix.SHORTENED))
        self.assertIsNone(self.ix.get(999999))
        self.assertIsNone(self.ix.get("x"))

    def test_delete_wipe_stats(self):
        y25 = dt.datetime(2025, 3, 1, tzinfo=UTC).timestamp()
        self.ix.upsert([item("m1", "a"), item("m2", "b", ts=y25), item("m3", "c", account="acc2"),
                        item("n1", "note", source="obsidian", account="vault")])
        st = self.ix.stats()
        self.assertEqual(st["total"], 4)
        self.assertEqual(st["sources"]["gmail"]["years"], {"2026": 2, "2025": 1})
        self.assertEqual(st["sources"]["gmail"]["accounts"], {"acc1": 2, "acc2": 1})
        self.assertGreater(st["bytes"], 0)
        self.assertEqual(self.ix.delete("gmail", "acc2"), 1)
        self.assertEqual(self.ix.delete_missing("obsidian", "vault", set()), 1)
        self.assertEqual(self.ix.delete("gmail"), 2)
        self.assertEqual(self.ix.search("a"), [])
        self.ix.upsert([item("m9", "again")])
        self.ix.set_meta("settings", {"vault": "/share/v"})
        self.ix.set_cursor("gmail", "acc1", "scan", {"page": "5"})
        self.ix.wipe()
        self.assertEqual(self.ix.count(), 0)
        self.assertIsNone(self.ix.cursor("gmail", "acc1"))
        self.assertEqual(self.ix.meta("settings"), {"vault": "/share/v"})
        with self.assertRaises(ValueError):
            self.ix.delete()


class Words(unittest.TestCase):
    def test_estimate_examples(self):
        sec, size, summary, warns = scan.estimate_words({"gmail": (48213, False)}, 50e9)
        self.assertEqual(summary, "About 48,000 emails. May take about 6 hours and use about 900 MB on this box.")
        self.assertIn("This can take hours.", warns[0])
        _, _, summary, warns = scan.estimate_words({"gmail": (2100, False), "calendar": (412, False),
                                                    "obsidian": (386, False)}, 50e9)
        self.assertEqual(summary, "About 2,100 emails, 412 events and 386 notes. May take about 15 minutes and "
                                  "use about 46 MB on this box.")
        self.assertEqual(warns, [])
        _, _, _, warns = scan.estimate_words({"gmail": (210000, False), "slack": (None, False)}, 2e9)
        self.assertTrue(any("Slack doesn't say" in w for w in warns))
        self.assertTrue(any(w.startswith("This box has only 2 GB free; the index may need about 4 GB") for w in warns),
                        warns)
        _, _, summary, warns = scan.estimate_words({"gmail": (9600, False), "obsidian": (1, False)}, 50e9)
        self.assertTrue(summary.startswith("About 9,600 emails and 1 note. May take about an hour"), summary)
        self.assertTrue(warns[0].startswith("This can take an hour or more. Briefs still run on time"), warns)
        self.assertEqual([scan.about_time(x) for x in (30, 600, 3000, 3700, 9 * 3600, 3 * 86400)],
                         ["a minute or two", "about 10 minutes", "about 50 minutes", "about an hour", "about 9 hours",
                          "about 3 days"])
        self.assertEqual([scan.about_size(x) for x in (5e5, 4.2e7, 9.12e8, 1.25e9)],
                         ["under 1 MB", "about 42 MB", "about 900 MB", "about 1.2 GB"])
        with self.assertRaises(InputError):
            scan.window("3y")


# --- sources and the scanner against the fakes ---------------------------------------------------------------------
class SlackIdx(fake_slack.FakeSlack):
    hist_cap = 200

    def answer(self, name, p):
        if name == "users.list":
            return {"ok": True, "members": [{"id": u, "name": v["name"], "profile": {"real_name": v["name"]}}
                                            for u, v in self.users.items()], "response_metadata": {"next_cursor": ""}}
        if name == "conversations.history":
            if p.get("channel") not in self.messages:
                return {"ok": False, "error": "channel_not_found"}
            lo, hi = float(p.get("oldest") or 0), float(p.get("latest") or 1e12)
            msgs = sorted((m for m in self._parents(p["channel"]) if lo <= float(m["ts"]) < hi),
                          key=lambda m: -float(m["ts"]))
            start, lim = int(p.get("cursor") or 0), min(int(p.get("limit") or 100), self.hist_cap)
            more = start + lim < len(msgs)
            return {"ok": True, "messages": msgs[start:start + lim], "has_more": more,
                    "response_metadata": {"next_cursor": str(start + lim) if more else ""}}
        return super().answer(name, p)


class Env(unittest.TestCase):
    """An App on a temp dir with a fixed clock, the fake Google (and Slack), and a vault root."""

    def setUp(self):
        self.now = [T0.timestamp()]
        self.fake = FakeGoogleIndex()
        self.addCleanup(self.fake.close)
        self.sl = SlackIdx()
        self.addCleanup(self.sl.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vroot = pathlib.Path(self.tmp.name) / "share"
        self.vroot.mkdir()
        env = mock.patch.dict(os.environ, {**self.fake.env(), **self.sl.env(),
                                           sources.VAULT_ROOTS_ENV: str(self.vroot)})
        env.start()
        self.addCleanup(env.stop)
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.sleeps = []
        self.app = self.new_app()

    def new_app(self):
        app = api.App(str(self.data), clock=lambda: self.now[0])
        app.scanner.sleep = self.sleeps.append
        app.scanner.log = lambda line: None
        app.store.update_settings({"timezone": "UTC"})
        self.addCleanup(app.close)
        return app

    def connect(self, email, events=None):
        if not self.app.store.google_client():
            self.app.handle("POST", "google-client", {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.fake.add_user(email, events=events)
        flow = self.app.handle("POST", "accounts/start", {"area_id": "personal"})
        return self.app.handle("POST", "accounts/finish", {"flow_id": flow["flow_id"],
                                                           "pasted": self.fake.approve(flow["auth_url"], email)})

    def mails(self, email, n, start_days=1, step_h=6, body="Can you confirm the venue for Friday?"):
        msgs = [message(f"{0x1000 + i:x}", f"{0x2000 + i // 2:x}", f"Eli Tellez <eli{i % 3}@venue.example>", f"Venue {i}",
                        f"{body} ref {i}\n\nOn Mon, 2 Nov 2026 at 10:00, Sam <me@example.com> wrote:\n> old quoted text",
                        T0 - dt.timedelta(days=start_days, hours=i * step_h)) for i in range(n)]
        self.fake.add_messages(email, msgs)
        return msgs

    def steps(self, n=500):
        return self.app.scanner.run_steps(n)

    def gets(self):
        return [r for r in self.fake.requests if r[1].startswith("/gmail/v1/users/me/messages/")]


class GmailTest(Env):
    def test_scan_pages_window_and_text(self):
        acc = self.connect("me@example.com")
        self.fake.page_cap = 40
        msgs = self.mails("me@example.com", 130)                  # 130 messages over ~33 days
        old = message("old1", "told", "x@y.example", "Ancient", "very old", T0 - dt.timedelta(days=200))
        self.fake.add_messages("me@example.com", [old])
        self.app.scanner.start("90d", ["gmail"])
        self.steps()
        st = self.app.scanner.status()["job"]
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["targets"][0]["done"], 130)
        self.assertEqual(st["targets"][0]["total"], 130)
        self.assertEqual(self.app.index.count("gmail"), 130)
        lists = [r for r in self.fake.requests if r[1] == "/gmail/v1/users/me/messages" and r[2].get("maxResults") != ["1"]]
        self.assertEqual(len(lists), 4)                            # 40 + 40 + 40 + 10
        self.assertIn("after:", lists[0][2]["q"][0])
        self.assertIn("-in:drafts", lists[0][2]["q"][0])
        self.assertTrue(all(r[2].get("format") == ["full"] for r in self.gets()))
        hit = self.app.index.search("venue", source="gmail", limit=1)[0]
        full = self.app.index.get(hit["id"])
        self.assertNotIn("old quoted text", full["body"])
        self.assertNotIn("wrote:", full["body"])
        self.assertIn("Eli Tellez <eli", full["people"])
        self.assertTrue(full["url"].startswith("https://mail.google.com/mail/u/?authuser=me@example.com#all/thread-f:"))
        self.assertEqual(full["area"], "personal")
        self.assertEqual(full["account"], acc["id"])
        self.assertFalse(any("attachments" in r[1] for r in self.fake.requests))
        self.assertEqual(self.app.index.search("Ancient"), [])
        self.assertTrue(self.sleeps and all(s > 0 for s in self.sleeps))   # paced

    def test_resume_after_restart(self):
        self.connect("me@example.com")
        self.fake.page_cap = 50
        self.mails("me@example.com", 120)
        self.app.scanner.start("1y", ["gmail"])
        self.steps(3)                       # estimate + list page 1, then two chunks of 20
        self.assertEqual(self.app.index.count("gmail"), 40)
        self.app.close()
        mark = len(self.fake.requests)
        self.app = self.new_app()         # the app restarts; the job is still "running"
        self.assertEqual(self.app.scanner.status()["job"]["state"], "running")
        self.steps()
        self.assertEqual(self.app.scanner.status()["job"]["state"], "done")
        self.assertEqual(self.app.index.count("gmail"), 120)
        after = self.fake.requests[mark:]
        got = [r[1] for r in after if r[1].startswith("/gmail/v1/users/me/messages/")]
        self.assertEqual(len(got), 80)    # nothing fetched twice
        lists = [r for r in after if r[1] == "/gmail/v1/users/me/messages"]
        self.assertEqual(lists[0][2].get("pageToken"), ["50"])   # resumed from the stored page token

    def test_backoff_and_quota_words(self):
        self.connect("me@example.com")
        self.mails("me@example.com", 10)
        self.app.scanner.start("30d", ["gmail"])
        self.fake.inject.append(("/gmail/v1/users/me/messages/", 429, "rateLimitExceeded"))
        self.steps(3)
        t = self.app.scanner.status()["job"]["targets"][0]
        self.assertEqual(t["status"], "waiting")
        self.assertEqual(t["error"], "Google asked Callsheet to slow down. The scan waits and tries again.")
        self.assertGreaterEqual(t["retry_at"], self.now[0] + 60)
        self.assertEqual(self.steps(5), 0)  # nothing to do until the wait is over
        self.now[0] += 61
        self.fake.inject.append(("/gmail/v1/users/me/messages/", 403, "userRateLimitExceeded"))
        self.steps(1)
        t = self.app.scanner.status()["job"]["targets"][0]
        self.assertEqual(t["status"], "waiting")
        self.assertGreaterEqual(t["retry_at"] - self.now[0], 60)   # the second failure waits longer
        self.now[0] += 3600
        self.steps()
        self.assertEqual(self.app.scanner.status()["job"]["state"], "done")
        self.assertEqual(self.app.index.count("gmail"), 10)

    def test_daily_quota_and_access_errors(self):
        acc = self.connect("me@example.com")
        self.mails("me@example.com", 3)
        e = google.GoogleError(google._plain(403, "dailyLimitExceeded"), 403, "dailyLimitExceeded")
        r = sources._mapped(e)
        self.assertIsInstance(r, sources.Retry)
        self.assertEqual(r.wait, 3600)
        self.assertEqual(str(e), "Google asked Callsheet to slow down (usage limit reached). It will try again later.")
        self.app.scanner.start("30d", ["gmail"])
        self.fake.inject.append(("/gmail/v1/users/me/messages", 403, "accessNotConfigured"))
        self.fake.inject.append(("/gmail/v1/users/me/messages", 403, "accessNotConfigured"))
        self.steps(3)
        t = self.app.scanner.status()["job"]["targets"][0]
        self.assertEqual(t["status"], "error")
        self.assertEqual(t["error"], "This Google API isn't turned on in your Google Cloud project. Turn it on, then "
                                     "try again.")
        self.assertEqual(self.app.scanner.status()["job"]["state"], "done")
        self.assertTrue(acc["id"])

    def test_estimates(self):
        self.connect("me@example.com")
        self.mails("me@example.com", 130)   # ~33 days
        est = self.app.scanner.estimate("30d", ["gmail"])
        self.assertLess(est["targets"][0]["count"], 130)
        self.assertGreater(est["targets"][0]["count"], 100)
        est = self.app.scanner.estimate("all", ["gmail"])
        self.assertEqual(est["targets"][0]["count"], 130)  # profile messagesTotal
        self.assertTrue(est["summary"].startswith("About 130 emails. May take"))
        n = len(self.fake.requests)
        self.app.scanner.estimate("all", ["gmail"])
        self.assertEqual(len(self.fake.requests), n)       # cached


class CalendarSlackTest(Env):
    def test_calendar_pages_and_fields(self):
        evs = [event(f"Rota call {i}", T0 + dt.timedelta(days=i), T0 + dt.timedelta(days=i, hours=1),
                     htmlLink=f"https://www.google.com/calendar/event?eid=e{i}",
                     description="<p>Bring the <b>sign-up</b> sheet</p>", location="Hall",
                     attendees=[{"email": "ruth@x.example", "displayName": "Ruth Okafor"}]) for i in range(7)]
        for i, e in enumerate(evs):
            e["id"] = f"ev{i}"
        self.connect("me@example.com", events={"me@example.com": evs,
                                               "family@group.calendar.google.com": [dict(evs[0], id="fam")]})
        self.fake.page_cap = 3
        self.app.scanner.start("30d", ["calendar"])
        self.steps()
        self.assertEqual(self.app.index.count("calendar"), 7)          # only the selected (primary) calendar
        r = self.app.index.get(self.app.index.search("sign-up", limit=1)[0]["id"])
        self.assertIn("Bring the sign-up sheet", r["body"])
        self.assertIn("Where: Hall", r["body"])
        self.assertIn("Ruth Okafor <ruth@x.example>", r["people"])
        self.assertTrue(r["url"].startswith("https://www.google.com/calendar/event?eid="))
        evq = [x for x in self.fake.requests if x[1].endswith("/events")]
        self.assertTrue(all(x[2]["singleEvents"] == ["true"] for x in evq))
        self.assertIn(["3"], [x[2].get("pageToken") for x in evq])

    def test_slack_channels_dms_threads_and_rate_limit(self):
        self.sl.user("U2", "Tomás Berg")
        self.sl.channel("C1", "billing")
        self.sl.channel("C9", "random")
        self.sl.dm("D1", "U2")
        self.app.handle("POST", "slack/connect", {"area_id": "personal", "token": fake_slack.TOKEN})
        base = T0.timestamp() - 5 * DAY
        root = self.sl.post("C1", "U2", "Migration plan for <@UME>", base)
        self.sl.post("C1", fake_slack.ME, "Looks good", base + 60, thread_ts=root)
        for i in range(5):
            self.sl.post("D1", "U2", f"dm number {i}", base + 100 + i)
        self.sl.post("C9", "U2", "ignored channel text", base)
        self.sl.post("C1", "U2", "way too old", T0.timestamp() - 400 * DAY)
        ws = self.app.store.slack_workspaces()[0]
        self.app.store.set_slack_ignored(ws["id"], ["C9"])
        self.sl.hist_cap = 2
        self.sl.limit["conversations.history"] = 1
        self.app.scanner.start("90d", ["slack"])
        self.steps()
        self.assertEqual(self.app.scanner.status()["job"]["state"], "done")
        self.assertEqual(self.app.index.count("slack"), 7)     # 1 root + 1 reply + 5 DMs
        self.assertEqual(self.app.index.search("ignored"), [])
        self.assertEqual(self.app.index.search("too old"), [])
        reply = self.app.index.get(self.app.index.search("Looks good")[0]["id"])
        self.assertEqual(reply["title"], "#billing")
        self.assertIn("thread_ts=", reply["url"])
        self.assertTrue(reply["url"].startswith("https://northwind.slack.com/archives/C1/p"))
        dm = self.app.index.search("dm number", limit=1)[0]
        self.assertEqual(dm["title"], "DM with Tomás Berg")
        plan = self.app.index.get(self.app.index.search("Migration")[0]["id"])
        self.assertIn("@sam", plan["body"])                      # <@UME> as a name, not markup
        self.assertIn(2.0, self.sleeps)                          # Retry-After honoured by slack.Client
        hist = self.sl.calls("conversations.history")
        self.assertTrue(any(p.get("cursor") for p in hist))      # pages
        self.assertTrue(all(p.get("oldest") for p in hist))

    def test_slack_persistent_rate_limit_waits(self):
        self.app.handle("POST", "slack/connect", {"area_id": "personal", "token": fake_slack.TOKEN})
        self.sl.channel("C1", "billing")
        self.sl.post("C1", "UME", "hello", T0.timestamp() - DAY)
        self.app.scanner.start("30d", ["slack"])
        self.steps(1)                                              # conversations + names
        self.sl.limit["conversations.history"] = 10
        self.steps(1)
        t = self.app.scanner.status()["job"]["targets"][0]
        self.assertEqual(t["status"], "waiting")
        self.assertIn("Slack asked Callsheet to slow down", t["error"])
        self.sl.limit.clear()
        self.now[0] += 120
        self.steps()
        self.assertEqual(self.app.index.count("slack"), 1)


class DriveTest(Env):
    def setUp(self):
        super().setUp()
        self.acc = self.connect("me@example.com")
        f = self.fake
        f.add_file("me@example.com", "doc1", "Q4 roadmap", "application/vnd.google-apps.document",
                   "2026-10-20T10:00:00.000Z", "Roadmap: launch in spring")
        f.add_file("me@example.com", "sh1", "Budget", "application/vnd.google-apps.spreadsheet",
                   "2026-10-21T10:00:00.000Z", "item,cost\nvenue,18400\n")
        f.add_file("me@example.com", "sl1", "Kickoff deck", "application/vnd.google-apps.presentation",
                   "2026-10-22T10:00:00.000Z", "Slide 1 kickoff agenda")
        f.add_file("me@example.com", "md1", "todo.md", "text/markdown", "2026-10-23T10:00:00.000Z", "- buy paint", size=12)
        f.add_file("me@example.com", "big", "huge.txt", "text/plain", "2026-10-23T10:00:00.000Z", "x", size=50_000_000)
        f.add_file("me@example.com", "old", "Old plan", "application/vnd.google-apps.document",
                   "2024-01-01T10:00:00.000Z", "ancient plan")

    def test_scope_check_and_skip_without_grant(self):
        self.assertIs(self.app.handle("POST", "index/drive/check", {})["accounts"][0]["granted"], False)
        self.app.scanner.start("90d", ["drive"])
        t = self.app.scanner.status()["job"]["targets"][0]
        self.assertEqual((t["status"], t["error"]), ("skipped", "Grant Drive access first"))
        self.steps()
        self.assertFalse([r for r in self.fake.requests if r[1].startswith("/drive/")])

    def test_incremental_grant_url_and_finish(self):
        out = self.app.handle("POST", "index/drive/start", {"account_id": self.acc["id"]})
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(out["auth_url"]).query)
        scopes = q["scope"][0].split()
        self.assertIn(DRIVE_SCOPE, scopes)
        self.assertIn(google.GMAIL_SCOPE, scopes)
        self.assertIn(google.CALENDAR_SCOPE, scopes)
        self.assertEqual(q["include_granted_scopes"], ["true"])
        self.assertEqual(q["login_hint"], ["me@example.com"])
        self.assertEqual(q["access_type"], ["offline"])
        normal = urllib.parse.parse_qs(urllib.parse.urlsplit(google.build_auth_url("id", "s", "c")).query)
        self.assertNotIn(DRIVE_SCOPE, normal["scope"][0])          # new logins don't ask for Drive
        self.fake.scope = FULL_SCOPE + " " + DRIVE_SCOPE
        done = self.app.handle("POST", "index/drive/finish", {"flow_id": out["flow_id"],
                                                               "pasted": self.fake.approve(out["auth_url"], "me@example.com")})
        self.assertTrue(done["granted"])
        self.assertEqual(done["account"]["id"], self.acc["id"])     # the same account, still connected
        self.assertEqual(done["account"]["status"], "connected")
        self.assertEqual(len(self.app.store.accounts()), 1)
        st = self.app.handle("GET", "index/status", None)
        drive = next(s for s in st["sources"] if s["key"] == "drive")
        self.assertTrue(drive["accounts"][0]["granted"])
        self.assertNotIn("refresh_token", json.dumps(st))

    def test_drive_index(self):
        self.fake.scope = FULL_SCOPE + " " + DRIVE_SCOPE
        self.app.handle("POST", "index/drive/check", {})
        est = self.app.scanner.estimate("90d", ["drive"])
        self.assertEqual(est["targets"][0]["count"], 5)
        self.app.scanner.start("90d", ["drive"])
        self.steps()
        self.assertEqual(self.app.index.count("drive"), 5)
        by = {r["title"]: self.app.index.get(r["id"]) for r in self.app.index.search("", source="drive", limit=10)}
        self.assertEqual(by["Q4 roadmap"]["kind"], "doc")
        self.assertIn("launch in spring", by["Q4 roadmap"]["body"])
        self.assertIn("venue,18400", by["Budget"]["body"])
        self.assertEqual(by["Kickoff deck"]["kind"], "slides")
        self.assertIn("buy paint", by["todo.md"]["body"])
        self.assertEqual(by["huge.txt"]["body"], "")                # too large: name only
        self.assertIn("Sam Rivera <me@example.com>", by["Q4 roadmap"]["people"])
        self.assertTrue(by["Q4 roadmap"]["url"].startswith("https://docs.google.com/"))
        exports = {x[2]["mimeType"][0] for x in self.fake.requests if x[1].endswith("/export")}
        self.assertEqual(exports, {"text/plain", "text/csv"})
        lst = next(x for x in self.fake.requests if x[1] == "/drive/v3/files")
        self.assertIn("modifiedTime >", lst[2]["q"][0])
        self.assertIn("trashed = false", lst[2]["q"][0])


class ObsidianTest(Env):
    def vault(self):
        v = self.vroot / "Obsidian" / "Work vault"
        (v / ".obsidian").mkdir(parents=True)
        (v / ".obsidian" / "workspace.md").write_text("# secret config")
        (v / "Projects").mkdir()
        (v / "attachments").mkdir()
        (v / "attachments" / "photo.png").write_bytes(b"\x89PNG")
        (v / "Projects" / "Kitchen.md").write_text(
            "---\ntitle: Kitchen renovation\ntags: [home, reno]\ndate: 2026-09-01\narea: Personal\npeople:\n"
            "  - Amber\n---\n# Heading ignored\nCall [[Schuyler Grant|Schuyler]] about [[Quotes#march]].\n"
            "![[photo.png]] #budget and `code` #todo/next\n")
        (v / "Inbox.md").write_text("# Inbox note\nplain text, no front matter\n")
        (v / "plain.txt").write_text("not markdown")
        return v

    def test_validation(self):
        v = self.vault()
        self.assertEqual(sources.check_vault(str(v)), os.path.realpath(v))
        outside = pathlib.Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (self.vroot / "link").symlink_to(outside)
        for bad, words in ((str(outside), "inside /share or /media"), ("relative/path", "full folder path"),
                           (str(self.vroot / "missing"), "can't find"), (str(v / "Inbox.md"), "not a folder"),
                           (str(self.vroot / "link"), "inside /share or /media"), ("", "Type the vault folder"),
                           (str(self.vroot / ".." / "elsewhere"), "inside /share or /media")):
            with self.assertRaises(InputError, msg=bad) as cm:
                sources.check_vault(bad)
            self.assertIn(words, str(cm.exception))
        with self.assertRaises(InputError):
            self.app.handle("PATCH", "index/settings", {"vault": "/etc"})
        self.assertEqual(self.app.handle("PATCH", "index/settings", {"vault": str(v)})["vault"], str(v))

    def test_parse_and_index(self):
        v = self.vault()
        self.app.handle("PATCH", "index/settings", {"vault": str(v)})
        self.assertEqual(self.app.scanner.estimate("30d", ["obsidian"])["targets"][0]["count"], 2)
        self.app.scanner.start("30d", ["obsidian"])
        self.steps()
        rows = {r["title"]: self.app.index.get(r["id"]) for r in self.app.index.search("", source="obsidian")}
        self.assertEqual(set(rows), {"Kitchen renovation", "Inbox note"})   # .obsidian and non-.md skipped
        k = rows["Kitchen renovation"]
        for hint in ("Schuyler Grant", "Quotes", "Amber", "#home", "#reno", "#budget", "#todo/next"):
            self.assertIn(hint, k["people"])
        self.assertNotIn("photo.png", k["people"])
        self.assertEqual(k["area"], "personal")
        self.assertEqual(k["url"], os.path.join("Projects", "Kitchen.md"))
        self.assertEqual(dt.datetime.fromtimestamp(k["ts"], UTC).date(), dt.date(2026, 9, 1))
        self.assertNotIn("title: Kitchen", k["body"])
        self.assertEqual(self.app.index.search("secret"), [])
        (v / "Inbox.md").unlink()                                   # deleted notes leave on the next read
        self.app.scanner.start("30d", ["obsidian"])
        self.steps()
        self.assertEqual(self.app.index.count("obsidian"), 1)
        self.app.handle("PATCH", "index/settings", {"vault": ""})   # changing the vault removes its notes
        self.assertEqual(self.app.index.count("obsidian"), 0)

    def test_front_matter_subset(self):
        fm, rest = sources.front_matter("---\na: 1\nb: [x, 'y']\nc:\n  - p\n  - \"q\"\n---\nbody")
        self.assertEqual(fm, {"a": "1", "b": ["x", "y"], "c": ["p", "q"]})
        self.assertEqual(rest, "body")
        self.assertEqual(sources.front_matter("---\nno end")[0], {})


class ScannerTest(Env):
    def test_call_time_and_sync_pause(self):
        self.connect("me@example.com")
        self.mails("me@example.com", 30)
        self.app.scanner.start("30d", ["gmail"])
        for hhmm, blocked in (("12:19", False), ("12:21", True), ("12:30", True), ("12:39", True), ("12:41", False),
                              ("06:25", True), ("16:55", True)):
            h, m = map(int, hhmm.split(":"))
            self.now[0] = T0.replace(hour=h, minute=m).timestamp()
            b = self.app.scanner.blocked()
            self.assertEqual(bool(b), blocked, hhmm)
        self.now[0] = T0.replace(hour=12, minute=25).timestamp()
        self.assertEqual(self.steps(5), 0)
        w = self.app.scanner.status()["waiting"]
        self.assertEqual(w["reason"], "call")
        self.assertIn("12:30 pm", w["text"])
        self.now[0] = T0.timestamp()
        with self.app.syncer.lock:
            self.assertEqual(self.app.scanner.blocked()[0], "sync")
            self.assertEqual(self.steps(5), 0)
        self.assertGreater(self.steps(2), 0)

    def test_pause_resume_cancel(self):
        self.connect("me@example.com")
        self.mails("me@example.com", 100)
        with self.assertRaises(Conflict):
            self.app.scanner.pause()
        self.app.scanner.start("90d", ["gmail"])
        with self.assertRaises(Conflict):
            self.app.scanner.start("90d", ["gmail"])
        self.steps(2)
        self.app.scanner.pause()
        n = self.app.index.count()
        self.assertEqual(self.steps(5), 0)
        self.assertEqual(self.app.index.count(), n)
        self.app.scanner.resume()
        self.steps(2)
        self.assertGreater(self.app.index.count(), n)
        st = self.app.scanner.cancel()
        self.assertEqual(st["job"]["state"], "cancelled")
        kept = self.app.index.count()
        self.assertGreater(kept, 0)                                  # stop keeps what was read
        self.assertEqual(self.steps(5), 0)
        with self.assertRaises(Conflict):
            self.app.scanner.resume()
        self.app.scanner.start("90d", ["gmail"])                     # a new scan reads again, idempotently
        self.steps()
        self.assertEqual(self.app.index.count(), 100)

    def test_progress_rate_eta_and_refresh(self):
        self.connect("me@example.com")
        self.mails("me@example.com", 60)
        self.app.scanner.start("90d", ["gmail"])
        self.steps(2)
        self.now[0] += 30
        self.steps(1)
        t = self.app.scanner.status()["job"]["targets"][0]
        self.assertEqual((t["done"], t["total"]), (40, 60))
        self.assertIsNotNone(t["rate_per_min"])
        self.steps()
        self.assertEqual(self.app.scanner.status()["job"]["state"], "done")
        # new mail arrives; the light refresh 30 minutes later reads only what's new (with the overlap)
        new = message("new1", "tn", "Priya <priya@x.example>", "Budget sign-off", "Please sign off today",
                      T0 + dt.timedelta(minutes=10))
        self.fake.add_messages("me@example.com", [new])
        self.now[0] += 31 * 60
        mark = len(self.fake.requests)
        self.steps()
        self.assertEqual(self.app.index.count(), 61)
        q = next(r for r in self.fake.requests[mark:] if r[1] == "/gmail/v1/users/me/messages")[2]["q"][0]
        since = int(q.split("after:")[1].split()[0])
        self.assertAlmostEqual(since, T0.timestamp() - scan.REFRESH_OVERLAP_S, delta=120)
        self.assertEqual(self.app.scanner.status()["refresh"]["state"], "done")

    def test_orphans_and_source_off(self):
        acc = self.connect("me@example.com")
        self.mails("me@example.com", 5)
        self.app.index.upsert([item("o1", "obsidian note", source="obsidian", account="vault")])
        self.app.scanner.start("30d", ["gmail"])
        self.steps()
        self.assertEqual(self.app.index.count("gmail"), 5)
        self.app.handle("DELETE", f"accounts/{acc['id']}", None)
        self.assertEqual(self.app.scanner.prune_orphans(), 5)
        self.assertEqual(self.app.index.count("gmail"), 0)
        self.assertEqual(self.app.index.count("obsidian"), 1)
        self.app.handle("PATCH", "index/settings", {"sources": {"obsidian": False}})
        self.assertEqual(self.app.index.count("obsidian"), 0)
        with self.assertRaises(InputError):
            self.app.handle("PATCH", "index/settings", {"sources": {"fax": True}})
        with self.assertRaises(InputError):
            self.app.handle("POST", "index/scan", {"window": "30d", "sources": ["obsidian"]})

    def test_source_interface_takes_a_new_source(self):
        class Notes(sources.Source):
            key, label, unit = "signal", "Signal", "messages"

            def targets(self, ctx):
                return [sources.target("signal", "local", "Signal", "", self.unit)]

            def estimate(self, ctx, tgt, since):
                return 3

            def step(self, ctx, tgt, since, until, cursor):
                n = (cursor or {}).get("n", 0)
                items = [{"source": "signal", "account": "local", "source_id": f"s{n}", "kind": "message",
                          "title": "Chat with Amber", "body": f"message {n}", "ts": T0.timestamp() - n}]
                return items, ({"n": n + 1} if n < 2 else None)
        sc = scan.Scanner(self.app.store, self.app.index, None, clock=lambda: self.now[0], sleep=lambda s: None,
                          sources=sources.all_sources() + [Notes()])
        sc.index.set_meta("settings", None)
        with mock.patch.object(scan, "SOURCE_KEYS", scan.SOURCE_KEYS + ("signal",)):
            sc.start("30d", ["signal"])
            sc.run_steps(10)
        self.assertEqual(self.app.index.count("signal"), 3)
        self.assertEqual(len(self.app.index.search("message", source="signal")), 3)


class ApiTest(Env):
    def setUp(self):
        super().setUp()
        self.httpd = server.make_server("127.0.0.1", 0, app=self.app)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None, header=True):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        headers = {"X-Callsheet": "1"} if header else {}
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, (json.loads(text) if text else None)

    def test_routes(self):
        self.connect("me@example.com")
        self.mails("me@example.com", 12)
        s, st = self.call("GET", "/api/index/status")
        self.assertEqual(s, 200)
        self.assertTrue(st["available"])
        self.assertIsNone(st["job"])
        self.assertEqual([w["key"] for w in st["windows"]], ["30d", "90d", "1y", "5y", "all"])
        self.assertEqual([x["key"] for x in st["sources"]], ["gmail", "calendar", "slack", "drive", "obsidian", "signal"])
        s, est = self.call("POST", "/api/index/estimate", {"window": "30d", "sources": ["gmail"]})
        self.assertEqual((s, est["targets"][0]["count"]), (200, 12))
        self.assertEqual(self.call("POST", "/api/index/estimate", {"window": "3y"})[0], 400)
        s, out = self.call("POST", "/api/index/scan", {"window": "30d", "sources": ["gmail"]})
        self.assertEqual((s, out["job"]["state"]), (200, "running"))
        self.assertEqual(self.call("POST", "/api/index/scan", {"window": "30d"})[0], 409)
        self.assertEqual(self.call("POST", "/api/index/pause", {})[1]["job"]["state"], "paused")
        self.assertEqual(self.call("POST", "/api/index/resume", {})[1]["job"]["state"], "running")
        self.steps()
        s, res = self.call("GET", "/api/index/search?q=venue+ref&source=gmail&limit=5&from=2026-10-01&to=2026-10-28")
        self.assertEqual(s, 200)
        self.assertEqual(len(res["results"]), 5)
        self.assertEqual(res["q"], "venue ref")
        self.assertEqual(self.call("GET", "/api/index/search?q=x&from=yesterday")[0], 400)
        self.assertEqual(self.call("GET", "/api/index/search?q=x&source=nope")[0], 400)
        s, one = self.call("GET", f"/api/index/item/{res['results'][0]['id']}")
        self.assertEqual(s, 200)
        self.assertIn("Can you confirm the venue", one["body"])
        self.assertEqual(self.call("GET", "/api/index/item/999999")[0], 404)
        self.assertEqual(self.call("GET", "/api/index/item/abc")[0], 404)
        self.assertEqual(self.call("POST", "/api/index/cancel", {})[0], 409)   # it's done
        s, st = self.call("GET", "/api/index/status")
        self.assertEqual(st["stats"]["sources"]["gmail"]["items"], 12)
        self.assertEqual(self.call("DELETE", "/api/index", {"confirm": "wrong"})[0], 400)
        self.assertEqual(self.call("DELETE", "/api/index")[0], 400)
        s, after = self.call("DELETE", "/api/index", {"confirm": st["wipe_token"]})
        self.assertEqual(s, 200)
        self.assertNotEqual(after["wipe_token"], st["wipe_token"])
        self.assertEqual(self.app.index.count(), 0)
        s, sett = self.call("PATCH", "/api/index/settings", {"vault": "/nope"})
        self.assertEqual(s, 400)
        self.assertIn("inside /share or /media", sett["error"])
        self.assertEqual(self.call("GET", "/api/index/settings")[1]["vault"], "")

    def test_auth(self):
        for method, path in (("POST", "/api/index/scan"), ("POST", "/api/index/pause"), ("DELETE", "/api/index"),
                             ("PATCH", "/api/index/settings"), ("POST", "/api/index/drive/start"),
                             ("POST", "/api/index/estimate")):
            s, _ = self.call(method, path, {"window": "30d"}, header=False)
            self.assertEqual(s, 403, path)
        self.assertEqual(self.call("GET", "/api/index/scan")[0], 405)
        self.assertEqual(self.call("POST", "/api/index/search", {})[0], 405)

    def test_no_secrets_and_drive_errors(self):
        self.connect("me@example.com")
        s, st = self.call("GET", "/api/index/status")
        text = json.dumps(st)
        for secret in ("1//rt-", "ya29.", CLIENT_SECRET):
            self.assertNotIn(secret, text)
        self.assertEqual(self.call("POST", "/api/index/drive/start", {"account_id": "nope"})[0], 404)
        self.assertEqual(self.call("POST", "/api/index/drive/start", {})[0], 400)
        s, out = self.call("POST", "/api/index/drive/finish", {"flow_id": "x", "pasted": "http://127.0.0.1:8765/?code=a"})
        self.assertEqual(s, 400)


class StaticChecks(unittest.TestCase):
    APP = ROOT / "callsheet" / "app"

    def test_config_maps_and_backup_exclude(self):
        cfg = (ROOT / "callsheet" / "config.yaml").read_text()
        self.assertIn("type: share", cfg)
        self.assertIn("type: media", cfg)
        self.assertEqual(cfg.count("read_only: true"), 2)
        # one backup_exclude list (0.5.1's signal-cli entries plus the index); a second key would override the first
        self.assertEqual(cfg.count("\nbackup_exclude:"), 1)
        m = re.search(r"^backup_exclude:\n((?:  - .*\n)+)", cfg, re.M)
        self.assertIn("index.db*", re.findall(r'  - "([^"]+)"', m.group(1)))

    def test_index_code_opens_no_connections(self):
        for name in ("index.py", "sources.py", "scan.py"):
            text = (self.APP / name).read_text()
            for mod in ("urllib.request", "http.client", "socket", "ssl", "subprocess"):
                self.assertNotIn("import " + mod, text, name)

    def test_page_section(self):
        js = (self.APP / "static" / "app.js").read_text()
        start = js.index("0.6.0 index")
        sec = js[start:]
        self.assertNotIn("innerHTML", sec)
        self.assertIn("api('GET', 'api/index/status')", sec)
        self.assertNotIn("'/api/", sec)
        self.assertIn("#knowme", js)


if __name__ == "__main__":
    unittest.main()
