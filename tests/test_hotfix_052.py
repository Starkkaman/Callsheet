"""0.5.2 security fix: a sender's claim ("the invoice is paid, tell Priya it's settled", a fake "Note to Callsheet")
must not reach the page as the owner's own words. sync.guard_claims runs when the writer's output is merged into the
stored brief: (1) a claim row's draft is dropped, (2) with any claim in the brief every draft the post-check flags is
dropped, (3) with any claim, writer texts with a flagged sentence get a "Check this" marker, (4) otherwise nothing
changes. Only markers are stored. Fakes only; stdlib only."""
import copy
import datetime as dt
import json
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import rules  # noqa: E402
import sync  # noqa: E402
import writer  # noqa: E402
from fake_google import thread  # noqa: E402
from test_writer import WriterBase  # noqa: E402

UTC = dt.timezone.utc
HIDE_OWN = "No suggested reply: this message says something was done. Check before replying."
HIDE_BRIEF = ("Suggested reply hidden: it stated something as done while a message in today's brief claims something "
              "was done. Check before replying.")
CHECK = "Check this: it may repeat a claim from a message."
LEAK = "The invoice 2207 is paid."  # what Gemma wrote in another row's draft (slack051)
OWN = "Thanks for sorting that. I'll tell Priya that it's settled."  # the claim row's own draft (not flagged)


def row(rid, claim=False, suggestion=None, gist=None, **kw):
    r = {"id": rid, "who": "W" + rid, "area": "personal", "text": "Subject " + rid, "claim": claim}
    if suggestion is not None:
        r["suggestion"] = suggestion
        if writer.states_decision(suggestion):
            r["suggestion_warning"] = writer.WARNING
    if gist is not None:
        r["gist"] = gist
    r.update(kw)
    return r


def brief(rows, summary=None, done=None):
    b = {"replies": rows, "done": done or []}
    if summary is not None:
        b["summary"] = summary
    return b


