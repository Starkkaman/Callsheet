"""Replay scorer 2 (the acceptance rules): p:auto only for the case's automated senders, first names only when
unique, allowed `types` (and their sections) for ambiguous wording, and the scorer version in every summary."""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tools"))

import replay_ledger as R  # noqa: E402


def it(who, name="", typ="event", sub="", section="coming_up"):
    return {"who": who, "who_name": name, "type": typ, "subtype": sub, "section": section}


def case(messages, expect, **kw):
    c = {"case": "S-T-01", "set": "synthetic", "set_version": "1.1", "replay": "isolated", "patterns": [],
         "owner": "Jordan Vale", "owner_tz": "America/Los_Angeles", "calendar": [], "messages": messages,
         "expect": expect, "updates": [], "conflicts": [], "follow_ups": [], "links": [], "no_items": False,
         "note": None, "unjudged": []}
    c.update(kw)
    return c


def msg(n, sender, text, direction="in"):
    return {"mid": f"S-T-01:m{n}", "source": "mail", "direction": direction, "sender": sender,
            "to": ["owner"] if direction == "in" else ["p:dana"], "chat": "mail:t", "chat_kind": "thread",
            "ts": f"2027-03-0{n}T09:00-08:00", "text": text, "subject": "t"}


class TestWho(unittest.TestCase):
    def test_auto_is_only_the_automated_sender_or_the_declared_one(self):
        c = case([msg(1, "p:auto", "Linkwave Internet: your autopay of $70.00 is scheduled for March 12."),
                  msg(2, "p:dana", "ok")], [])
        people, autos = R._people(c), R._autos(c)  # noqa: SLF001
        self.assertEqual(autos, {"auto"})
        self.assertTrue(R._who_eq(it("p:auto", "Auto"), "p:auto", people, autos))  # noqa: SLF001
        for other in (it("name:linkwave internet", "Linkwave Internet"), it("name:someone", "Someone"),
                      it("owner"), it("p:dana", "Dana")):
            self.assertFalse(R._who_eq(other, "p:auto", people, autos), other)  # noqa: SLF001
        c["auto_sender"] = "Linkwave Internet"
        self.assertTrue(R._who_eq(it("name:linkwave internet", "Linkwave Internet"), "p:auto", people,  # noqa: SLF001
                                  R._autos(c)))  # noqa: SLF001
        c["auto_sender"] = ["Dana"]                # a person of the case is never the automated sender
        self.assertFalse(R._who_eq(it("p:dana", "Dana"), "p:auto", people, R._autos(c)))  # noqa: SLF001

    def test_no_automated_message_no_auto_name(self):
        c = case([msg(1, "p:dana", "hi")], [])
        self.assertEqual(R._autos(c), set())  # noqa: SLF001
        self.assertFalse(R._who_eq(it("name:brightgrid power"), "p:auto", R._people(c), R._autos(c)))  # noqa: SLF001

    def test_first_name_only_when_unique_among_the_people(self):
        kofi = it("name:kofi mensah", "Kofi Mensah")
        self.assertTrue(R._who_eq(kofi, "p:kofi", {"p:kofi", "p:dana"}))  # noqa: SLF001
        self.assertFalse(R._who_eq(kofi, "p:kofi", {"p:kofi", "p:kofi_b"}))  # noqa: SLF001
        self.assertFalse(R._who_eq(kofi, "p:kofi", set()))  # noqa: SLF001
        self.assertTrue(R._who_eq(it("p:kofi", "Kofi"), "p:kofi", {"p:kofi", "p:kofi_b"}))  # noqa: SLF001

    def test_people_include_the_people_the_expectations_name(self):
        c = case([msg(1, "p:auto", "It's Kofi Mensah's birthday today.")],
                 [{"eid": "S-T-01:e1", "type": "event", "who": "p:kofi", "after": "S-T-01:m1"}])
        self.assertEqual(R._people(c), {"p:kofi"})  # noqa: SLF001


class TestTypes(unittest.TestCase):
    def test_types_list_and_its_sections(self):
        e = {"types": ["ask", "event"], "section": ["needs_you", "coming_up"]}
        self.assertTrue(R._type_eq(it("p:marco", typ="ask"), e))  # noqa: SLF001
        self.assertTrue(R._type_eq(it("p:marco", typ="event"), e))  # noqa: SLF001
        self.assertFalse(R._type_eq(it("p:marco", typ="commitment"), e))  # noqa: SLF001
        self.assertTrue(R.section_ok(e, it("p:marco", typ="ask", section="needs_you"), 1, None))
        self.assertFalse(R.section_ok(e, it("p:marco", section="clean_up"), 1, None))
        self.assertTrue(R._type_eq(it("x", typ="ask"), {"type": "ask"}))  # noqa: SLF001
        self.assertTrue(R.section_ok({"section": "coming_up"}, it("x"), 1, None))

    def test_oracle_and_summary_with_types(self):
        when = {"tz": "America/Los_Angeles", "phrase": "next Friday at 11", "all_day": False, "approx": False,
                "start": "2027-03-12T11:00-08:00", "end": None}
        c = case([msg(1, "p:marco", "Let's present the treatment next Friday at 11.")],
                 [{"eid": "S-T-01:e1", "types": ["ask", "event"], "who": "p:marco", "what": "treatment presentation",
                   "when": when, "status": "open", "section": ["needs_you", "coming_up"], "after": "S-T-01:m1",
                   "confidence": "sure"}])
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "cases.jsonl")
            p.write_text(json.dumps(c) + "\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = R.main(["--cases", str(p), "--quiet", "--out", f"{d}/s.json"])
            s = json.loads(pathlib.Path(d, "s.json").read_text())
        self.assertEqual((code, s["gate"]), (0, "pass"))
        self.assertEqual((s["scorer_version"], s["set_version"]), (R.SCORER_VERSION, "1.1"))
        self.assertEqual(R.SCORER_VERSION, "2.4")
        sure = s["metrics"]["all"]["sure"]
        self.assertEqual((sure["asks"]["n"], sure["asks"]["ok"]), (1, 1))      # an ask reading counts as an ask
        self.assertEqual(sure["sections"]["pct"], 100.0)
        self.assertEqual(s["false"].get("false_items", 0), 0)


if __name__ == "__main__":
    unittest.main()
