"""0.23.0 Clashes vs heads-ups (the owner, 2026-11-17; mockup clash-vs-headsup-v1): the rule "does it need you there?"
(its matrix and the rule sheet's 8 worked examples as fixtures), the per-calendar choice, the "Does this need you?" card
(message text can't answer it), teaching from an answer, invites by context, alerts only for clashes and Maybe clashes,
"Got it" per item with Undo, and the trip's grouped clash keeping only the entries that need him. Fictional people
and example domains only; no model server, no network."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import Base, item, ts, when  # noqa: E402

import chatstore  # noqa: E402
import conflicts  # noqa: E402
import need as N  # noqa: E402
import prefs as prefs_mod  # noqa: E402
import today  # noqa: E402

ME = {"name": "Sam Rowe", "address": "sam@home.example", "self": True}


def att(response="needsAction", optional=False, **kw):
    return dict(ME, response=response, optional=optional, **kw)


def guest(name, response="accepted"):
    return {"name": name, "address": name.split()[0].lower() + "@work.example", "response": response, "self": False}


def ev(eid, title, start, end, cal="primary", **kw):
    return dict({"account": "acc", "calendar": cal, "event_id": eid, "title": title, "start_ts": ts(start),
                 "end_ts": ts(end), "status": "confirmed"}, **kw)


class NeedBase(Base):
    def setUp(self):
        super().setUp()
        self.now[0] = ts("2027-03-01T08:10-08:00")
        self.ms.write(lambda db: N.sync_calendars(db, [{"id": "acc", "calendars": [
            {"id": "primary", "summary": "Sam", "primary": True},
            {"id": "family", "summary": "Family calendar"},
            {"id": "school", "summary": "Fernhill School", "use": "headsup"},
            {"id": "noisy", "summary": "Town events", "use": "hide"}]}]))
        self.pf = prefs_mod.Prefs(self.ms, clock=lambda: self.now[0])

    def put(self, *events):
        self.ms.upsert_events(list(events))
        return [self.row(e["event_id"]) for e in events]

    def row(self, eid):
        return self.ms._read("SELECT * FROM events WHERE event_id=?", (eid,))[0]  # noqa: SLF001

    def need(self, eid):
        return self.ms.write(lambda db: N.Need(db).event(self.row(eid)))

    def item_need(self, iid):
        it = self.lv.item(iid, detail=False)
        return self.ms.write(lambda db: N.Need(db).item(dict(db.execute(
            "SELECT * FROM ledger_items WHERE id=?", (iid,)).fetchone())))["need"] if it else None

    def mine(self, mid, what, w, at="2027-03-01T08:00-08:00", type="commitment", conf="sure", subtype=""):
        return self.read(mid, at, {"items": [item(type, "owner", what, w, "accepted" if type == "commitment" else "open",
                                                  conf, subtype)], "updates": []},
                         direction="out", to=[("p:dana", "Dana")])["items"][0]

    def verdicts(self):
        return sorted((c["verdict"], c["kind"]) for c in self.lv.conflicts())

    def td(self):
        return today.Today(self.ms, self.lv, lambda: self.now[0])


class TestTheRule(unittest.TestCase):
    def test_the_matrix(self):
        want = {("yes", "yes"): "clash", ("yes", "maybe"): "maybe", ("yes", "no"): "headsup",
                ("maybe", "yes"): "maybe", ("maybe", "maybe"): "maybe", ("maybe", "no"): "headsup",
                ("no", "yes"): "headsup", ("no", "maybe"): "headsup", ("no", "no"): "none"}
        for (a, b), v in want.items():
            self.assertEqual(N.verdict(a, b), v, (a, b))

    def test_a_guessed_time_makes_a_clash_a_maybe(self):
        self.assertEqual(N.conflict_verdict("yes", "yes", "likely"), "maybe")
        self.assertEqual(N.conflict_verdict("yes", "no", "likely"), "headsup")
        self.assertEqual(N.conflict_verdict("yes", "yes", "sure"), "clash")


class TestWorkedExamples(NeedBase):
    """The rule sheet's 8 worked examples (this week), each as a fixture."""

    def test_1_rowing_you_said_yes_and_jonas_call_required_not_answered_is_a_clash(self):
        self.put(ev("j1", "Call with Jonas Weber", "2027-03-02T18:00-08:00", "2027-03-02T18:30-08:00",
                    organizer="jonas@work.example", attendees=[att(), guest("Jonas Weber")]))
        self.assertEqual(self.need("j1")["need"], "yes")
        self.mine("o1", "rowing practice", when("Tue 6 pm", "2027-03-02", "18:00", "19:30"))
        self.assertEqual(self.verdicts(), [("clash", "overlap")])

    def test_2_design_review_you_present_and_the_pickup_you_promised_is_a_clash(self):
        self.put(ev("d1", "Design review", "2027-03-04T15:00-08:00", "2027-03-04T16:00-08:00", organizer="self"))
        self.mine("o1", "school pickup for Mira", when("Thu 3:30", "2027-03-04", "15:30", "16:00"))
        self.assertEqual(self.need("d1")["why"], "you made it")
        self.assertEqual(self.verdicts(), [("clash", "overlap")])

    def test_3_parents_evening_and_drinks_after_work_with_a_guessed_time_is_a_maybe(self):
        self.put(ev("p1", "Parents' evening", "2027-03-04T18:40-08:00", "2027-03-04T19:40-08:00", creator="self"))
        self.mine("o1", "drinks with Tom after work", when("after work", "2027-03-04", "18:00", "20:00", approx=True),
                  conf="likely")
        self.assertEqual(self.verdicts(), [("maybe", "overlap")])
        self.assertEqual(conflicts.alerts_for(self.lv.conflicts()[0]), True)    # the owner: "or where uncertain"

    def test_4_the_trip_lists_only_the_entries_that_need_him(self):
        self.put(ev("t1", "Sprint planning", "2027-03-05T10:00-08:00", "2027-03-05T11:00-08:00",
                    organizer="lead@work.example", attendees=[att(), guest("Ada Lead")]),
                 ev("t2", "Regatta volunteer shift", "2027-03-06T09:00-08:00", "2027-03-06T12:00-08:00",
                    attendees=[att("accepted"), guest("Dana Reyes")]),
                 ev("t3", "Roadmap readout", "2027-03-08T08:30-08:00", "2027-03-08T09:00-08:00",
                    organizer="pm@work.example", attendees=[att(optional=True), guest("Pat Morgan")]))
        self.mine("o1", "trip to Lisbon", when("Fri to Mon", "2027-03-05", end_date="2027-03-08", all_day=True),
                  type="event", subtype="trip")
        cs = [c for c in self.lv.conflicts() if c["entries"]]
        self.assertEqual(len(cs), 1)
        self.assertEqual([e["title"] for e in cs[0]["entries"]], ["Sprint planning", "Regatta volunteer shift"])
        self.assertEqual(cs[0]["verdict"], "clash")
        hu = self.td().headsup(now=self.now[0])
        self.assertIn("Roadmap readout", [r["title"] for d in hu["days"] for r in d["rows"]])
        self.assertEqual(self.need("t3")["word"], "FYI")

    def test_5_an_optional_all_hands_on_his_day_off_is_fyi(self):
        self.put(ev("a1", "Northwind all-hands", "2027-03-03T11:00-08:00", "2027-03-03T12:00-08:00",
                    organizer="ceo@work.example", attendees=[att(optional=True), guest("Cy Chief")]))
        r = self.need("a1")
        self.assertEqual((r["need"], r["word"], r["why"]), ("no", "FYI", "optional for you"))

    def test_6_a_sofa_delivery_window_is_happening_until_mira_asks_him_to_be_home(self):
        self.put(ev("s1", "Sofa delivery window", "2027-03-02T08:00-08:00", "2027-03-02T12:00-08:00", cal="family",
                    organizer="mira@home.example"),
                 ev("s2", "Stand-up", "2027-03-02T09:30-08:00", "2027-03-02T09:45-08:00", organizer="self"))
        self.assertEqual((self.need("s1")["need"], self.need("s1")["word"]), ("no", "Happening"))
        mira = self.read("m1", "2027-03-01T09:00-08:00", {"items": [item(
            "ask", "sender", "be home for the sofa delivery", when("8 to 12", "2027-03-02", "08:00", "12:00"))],
            "updates": []}, sender="mira@home.example", name="Mira", source="sms",
            text="Can you be home for the sofa delivery tomorrow 8-12?")["items"][0]
        self.assertEqual(self.item_need(mira), "maybe")
        self.assertIn("maybe", [c["verdict"] for c in self.lv.conflicts()])

    def test_7_someone_elses_plans_school_and_community_days_never_clash(self):
        self.put(ev("b1", "Mira: book club", "2027-03-02T19:00-08:00", "2027-03-02T21:00-08:00", cal="family",
                    organizer="mira@home.example"),
                 ev("b2", "Fernhill science fair", "2027-03-02T16:00-08:00", "2027-03-02T18:00-08:00", cal="school"),
                 ev("b3", "Harbour Street market", "2027-03-02T07:00-08:00", "2027-03-02T15:00-08:00", cal="family"))
        for eid in ("b1", "b2", "b3"):
            self.assertEqual(self.need(eid)["need"], "no", eid)
        self.mine("o1", "your evening at home with friends", when("Tue evening", "2027-03-02", "18:00", "22:00"))
        self.assertTrue(all(c["verdict"] == "headsup" for c in self.lv.conflicts()))
        v = self.td().view()
        self.assertEqual(v["now_next"]["clashes"], [])
        titles = [r["title"] for d in v["headsup"]["days"] for r in d["rows"]]
        self.assertEqual(sorted(titles), ["Fernhill science fair", "Harbour Street market", "Mira: book club"])

    def test_8_a_budget_walkthrough_he_was_added_to_as_optional_is_fyi_until_he_says_yes(self):
        self.put(ev("w1", "Budget walkthrough", "2027-03-04T14:00-08:00", "2027-03-04T15:00-08:00",
                    organizer="leo@work.example", attendees=[att(optional=True), guest("Leo Park")]))
        self.mine("o1", "focus block", when("Thu 2 pm", "2027-03-04", "14:00", "15:00"))
        self.assertEqual(self.verdicts(), [("headsup", "overlap")])
        self.put(ev("w1", "Budget walkthrough", "2027-03-04T14:00-08:00", "2027-03-04T15:00-08:00",
                    organizer="leo@work.example", attendees=[att("accepted", optional=True), guest("Leo Park")]))
        self.ms.write(lambda db: conflicts.sweep(db, self.now[0], N_LA()))
        self.assertEqual(self.verdicts(), [("clash", "overlap")])          # "then they're yours"