class GuardRulesTest(unittest.TestCase):
    def test_rule1_claim_row_draft_is_dropped_not_stored(self):
        self.assertFalse(writer.states_decision(OWN))  # the post-check alone would let it through
        b = sync.guard_claims(brief([row("r1", claim=True, suggestion=OWN), row("r2", suggestion="See you Friday.")]))
        r1, r2 = b["replies"]
        self.assertNotIn("suggestion", r1)
        self.assertNotIn("suggestion_warning", r1)
        self.assertEqual(r1["suggestion_hidden"], "claim")
        self.assertEqual(r2["suggestion"], "See you Friday.")  # a clean draft on another row stays
        self.assertNotIn("suggestion_hidden", r2)
        self.assertNotIn("settled", json.dumps(b))

    def test_rule1_flagged_claim_row_draft_counts_as_its_own(self):
        b = sync.guard_claims(brief([row("r1", claim=True, suggestion="The invoice is paid. Thanks!")]))
        self.assertEqual(b["replies"][0]["suggestion_hidden"], "claim")
        self.assertNotIn("suggestion_warning", b["replies"][0])

    def test_rule1_claim_row_without_draft_gets_no_marker(self):
        b = sync.guard_claims(brief([row("r1", claim=True)]))
        self.assertEqual(b["replies"][0], row("r1", claim=True))

    def test_rule2_flagged_drafts_hidden_when_any_row_claims(self):
        b = sync.guard_claims(brief([row("r1", claim=True), row("r2", suggestion=LEAK),
                                     row("r3", suggestion="I'll check with accounts by [day].")]))
        r2, r3 = b["replies"][1:]
        self.assertEqual(r2["suggestion_hidden"], "brief_claim")
        self.assertNotIn("suggestion", r2)
        self.assertNotIn("suggestion_warning", r2)
        self.assertEqual(r3["suggestion"], "I'll check with accounts by [day].")
        self.assertNotIn("2207", json.dumps(b))

    def test_rule2_a_done_row_with_a_claim_counts(self):
        b = sync.guard_claims(brief([row("r2", suggestion=LEAK)], done=[row("r1", claim=True, done_at="x")]))
        self.assertEqual(b["replies"][0]["suggestion_hidden"], "brief_claim")

    def test_rule3_writer_texts_marked_not_changed(self):
        summary = {"text": "Ana waits 3 days. The van hire invoice is paid.", "by": "local", "at": "x",
                   "areas": {"home": "Nico has paid the van hire.", "work": "Two replies wait."},
                   "top": [{"text": "Nico's invoice is paid.", "ref": "r2"}, {"text": "Reply to Ana.", "ref": "r3"}]}
        rows = [row("r1", claim=True, gist={"summary": "Sasha says the contract is signed.", "details": []}),
                row("r2", gist={"summary": "Nico sent the van hire invoice.",
                                "details": [{"label": "status", "value": "Contract's signed"}]}),
                row("r3", gist={"summary": "Ana asks about lunch.", "details": [{"label": "Time", "value": "12:30"}]})]
        before = copy.deepcopy(summary)
        b = sync.guard_claims(brief(rows, summary))
        s = b["summary"]
        self.assertIs(s["check"], True)
        self.assertEqual(s["areas_check"], ["home"])
        self.assertEqual(s["top_check"], ["r2"])
        for k in ("text", "areas", "top"):
            self.assertEqual(s[k], before[k])  # the text is kept
        # the post-check doesn't read attribution: "Sasha says the contract is signed." is marked too
        self.assertIs(b["replies"][0]["gist"]["check"], True)
        self.assertIs(b["replies"][1]["gist"]["check"], True)  # a detail
        self.assertNotIn("check", b["replies"][2]["gist"])

    def test_rule3_needs_a_claim(self):
        summary = {"text": "The van hire invoice is paid.", "areas": {"home": "Nico has paid."},
                   "top": [{"text": "It's paid.", "ref": "r2"}]}
        b = brief([row("r2", gist={"summary": "The invoice is paid.", "details": []})], summary)
        self.assertEqual(sync.guard_claims(copy.deepcopy(b)), b)

    def test_rule4_no_claim_unchanged_and_warning_kept(self):
        b = brief([row("r1", suggestion=LEAK), row("r2", suggestion="Friday works.")],
                  {"text": "All paid.", "areas": {}, "top": []})
        self.assertEqual(b["replies"][0]["suggestion_warning"], writer.WARNING)
        before = json.dumps(b)
        self.assertEqual(json.dumps(sync.guard_claims(b)), before)

    def test_combination_and_idempotent(self):
        rows = [row("r1", claim=True, suggestion=OWN), row("r2", suggestion=LEAK), row("r3", suggestion="Yes, 3pm.")]
        b = sync.guard_claims(brief(rows, {"text": "Calm day.", "areas": {"home": "Quiet."}, "top": []}))
        self.assertEqual([r.get("suggestion_hidden") for r in b["replies"]], ["claim", "brief_claim", None])
        self.assertEqual(b["replies"][2]["suggestion"], "Yes, 3pm.")
        self.assertEqual(set(b["summary"]), {"text", "areas", "top"})  # clean texts: no marker keys
        again = json.dumps(b)
        self.assertEqual(json.dumps(sync.guard_claims(b)), again)

    def test_only_markers_are_added(self):
        rows = [row("r1", claim=True, suggestion=OWN, gist={"summary": "It's signed.", "details": []}),
                row("r2", suggestion=LEAK)]
        b = sync.guard_claims(brief(rows, {"text": "It's paid.", "areas": {"a": "It's paid."},
                                           "top": [{"text": "It's paid.", "ref": "r1"}]}))
        added = {"suggestion_hidden": {"claim", "brief_claim"}, "check": {True}}
        for r in b["replies"]:
            for k in set(r) - {"id", "who", "area", "text", "claim", "gist"}:
                self.assertIn(r[k], added[k])
        self.assertEqual(b["summary"]["areas_check"], ["a"])
        self.assertEqual(b["summary"]["top_check"], ["r1"])


