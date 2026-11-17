"""0.19.0 (rework stage 6a): visual blocks. Schemas and the checker, quotes, filling from fakes and from a real store,
the API routes and the renderer's static rules. Stdlib only, no model server, no network."""
import copy
import datetime as dt
import json
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
STATIC = APP / "static"
sys.path.insert(0, str(APP))

import api  # noqa: E402
import extract  # noqa: E402
import msgstore  # noqa: E402
import visuals  # noqa: E402
import visuals_sample as S  # noqa: E402

TZ = "America/Los_Angeles"


def ts(s):
    return dt.datetime.fromisoformat(s).timestamp()


class TestSchemas(unittest.TestCase):
    def test_every_type_has_a_versioned_schema(self):
        want = {"day", "clash", "asks", "upcoming", "money", "trip", "person", "cleanup", "bills", "ledger", "package",
                "ticket", "tasks", "health", "stat", "local", "price", "leads", "series", "invite", "subs", "composite"}
        self.assertEqual(set(visuals.TYPES), want)
        for t in visuals.TYPES:
            s = visuals.schema(t)
            self.assertEqual(s["$id"], f"callsheet:visual/{t}/1")
            self.assertFalse(s.get("additionalProperties", True), t)
            self.assertIn("type", s["required"])
        cat = visuals.catalogue()
        self.assertEqual(cat["version"], 1)
        self.assertEqual(set(cat["types"]) if isinstance(cat["types"], dict) else
                         {x["type"] for x in cat["types"]}, want)
        json.dumps(cat)                                       # JSON-able, as the API returns it

    def test_every_sample_is_valid(self):
        self.assertEqual({b["type"] for b in S.BLOCKS}, set(visuals.TYPES))
        for b in S.BLOCKS:
            ok, bad = visuals.validate(b)
            self.assertIsNone(bad, (b["type"], bad))

    def test_bad_blocks_are_invalid(self):
        bad = [{"type": "asks", "items": ["msg:1"], "x": 1},              # unknown property
               {"type": "clash"},                                         # neither ref nor sides
               {"type": "asks", "items": ["message 1"]},                  # not a ref
               {"type": "asks", "items": ["msg:1"] * 40},                  # too many
               {"type": "health"},                                        # required ref
               {"type": "day", "items": ["cal:1"], "from": "<b>7</b>"},  # not an hour
               {"type": "money", "bills": [{"ref": "item:1", "amount": "1240\u0000"}]},  # control character
               {"type": "trip", "to": 5, "legs": ["item:1"]},             # wrong JSON type
               {"type": "asks", "items": ["msg:1"], "v": 2}]              # another version
        for b in bad:
            ok, why = visuals.validate(b)
            self.assertIsNone(ok, b)
            self.assertEqual(why["why"], "invalid", b)
            self.assertTrue(why["errors"], b)

    def test_unknown_and_oversized(self):
        for b in ({"type": "weather", "city": "Austin"}, {"type": "<svg onload=x>"}, {"items": ["msg:1"]}, "asks", [1]):
            ok, why = visuals.validate(b)
            self.assertIsNone(ok)
            self.assertIn(why["why"], ("unknown", "invalid"))
        self.assertEqual(visuals.validate({"type": "weather"})[1]["why"], "unknown")
        big = {"type": "person", "ref": "msg:1", "mark": "x" * (visuals.MAX_BLOCK_BYTES + 10)}
        self.assertEqual(visuals.validate(big)[1]["why"], "oversized")

    def test_composite_limits(self):
        leaf = {"t": "text", "text": "hi"}
        deep = leaf
        for _ in range(visuals.MAX_DEPTH + 1):
            deep = {"t": "row", "c": [deep]}
        self.assertEqual(visuals.validate({"type": "composite", "name": "x", "body": deep})[1]["why"], "invalid")
        wide = {"t": "column", "c": [{"t": "row", "c": [leaf] * 8} for _ in range(8)]}
        self.assertEqual(visuals.validate({"type": "composite", "name": "x", "body": wide})[1]["why"], "invalid")
        for part in ({"t": "svg", "d": "M0 0"}, {"t": "html", "text": "<b>x</b>"}, {"t": "image", "src": "x.png"}):
            ok, why = visuals.validate({"type": "composite", "name": "x", "body": {"t": "row", "c": [part]}})
            self.assertIsNone(ok, part)
        ok, _ = visuals.validate({"type": "composite", "name": "shoot_prep", "body": {"t": "row", "c": [leaf]}})
        self.assertIsNotNone(ok)
        self.assertIsNone(visuals.validate({"type": "composite", "name": "Shoot Prep!", "body": leaf})[0])

    def test_compose_counts_what_it_drops(self):
        f = S.facts()
        out = visuals.compose([{"type": "weather"}, {"type": "asks", "x": 1}, {"type": "asks", "items": ["msg:404"]},
                               {"type": "cleanup"}, {"type": "health", "ref": "msg:90450"},
                               {"type": "clash", "ref": "clash:2"}, {"type": "cleanup"}], f)
        self.assertEqual([b["type"] for b in out["blocks"]], ["cleanup", "health", "clash"])
        self.assertEqual(out["dropped"], {"unknown": 1, "invalid": 1, "oversized": 0, "unfilled": 1, "too_many": 1})
        self.assertEqual(len(out["problems"]), 4)
        self.assertEqual(visuals.compose(None, f)["blocks"], [])
        self.assertEqual(visuals.compose({"type": "cleanup"}, f)["blocks"][0]["type"], "cleanup")


