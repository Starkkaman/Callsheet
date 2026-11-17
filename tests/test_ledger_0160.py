"""0.16.0 (rework stage 2): the ledger, the read-once extractor, clashes, follow-ups, alerts and the replay runner.
In-process fakes only: no model server, no network."""
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "tools"))

import conflicts  # noqa: E402
import extract  # noqa: E402
import ledger  # noqa: E402
import msgstore  # noqa: E402
import push  # noqa: E402
import writer  # noqa: E402

TZ = "America/Los_Angeles"
LA = ledger.zone(TZ)
# the synthetic acceptance set (cases.jsonl) lives outside the repo; set CALLSHEET_ACCEPTANCE to its path to run TestReplay
SYN = pathlib.Path(os.path.expanduser(os.environ.get("CALLSHEET_ACCEPTANCE", "")))


def ts(s):
    return dt.datetime.fromisoformat(s).timestamp()


def when(phrase="", date="", start="", end="", end_date="", all_day=False, approx=False, tz="", options=()):
    return {"phrase": phrase, "date": date, "start": start, "end": end, "end_date": end_date, "all_day": all_day,
            "approx": approx, "tz": tz, "options": list(options)}


def item(type="ask", who="sender", what="help", w=None, status="open", conf="sure", subtype="", says="",
         never_hide=False):
    return {"type": type, "subtype": subtype, "who": who, "what": what, "for": "", "when": w or when(),
            "status": status, "says": says, "confidence": conf, "never_hide": never_hide}


def upd(ref, status="", w=None, note="", tell="", due=""):
    return {"id": ref, "status": status, "when": w or when(), "note": note, "tell": tell, "due": due}