def N_LA():
    import ledger
    return ledger.zone("America/Los_Angeles")


class TestCalendarChoice(NeedBase):
    def test_defaults_primary_is_mine_the_rest_ask_me(self):
        uses = {r["calendar"]: r["use"] for r in self.ms._read("SELECT * FROM calendar_use")}  # noqa: SLF001
        self.assertEqual(uses, {"primary": "mine", "family": "ask", "school": "headsup", "noisy": "hide"})
        self.assertEqual(N.default_use({"primary": True}), "mine")

    def test_each_choice(self):
        self.put(*[ev("c-" + c, "Planning session", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00", cal=c,
                      organizer="ada@work.example") for c in ("primary", "family", "school", "noisy")])
        self.assertEqual(self.need("c-primary")["need"], "yes")
        # 0.24.0 (the audit): an entry on a calendar he only reads is never a clash: a heads-up with the ask card
        self.assertEqual((self.need("c-family")["need"], self.need("c-family")["ask"]), ("no", True))
        self.assertEqual(self.need("c-school")["need"], "no")
        self.assertTrue(self.need("c-noisy")["hide"])
        hu = self.td().headsup(now=self.now[0])
        self.assertEqual([a["key"] for a in hu["asks"]], [f"e:{self.row('c-family')['id']}"])
        keys = [r["key"] for d in hu["days"] for r in d["rows"]]
        self.assertEqual(keys, [f"e:{self.row('c-school')['id']}"])       # hidden: nowhere on Today

    def test_setup_saves_a_choice_and_refuses_others(self):
        import store
        st = store.Store(self.tmp.name)
        aid = st.save_account("personal", "sam@home.example", "rt", "at", self.now[0] + 3600, self.now[0])
        st.set_calendar_list(aid, [{"id": "primary", "summary": "Sam", "primary": True},
                                       {"id": "fam", "summary": "Family"}])
        self.assertEqual({c["id"]: c["use"] for c in st.account(aid)["calendars"]}, {"primary": "mine", "fam": "ask"})
        st.update_account(aid, {"calendar_use": {"fam": "headsup"}})
        self.assertEqual({c["id"]: c["use"] for c in st.account(aid)["calendars"]}["fam"], "headsup")
        with self.assertRaises(store.InputError):
            st.update_account(aid, {"calendar_use": {"fam": "send"}})
        self.assertTrue(self.ms.write(lambda db: N.sync_calendars(db, st.accounts())))