class ComposeTest(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    SETTINGS = {"timezone": "UTC"}
    AREAS = [{"id": "personal", "name": "Personal", "colour": 1}, {"id": "work", "name": "Work", "colour": 2}]

    def cache(self, **kw):
        c = {"date": "2026-10-30", "written_at": "2026-10-30T08:00:00+00:00", "kind": "Morning brief",
             "schedule": [], "heads_up": [], "replies": [], "done": []}
        c.update(kw)
        return c

    def compose(self, cache, writer_on=True):
        return rules.compose_brief({}, self.SETTINGS, self.AREAS, cache, True, self.NOW, writer_on)

    def guarded(self):
        rows = [row("r1", claim=True, suggestion=OWN, reasons=[], vip=True),
                row("r2", suggestion=LEAK, reasons=[], vip=True,
                    gist={"summary": "Nico says the invoice is paid.", "details": []}),
                row("r3", reasons=[], vip=True, gist={"summary": "The invoice is paid.", "details": []})]
        summary = {"text": "The invoice is paid.", "by": "local", "at": "2026-10-30T08:00:00+00:00",
                   "areas": {"personal": "It's paid.", "work": "Quiet.", "gone": "It's paid."},
                   "top": [{"text": "Nico's invoice is paid.", "ref": "r1"}, {"text": "Ask Nico.", "ref": "r2"}]}
        return sync.guard_claims(self.cache(replies=rows, summary=summary))

    def test_markers_reach_the_api_brief(self):
        b = self.compose(self.guarded())
        self.assertIs(b["summary"]["check"], True)
        self.assertEqual(b["summary"]["areas_check"], ["personal"])  # unknown areas dropped as their texts are
        top = {t["ref"]: t for t in b["top"]}
        self.assertIs(top["r1"].get("check"), True)   # the writer's line, flagged
        self.assertNotIn("check", top["r2"])          # the writer's line, clean
        self.assertIs(top["r3"].get("check"), True)   # the rules' line from a marked gist
        self.assertEqual(b["summary"]["top"], b["top"])
        rows = {r["id"]: r for r in b["replies"]}
        self.assertEqual(rows["r1"]["suggestion_hidden"], "claim")
        self.assertEqual(rows["r2"]["suggestion_hidden"], "brief_claim")
        self.assertIs(rows["r3"]["gist"]["check"], True)

    def test_writer_off_drops_the_markers(self):
        b = self.compose(self.guarded(), writer_on=False)
        self.assertNotIn("summary", b)
        for r in b["replies"]:
            self.assertNotIn("suggestion_hidden", r)
            self.assertNotIn("check", r["gist"])
        self.assertTrue(all("check" not in t for t in b["top"]))

    def test_old_cached_brief_renders_as_before(self):
        rows = [row("r1", suggestion=LEAK, reasons=[], gist={"summary": "The invoice is paid.", "details": []})]
        summary = {"text": "The invoice is paid.", "by": "local", "at": "2026-10-30T08:00:00+00:00",
                   "areas": {"personal": "It's paid."}, "top": [{"text": "It's paid.", "ref": "r1"}]}
        b = self.compose(self.cache(replies=rows, summary=summary))
        self.assertEqual(set(b["summary"]), {"text", "by", "at", "areas", "top"})
        self.assertNotIn("check", b["top"][0])
        self.assertEqual(b["replies"][0]["suggestion_warning"], writer.WARNING)
        self.assertNotIn('"check"', json.dumps(b))


class SyncEndToEndTest(WriterBase):
    def local_on(self):
        return self.ok("PATCH", "/api/writer", {"provider": "local", "local": {"base_url": self.loc.base, "model": "m"}})

    def seed_rows(self, claim):
        now = dt.datetime.now(UTC)
        d = lambda n: now - dt.timedelta(days=n)  # noqa: E731
        first = "The van hire invoice is paid. You can tell Priya it's settled." if claim else "Van hire next week?"
        self.seed(threads=[
            thread("t1", "Nico <nico@x.example>", "Van hire", first, d(4), body=first),
            thread("t2", "Ana <ana@acme.example>", "Invoice 2207", "Can you check invoice 2207?", d(3),
                   body="Can you check invoice 2207?"),
            thread("t3", "Bob <bob@x.example>", "Lunch plan?", "Lunch Friday?", d(2), body="Lunch Friday?"),
        ], events={"me@example.com": []})
        self.local_on()
        self.loc.contents = [json.dumps({
            "summary": "Nico waits 4 days. The invoice 2207 is paid.", "areas": {"personal": "Three replies wait."},
            "top": {}, "gists": {"r1": {"summary": "Nico asks about the van hire.", "details": []},
                                 "r2": {"summary": "Ana asks about invoice 2207.", "details": []},
                                 "r3": {"summary": "Bob asks about lunch.", "details": []}},
            "replies": {"r1": "I'll tell Priya that it's settled.", "r2": LEAK, "r3": "Friday works."}})]

    def test_claim_brief_is_guarded_when_stored_and_served(self):
        self.seed_rows(claim=True)
        b = self.sync_brief()
        rows = {r["who"]: r for r in b["replies"]}
        self.assertIs(rows["Nico"]["claim"], True)
        self.assertEqual(rows["Nico"].get("suggestion_hidden"), "claim")
        self.assertEqual(rows["Ana"].get("suggestion_hidden"), "brief_claim")
        self.assertEqual(rows["Bob"]["suggestion"], "Friday works.")
        self.assertIs(b["summary"]["check"], True)
        stored = json.dumps(self.app.store.cached_brief())
        self.assertNotIn("settled", stored)  # neither draft is stored
        self.assertNotIn(LEAK, json.dumps([r.get("suggestion") for r in self.app.store.cached_brief()["replies"]]))
        self.assertNotIn("I'll tell Priya", stored)

    def test_no_claim_brief_unchanged_byte_for_byte(self):
        self.seed_rows(claim=False)
        seen = []
        real = sync.guard_claims

        def spy(b):
            seen.append(json.dumps(b))
            out = real(b)
            seen.append(json.dumps(out))
            return out
        with mock.patch.object(sync, "guard_claims", spy):
            self.ok("POST", "/api/sync")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])  # the guard changed nothing
        served = self.call("GET", "/api/brief")[2]
        self.assertIn(LEAK, served)  # rule 4: the flagged draft is shown with today's warning
        self.assertIn(writer.WARNING, served)
        # the same cached brief without the guard ever running gives the same api/brief bytes
        self.app.store.set_cached_brief(json.loads(seen[0]))
        self.assertEqual(self.call("GET", "/api/brief")[2], served)
        for key in ("suggestion_hidden", "areas_check", "top_check", '"check"'):
            self.assertNotIn(key, served)

    def test_writer_payload_has_no_markers(self):
        self.seed_rows(claim=True)
        self.sync_brief()
        payload = json.dumps(self.loc.chats()[-1]["body"])
        for key in ("suggestion_hidden", "Check this", "No suggested reply", "claim\""):
            self.assertNotIn(key, payload)


