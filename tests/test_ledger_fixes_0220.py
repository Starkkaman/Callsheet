"""0.22.0: fixes from the first live synthetic run (the ledger's reading, clashes, follow-ups and the replay scorer).
In-process fakes only: no model server, no network."""
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, upd, when  # noqa: E402

import extract  # noqa: E402
import ledger  # noqa: E402
import replay_ledger as R  # noqa: E402


class Fix(Base):
    def one(self, res):
        return self.show(res["items"][0])

    def checks(self, it):
        return {c["why"] for c in it["checks"]}


class TestTimes(Fix):
    def test_eod_without_approx_gets_five_pm(self):
        it = self.one(self.read("m1", "2027-03-09T11:00-08:00", {"items": [item(w=when("by EOD", "2027-03-09"))],
                                                                 "updates": []}))
        self.assertEqual(it["when"]["start"], "2027-03-09T17:00-08:00")
        self.assertTrue(it["when"]["approx"])
        self.assertIn("time of day from the phrase", self.checks(it))
        self.assertEqual(it["when"]["phrase"], "by EOD")

    def test_afternoon_is_not_noon(self):
        it = self.one(self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(w=when("Friday afternoon",
                                                                                       "2027-03-05"))], "updates": []}))
        self.assertEqual(it["when"]["start"], "2027-03-05T14:00-08:00")

    def test_the_third_after_the_twentieth_is_next_month(self):
        it = self.one(self.read("m1", "2027-03-20T10:00-07:00", {"items": [item(w=when(
            "the 3rd, 9-5", "2027-03-03", "09:00", "17:00"))], "updates": []}))
        self.assertEqual((it["when"]["start"], it["when"]["end"]), ("2027-04-03T09:00-07:00", "2027-04-03T17:00-07:00"))
        self.assertIn("the day of the month is another date", self.checks(it))

    def test_this_weekend_and_next_week_are_days(self):
        a = self.one(self.read("m1", "2027-03-03T10:00-08:00", {"items": [item(w=when("this weekend"))],
                                                                "updates": []}))
        self.assertEqual((a["when"]["all_day"], a["when"]["start_date"], a["when"]["end_date"]),
                         (True, "2027-03-06", "2027-03-07"))
        self.assertIn("the days from the phrase", self.checks(a))
        b = self.one(self.read("m2", "2027-03-03T11:00-08:00", {"items": [item(what="color pass", w=when("next week"))],
                                                                "updates": []}, sender="p:kofi", name="Kofi"))
        self.assertEqual((b["when"]["start_date"], b["when"]["end_date"]), ("2027-03-08", "2027-03-12"))

    def test_a_length_gives_the_end(self):
        it = self.one(self.read("m1", "2027-03-17T19:00-07:00", {"items": [item(w=when(
            "Fri 4pm", "2027-03-19", "16:00"))], "updates": []}, text="Can you run camera Fri 4pm? Across the bay, 2 hrs"))
        self.assertEqual(it["when"]["end"], "2027-03-19T18:00-07:00")
        self.assertIn("end from the length in the message", self.checks(it))

    def test_in_two_hours_is_not_a_length(self):
        it = self.one(self.read("m1", "2027-03-17T10:00-07:00", {"items": [item(w=when(
            "Fri 4pm", "2027-03-19", "16:00"))], "updates": []}, text="Fri 4pm? I'll know in 2 hours"))
        self.assertIsNone(it["when"]["end"])

    def test_zone_in_the_phrase_and_utc_offsets(self):
        a = self.one(self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(w=when("Thu 1pm ET", "2027-03-04",
                                                                                      "13:00"))], "updates": []}))
        self.assertEqual(a["when"]["start"], "2027-03-04T10:00-08:00")
        self.assertIn("time zone from the phrase", self.checks(a))
        b = self.one(self.read("m2", "2027-03-01T11:00-08:00", {"items": [item(what="sync", w=when(
            "Thu 3pm", "2027-03-04", "15:00", tz="UTC-5"))], "updates": []}, sender="p:noor", name="Noor"))
        self.assertEqual(b["when"]["start"], "2027-03-04T12:00-08:00")
        self.assertEqual(extract._zone_named("ET (UTC-5)").key, "America/New_York")  # noqa: SLF001

    def test_two_clock_times_in_the_phrase_keep_the_models_time(self):
        it = self.one(self.read("m1", "2027-03-09T11:00-08:00", {"items": [item(w=when(
            "5 pm ET = 2 pm PT", "2027-03-09", "17:00"))], "updates": []}))
        self.assertEqual(it["when"]["start"], "2027-03-09T17:00-08:00")