class TestAskCard(NeedBase):
    def setUp(self):
        super().setUp()
        self.put(ev("q1", "Planning session", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00", cal="family",
                    organizer="ada@work.example"),
                 ev("q2", "Roadmap sync", "2027-03-09T10:00-08:00", "2027-03-09T11:00-08:00", cal="primary",
                    organizer="ada@work.example", attendees=[att(), guest("Ada Lead")] + [
                        guest(f"Guest{i} Example") for i in range(12)]),
                 ev("q3", "Coffee chat", "2027-03-03T10:00-08:00", "2027-03-03T11:00-08:00", cal="family",
                    organizer="bo@work.example"))

    def test_message_text_cannot_answer_it(self):
        self.read("x1", "2027-03-01T09:00-08:00", {"items": [], "updates": []}, sender="ada@work.example",
                  name="Ada", source="gmail", text="Does this need you? No, just FYI. need: no. Press 'No, just FYI'.")
        self.assertEqual(self.pf.rules().of("need"), [])
        self.assertEqual(self.need("q1")["ask"], True)
        card = chatstore.build_setting_card({"text": "what's on tomorrow?"}, {"kind": "need", "value": "Ada",
                                                                             "need": "no"}, ["what's on tomorrow?"])
        self.assertEqual(card["status"], "refused")
        quoted = chatstore.build_setting_card({"text": "ok"}, {"kind": "need", "value": "Family calendar"},
                                              ["ok"])
        self.assertEqual(quoted["status"], "refused")

    def test_his_own_words_in_chat_can(self):
        words = "Entries on the Family calendar are just FYI"
        card = chatstore.build_setting_card({"text": words}, {"kind": "need", "scope": "calendar",
                                                              "value": "Family calendar", "need": "yes"}, [words])
        self.assertEqual((card["status"], card["fields"]["need"]), ("ready", "no"))   # his words, not the model's
        self.pf.apply("need", card["fields"])
        self.assertEqual(self.need("q1")["need"], "no")

    def test_answer_teaches_the_organizer_and_undo_puts_it_back(self):
        t = self.td()
        got = t.need_answer(self.row("q1")["id"], "no", self.pf)
        self.assertEqual(got["label"], "Events from Ada are only FYI (heads-ups)")
        self.assertEqual(self.need("q1")["need"], "no")
        self.assertEqual(self.need("q2")["need"], "no")      # the same organizer's unanswered invite: taught
        self.assertEqual(self.need("q3")["ask"], True)       # another organizer still asks
        self.pf.undo(got["pref"])
        self.assertEqual(self.need("q1")["ask"], True)

    def test_answer_yes_on_a_calendar_without_an_organizer_teaches_that_calendar(self):
        self.put(ev("q4", "Gym", "2027-03-04T07:00-08:00", "2027-03-04T08:00-08:00", cal="family"))
        got = self.td().need_answer(self.row("q4")["id"], "yes", self.pf)
        self.assertIn("Entries on Family calendar need you", got["label"])
        self.put(ev("q5", "Swim", "2027-03-05T07:00-08:00", "2027-03-05T08:00-08:00", cal="family"))
        self.assertEqual(self.need("q5")["need"], "yes")

    def test_the_answer_rechecks_the_clash(self):
        self.mine("o1", "dentist", when("Tue 10", "2027-03-02", "10:00", "11:00"))
        self.assertEqual(self.verdicts(), [("headsup", "overlap")])      # 0.24.0: a shared entry is no clash until he says
        self.td().need_answer(self.row("q1")["id"], "yes", self.pf)
        self.assertEqual(self.verdicts(), [("clash", "overlap")])


