"""0.4.1: the inbound sender's-claim check. A reply row whose latest incoming message says something was approved,
paid, signed, released and so on gets claim: true (writer.states_decision over that message only), stored with the
brief, shown on the page with textContent, never sent to the writer, no message text kept. Fakes only; stdlib only."""
import datetime as dt
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import rules  # noqa: E402
import sync  # noqa: E402
from fake_google import thread  # noqa: E402
from test_writer import WriterBase  # noqa: E402

UTC = dt.timezone.utc
NOTE = "This message says something was approved, paid, signed or released. Check before replying."


class RowFlagTest(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    ME = "me@example.com"
    ANA = "Ana <ana@x.example>"

    def d(self, n):
        return self.NOW - dt.timedelta(days=n)

    def mail(self, t):
        row = rules.reply_row(t, self.ME, "personal", self.NOW.timestamp(), 2, {})
        self.assertIsNotNone(row)
        claim = sync.inbound_claim(row)
        self.assertNotIn("_inbound", row)  # popped: no message text is left on the row
        return claim

    def test_mail_last_incoming_message_says_paid(self):
        self.assertIs(self.mail(thread("t1", self.ANA, "Invoice 1042", "Hi Sam, the invoice has been paid.", self.d(3))),
                      True)
        # a subject the sender wrote counts too
        self.assertIs(self.mail(thread("t2", self.ANA, "Budget approved", "See you Friday.", self.d(3))), True)

    def test_owners_own_message_is_not_inbound(self):
        t = thread("t1", self.ANA, "Re: Budget approved", "Thanks, got it.", self.d(3), earlier=[
            ("Sam <me@example.com>", self.d(4), ("SENT",))])
        t["messages"][0]["snippet"] = "The invoice has been paid and the budget is approved."
        # the owner's earlier message and the reply subject (an earlier writer chose it) don't count
        self.assertIs(self.mail(t), False)
        # the owner's message last: no row at all
        t = thread("t2", "Sam <me@example.com>", "Invoice", "The invoice has been paid.", self.d(3), labels=("SENT",))
        self.assertIsNone(rules.reply_row(t, self.ME, "personal", self.NOW.timestamp(), 2, {}))

    def test_questions_and_conditions_are_not_claims(self):
        self.assertIs(self.mail(thread("t1", self.ANA, "Invoice", "Has the invoice been paid?", self.d(3))), False)
        self.assertIs(self.mail(thread("t2", self.ANA, "Invoice", "Once the invoice is paid, I'll send the keys.",
                                       self.d(3))), False)

    def test_slack_dm(self):
        now = self.NOW.timestamp()
        ts = lambda h: f"{now - h * 3600:.6f}"  # noqa: E731
        msg = lambda user, h, text: {"user": user, "ts": ts(h), "text": text}  # noqa: E731

        def flags(dm):
            data = {"me": "U1", "users": {"U2": {"name": "Ana"}}, "convs": {"D1": {"kind": "dm"}}, "dm": {"D1": dm}}
            rows, _ = rules.slack_rows(data, {"team_id": "T1"}, "work", now, {})
            return [sync.inbound_claim(r) for r in rows]

        self.assertEqual(flags([msg("U1", 9, "Hi"), msg("U2", 8, "The contract is signed.")]), [True])
        self.assertEqual(flags([msg("U1", 9, "I've paid the invoice."), msg("U2", 8, "thanks!")]), [False])
        self.assertEqual(flags([msg("U2", 8, "Is the contract signed?")]), [False])
        self.assertEqual(flags([msg("U2", 8, "I put [paid] in the subject line.")]), [False])


class SyncClaimTest(WriterBase):
    def seed_claims(self):
        now = dt.datetime.now(UTC)
        d = lambda n: now - dt.timedelta(days=n)  # noqa: E731
        self.seed(threads=[
            thread("t1", "Priya N <priya@acme.example>", "Invoice 1042", "Hi Sam, the invoice has been paid.", d(3),
                   body="Hi Sam, the invoice has been paid."),
            thread("t2", "Bob <bob@x.example>", "Invoice 1043", "Has the invoice been paid?", d(3),
                   body="Has the invoice been paid?"),
            thread("t3", "Eve <eve@x.example>", "Quick update", "Quick update from me", d(3),
                   body="Quick update from me.\n\nThe deposit was released this morning."),
        ], events={"me@example.com": []})

    def test_flag_is_stored_and_served_without_a_writer(self):
        self.seed_claims()
        b = self.sync_brief()
        claim = {r["who"]: r.get("claim") for r in b["replies"]}
        # no writer: the body isn't read, so Eve's row (claim only in the body) stays unflagged
        self.assertEqual(claim, {"Priya N": True, "Bob": False, "Eve": False})
        stored = self.app.store.cached_brief()
        self.assertEqual({r["who"]: r.get("claim") for r in stored["replies"]}, claim)
        # only the flag is kept: no inbound text on rows, nothing new in brief_refs
        refs = json.dumps(self.app.store.brief_refs())
        self.assertNotIn("_inbound", json.dumps(stored))
        self.assertNotIn("has been paid", refs)
        self.assertNotIn("claim", refs)

    def test_writer_reads_the_body_and_never_gets_the_flag(self):
        self.seed_claims()
        self.ok("PATCH", "/api/writer", {"provider": "local", "local": {"base_url": self.loc.base, "model": "m"}})
        b = self.sync_brief()
        claim = {r["who"]: r.get("claim") for r in b["replies"]}
        self.assertEqual(claim, {"Priya N": True, "Bob": False, "Eve": True})
        self.assertEqual({r["who"]: r.get("claim") for r in self.app.store.cached_brief()["replies"]}, claim)
        payload = self.loc.chats()[-1]["body"]["messages"][1]["content"]
        self.assertNotIn("claim", payload)
        self.assertNotIn("Check before replying", payload)
        self.assertNotIn("released this morning", json.dumps(self.app.store.cached_brief()))
        # the flag stays when the writer is turned off again
        self.ok("PATCH", "/api/writer", {"provider": "off"})
        self.assertEqual({r["who"]: r.get("claim") for r in self.ok("GET", "/api/brief")["replies"]}, claim)


class PageTest(unittest.TestCase):
    def test_app_js_renders_the_note_as_text(self):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        self.assertIn("var CLAIM_NOTE = '" + NOTE + "';", js)
        # the panel note and the list marker go through h() (text nodes), only for claim === true
        self.assertIn("r.claim === true ? h('p', { class: 'pflag claim' }, CLAIM_NOTE) : null", js)
        self.assertRegex(js, r"r\.claim === true \? h\('span', \{ class: 'cmk', 'aria-hidden': 'true', "
                             r"title: CLAIM_NOTE \}, '!'\) : null")
        self.assertIsNone(re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", js))
        self.assertIn(".bv .rmain .cmk{", (ROOT / "callsheet" / "app" / "static" / "app.css").read_text())


if __name__ == "__main__":
    unittest.main()
