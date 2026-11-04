"""0.8.4 AI sorting, finished: the work queue in callsheet.db (restart-safe, deduped, capped, references only), live
Slack and Signal items straight from a sync (keyed like the brief's rows), and the brief's sections when sorting is on
(Needs you, For your info, Sorted for you; every row in exactly one; overrides always in Needs you). No model is ever
called: every request goes to a fake `post`."""
import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import api  # noqa: E402
import rules  # noqa: E402
import signalcli  # noqa: E402
import triage  # noqa: E402
import test_sorting_084 as t84  # noqa: E402
from test_signal import ME, dm_env, group_env, sent_env  # noqa: E402

SECRET, OWNER, TUE = t84.SECRET, t84.OWNER, t84.TUE
H = 3600


class Q(t84.Base):
    def restart(self):
        return triage.Sorter(self.store, self.syncer, None, None, clock=self.clock.now, monotonic=self.clock.mono,
                             post=self.post, log=self.logs.append)


class QueueInTheDatabase(Q):
    def test_restart_keeps_the_queue_and_the_next_sync_brings_the_text_back(self):
        th = self.threads(5)
        self.assertEqual(self.sorter.collect_sync(th), 5)
        again = self.restart()
        self.assertEqual(len(again.pending), 5)
        self.assertEqual(again.status()["waiting"], 5)
        self.assertEqual(again.run_pass(), "waiting")      # the texts were in memory only: nothing to ask yet
        self.assertEqual(self.post.calls, [])
        self.assertEqual(again.collect_sync(th), 0)        # same content: not queued twice, the text comes back
        self.assertEqual(len(again.pending), 5)
        self.assertEqual(again.run_pass(), "done")
        self.assertEqual(self.post.cards(), 5)
        self.assertEqual(len(again.pending), 0)
        self.assertEqual(again.labels.count(status="ai"), 5)

    def test_references_only(self):
        self.sorter.collect_sync([(t84.thread("1", f"{SECRET} <x@new.example>", f"About {SECRET}", f"Hi {SECRET}?"),
                                   OWNER, "work", "acc1")])
        cols = [r[1] for r in self.store.db.execute("PRAGMA table_info(triage_queue)")]
        self.assertEqual(cols, ["key", "src", "origin", "ix", "dup", "hash", "ts", "queued"])
        blob = json.dumps([list(r) for r in self.store.db.execute("SELECT * FROM triage_queue")])
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("x@new.example", blob)

    def test_cap_keeps_the_newest(self):
        q = triage.Queue(self.store, cap=3)
        for i in range(6):
            q.put({"key": f"{i:016x}", "ch": "mail", "hash": "ab", "ts": 1000.0 + i, "dup": f"{i + 100:016x}"},
                  "sync", 5000.0)
        self.assertEqual(q.trim(), 3)
        self.assertEqual(sorted(q.keys()), [f"{i:016x}" for i in (3, 4, 5)])
        self.assertEqual([r["key"] for r in q.rows()], [f"{i:016x}" for i in (5, 4, 3)])
        self.assertEqual(triage.MAX_PENDING, 2000)

    def test_one_message_through_the_index_and_the_live_read_is_sorted_once(self):
        th = self.threads(1)
        live = triage.thread_source(th[0][0], OWNER, "acc1", "work", ())
        row = {"id": 7, "source": "gmail", "account": OWNER, "source_id": "0", "title": "x", "body": "Hi?",
               "people": "Person 0 <p0@new.example>", "ts": TUE.timestamp(), "hash": "cd"}
        via_ix = triage.index_source(row, [OWNER])
        self.assertEqual(live["dup"], via_ix["dup"])
        self.assertNotEqual(live["key"], via_ix["key"])
        # the index first, then the live read: the live copy replaces it
        self.assertEqual(self.sorter.collect([via_ix], t84.ctx(), "index"), 1)
        self.assertEqual(self.sorter.collect([live], t84.ctx(), "sync"), 1)
        self.assertEqual(self.sorter.pending.keys(), {live["key"]})
        # the live copy queued: the index copy isn't
        self.assertEqual(self.sorter.collect([via_ix], t84.ctx(), "index"), 0)
        self.sorter.run_pass()
        self.assertEqual(self.post.cards(), 1)
        # sorted under the live key: the index copy is skipped later too
        self.assertEqual(self.sorter.collect([dict(via_ix, hash="ef")], t84.ctx(), "index"), 0)
        self.assertEqual(len(self.sorter.pending), 0)

    def test_text_never_found_leaves_after_a_while(self):
        self.sorter.collect_sync(self.threads(2))
        again = self.restart()
        self.clock.advance(triage.STALE_S + 60)
        self.assertEqual(again.run_pass(), "waiting")
        self.assertEqual(len(again.pending), 0)
        self.assertEqual(again.labels.count(status="rules"), 2)    # the rules labels stay