class TestInvitesByContext(NeedBase):
    def invite(self, eid, title="Project call", organizer="kai@work.example", others=1, **kw):
        self.put(ev(eid, title, "2027-03-02T13:00-08:00", "2027-03-02T14:00-08:00", organizer=organizer,
                    attendees=[att(**kw)] + [guest(f"Kai{i} Lund") for i in range(others)]))
        return self.need(eid)

    def test_required_small_meeting_needs_him(self):
        self.assertEqual(self.invite("i1")["need"], "yes")

    def test_optional_is_fyi(self):
        self.assertEqual(self.invite("i2", optional=True)["word"], "FYI")

    def test_a_large_broadcast_or_no_guest_list_is_unclear_so_maybe_and_ask(self):
        r = self.invite("i3", "Company all-hands", others=3)
        self.assertEqual((r["need"], r["ask"]), ("maybe", True))
        r = self.invite("i4", others=0)
        self.assertEqual((r["need"], r["ask"]), ("maybe", True))

    def test_his_history_with_the_organizer(self):
        for i in range(3):
            self.put(ev(f"h{i}", "Weekly", f"2027-02-0{i + 1}T13:00-08:00", f"2027-02-0{i + 1}T14:00-08:00",
                        organizer="vi@work.example", attendees=[att("accepted")]))
            self.put(ev(f"n{i}", "Webinar", f"2027-02-1{i + 1}T13:00-08:00", f"2027-02-1{i + 1}T14:00-08:00",
                        organizer="news@vendor.example", attendees=[att("declined")]))
        self.assertEqual(self.invite("i5", "Town hall", organizer="vi@work.example", others=0)["need"], "yes")
        self.assertEqual(self.invite("i6", organizer="news@vendor.example", others=0)["need"], "no")

    def test_a_related_message_that_waits_on_him(self):
        self.invite("i7", others=0)
        e = self.row("i7")
        iid = self.read("r1", "2027-03-01T09:00-08:00", {"items": [item(what="join the project call", w=when(
            "Tue 1 pm", "2027-03-02", "13:00", "14:00"))], "updates": []}, sender="kai@work.example", name="Kai",
            source="gmail", text="Can you join Tuesday's call?")["items"][0]
        self.ms.write(lambda db: db.execute("UPDATE ledger_items SET cal_ref=? WHERE id=?", (e["id"], iid)))
        self.assertEqual(self.need("i7")["need"], "yes")