class Script:
    """The fake model: answers by message id; records what it was asked."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.bodies = []

    def __call__(self, body, info):
        self.bodies.append((body, info))
        a = self.answers.get(info["msg_id"], {"items": [], "updates": []})
        return a(info) if callable(a) else a


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [ts("2027-03-01T09:00-08:00")]
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.fake = Script()
        self.ex = extract.Extractor(self.ms, TZ, "Jordan Vale", self.fake, clock=lambda: self.now[0])
        self.lv = self.ex.lv

    def tearDown(self):
        self.ms.close()
        self.tmp.cleanup()

    def msg(self, mid, at, text="", direction="in", sender="p:dana", name="Dana", source="signal", thread=None,
            to=None, subject="", labels=()):
        self.now[0] = ts(at)
        out = direction == "out"
        recips = [{"name": n, "address": a} for a, n in (to or ([("me", "Jordan Vale")] if not out else []))]
        self.ms.upsert_message({"source": source, "account": "acct", "msg_id": mid, "thread": thread or f"t-{sender}",
                                "direction": direction, "sender": "me" if out else sender,
                                "sender_name": "Jordan Vale" if out else name, "recipients": recips,
                                "ts": ts(at), "subject": subject, "text": text, "labels": list(labels)})
        return self.ms.message(source, "acct", mid)

    def read(self, mid, at, answer, **kw):
        self.fake.answers[mid] = answer
        return self.ex.read(self.msg(mid, at, **kw))

    def items(self):
        return {i["id"]: i for i in self.lv.items()}

    def show(self, item_id):
        return self.lv.item(item_id)


class TestValidation(Base):
    def test_not_json_is_kept_as_a_failed_read(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", "this is not json")
        self.assertTrue(res["failed"])
        self.assertEqual(self.lv.reads()["by_state"], {"failed": 1})
        self.assertEqual(len(self.ex.pending()), 1)          # retried later (up to extract.TRIES)

    def test_unknown_type_and_extra_items_are_dropped_and_listed(self):
        many = [item(what=f"thing {i}") for i in range(extract.MAX_ITEMS + 1)]
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [{"type": "todo", "what": "x"}] + many,
                                                         "updates": [upd("L99", "done")]})
        whys = sorted(d["why"] for d in res["drops"])
        self.assertIn("unknown type", whys)
        self.assertIn("no such item", whys)
        self.assertTrue(any("more than" in w for w in whys))
        self.assertEqual(self.lv.reads()["drops"], len(res["drops"]))
        self.assertEqual(self.lv.rejected()[0]["drops"], res["drops"])

    def test_bad_fields_are_checked_not_dropped(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [dict(item(what="", w=when(
            "on the 31st of Feb", date="2027-02-31", start="25:00"))), dict(item(), status="maybe")], "updates": []},
            subject="Quick question")
        a, b = (self.show(i) for i in res["items"])
        self.assertEqual(a["what"], "Quick question")
        fields = {c["field"] for c in a["checks"]}
        self.assertTrue({"what", "when.date", "when.start", "when"} <= fields, fields)
        self.assertEqual(a["confidence"], "likely")
        self.assertEqual(a["when"]["phrase"], "on the 31st of Feb")
        self.assertFalse(a["when"]["date_resolved"])
        self.assertEqual(b["status"], "open")

    def test_request_is_strict_json_thinking_off_background_slot(self):
        self.read("m1", "2027-03-01T10:00-08:00", {"items": [], "updates": []}, text="hi")
        body, info = self.fake.bodies[0]
        self.assertEqual(body["response_format"]["json_schema"]["strict"], True)
        self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(body["id_slot"], 1)
        self.assertIn("Tue 2027-03-02 tomorrow", body["messages"][1]["content"])

    def test_security_alerts_are_never_hidden(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [item("cleanup", what="sign-in")], "updates": []},
                        text="Security alert: new sign-in on your account", source="gmail", sender="no-reply@x.com")
        self.assertTrue(self.show(res["items"][0])["never_hide"])


class TestTimes(Base):
    def start(self, res):
        return self.show(res["items"][0])["when"]

    def test_relative_range(self):
        res = self.read("m1", "2027-03-02T20:03-08:00", {"items": [item(w=when("tomorrow 10-1", "2027-03-03",
                                                                                 "10:00", "13:00"))], "updates": []})
        w = self.start(res)
        self.assertEqual((w["start"], w["end"]), ("2027-03-03T10:00-08:00", "2027-03-03T13:00-08:00"))
        self.assertIn("10 am", w["label"])

    def test_date_from_the_phrase_when_the_model_leaves_it_out(self):
        res = self.read("m1", "2027-03-02T20:03-08:00", {"items": [item(w=when("tomorrow at 9", "", "09:00"))],
                                                         "updates": []})
        self.assertEqual(self.start(res)["start"], "2027-03-03T09:00-08:00")

    def test_other_zone_converted(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(w=when("Thu 1pm ET", "2027-03-04", "13:00",
                                                                                 tz="ET"))], "updates": []})
        self.assertEqual(self.start(res)["start"], "2027-03-04T10:00-08:00")

    def test_dst(self):
        res = self.read("m1", "2027-03-12T10:00-08:00", {"items": [item(w=when("Monday 10am", "2027-03-15",
                                                                                 "10:00"))], "updates": []})
        self.assertEqual(self.start(res)["start"], "2027-03-15T10:00-07:00")

    def test_weekday_mismatch_uses_the_phrase(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(w=when("Friday at 2", "2027-03-04", "14:00"))],
                                                         "updates": []})
        it = self.show(res["items"][0])
        self.assertEqual(it["when"]["start"], "2027-03-05T14:00-08:00")
        self.assertEqual(it["confidence"], "likely")
        self.assertTrue(any(c["field"] == "when.date" for c in it["checks"]))

    def test_after_midnight_tomorrow_means_later_today(self):
        res = self.read("m1", "2027-03-06T00:30-08:00", {"items": [item(w=when("tomorrow at 10", "2027-03-07",
                                                                                 "10:00"))], "updates": []})
        it = self.show(res["items"][0])
        self.assertEqual(it["when"]["start"], "2027-03-06T10:00-08:00")
        self.assertEqual(it["confidence"], "likely")

    def test_time_only_stays_undated(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(w=when("call at 3", "", "15:00"))],
                                                         "updates": []})
        w = self.start(res)
        self.assertEqual((w["time"], w["date_resolved"], w["start"]), ("15:00", False, None))

    def test_overnight_and_all_day_and_options(self):
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [
            item("event", w=when("Fri 8pm to 4am", "2027-03-05", "20:00", "04:00"), subtype="gig"),
            item("event", w=when("Mar 18-20", "2027-03-18", end_date="2027-03-20", all_day=True), subtype="trip"),
            item(w=when("Tue or Wed at 1", options=["2027-03-09 13:00-14:00", "2027-03-10 13:00-14:00"]))],
            "updates": []})
        a, b, c = (self.show(i)["when"] for i in res["items"])
        self.assertEqual(a["end"], "2027-03-06T04:00-08:00")
        self.assertEqual((b["all_day"], b["start_date"], b["end_date"]), (True, "2027-03-18", "2027-03-20"))
        self.assertEqual(len(c["candidates"]), 2)


class TestUpdates(Base):
    def setUp(self):
        super().setUp()
        res = self.read("m1", "2027-03-05T10:00-08:00", {"items": [item("commitment", "owner", "lunch with Owen",
                                                                        when("Tue 12:30", "2027-03-09", "12:30",
                                                                             "13:30"), "accepted")], "updates": []})
        self.lid = res["items"][0]

    def test_moved_keeps_its_length_and_history(self):
        self.read("m2", "2027-03-08T18:00-08:00", {"items": [], "updates": [upd(f"L{self.lid}", "moved", when(
            "Wed 12:30", "2027-03-10", "12:30"))]})
        it = self.show(self.lid)
        self.assertEqual((it["status"], it["when"]["start"], it["when"]["end"]),
                         ("moved", "2027-03-10T12:30-08:00", "2027-03-10T13:30-08:00"))
        self.assertIn("moved", [h["kind"] for h in it["history"]])
        self.assertEqual(len(it["from"]), 2)

    def test_cancelled_and_done(self):
        self.read("m2", "2027-03-08T18:00-08:00", {"items": [], "updates": [upd(f"L{self.lid}", "cancelled")]})
        self.assertEqual(self.show(self.lid)["status"], "cancelled")
        self.assertEqual(self.show(self.lid)["section"], "none")

    def test_the_open_items_are_shown_by_id(self):
        self.read("m2", "2027-03-08T18:00-08:00", {"items": [], "updates": []}, sender="p:owen", name="Owen",
                  thread="t-owen")
        self.read("m3", "2027-03-08T18:05-08:00", {"items": [], "updates": []}, sender="p:owen", name="Owen",
                  thread="t-p:dana")
        user = self.fake.bodies[-1][0]["messages"][1]["content"]
        self.assertIn(f"L{self.lid} | commitment | Jordan Vale | lunch with Owen", user)

    def test_follow_up_on_a_new_item_of_the_same_answer(self):
        res = self.read("m2", "2027-03-03T10:30-08:00", {"items": [item("commitment", "owner", "answer Kofi", when(
            "tomorrow", "2027-03-04", approx=True), "accepted")], "updates": [upd("N1", tell="Kofi",
                                                                                note="promised an answer",
                                                                                due="2027-03-04T17:00")]},
            direction="out", to=[("p:kofi", "Kofi")], thread="t-kofi")
        f = self.lv.followups()
        self.assertEqual((len(f), f[0]["item"], f[0]["tell"]), (1, res["items"][0], "p:kofi"))
        self.assertEqual(f[0]["due_ts"], ts("2027-03-04T17:00-08:00"))


class TestDedupe(Base):
    def test_same_ask_over_two_sources_is_one_item(self):
        self.ex.person = lambda source, handle: ("person:7", "Dana") if handle in ("dana@x.com", "+1555") else \
            (handle, "")
        a = self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(what="agency call", w=when(
            "Tuesday 3/9 at 1", "2027-03-09", "13:00", "13:30"))], "updates": []}, sender="+1555", source="signal")
        b = self.read("m2", "2027-03-01T10:05-08:00", {"items": [item(what="agency call availability", w=when(
            "Tue 3/9 1pm", "2027-03-09", "13:00"))], "updates": []}, sender="dana@x.com", source="gmail",
            thread="mail-1")
        self.assertEqual(a["items"], b["items"])
        it = self.show(a["items"][0])
        self.assertEqual(sorted(f["source"] for f in it["from"]), ["gmail", "signal"])
        self.assertEqual(self.lv.conflicts(), [])                      # no clash with itself

    def test_calendar_repeat_links_and_does_not_clash(self):
        self.ms.upsert_events([{"account": "a", "calendar": "c", "event_id": "k1", "title": "Kickoff: Dawnline",
                                "start_ts": ts("2027-03-16T10:00-07:00"), "end_ts": ts("2027-03-16T11:00-07:00"),
                                "status": "confirmed"}])
        res = self.read("m1", "2027-03-10T10:00-08:00", {"items": [item("event", what="Dawnline kickoff", w=when(
            "Tue 10am", "2027-03-16", "10:00", "11:00"))], "updates": []})
        self.assertIsNotNone(self.show(res["items"][0])["cal_ref"])
        self.assertEqual(self.lv.conflicts(), [])


class TestConflicts(Base):
    def test_any_date_against_calendar_and_ledger(self):
        self.ms.upsert_events([{"account": "a", "calendar": "c", "event_id": "e1", "title": "Client review",
                                "start_ts": ts("2027-10-06T12:00-07:00"), "end_ts": ts("2027-10-06T13:00-07:00"),
                                "status": "confirmed"}])
        res = self.read("m1", "2027-03-01T10:00-08:00", {"items": [item(what="lunch", w=when(
            "Oct 6 at 12:30", "2027-10-06", "12:30"))], "updates": []})
        c = self.lv.conflicts()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["sides"][1]["kind"], "event")
        self.assertEqual((c[0]["kind"], c[0]["confidence"], c[0]["urgency"]), ("overlap", "sure", "later"))
        self.assertEqual(res["conflicts"]["new"], [c[0]["id"]])
        self.read("m2", "2027-03-02T10:00-08:00", {"items": [], "updates": [upd(f"L{res['items'][0]}", "moved", when(
            "Oct 7 at 12:30", "2027-10-07", "12:30"))]})
        self.assertEqual(self.lv.conflicts(), [])
        self.assertEqual(self.lv.conflicts(state="resolved")[0]["resolved_why"], "moved away")

    def test_soft_all_day_items_do_not_flag_every_day(self):
        self.ms.upsert_events([{"account": "a", "calendar": "c", "event_id": "e1", "title": "Mom's birthday",
                                "all_day": True, "start_ts": ts("2027-03-09T00:00-08:00"),
                                "end_ts": ts("2027-03-10T00:00-08:00"), "status": "confirmed"}])
        self.read("m1", "2027-03-01T10:00-08:00", {"items": [
            item("commitment", "owner", "send the deck", when("Tuesday", "2027-03-09", all_day=True), "accepted"),
            item(what="call", w=when("Tue at 1", "2027-03-09", "13:00"))], "updates": []})
        self.assertEqual(self.lv.conflicts(), [])

    def test_tight_next_to_a_flight(self):
        self.read("m1", "2027-03-22T12:00-07:00", {"items": [item("commitment", "owner", "night shoot", when(
            "Fri 8pm to 4am", "2027-03-26", "20:00", "04:00"), "accepted", subtype="gig")], "updates": []},
            thread="t1")
        self.read("m2", "2027-03-23T08:00-07:00", {"items": [item("event", what="flight YHZ-KEF", w=when(
            "Sat 6:05am", "2027-03-27", "06:05"), subtype="flight")], "updates": []}, sender="p:auto", thread="t2")
        c = self.lv.conflicts()
        self.assertEqual((len(c), c[0]["kind"], c[0]["confidence"]), (1, "tight", "likely"))

    def test_yes_carries_the_clash_and_raises_a_follow_up_cleared_by_a_sent_message(self):
        e1 = self.read("m1", "2027-03-01T15:12-08:00", {"items": [item(what="client call", w=when(
            "Wednesday 10-12", "2027-03-03", "10:00", "12:00"))], "updates": []}, sender="p:marco", name="Marco",
            source="gmail", thread="mail-marco")["items"][0]
        e2 = self.read("m2", "2027-03-02T20:03-08:00", {"items": [item(what="truck prep", w=when(
            "tomorrow 10-1", "2027-03-03", "10:00", "13:00"))], "updates": []})["items"][0]
        self.assertEqual(len(self.lv.conflicts()), 1)
        res = self.read("m3", "2027-03-02T20:30-08:00", {"items": [item("commitment", "owner", "truck prep", when(
            "tomorrow 10-1", "2027-03-03", "10:00", "13:00"), "accepted")], "updates": [upd(f"L{e2}", "done")]},
            direction="out", to=[("p:dana", "Dana")], thread="t-p:dana")
        c = self.lv.conflicts()
        self.assertEqual(len(c), 1)
        self.assertEqual({s["id"] for s in c[0]["sides"]}, {e1, res["items"][0]})
        self.assertEqual(res["conflicts"]["new"], [])                  # carried, not a new alert
        f = self.lv.followups()
        self.assertEqual((len(f), f[0]["item"], f[0]["tell"]), (1, e1, "p:marco"))
        self.assertIn("Marco hasn't heard from you", f[0]["reason"])
        self.read("m4", "2027-03-02T20:40-08:00", {"items": [], "updates": []}, direction="out",
                  to=[("p:theo", "Theo")], thread="t-theo")
        self.assertEqual(len(self.lv.followups()), 1)                  # someone else: still open
        self.read("m5", "2027-03-02T21:00-08:00", {"items": [], "updates": [upd(f"L{e1}", "declined")]},
                  direction="out", source="gmail", to=[("p:marco", "Marco")], thread="mail-marco")
        self.assertEqual(self.lv.followups(), [])
        self.assertEqual(self.lv.followups(state="cleared")[0]["cleared_message"],
                         self.ms.message("gmail", "acct", "m5")["id"])
        self.assertEqual(self.lv.conflicts(), [])


class TestAllDay(Base):
    """Whole days never make hard time clashes; only time off, a hold, the owner's trip or the owner's own busy day make a
    soft "away" clash, and only with a timed ledger item."""

    def day(self, eid, title, d="2027-03-09", days=1, **kw):
        start = ts(f"{d}T00:00-08:00")
        self.ms.upsert_events([dict({"account": "a", "calendar": "c", "event_id": eid, "title": title, "all_day": True,
                                     "start_ts": start, "end_ts": start + 86400 * days, "status": "confirmed"}, **kw)])

    def ask(self, mid="m1", d="2027-03-09", sender="p:owen"):
        day = dt.date.fromisoformat(d).strftime("%a")
        return self.read(mid, "2027-03-01T10:00-08:00", {"items": [item(what="help with the set", w=when(
            f"{day} at 1", d, "13:00", "14:00"))], "updates": []}, sender=sender, name=sender[2:].title(),
            thread=f"t-{mid}")

    def test_plain_whole_days_never_clash(self):
        self.day("h", "Holiday: Presidents' Day")
        self.day("b", "Mom's birthday")
        self.day("o", "Brightwater offsite", days=3)
        self.day("s", "Dawnline shoot", days=2)
        self.ask()
        self.assertEqual(self.lv.conflicts(), [])

    def test_time_off_is_a_soft_away_clash_unless_marked_free(self):
        self.day("off", "OOO - Jordan")
        self.ask()
        c = self.lv.conflicts()
        self.assertEqual([(x["kind"], x["confidence"]) for x in c], [("away", "likely")])
        self.assertIn("off that day", c[0]["why"])
        self.day("off", "OOO - Jordan", transparency="transparent")
        self.ask("m2")                                                  # the same ask again: re-checked
        self.assertEqual(self.lv.conflicts(), [])                       # marked free: no clash

    def test_out_of_office_type_and_holds(self):
        self.day("x", "Busy", event_type="outOfOffice")
        self.day("h", "HOLD: Brightwater pitch", d="2027-03-10")
        self.ask("m1")
        self.ask("m2", d="2027-03-10", sender="p:sam")
        self.assertEqual(sorted(x["kind"] for x in self.lv.conflicts()), ["away", "away"])

    def test_his_trip_by_mail_is_away_a_friends_trip_is_not(self):
        self.read("t1", "2027-03-01T09:00-08:00", {"items": [item("event", what="trip to Tromsø", subtype="trip", w=when(
            "Mar 8-10", "2027-03-08", end_date="2027-03-10", all_day=True))], "updates": []}, source="gmail",
            sender="trips@air.example", thread="mail-t1")
        self.read("t2", "2027-03-01T09:05-08:00", {"items": [item("event", what="trip to Lisbon", subtype="trip", w=when(
            "Mar 8-10", "2027-03-08", end_date="2027-03-10", all_day=True))], "updates": []}, sender="p:ines", name="Ines",
            thread="t-ines")
        self.ask()
        c = self.lv.conflicts()
        self.assertEqual(len(c), 1)
        self.assertEqual({s.get("what") for s in c[0]["sides"]} & {"trip to Tromsø", "trip to Lisbon"}, {"trip to Tromsø"})

    def test_his_busy_day_against_an_ask_but_not_against_his_calendar(self):
        self.ms.upsert_events([{"account": "a", "calendar": "c", "event_id": "e1", "title": "Dentist",
                                "start_ts": ts("2027-03-09T09:00-08:00"), "end_ts": ts("2027-03-09T10:00-08:00"),
                                "status": "confirmed"}])
        self.read("o1", "2027-03-01T09:00-08:00", {"items": [item("event", "owner", "at the color lab all day", when(
            "Tuesday all day", "2027-03-09", all_day=True))], "updates": []}, direction="out", to=[("p:theo", "Theo")])
        self.assertEqual(self.lv.conflicts(), [])
        self.ask()
        self.assertEqual([x["kind"] for x in self.lv.conflicts()], ["away"])

    def test_alert_says_away_not_both_want_this_time(self):
        self.day("off", "Time off")
        self.ask()
        al = conflicts.Alerter(self.ms, self.lv, lambda: {}, FakeSend(), lambda t, m, tag=None, urgent=False: {
            "title": t, "message": m}, clock=lambda: self.now[0])
        p = al.clash_payload(self.lv.conflicts()[0])
        self.assertTrue(p["title"].startswith("Away-day clash"))
        self.assertNotIn("both want", p["message"])

    def test_the_store_keeps_free_busy_and_out_of_office(self):
        import live
        rec = live.event_record({"id": "e9", "summary": "OOO", "start": {"date": "2027-03-09"},
                                 "end": {"date": "2027-03-10"}, "transparency": "transparent",
                                 "eventType": "outOfOffice"}, "a", "c", LA)
        self.assertEqual((rec["transparency"], rec["event_type"]), ("transparent", "outOfOffice"))
        plain = live.event_record({"id": "e8", "summary": "x", "start": {"date": "2027-03-09"},
                                   "end": {"date": "2027-03-10"}, "eventType": "default"}, "a", "c", LA)
        self.assertEqual((plain["transparency"], plain["event_type"]), ("", ""))
        self.ms.upsert_events([rec])
        row = self.ms._read("SELECT transparency, event_type FROM events WHERE event_id='e9'")[0]  # noqa: SLF001
        self.assertEqual(tuple(row), ("transparent", "outOfOffice"))


class TestFollowUpsNeedTheItem(Base):
    def test_a_message_about_something_else_does_not_clear(self):
        res = self.read("m1", "2027-03-03T10:30-08:00", {"items": [item("commitment", "owner", "answer Kofi", when(
            "tomorrow", "2027-03-04", approx=True), "accepted")], "updates": [upd("N1", tell="Kofi", note="owes an answer")]},
            direction="out", to=[("p:kofi", "Kofi")], thread="t-kofi")
        self.read("m2", "2027-03-03T11:00-08:00", {"items": [], "updates": []}, direction="out",
                  to=[("p:kofi", "Kofi")], thread="t-kofi")
        self.assertEqual(len(self.lv.followups()), 1)
        self.read("m3", "2027-03-03T12:00-08:00", {"items": [], "updates": [upd(f"L{res['items'][0]}", "done")]},
                  direction="out", to=[("p:kofi", "Kofi")], thread="t-kofi")
        self.assertEqual(self.lv.followups(), [])


class TestSpans(Base):
    def test_a_span_may_name_a_day_inside_it(self):
        res = self.read("m1", "2027-03-05T10:00-08:00", {"items": [item("commitment", "owner", "send the cut", when(
            "next week, by Friday", "2027-03-08", end_date="2027-03-12", all_day=True, approx=True), "accepted")],
            "updates": []}, direction="out", to=[("p:kofi", "Kofi")])
        w = self.show(res["items"][0])["when"]
        self.assertEqual((w["start_date"], w["end_date"]), ("2027-03-08", "2027-03-12"))

    def test_two_things_for_next_week_stay_two(self):
        w = when("next week", "2027-03-08", end_date="2027-03-12", all_day=True, approx=True)
        a = self.read("m1", "2027-03-05T10:00-08:00", {"items": [item("commitment", "owner", "send the edit to Kofi", w,
                                                                      "accepted")], "updates": []}, direction="out")
        b = self.read("m2", "2027-03-05T11:00-08:00", {"items": [item("commitment", "owner", "send the invoice to Mia",
                                                                      w, "accepted")], "updates": []}, direction="out")
        self.assertNotEqual(a["items"], b["items"])


class FakeSend:
    def __init__(self):
        self.sent = []

    def __call__(self, payload, targets, kind):
        self.sent.append((kind, payload))
        return len(targets), []


class TestAlerts(Base):
    def setUp(self):
        super().setUp()
        self.cfg = dict(push.defaults(), targets=["notify.mobile_app_x"], quiet_start="22:00", quiet_end="07:00")
        self.send = FakeSend()
        self.vips = {"p:vip"}
        self.al = conflicts.Alerter(self.ms, self.lv, lambda: self.cfg, self.send,
                                    lambda title, message, tag=None, urgent=False: {"title": title, "message": message,
                                                                                    "tag": tag, "urgent": urgent},
                                    vip=lambda source, handle: handle in self.vips, clock=lambda: self.now[0])
        self.ex.alerter = self.al

    def clash(self, at, sender="p:dana"):
        self.read("a1", at, {"items": [item(what="call", w=when("Wed 10", "2027-03-03", "10:00"))], "updates": []},
                  sender="p:marco", name="Marco", source="gmail", thread="m")
        return self.read("a2", at, {"items": [item(what="truck", w=when("Wed 10", "2027-03-03", "10:00"))],
                                    "updates": []}, sender=sender, name="Dana", text="Can you help Wednesday at 10?")

    def test_clash_alert_has_names_not_message_text(self):
        res = self.clash("2027-03-02T12:00-08:00")
        self.assertEqual([a[2] for a in res["alerts"]], ["sent"])
        kind, p = self.send.sent[0]
        self.assertEqual(kind, "clash")
        self.assertIn("Dana (Signal)", p["message"])
        self.assertIn("Marco (email)", p["message"])
        self.assertNotIn("help", p["message"] + p["title"])
        self.assertTrue(p["urgent"])

    def test_quiet_hours_hold_until_flush(self):
        res = self.clash("2027-03-01T23:00-08:00")
        self.assertEqual([a[2] for a in res["alerts"]], ["held"])
        self.assertEqual(self.send.sent, [])
        self.now[0] = ts("2027-03-02T07:30-08:00")
        self.assertEqual([a[2] for a in self.al.flush()], ["sent"])

    def test_vips_come_through_quiet_hours(self):
        res = self.clash("2027-03-01T23:00-08:00", sender="p:vip")
        self.assertEqual(sorted(a[:1] + a[2:] for a in res["alerts"]), [("clash", "sent"), ("vip_ask", "sent")])
        self.cfg["vip_through_quiet"] = False
        res = self.read("a3", "2027-03-01T23:05-08:00", {"items": [item(what="other thing")], "updates": []},
                        sender="p:vip", thread="v2")
        self.assertEqual([a[2] for a in res["alerts"]], ["held"])

    def test_off_and_rate_limit(self):
        self.cfg["clashes"] = False
        self.assertEqual([a[2] for a in self.clash("2027-03-02T12:00-08:00")["alerts"]], ["off"])
        self.cfg.update(clashes=True, max_per_hour=1)
        got = []
        for mid in ("b1", "b2"):
            got += [a[2] for a in self.read(mid, "2027-03-02T12:01-08:00", {"items": [item(what=f"x {mid}", w=when(
                "Wed 10", "2027-03-03", "10:00"))], "updates": []}, sender=f"p:{mid}", thread=mid)["alerts"]]
        self.assertEqual(got[0], "sent")
        self.assertTrue(len(got) > 1 and all(g == "held" for g in got[1:]), got)

    def test_push_settings_accept_the_new_switches(self):
        c = push.patch_config(push.defaults(), {"clashes": False, "vip_asks": False, "vip_through_quiet": False})
        self.assertEqual((c["clashes"], c["vip_asks"], c["vip_through_quiet"]), (False, False, False))
        self.assertTrue(push.LEDGER_DEFAULTS["vip_through_quiet"])


class FakeSorter:
    def __init__(self, base="http://m:8080/v1"):
        self.base = base
        self.chat = False
        self.model = "m"
        self.at = dt.datetime(2027, 3, 1, 10, 0, tzinfo=LA)

    def _server(self):
        return self.base, ""

    def settings(self):
        return {"model": self.model, "quiet_times": [["12:16", "12:45"], ["01:00", "04:00"]]}

    def _now(self):
        return self.at

    def call_guard(self, now=None):
        return 300, None

    def chat_first(self):
        return self.chat

    def post_bg(self, body, timeout, kind="sorter", units=None):
        return {"choices": [{"message": {"content": json.dumps({"items": [item(what="ok")], "updates": []})}}]}

    def time_cap(self, s, kind, units):
        return 4000


class TestRunner(Base):
    def test_gate_and_pass(self):
        s = FakeSorter()
        r = extract.Runner(self.ex, s, clock=lambda: self.now[0])
        self.ex.call = r.call
        self.msg("m1", "2027-03-01T09:30-08:00", "hello")
        self.assertIsNone(r.gate())
        s.at = dt.datetime(2027, 3, 1, 12, 20, tzinfo=LA)
        self.assertEqual(r.gate(), "blocked")
        s.at = dt.datetime(2027, 3, 1, 2, 0, tzinfo=LA)
        self.assertEqual(r.gate(), "blocked")                         # the owner's quiet times
        s.at = dt.datetime(2027, 3, 1, 12, 20, tzinfo=LA)
        s.model = ""
        self.assertEqual(r.gate(), "no_model")                        # no pick and nothing listed
        s.model = "m"
        s.at = dt.datetime(2027, 3, 1, 10, 0, tzinfo=LA)
        s.chat = True
        self.assertEqual(r.gate(), "chat")
        s.chat = False
        self.ms.set_meta("ledger_enabled", "0")
        self.assertEqual(r.gate(), "off")
        self.ms.set_meta("ledger_enabled", "1")
        self.assertEqual(r.once()["read"], 1)
        self.assertEqual(self.ex.backlog(), 0)
        self.assertEqual(r.once()["read"], 0)
        s.base = ""
        self.assertEqual(r.gate(), "no_server")

    def test_busy_stops_the_pass_and_keeps_the_message_queued(self):
        def busy(body, info):
            raise writer.Busy("preempted")
        self.ex.call = busy
        self.msg("m1", "2027-03-01T09:30-08:00", "hello")
        with self.assertRaises(writer.Busy):
            self.ex.run_pass(60)
        self.assertEqual(self.ex.backlog(), 1)

    def test_gmail_promotions_get_the_narrow_read_and_clean_up(self):
        # 0.22.0: no rule without a request any more; the narrow read (tests/test_ledger_noise_0220.py)
        res = self.ex.read(self.msg("p1", "2027-03-01T09:30-08:00", "50% off", source="gmail",
                                    sender="deals@shop.com", subject="Sale", labels=["CATEGORY_PROMOTIONS"]))
        self.assertEqual(self.show(res["items"][0])["section"], "clean_up")
        self.assertEqual(len(self.fake.bodies), 1)
        self.assertEqual(self.fake.bodies[0][1]["tier"], "narrow")


@unittest.skipUnless(os.environ.get("CALLSHEET_ACCEPTANCE") and SYN.is_file(), "CALLSHEET_ACCEPTANCE (the synthetic cases.jsonl) is not set")
class TestReplay(unittest.TestCase):
    def run_replay(self, *extra):
        import contextlib
        import io
        import replay_ledger
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            code = replay_ledger.main(["--cases", str(SYN), "--quiet", "--out", f"{d}/s.json", *extra])
            return code, json.loads(pathlib.Path(d, "s.json").read_text())

    def test_oracle_scores_full_marks_and_bad_scores_badly(self):
        code, s = self.run_replay()
        self.assertEqual((code, s["gate"]), (0, "pass"))
        sure = s["metrics"]["all"]["sure"]
        for m in ("asks", "clashes", "times", "updates", "follow_ups", "dedupe", "never_hide"):
            self.assertEqual(sure[m]["pct"], 100.0, m)
        self.assertEqual(s["missed"], {"asks": 0, "clashes": 0, "wrong_times": 0})
        self.assertEqual(s["false"].get("false_items", 0), 0)
        self.assertEqual(s["run"]["messages"], 256)                      # set 1.2 (1.1: 204)
        code, b = self.run_replay("--extractor", "bad")
        self.assertEqual((code, b["gate"]), (1, "fail"))
        self.assertGreaterEqual(b["missed"]["clashes"], 15)
        self.assertGreater(b["false"]["per_20_cases"], 5)

    def test_clash_follow_ups_without_the_model(self):
        code, s = self.run_replay("--no-followups", "--case", "S-FUP-01", "--case", "S-FUP-02")
        self.assertEqual(s["metrics"]["all"]["sure"]["follow_ups"]["pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