class TestQuotes(unittest.TestCase):
    def test_quote(self):
        self.assertTrue(visuals.quote("$1,240", "Rent is $1,240 this month"))
        self.assertTrue(visuals.quote("elmwood  ARTS center", "See you at Elmwood Arts Center!"))
        self.assertIsNone(visuals.quote("$124", "Rent is $1,240 this month"))         # numbers match whole
        self.assertIsNone(visuals.quote("$1,250", "Rent is $1,240 this month"))
        self.assertIsNone(visuals.quote("", "anything"))

    def test_money(self):
        sym, v, cents = visuals.money("due $1,284.50 by Friday")
        self.assertEqual((sym, str(v), cents), ("$", "1284.50", True))
        self.assertIsNone(visuals.money("no amount here"))


class TestFill(unittest.TestCase):
    def setUp(self):
        self.f = S.facts()

    def one(self, block, facts=None):
        out = visuals.compose(block, facts or self.f)
        return out["blocks"][0] if out["blocks"] else out

    def test_gallery_fills_every_type_with_nothing_left_out(self):
        g = S.gallery()
        self.assertEqual(len(g["blocks"]), len(visuals.TYPES))
        for x in g["blocks"]:
            b = x["block"]
            self.assertIsNotNone(b, x["type"])
            self.assertEqual((b["v"], b["type"], b["checks"]), (1, x["type"], []), x["type"])
            self.assertTrue(b["label"] and b["alt"], x["type"])
        self.assertEqual(g["dropped_demo"]["dropped"]["unknown"], 1)
        self.assertEqual(g["dropped_demo"]["dropped"]["invalid"], 1)

    def test_names_and_times_come_from_the_store(self):
        b = self.one({"type": "clash", "ref": "clash:2"})
        self.assertEqual([x["l"] for x in b["d"]["lanes"]], ["Dana", "Marco"])
        self.assertIn("7:00 pm", b["alt"])
        b = self.one({"type": "asks", "items": ["msg:87710", "msg:3302"]})
        self.assertTrue(all(r["who"] for r in b["d"]["rows"]))

    def test_an_amount_not_in_the_message_is_left_out_and_listed(self):
        b = self.one({"type": "money", "bills": [{"ref": "item:51", "amount": "$9,999"}, {"ref": "item:44",
                                                                                      "amount": "$86"}]})
        amts = [r["amt"] for r in b["d"]["rows"]]
        self.assertNotIn("$9,999", json.dumps(b))
        self.assertEqual(amts, ["$1,240", "$86"])          # 0.20.0: the ledger's amount is shown, the model's compared
        self.assertEqual(b["checks"], [{"ref": "item:51", "field": "amount", "why": "the ledger's amount is shown"}])
        recs = [dict(r, amount="") for r in S.records()]   # an item read before 0.20.0: the amount is a quote again
        f = visuals.DictFacts(self.f.tz, self.f.now, recs, self.f.conflicts)
        b = self.one({"type": "money", "bills": [{"ref": "item:51", "amount": "$9,999"}, {"ref": "item:44",
                                                                                      "amount": "$86"}]}, f)
        self.assertEqual([r["amt"] for r in b["d"]["rows"]], [None, "$86"])
        self.assertTrue(any("amount" in (c.get("field") or "") for c in b["checks"]))

    def test_a_ref_the_store_lacks_drops_the_block(self):
        out = visuals.compose({"type": "health", "ref": "msg:404"}, self.f)
        self.assertEqual((out["blocks"], out["dropped"]["unfilled"]), ([], 1))

    def test_health_never_carries_the_content(self):
        b = self.one({"type": "health", "ref": "msg:90450"})
        s = json.dumps(b).lower()
        self.assertNotIn("lab results", s)
        self.assertNotIn("sign in to read", s)
        self.assertIn("westside health", s)

    def test_composite_numbers_must_be_quoted(self):
        base = next(b for b in S.BLOCKS if b["type"] == "composite")
        bad = copy.deepcopy(base)
        bad["body"] = {"t": "column", "c": [{"t": "number", "value": "999", "unit": "crew"}]}
        out = visuals.compose(bad, self.f)                  # a number with no ref: invalid
        self.assertEqual(out["dropped"]["invalid"], 1)
        bad["body"] = {"t": "column", "c": [{"t": "text", "text": "Prep list"},
                                            {"t": "number", "value": "999", "unit": "crew", "ref": "msg:90600"}]}
        b = visuals.compose(bad, self.f)["blocks"][0]      # a number the message doesn't say: left out, listed
        self.assertNotIn("999", json.dumps(b["d"]))
        self.assertEqual(b["checks"], [{"ref": "msg:90600", "field": "value", "why": "not found in the message"}])

    def test_dictfacts_minimal(self):
        now = ts("2027-03-01T09:00-08:00")
        f = visuals.DictFacts(S.TZ, now, [
            {"ref": "msg:1", "kind": "msg", "type": "ask", "subtype": "", "title": "Lend a hand Saturday",
             "who": "Rosa", "purpose": "moving", "source": "text", "start": None, "end": None, "all_day": False,
             "created": now - 3 * 86400, "status": "open", "on_calendar": False, "link": "", "msg_ts": now - 3 * 86400,
             "sender": "Rosa", "hay": "Can you lend a hand Saturday?", "text": "Can you lend a hand Saturday?",
             "subject": "", "direction": "in"}])
        b = self.one({"type": "asks", "items": ["msg:1"]}, f)
        self.assertEqual(b["d"]["rows"][0]["who"], "Rosa")
        self.assertEqual(b["d"]["rows"][0]["age"], "3 days")