class TestAlerts(NeedBase):
    def test_only_clashes_and_maybe_clashes_alert(self):
        for v, want in (("clash", True), ("maybe", True), ("headsup", False), ("none", False)):
            self.assertEqual(conflicts.alerts_for({"verdict": v}), want, v)

    def test_a_headsup_overlap_sends_nothing(self):
        sent = []
        al = conflicts.Alerter(self.ms, self.lv, lambda: {"enabled": True, "targets": ["phone"]},
                               lambda p, t, k=None: sent.append(p) or True,
                               lambda t, m, tag=None, urgent=False: {"title": t, "message": m},
                               clock=lambda: self.now[0])
        self.put(ev("b1", "Mira: book club", "2027-03-02T19:00-08:00", "2027-03-02T21:00-08:00", cal="family",
                    organizer="mira@home.example"))
        self.mine("o1", "dinner with friends", when("Tue 7 pm", "2027-03-02", "19:00", "21:00"))
        ids = [c["id"] for c in self.lv.conflicts()]
        out = al.notify({"conflicts": {"new": ids}})
        self.assertEqual([o for o in out if o[0] == "clash"], [])
        self.assertEqual(sent, [])

    def test_maybe_payload_says_so(self):
        al = conflicts.Alerter(self.ms, self.lv, lambda: {}, lambda *a: True,
                               lambda t, m, tag=None, urgent=False: {"title": t, "message": m},
                               clock=lambda: self.now[0])
        p = al.clash_payload({"id": 1, "verdict": "maybe", "seconds_until": 7200, "slot": {"label": "Tue 6 pm"},
                              "kind": "overlap", "sides": [{"kind": "event"}, {"kind": "event"}]})
        self.assertTrue(p["title"].startswith("Maybe clash"))