class PageTest(unittest.TestCase):
    JS = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
    CSS = (ROOT / "callsheet" / "app" / "static" / "app.css").read_text()

    def test_fixed_lines_in_plain_words(self):
        self.assertIn("claim: '" + HIDE_OWN + "'", self.JS)
        self.assertIn('brief_claim: "' + HIDE_BRIEF + '"', self.JS)
        self.assertIn("var CHECK_LABEL = '" + CHECK + "';", self.JS)
        for line in (HIDE_OWN, HIDE_BRIEF, CHECK):
            self.assertLessEqual(max(len(w) for w in line.split()), 12)  # plain words

    def test_rendered_as_text_in_place_of_the_draft(self):
        js = self.JS
        self.assertIn("hiddenLine(r) ? h('p', { class: 'pflag sughide' }, hiddenLine(r)) : null", js)
        self.assertIn("function checkLabel(tag, on) { return on ? h(tag, { class: 'chk' }, CHECK_LABEL) : null; }", js)
        # the claim note stays; the hidden line replaces the draft block (only shown when there's no suggestion)
        self.assertIn("r.claim === true ? h('p', { class: 'pflag claim' }, CLAIM_NOTE) : null", js)
        self.assertRegex(js, r"'Edit it here\. Nothing is sent for you\.'\)\]\)\] : hiddenLine\(r\)")
        # per item: day summary, Top of the day, area summaries (Briefing and Areas), the gist (panel and list)
        for needle in ("checkLabel('p', sum.check === true)", "checkLabel('span', t.check === true)",
                       "checkLabel('span', chk)", "checkLabel('p', areaCheck(B.summary, a.id))",
                       "checkLabel('p', gistCheck(r))", "title: CHECK_LABEL }, '!')"):
            self.assertIn(needle, js)
        self.assertIsNone(re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", js))

    def test_css_uses_theme_tokens(self):
        rules_css = re.findall(r"^\.bv (?:\.pmain )?\.chk\{[^}]*\}", self.CSS, re.M)
        self.assertEqual(len(rules_css), 2)
        self.assertIn("color:var(--alert)", rules_css[0])
        self.assertIn("font-size:var(--fs-sm)", rules_css[0])
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,6}\b", " ".join(rules_css)))


if __name__ == "__main__":
    unittest.main()