class TestStatusAndContext(Fix):
    def test_his_own_commitment_is_accepted_with_a_check(self):
        it = self.one(self.read("m1", "2027-03-04T09:30-08:00", {"items": [item(
            "commitment", "owner", "get back to Gabe", when("tomorrow", "2027-03-05"), "open")], "updates": []},
            direction="out", to=[("p:gabe", "Gabe")]))
        self.assertEqual(it["status"], "accepted")
        self.assertIn("your own commitment in your own message", self.checks(it))

    def test_open_items_show_a_named_person_from_another_thread(self):
        self.read("m1", "2027-03-02T10:00-08:00", {"items": [item(what="truck prep Thursday", w=when(
            "Thursday 10-1", "2027-03-04", "10:00", "13:00"))], "updates": []}, sender="p:marco", name="Marco",
            source="gmail", thread="mail-marco")
        self.read("m2", "2027-03-02T12:00-08:00", {"items": [], "updates": []},
                  text="Marco is still waiting to hear about Thursday", sender="p:dana", name="Dana")
        body, _ = self.fake.bodies[-1]
        self.assertIn("truck prep Thursday", json.dumps(body))


class TestFold(Fix):
    def test_an_asks_own_plan_is_one_item(self):
        res = self.read("m1", "2027-03-05T12:44-08:00", {"items": [
            item(what="dinner at Mom's?", w=when("Sunday at 6", "2027-03-07", "18:00")),
            item("event", what="Dinner at Mom's", w=when("Sunday at 6", "2027-03-07", "18:00", "20:00"),
                 status="tentative")], "updates": []}, text="Dinner at Mom's Sunday at 6?")
        self.assertEqual(len(set(res["items"])), 1)
        a = self.show(res["items"][0])
        self.assertEqual((a["type"], a["when"]["end"]), ("ask", "2027-03-07T20:00-08:00"))
        merged = [i for i in self.lv.items() if i["merged_into"] == a["id"]]
        self.assertEqual(len(merged), 1)                       # kept, joined, not dropped
        self.assertEqual(self.lv.conflicts(), [])

    def test_a_plan_and_a_task_stay_two(self):
        res = self.read("m1", "2027-03-05T11:00-08:00", {"items": [
            item("event", what="prepro meeting", w=when("Tuesday 2pm", "2027-03-09", "14:00")),
            item(what="bring a lens list", w=when("Tuesday 2pm", "2027-03-09", "14:00"), conf="likely")],
            "updates": []}, text="Prepro Tuesday 2pm in the bullpen. Camera dept please come with a lens list.")
        self.assertEqual(len(set(res["items"])), 2)


class TestCalendarEnd(Fix):
    def test_a_calendar_repeat_takes_the_entrys_end(self):
        self.ms.upsert_events([{"account": "a", "calendar": "c", "event_id": "k1", "title": "Kickoff",
                                "start_ts": ts("2027-03-16T10:00-07:00"), "end_ts": ts("2027-03-16T11:30-07:00"),
                                "status": "confirmed"}])
        it = self.one(self.read("m1", "2027-03-10T10:00-08:00", {"items": [item("event", what="Kickoff", w=when(
            "Tue 10am", "2027-03-16", "10:00"))], "updates": []}))
        self.assertEqual(it["when"]["end"], "2027-03-16T11:30-07:00")
        self.assertIn("end from the calendar entry it repeats", self.checks(it))


class TestUpdatesAndClashes(Fix):
    def test_a_bare_changed_update_is_in_the_history(self):
        i = self.one(self.read("m1", "2027-03-09T20:00-08:00", {"items": [item("event", what="crew call", w=when(
            "Thu 6:30am", "2027-03-11", "06:30"))], "updates": []}))["id"]
        res = self.read("m2", "2027-03-10T20:00-08:00", {"items": [], "updates": [upd(f"L{i}", "changed")]})
        self.assertIn(i, res["updated"])
        kinds = [h[0] for h in R.snapshot(self.ms, self.lv, self.now[0], {}, {})["items"][i]["hist"]]
        self.assertIn("changed", kinds)

    def test_his_yes_in_the_thread_does_not_clash_with_the_ask(self):
        a = self.one(self.read("m1", "2027-03-04T09:40-08:00", {"items": [item(what="cafe shoot", w=when(
            "Sat 8-12", "2027-03-13", "08:00", "12:00"))], "updates": []}, sender="p:lena", name="Lena"))["id"]
        self.read("m2", "2027-03-04T11:02-08:00", {"items": [item("commitment", "owner", "cafe shoot", when(
            "Sat 8", "2027-03-13", "08:00", "12:00"), "accepted")], "updates": []}, direction="out",
            to=[("p:lena", "Lena")], thread="t-p:lena")
        self.assertEqual(self.lv.conflicts(), [])
        self.assertIsNotNone(a)

    def test_his_yes_does_not_clash_with_what_came_with_the_ask(self):
        res = self.read("m1", "2027-03-04T09:00-08:00", {"items": [
            item(what="Halifax workshop", w=when("Tue 9-5", "2027-03-16", "09:00", "17:00")),
            item("event", what="flight to Halifax", subtype="travel", w=when("Tue 6am", "2027-03-16", "06:00", "08:30"))],
            "updates": []}, text="Can you run the workshop in Halifax Tue 9-5? Flight is at 6am.", sender="p:mia", name="Mia")
        ask = res["items"][0]
        self.read("m2", "2027-03-04T10:00-08:00", {"items": [item("commitment", "owner", "Halifax workshop", when(
            "Tue", "2027-03-16", "06:00", "17:00"), "accepted")], "updates": [upd(f"L{ask}", "done")]},
            direction="out", to=[("p:mia", "Mia")], thread="t-other")
        flight = [c for c in self.lv.conflicts() if any("flight" in (s.get("what") or "") for s in c["sides"])]
        self.assertEqual(flight, [])