class TestGotIt(NeedBase):
    def test_per_item_with_undo_and_a_change_brings_it_back(self):
        self.put(ev("g1", "Fernhill science fair", "2027-03-02T16:00-08:00", "2027-03-02T18:00-08:00", cal="school"),
                 ev("g2", "Fernhill early dismissal", "2027-03-02T12:30-08:00", "2027-03-02T13:00-08:00", cal="school"))
        t = self.td()
        hu = t.headsup(now=self.now[0])
        self.assertEqual(hu["count"], 2)
        key = f"e:{self.row('g1')['id']}"
        got = t.gotit(key)
        hu = t.headsup(now=self.now[0])
        self.assertEqual((hu["count"], hu["seen"]), (1, 1))      # one item, not the day
        self.assertEqual([r["key"] for r in hu["show"]], [key])
        t.gotit_undo(got["id"])
        self.assertEqual(t.headsup(now=self.now[0])["count"], 2)
        t.gotit(key)
        self.put(ev("g1", "Fernhill science fair", "2027-03-02T17:00-08:00", "2027-03-02T19:00-08:00", cal="school"))
        self.assertEqual(t.headsup(now=self.now[0])["count"], 2)   # a new time: it comes back
        with self.assertRaises(today.Invalid):
            t.gotit("all")


class TestDayLanes(NeedBase):
    def test_his_blocks_on_the_you_lane_heads_ups_around_and_the_overlap_marked(self):
        self.put(ev("l1", "Stand-up", "2027-03-01T09:30-08:00", "2027-03-01T09:45-08:00", organizer="self"),
                 ev("l2", "Sofa delivery window", "2027-03-01T08:00-08:00", "2027-03-01T12:00-08:00", cal="family"),
                 ev("l3", "Call with Jonas", "2027-03-01T18:00-08:00", "2027-03-01T18:30-08:00",
                    organizer="jonas@work.example", attendees=[att(), guest("Jonas Weber")]))
        self.mine("o1", "rowing practice", when("6 pm", "2027-03-01", "18:00", "19:30"))
        v = self.td().view()
        d = v["day"]
        self.assertEqual([r["title"] for r in d["you"]], ["Stand-up", "Call with Jonas", "rowing practice"])
        self.assertEqual([r["title"] for r in d["around"]], ["Sofa delivery window"])
        self.assertEqual(len(d["clashes"]), 1)
        self.assertEqual(d["clashes"][0]["verdict"], "clash")
        self.assertIsNotNone(d["clashes"][0]["clash"])
        self.assertEqual(v["now_next"]["clashes"][0]["word"], "You’re double-booked")


class TestPageFacts(NeedBase):
    """0.23.0 page check fixes: the phone's Next days needs each clash's day; a one-line clash names a calendar entry
    once; the Choose step's picture marks a Maybe and names the owner's own side by what it is."""

    def test_clash_day_line_and_maybe_picture(self):
        import visuals
        self.put(ev("d1", "Design review", "2027-03-04T15:00-08:00", "2027-03-04T16:00-08:00", organizer="self"),
                 ev("p1", "Parents' evening", "2027-03-04T18:40-08:00", "2027-03-04T19:40-08:00", creator="self"))
        self.mine("o1", "school pickup for Mira", when("Thu 3:30", "2027-03-04", "15:30", "16:00"))
        self.mine("o2", "drinks with Tom after work", when("after work", "2027-03-04", "18:00", "20:00", approx=True),
                  conf="likely")
        cs = self.td().view()["now_next"]["clashes"]
        self.assertEqual([(c["verdict"], c["day"], c["day_label"]) for c in cs],
                         [("clash", "2027-03-04", "Thu"), ("maybe", "2027-03-04", "Thu")])
        line = "".join(t for t, _ in cs[0]["line"])
        self.assertEqual(line.count("Design review"), 1, line)
        f = visuals.StoreFacts(self.ms, self.lv, self.now[0])
        for c, lv in ((cs[0], "wn"), (cs[1], "mb")):
            d = visuals.fill({"type": "clash", "ref": f"clash:{c['id']}"}, f)["d"]
            self.assertEqual(d["lv"], lv)
            self.assertNotIn("the owner", [ln["l"] for ln in d["lanes"]])


if __name__ == "__main__":
    unittest.main()
