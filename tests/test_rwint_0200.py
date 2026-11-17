"""0.20.0 integration of the rework stages (texts, chat and preferences, the new Today, visual blocks): the seams
between them. A live text notification (sms_live, 0.16.1) counts as a text for rules set by chat (0.17.0) and on Today
(0.18.0); a VIP's asks (0.17.0) come first in Today's Needs you. Ledger facts for the visual blocks (0.19.0): the
amount (+ currency) is copied from the sentence the model cited by the 0.12.2 amount rules, never from the model's own
words; a package's stage and a task's "due" need the message's words (and a date); visuals.py shows the ledger's facts.
No live model (a scripted fake)."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "callsheet" / "app"), str(ROOT / "tests")]
import extract  # noqa: E402
import ledger as L  # noqa: E402
import msgstore  # noqa: E402
import prefs  # noqa: E402
import today  # noqa: E402
import today_fixtures as F  # noqa: E402
import visuals  # noqa: E402


def _row(kind, **f):
    base = {"vip": {"person": "", "sources": [], "dm_only": False, "alerts": True, "needs_you": True},
            "alert_rule": {"what": "all", "sources": [], "except": [], "mode": "never", "start": "", "end": ""}}
    return {"kind": kind, "fields": {**base[kind], **f}}


class LiveTextsFollowTextRules(unittest.TestCase):
    def test_vip_for_texts_covers_live_texts(self):
        # 0.22.0 VIPs in one store: a VIP for texts is a VIP on Setup's list with its options (test_vips_0220.py)
        r = prefs.Rules([], vips=[{"id": "vip_1", "name": "Lena", "email": "", "area_id": "personal",
                                   "opts": {"sources": ["sms"]}}])
        for src in ("sms", "sms_live"):
            self.assertIsNotNone(r.vip(src, "+15550100", ["Lena Park"]), src)
        self.assertIsNone(r.vip("slack", "U1", ["Lena Park"]))

    def test_alert_rule_for_texts_covers_live_texts(self):
        r = prefs.Rules([_row("alert_rule", sources=["sms"], mode="never")])
        self.assertEqual(r.alert("asks", [("sms_live", "+15550100", ["Lena"])], None), "off")
        self.assertIsNone(r.alert("asks", [("gmail", "a@x.org", ["Lena"])], None))


class TodaySeams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [0.0]
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.ex = extract.Extractor(self.ms, F.TZ, F.OWNER, None, clock=lambda: self.now[0])
        self.td = today.Today(self.ms, self.ex.lv, lambda: self.now[0], alerter=F.FakeAlerter())
        F.seed(self.ms, self.ex, self.now, self.td, "1220")

    def tearDown(self):
        self.ms.close()
        self.tmp.cleanup()

    def test_vip_asks_first_in_needs_you(self):
        rows = self.td.view()["needs_you"]["rows"]
        self.assertGreaterEqual(len(rows), 2)
        newest = rows[-1]["id"]
        self.ex.lv.vip_of = lambda r: r["id"] == newest
        rows2 = self.td.view()["needs_you"]["rows"]
        self.assertEqual(rows2[0]["id"], newest)
        self.assertTrue(rows2[0]["vip"])
        self.assertEqual([r["id"] for r in rows2[1:]], [r["id"] for r in rows[:-1]])   # the rest stay oldest first

    def test_live_texts_read_as_texts(self):
        self.assertEqual(today.SOURCES["sms_live"], today.SOURCES["sms"])
        self.assertEqual(today.reply_link({"source": "sms_live", "sender": "+15550100"}),
                         {"href": "sms:+15550100", "app": "Messages"})


def fact_item(what, amount_from="", stage="", w=None, typ="event", subtype="bill"):
    return dict(F.item(typ, "sender", what, w=w, subtype=subtype), amount_from=amount_from, stage=stage)


class LedgerFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [F.ts("2026-11-11T08:00")]
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.ex = extract.Extractor(self.ms, F.TZ, F.OWNER, None, clock=lambda: self.now[0])
        self.s = F.Seeder(self.ms, self.ex, self.now)
        self.k = 0

    def tearDown(self):
        self.ms.close()
        self.tmp.cleanup()

    def read(self, text, items=(), updates=(), who="kestrel", subject=""):
        self.k += 1
        self.now[0] += 600
        at = F.dt.datetime.fromtimestamp(self.now[0], F.dt.timezone(F.dt.timedelta(hours=-8))).strftime("%Y-%m-%dT%H:%M")
        return self.s.msg(f"m{self.k}", at, who, text, subject=subject,
                          answer={"items": list(items), "updates": list(updates)})

    def item(self, i):
        return self.ex.lv.item(i)

    def test_amount_copied_from_the_cited_sentence(self):
        due = F.when("Mon Nov 16", "2026-11-16", all_day=True)
        i = self.read("Hi Jordan. Invoice 4471: $1,240 due Mon Nov 16. Thanks!",
                      [fact_item("Kestrel invoice", "Invoice 4471: $1,240 due Mon Nov 16.", "due", due)])
        it = self.item(i)
        self.assertEqual((it["amount"], it["currency"], it["stage"]), ("$1,240", "$", "due"))
        self.assertEqual(L.get_item(self.ms._wdb, i)["amount"], "$1,240")  # noqa: SLF001 (stored, not computed)

    def test_the_models_words_never_become_the_amount(self):
        cases = [("Invoice 4471 is due Monday. The total is $1,240.", "The total is $9,999.", "isn't in the message"),
                 ("Minimum payment $35, balance $1,284.50.", "Minimum payment $35, balance $1,284.50.", "more than one"),
                 ("Budget is $2-3k for the shoot.", "Budget is $2-3k for the shoot.", "range"),
                 ("Lens rental $200 each. Total due $600.", "Lens rental $200 each.", "line item"),
                 ("Thanks for the update.", "Thanks for the update.", "no amount")]
        for text, cited, why in cases:
            i = self.read(text, [fact_item("Bill", cited)])
            it = self.item(i)
            self.assertEqual((it["amount"], it["currency"]), ("", ""), text)
            self.assertTrue(any(c["field"] == "amount" and why in c["why"] for c in it["checks"]), (text, it["checks"]))

    def test_total_wins_and_currency_codes(self):
        i = self.read("Lens rental: 2 days. Total EUR 480 by Friday.", [fact_item("Rental", "Total EUR 480 by Friday.")])
        self.assertEqual((self.item(i)["amount"], self.item(i)["currency"]), ("EUR 480", "EUR"))
        i = self.read("Your balance is £64.20 this month.", [fact_item("Power", "balance is £64.20")])  # part of it
        self.assertEqual((self.item(i)["amount"], self.item(i)["currency"]), ("£64.20", "GBP"))

    def test_package_stages_move_forward_on_the_messages_words(self):
        i = self.read("Thanks for your order! Lens filter kit, qty 1.",
                      [fact_item("Lens filter kit", stage="ordered", subtype="delivery")], who="north")
        self.assertEqual(self.item(i)["stage"], "ordered")
        up = lambda st: dict(F.upd(f"L{i}", "updated"), amount_from="", stage=st)  # noqa: E731
        self.read("Your Lens filter kit has shipped. Tracking 1Z999AA10123457781.", updates=[up("shipped")], who="north")
        self.assertEqual(self.item(i)["stage"], "shipped")
        self.read("Your package is out for delivery.", updates=[up("out_for_delivery")], who="north")
        self.assertEqual(self.item(i)["stage"], "out_for_delivery")
        self.read("Thanks for your order! Reminder of what you bought.", updates=[up("ordered")], who="north")
        self.assertEqual(self.item(i)["stage"], "out_for_delivery")           # never backwards
        self.read("Hope you like it.", updates=[up("delivered")], who="north")  # no word for it: kept as it was
        it = self.item(i)
        self.assertEqual(it["stage"], "out_for_delivery")
        self.assertTrue(any(c["field"] == "stage" for c in it["checks"]))

    def test_due_needs_a_date_and_bad_stages_are_listed(self):
        i = self.read("Please renew your toll tag soon.", [fact_item("Renew toll tag", stage="due", typ="ask",
                                                                     subtype="")])
        it = self.item(i)
        self.assertEqual(it["stage"], "")
        self.assertIn("due without a date", [c["why"] for c in it["checks"]])
        raw = {"items": [dict(fact_item("x"), stage="teleported")], "updates": []}
        ctx = {"text": "x", "subject": "", "tz": F.dt.timezone.utc, "anchor": F.dt.datetime.now(F.dt.timezone.utc),
               "ts": 0, "owner": "", "owner_names": set(), "sender": ("s", "S"), "recipients": [], "people": {},
               "thread": "gmail:a:t", "direction": "in", "shown": set(), "source": "gmail"}
        out = extract.normalize(raw, ctx)
        self.assertEqual(out["items"][0]["stage"], "")
        self.assertIn("not a stage", [c["why"] for c in out["items"][0]["checks"]])

    def test_schema_asks_for_the_facts(self):
        props = extract.SCHEMA["properties"]["items"]["items"]
        self.assertIn("amount_from", props["required"])
        self.assertEqual(props["properties"]["stage"]["enum"], [""] + list(L.STAGES))
        self.assertIn("stage", extract.SCHEMA["properties"]["updates"]["items"]["required"])

    def test_older_store_gets_the_columns(self):
        with tempfile.TemporaryDirectory() as d:
            ms = msgstore.MessageStore(d)
            old = L._SCHEMA  # noqa: SLF001
            for col in ("amount", "currency", "stage"):
                old = old.replace(f"  {col} TEXT NOT NULL DEFAULT '',\n", "")
            ms.write(lambda db: [db.execute(x) for x in old.split(";") if x.strip()])
            L.ensure(ms)
            cols = {r[1] for r in ms._read("PRAGMA table_info(ledger_items)")}  # noqa: SLF001
            self.assertTrue({"amount", "currency", "stage"} <= cols)
            ms.close()

    def test_visuals_show_the_ledgers_facts(self):
        due = F.when("Mon Nov 16", "2026-11-16", all_day=True)
        bill = self.read("Invoice 4471: $1,240 due Mon Nov 16.",
                         [fact_item("Kestrel invoice", "Invoice 4471: $1,240 due Mon Nov 16.", "due", due)])
        pk = self.read("Thanks for your order! Lens filter kit, qty 1.",
                       [fact_item("Lens filter kit", stage="ordered", subtype="delivery")], who="north")
        self.read("Your Lens filter kit is out for delivery.",
                  updates=[dict(F.upd(f"L{pk}", "updated"), amount_from="", stage="out_for_delivery")], who="north")
        facts = visuals.StoreFacts(self.ms, self.ex.lv)
        out = visuals.compose({"type": "money", "bills": [{"ref": f"item:{bill}"}]}, facts)
        b = out["blocks"][0]
        self.assertEqual((b["d"]["rows"][0]["amt"], b["checks"]), ("$1,240", []))
        out = visuals.compose({"type": "package", "name": "Lens filter kit", "steps": [{"ref": f"item:{pk}"}]}, facts)
        b = out["blocks"][0]
        self.assertEqual([x["st"] for x in b["d"]["steps"]], ["done", "done", "now", "next"])
        self.assertIn("on the way", b["alt"])


if __name__ == "__main__":
    unittest.main()