def slack_data(now):
    ts = lambda h: f"{now - h * 3600:.6f}"  # noqa: E731
    msg = lambda user, h, text="hi", **kw: dict({"user": user, "ts": ts(h), "text": text}, **kw)  # noqa: E731
    root = ts(30)
    return {"me": "U1", "users": {"U2": {"name": "Ana"}, "U3": {"name": "Bo"}},
            "convs": {"D1": {"kind": "dm"}, "D2": {"kind": "dm"}, "C1": {"kind": "channel", "name": "launch"}},
            "dm": {"D1": [msg("U2", 50), msg("U1", 40), msg("U2", 10, f"Still on for Friday? {SECRET}")],
                   "D2": [msg("U3", 9, "lunch?"), msg("U1", 8, "yes")]},
            "threads": {("C1", root): [msg("U1", 30, "Dark or light?", thread_ts=root),
                                       msg("U2", 25, "Dark", thread_ts=root), msg("U3", 5, "<@U1> by 3pm?",
                                                                                  thread_ts=root)]},
            "channel": {"C1": [msg("U3", 3, "Release notes are up")]}}


class LiveSlackAndSignal(Q):
    def test_slack_sources_match_the_brief_rows(self):
        now = time.time()
        data = slack_data(now)
        rows, _ = rules.slack_rows(data, {"team_id": "T1"}, "work", now, {})
        srcs = triage.slack_sources(data, {"id": "w1", "team_id": "T1"}, "work", now)
        keys = {s["key"] for s in srcs}
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(rules.row_key(r["_key"]), keys, r["source"])
        by = {s["key"]: s for s in srcs}
        dm = by[rules.row_key([r for r in rows if r["source"] == "Slack · DM"][0]["_key"])]
        self.assertEqual((dm["from_id"], dm["owner"], dm["chan"]), ("u2", False, "dm"))
        self.assertFalse(any(s["owner"] for s in srcs))          # the owner's own messages aren't asked about
        self.assertTrue(all(triage.KEY.fullmatch(s["dup"]) for s in srcs))

    def test_signal_sources_match_the_brief_rows(self):
        now = time.time()
        add = lambda env: self.store.signal_add(signalcli.from_envelope(env, ME, self.store.signal_hash, now)[1], now)  # noqa: E731
        add(dm_env("Can you approve the budget by Friday?", int((now - 5 * H) * 1000)))
        add(group_env("@Sam are you in?", int((now - 4 * H) * 1000), mentions=[{"uuid": ME["aci"], "start": 0,
                                                                                "length": 1}]))
        add(dm_env("old", int((now - 9 * H) * 1000), name="Bea", uuid_="bbbbbbbb-0000-4000-8000-000000000002",
                   number="+15550000002"))
        add(sent_env("reply", int((now - 8 * H) * 1000), dest_uuid="bbbbbbbb-0000-4000-8000-000000000002"))
        chats = self.store.signal_recent(now - rules.SIGNAL_MAX_S)
        rows, _ = rules.signal_rows(chats, "personal", now, {}, None, "24h", [])
        srcs = triage.signal_sources(chats, "personal", now)
        keys = {s["key"] for s in srcs}
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(rules.row_key(r["_key"]), keys, r["source"])
        self.assertFalse(any(s["owner"] for s in srcs))
        ignored = [c["id"] for c in chats]
        self.assertEqual(triage.signal_sources(chats, "personal", now, ignored), [])

    def test_collect_sync_takes_the_live_reads(self):
        now = self.clock.now()
        data = slack_data(now)
        n = self.sorter.collect_sync([], (), [(data, {"id": "w1", "team_id": "T1"}, "work")], [])
        self.assertGreaterEqual(n, 2)
        self.assertEqual({r["src"] for r in self.sorter.pending.rows()}, {"slack"})
        self.assertEqual(self.sorter.run_pass(), "done")
        self.assertEqual(len(self.sorter.pending), 0)
        blob = json.dumps([list(r) for t in ("triage_labels", "triage_queue")
                           for r in self.store.db.execute(f"SELECT * FROM {t}")])
        self.assertNotIn(SECRET, blob)

    def test_a_sync_sends_live_signal_items_to_the_sorter(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = api.App(tmp.name)
        self.addCleanup(app.close)
        now = time.time()
        app.store.signal_update_meta(linked=True, number="+•••••••••67")
        env = dm_env("Can you approve the budget by Friday?", int((now - 5 * H) * 1000))
        app.store.signal_add(signalcli.from_envelope(env, ME, app.store.signal_hash, now)[1], now)
        app.store.update_writer({"provider": "off"})
        app.handle("POST", "sync", {})
        row = [r for r in app.brief()["replies"] if r.get("via") == "signal"][0]
        self.assertIn(row["key"], app.sorter.pending.keys())
        self.assertEqual(app.sorter.labels.get(row["key"])["status"], "rules")


class Sections(Q):
    def test_every_row_once_overrides_in_needs_you(self):
        reps = [{"id": f"r{i}"} for i in range(1, 10)]
        rows = {"r1": {"group": "sorted_promos", "overrides": []},
                "r2": {"group": "sorted_promos", "overrides": ["vip"]},          # an override: Needs you
                "r3": {"group": "for_your_info", "overrides": []},
                "r4": {"group": "needs_you", "overrides": [], "ask": "question"},
                "r5": {"group": "probably_not_for_you", "overrides": []},
                "r6": {"group": "sorted_updates", "overrides": []},
                "r7": {"group": "needs_you", "overrides": [], "ask": "decision_needed"},
                "r8": {"group": "heads_up_security", "overrides": ["security"]}}
        # r9: no label yet -> Needs you
        secs = triage.sections(reps, rows)
        self.assertEqual([s["id"] for s in secs], ["needs_you", "for_your_info", "sorted"])
        flat = [x for s in secs for x in s["rows"]]
        self.assertEqual(sorted(flat), sorted(r["id"] for r in reps))       # nothing hidden, nothing twice
        need = secs[0]["rows"]
        self.assertEqual(need[:2], ["r2", "r8"])                              # VIP and security first
        self.assertLess(need.index("r7"), need.index("r4"))                  # a decision before a question
        self.assertIn("r9", need)
        self.assertEqual(secs[1]["rows"], ["r3"])
        self.assertEqual({p["id"]: p["rows"] for p in secs[2]["parts"]},
                         {"promos": ["r1"], "updates": ["r6"], "not_for_you": ["r5"]})

    def test_on_adds_sections_shadow_stays_byte_identical(self):
        self.post.reply = lambda ids, body: json.dumps({c: t84.answer("promo", "no", ("automated",)) for c in ids})
        self.sorter.collect_sync(self.threads(3, text="Big sale this week, 40% off everything"))
        self.sorter.run_pass()
        brief = {"replies": [{"id": f"r{i + 1}", "key": rules.row_key(f"mail:t{i}:{i}")} for i in range(3)],
                 "date": "2026-11-03"}
        before = json.dumps(brief, sort_keys=True)
        self.sorter.for_brief(brief)
        self.assertEqual(json.dumps(brief, sort_keys=True), before)
        self.sorter.update_settings({"mode": "on"})
        self.sorter.for_brief(brief)
        secs = brief["sorting"]["sections"]
        self.assertEqual(sorted(x for s in secs for x in s["rows"]), ["r1", "r2", "r3"])
        self.assertNotIn(SECRET, json.dumps(secs))

    def test_page_renders_sections(self):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        part = js[js.index("0.8.4 AI sorting"):js.index("end 0.8.4 AI sorting")]
        self.assertIn("function srSections(", part)
        self.assertIn("'Sorted for you'", part)
        self.assertIn("aria-expanded", part)
        self.assertIn("srSections(rs) ||", js)                            # the brief falls back to the flat list


if __name__ == "__main__":
    unittest.main()