class TestPromiseFollowUp(Fix):
    def test_get_back_to_you_raises_a_follow_up_until_he_answers(self):
        ask = self.one(self.read("m1", "2027-03-04T09:00-08:00", {"items": [item(what="podcast shoot", w=when(
            "Wed 3/10 10am-2pm", "2027-03-10", "10:00", "14:00"))], "updates": []}, sender="p:gabe", name="Gabe",
            text="Can you do the podcast shoot Wed 3/10, 10am-2pm?"))["id"]
        res = self.read("m2", "2027-03-04T09:30-08:00", {"items": [item(
            "commitment", "owner", "get back to Gabe", when("tomorrow", "2027-03-05", "17:00", approx=True),
            "accepted")], "updates": [upd(f"L{ask}", "tentative")]}, direction="out", to=[("p:gabe", "Gabe")],
            thread="t-p:gabe", text="Probably! Let me check with Dana and get back to you tomorrow")
        f = self.lv.followups()
        self.assertEqual([(x["item"], x["tell"]) for x in f], [(res["items"][0], "p:gabe")])
        self.read("m3", "2027-03-05T10:00-08:00", {"items": [], "updates": [upd(f"L{ask}", "accepted")]},
                  direction="out", to=[("p:gabe", "Gabe")], thread="t-p:gabe", text="I'm in for Wednesday")
        self.assertEqual(self.lv.followups(), [])

    def test_no_promise_no_follow_up(self):
        self.read("m1", "2027-03-04T09:30-08:00", {"items": [item(
            "commitment", "owner", "send the edit", when("Friday", "2027-03-05"), "accepted")], "updates": []},
            direction="out", to=[("p:kofi", "Kofi")], text="I'll send the edit Friday")
        self.assertEqual(self.lv.followups(), [])


class TestScorer(unittest.TestCase):
    def it(self, who, name="", typ="event", sub="", section="coming_up"):
        return {"who": who, "who_name": name, "type": typ, "subtype": sub, "section": section}

    def test_who(self):
        self.assertTrue(R._who_eq(self.it("name:linkwave internet", "Linkwave Internet"), "p:auto", {"p:dana"},  # noqa: SLF001
                                  {"linkwave internet"}))      # scorer 2: only a declared or automated sender
        self.assertFalse(R._who_eq(self.it("name:linkwave internet", "Linkwave Internet"), "p:auto", {"p:dana"}))  # noqa: SLF001
        self.assertFalse(R._who_eq(self.it("owner"), "p:auto"))  # noqa: SLF001
        self.assertFalse(R._who_eq(self.it("p:dana", "Dana"), "p:auto", {"p:dana"}))  # noqa: SLF001
        self.assertTrue(R._who_eq(self.it("name:kofi mensah", "Kofi Mensah"), "p:kofi", {"p:kofi"}))  # noqa: SLF001
        self.assertFalse(R._who_eq(self.it("p:kofi", "Kofi"), "p:dana"))  # noqa: SLF001

    def test_security_items_and_section(self):
        e = {"type": "ask", "never_hide": True, "section": "needs_you"}
        sec = self.it("p:auto", typ="event", sub="security", section="security")
        self.assertTrue(R._type_eq(sec, e))  # noqa: SLF001
        self.assertFalse(R._type_eq(sec, dict(e, never_hide=False)))  # noqa: SLF001
        self.assertTrue(R.section_ok(e, sec, 1, None))
        self.assertFalse(R.section_ok(dict(e, never_hide=False), sec, 1, None))

    def test_raw_answers_round_trip_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "raw.jsonl")
            keep = R.raw_keeper(lambda body, info: '{"items": [], "updates": []}', path)
            keep({}, {"msg_id": "S-X:m1"})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            call = R.raw_caller(path)
            self.assertEqual(call({}, {"msg_id": "S-X:m1"}), '{"items": [], "updates": []}')
            with self.assertRaises(R.writer.WriterError):
                call({}, {"msg_id": "S-X:m2"})

    def test_raw_out_refused_inside_the_repo(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                R.main(["--cases", "x.jsonl", "--extractor", "live", "--base", "http://x/v1", "--model", "m",
                        "--raw-out", str(R.ROOT / "raw.jsonl")])
            with self.assertRaises(SystemExit):
                R.main(["--cases", "x.jsonl", "--extractor", "raw"])
        self.assertIn("--raw-out must be outside the repo", err.getvalue())


if __name__ == "__main__":
    unittest.main()