class TestStore(unittest.TestCase):
    """Refs filled from a real messages.db and ledger, written the way the extractor writes them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [ts("2027-03-01T09:00-08:00")]
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.answers = {}
        self.ex = extract.Extractor(self.ms, TZ, "Jordan Vale", lambda body, info: self.answers.get(
            info["msg_id"], {"items": [], "updates": []}), clock=lambda: self.now[0])
        self.lv = self.ex.lv

    def tearDown(self):
        self.ms.close()
        self.tmp.cleanup()

    def read(self, mid, at, items, sender="p:dana", name="Dana", source="signal", text="", subject=""):
        self.now[0] = ts(at)
        self.answers[mid] = {"items": items, "updates": []}
        self.ms.upsert_message({"source": source, "account": "acct", "msg_id": mid, "thread": f"t-{mid}",
                                "direction": "in", "sender": sender, "sender_name": name,
                                "recipients": [{"name": "Jordan Vale", "address": "me"}], "ts": ts(at),
                                "subject": subject, "text": text, "labels": []})
        m = self.ms.message(source, "acct", mid)
        res = self.ex.read(m)
        return m, res

    @staticmethod
    def ask(what, date, start, who="sender"):
        return {"type": "ask", "subtype": "", "who": who, "what": what, "for": "", "status": "open",
                "says": "", "confidence": "sure", "never_hide": False,
                "when": {"phrase": "", "date": date, "start": start, "end": "", "end_date": "", "all_day": False,
                         "approx": False, "tz": "", "options": []}}

    def test_asks_and_clash_from_the_store(self):
        m1, r1 = self.read("a1", "2027-03-01T08:00-08:00", [self.ask("second camera", "2027-03-03", "10:00")],
                           text="Can you run second camera Wednesday at 10? I can pay $300.")
        m2, r2 = self.read("a2", "2027-03-01T08:30-08:00", [self.ask("promo review", "2027-03-03", "10:00")],
                           sender="p:marco", name="Marco", source="gmail", subject="Promo review",
                           text="Can we do the promo review Wednesday at 10?")
        f = visuals.StoreFacts(self.ms, self.lv, now=ts("2027-03-01T09:00-08:00"))
        out = visuals.compose([{"type": "asks", "items": [f"msg:{m1['id']}", f"msg:{m2['id']}"]},
                               {"type": "money", "bills": [{"ref": f"item:{r1['items'][0]}", "amount": "$300"}]},
                               {"type": "clash", "sides": [f"item:{r1['items'][0]}", f"item:{r2['items'][0]}"]}], f)
        self.assertEqual(out["dropped"]["unfilled"], 0, out["problems"])
        asks, money, clash = out["blocks"]
        self.assertEqual([r["who"] for r in asks["d"]["rows"]], ["Dana", "Marco"])
        self.assertEqual([r["age"] for r in asks["d"]["rows"]], ["1 h", "30 min"])
        self.assertEqual(money["d"]["rows"][0]["amt"], "$300")
        self.assertEqual({x["l"] for x in clash["d"]["lanes"]}, {"Dana", "Marco"})
        self.assertIn("10:00 am", clash["alt"])
        rows = self.ms._read("SELECT id FROM ledger_conflicts")  # noqa: SLF001
        if rows:
            out = visuals.compose({"type": "clash", "ref": f"clash:{rows[0]['id']}"}, f)
            self.assertEqual(len(out["blocks"]), 1, out["problems"])

    def test_health_from_the_store_has_no_content(self):
        m, _ = self.read("h1", "2027-02-28T16:00-08:00", [], sender="portal@health.example", name="Westside Health",
                         source="gmail", subject="Your lab results are ready", text="Results: glucose 92")
        f = visuals.StoreFacts(self.ms, self.lv, now=self.now[0])
        b = visuals.compose({"type": "health", "ref": f"msg:{m['id']}"}, f)["blocks"][0]
        s = json.dumps(b)
        self.assertNotIn("lab results", s.lower())
        self.assertNotIn("92", s)
        self.assertIn("Westside Health", s)


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = api.App(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls.tmp.cleanup()

    def test_schema_route(self):
        out = self.app.handle("GET", "visuals/schema", None)
        self.assertEqual(out["version"], 1)
        self.assertIn("limits", out)

    def test_preview_sample(self):
        out = self.app.handle("POST", "visuals/preview", {"block": {"type": "clash", "ref": "clash:2"}})
        self.assertEqual(out["blocks"][0]["type"], "clash")
        out = self.app.handle("POST", "visuals/preview", {"show": [{"type": "weather"}, {"type": "cleanup"}]})
        self.assertEqual((len(out["blocks"]), out["dropped"]["unknown"]), (1, 1))
        with self.assertRaises(api.InputError):
            self.app.handle("POST", "visuals/preview", {})
        with self.assertRaises(api.InputError):
            self.app.handle("POST", "visuals/preview", {"block": {"type": "cleanup"}, "store": "elsewhere"})

    def test_preview_live(self):
        if not self.app.ledger:
            self.skipTest("no message store here")
        out = self.app.handle("POST", "visuals/preview", {"block": {"type": "health", "ref": "msg:1"}, "store": "live"})
        self.assertEqual((out["blocks"], out["dropped"]["unfilled"]), ([], 1))   # an empty store fills nothing

    def test_gallery_route(self):
        out = self.app.handle("GET", "visuals/gallery", None)
        self.assertEqual(len(out["blocks"]), len(visuals.TYPES))
        json.dumps(out)


class TestStatic(unittest.TestCase):
    def setUp(self):
        self.js = (STATIC / "visuals.js").read_text(encoding="utf-8")
        self.css = (STATIC / "visuals.css").read_text(encoding="utf-8")

    def test_data_is_text(self):
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                    "srcdoc", "DOMParser", "createContextualFragment"):
            self.assertNotIn(bad, self.js, bad)

    def test_relative_and_offline(self):
        for text in (self.js, self.css):
            self.assertIsNone(re.search(r"https?://", text.replace("^https:\\/\\/", "")), "no absolute URLs")
            self.assertNotIn("@import", text)
            self.assertNotIn("url(", text)
        self.assertIn("fetch('api/visuals/gallery'", self.js)
        self.assertEqual(re.findall(r"fetch\(", self.js), ["fetch("])

    def test_links_only_https_and_no_opener(self):
        self.assertIn("noopener noreferrer", self.js)
        self.assertIn("/^https:\\/\\//.test(href", self.js)

    def test_accessible_and_reduced_motion(self):
        self.assertIn("'aria-label': b.label", self.js)
        self.assertIn("figcaption", self.js)
        self.assertIn("prefers-reduced-motion:reduce", self.css)
        self.assertIn("min-height:44px", self.css)

    def test_loaded_by_the_page_and_listed_in_advanced(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="visuals.css?v={{version}}"', html)
        self.assertLess(html.index('src="visuals.js?v={{version}}"'), html.index('src="app.js?v={{version}}"'))
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("['visuals', 'Visuals',", app)

    def test_every_type_has_a_renderer(self):
        for t in visuals.TYPES:
            self.assertRegex(self.js, rf"R\.{t} = function", t)


if __name__ == "__main__":
    unittest.main()
