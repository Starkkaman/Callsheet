"""0.24.4 one-time codes and the voice API's model-server times.

Codes: verification and sign-in codes from automated senders are never Needs you, never first in a list, never a
clash or a bill. While fresh (15 minutes) one may show in the Security block (after the real alerts); then it is a
Clean up line ("Code, expired"). At read time (heads/extract), at view time (ledger.section_of) and for stored items
(Tidy up, with Undo). Chat's and the voice API's "what needs my attention" lists follow it.

Status: GET /v1/status gains background_busy/background_since (background.busy_view), next_brief, brief_times,
quiet_windows and suggested_engineer_quiet (call times ±20 min merged with the quiet times). Read-only.

Fictional senders only (Larkspur, Fernway); no live model."""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "callsheet" / "app"), str(ROOT / "tests")]
import background  # noqa: E402
import ledger as L  # noqa: E402
import tidy as tidy_mod  # noqa: E402
import today_fixtures as F  # noqa: E402
import voiceapi  # noqa: E402
import test_chat_store_0170 as cs  # noqa: E402
import test_voice_0243 as V0  # noqa: E402
from test_heads_0210 import GOOGLE, TZI, Base  # noqa: E402

LARK = ("no-reply@larkspur.example", "Larkspur")
SUBJ = "Your Larkspur verification code"
TEXT = "Your Larkspur verification code is 482913. It expires in 10 minutes."


def msg(subject="", text="", sender="no-reply@larkspur.example", source="gmail"):
    return {"subject": subject, "text": text, "sender": sender, "source": source, "direction": "in", "labels": "[]"}


class Detect(unittest.TestCase):
    def test_codes_from_automated_senders(self):
        yes = [msg(SUBJ, TEXT), msg("Sign in", "Code: 773120", "auth@fernway.example"),
               msg("", "482913 is your Fernway code", "52345", "sms"),
               msg("", "Fernway: your security code is 1234", "Fernway", "sms_live"),
               msg("Your one-time passcode", "Use it within 5 minutes.", "accounts@fernway.example"),
               msg("Your 2FA code", "", "notifications@fernway.example")]
        for m in yes:
            self.assertTrue(L.code_of(m), m)

    def test_not_codes(self):
        no = [msg("Lunch?", "my verification code didn't arrive, can you check?", "marco@friends.example"),
              msg("", "The gate code is 4821", "+15551234567", "sms"),
              msg("", "Your gate code is 4821", "52345", "sms"),
              msg("Your promo code: 4821 for 20% off", "", "deals@shop.example"),
              msg("Your booking", "Your confirmation code: 482913", "no-reply@shop.example"),
              msg("Security alert", "New sign-in to your account from Chrome", "no-reply@accounts.google.com"),
              dict(msg(SUBJ, TEXT), direction="out")]
        for m in no:
            self.assertFalse(L.code_of(m), m)

    def test_item_words(self):
        self.assertTrue(L.is_code_item({"subtype": "security", "what": "Fernway verification code"}))
        self.assertTrue(L.is_code_item({"subtype": "code", "what": "anything"}))
        self.assertFalse(L.is_code_item({"subtype": "security", "what": "New sign-in from Chrome"}))
        self.assertFalse(L.is_code_item({"subtype": "", "what": "send me the verification code"}))   # a person's ask
        self.assertFalse(L.is_code_item({"subtype": "invite", "never_hide": 1, "what": "code review: 2FA code"}))


class ReadTime(Base):
    def codes(self):
        return self.items(f"subtype='{L.CODE_GROUP}'")

    def test_fresh_code_in_security_after_real_alerts_never_needs_you(self):
        self.put("2026-11-11T11:30", *GOOGLE, "Security alert", "New sign-in to your account from Chrome")
        self.put("2026-11-11T11:55", *LARK, SUBJ, TEXT,
                 answer={"items": [F.item("ask", "sender", "enter the Larkspur verification code")], "updates": []})
        code = self.codes()
        self.assertEqual([(c["type"], c["never_hide"]) for c in code], [("cleanup", 0)])
        self.assertEqual(len(self.items("status='open'")), 2)              # the model's ask joined the code
        v = self.td.view()
        sec = v["now_next"]["security"]
        self.assertEqual(len(sec), 2)
        self.assertEqual(sec[-1]["id"], code[0]["id"])                      # after the real alert, newer or not
        self.assertNotIn(code[0]["id"], [r["id"] for r in self.ex.lv.view()["needs_you"]])
        self.assertEqual(v["clean_up"]["total"], 0)

    def test_after_15_minutes_clean_up_code_expired(self):
        self.put("2026-11-11T11:50", *LARK, SUBJ, TEXT)
        self.put("2026-11-11T11:00", "dana@friends.example", "Dana Ruiz", "Lunch", "Thanks for lunch!",
                 answer={"items": [F.item("cleanup", "sender", "thanks for lunch")], "updates": []})
        cid = self.codes()[0]["id"]
        self.assertEqual(L.section_of(self.codes()[0], self.now[0], TZI), "security")
        self.now[0] += 6 * 60
        self.assertEqual(L.section_of(self.codes()[0], self.now[0], TZI), "clean_up")
        v = self.td.view()
        self.assertEqual(v["now_next"]["security"], [])
        page = self.td.cleanup()
        grp = [g for g in page["groups"] if g["key"] == L.CODE_GROUP]
        self.assertEqual((grp[0]["label"], [r["id"] for r in grp[0]["rows"]]), (L.CODE_WHY, [cid]))
        self.assertEqual(page["groups"][-1]["key"], L.CODE_GROUP)           # last
        self.assertEqual(page["ids"][-1], cid)
        self.assertNotIn(cid, [r["id"] for r in v["clean_up"]["samples"]])
        self.assertEqual(self.ex.lv.view()["clean_up"][-1]["id"], cid)

    def test_never_a_clash_or_a_bill(self):
        self.events({"event_id": "e1", "title": "Dentist", "start": "2026-11-11T15:00", "end": "2026-11-11T16:00",
                     "start_ts": F.ts("2026-11-11T15:00"), "end_ts": F.ts("2026-11-11T16:00")})
        bill = dict(F.item("ask", "sender", "pay $12.00 with code 482913",
                           w=F.when("today at 3", "2026-11-11", "15:00", "16:00")), subtype="bill")
        self.put("2026-11-11T11:58", *LARK, "Your verification code", "Your verification code is 482913.",
                 answer={"items": [bill], "updates": []})
        it = self.codes()[0]
        self.assertEqual((it["type"], it["subtype"], it["start_ts"], it["amount"]), ("cleanup", "code", None, ""))
        self.assertEqual(self.ms._read("SELECT count(*) n FROM ledger_conflicts")[0]["n"], 0)  # noqa: SLF001
        self.assertEqual(self.items("subtype='bill'"), [])


class Stored(Base):
    """Items made before 0.24.4: a security alert from a code, and the model's ask from another code."""

    def seed(self):
        real = L.code_of
        L.code_of = lambda row: False                         # as 0.24.3 read them
        try:
            self.put("2026-11-11T09:00", *LARK, SUBJ, TEXT)
            self.put("2026-11-11T09:30", "52345", "52345", "", "482913 is your Fernway code", source="sms",
                     answer={"items": [F.item("ask", "sender", "use the Fernway code")], "updates": []})
        finally:
            L.code_of = real
        sec = self.items("subtype='security'")[0]
        ask = self.items("type='ask'")[0]
        return sec, ask

    def test_view_time_and_tidy_up(self):
        sec, ask = self.seed()
        lv = self.ex.lv.view()
        self.assertNotIn(sec["id"], [r["id"] for r in lv["security"] + lv["needs_you"]])   # view time: its words
        self.assertIn(sec["id"], [r["id"] for r in lv["clean_up"]])
        t = tidy_mod.Tidy(self.ms, lambda: self.now[0])
        plan = {m["item"]: (m["grp"], m["why"]) for m in t.plan()}
        self.assertEqual(plan, {sec["id"]: ("code", L.CODE_WHY), ask["id"]: ("code", L.CODE_WHY)})
        self.assertIn("Tidy up 2 items", t.banner()["text"])
        run = t.run()
        self.assertEqual(run["counts"], {"code": 2})
        moved = {i["id"]: (i["type"], i["subtype"]) for i in self.items(f"id IN ({sec['id']},{ask['id']})")}
        self.assertEqual(moved, {sec["id"]: ("cleanup", "code"), ask["id"]: ("cleanup", "code")})
        lv = self.ex.lv.view()
        self.assertEqual(lv["needs_you"], [])
        self.assertEqual([r["id"] for r in lv["clean_up"]][-2:], sorted([sec["id"], ask["id"]]))
        t.undo(run["run"])
        back = {i["id"]: (i["type"], i["subtype"]) for i in self.items(f"id IN ({sec['id']},{ask['id']})")}
        self.assertEqual(back[ask["id"]], ("ask", ""))

    def test_a_fresh_code_waits(self):
        self.now[0] = F.ts("2026-11-11T09:35")
        sec, ask = self.seed()
        plan = {m["item"] for m in tidy_mod.Tidy(self.ms, lambda: self.now[0]).plan()}
        self.assertEqual(plan, {sec["id"]})                 # the 09:30 text is still fresh


class ChatLists(cs.Base):
    def test_needs_you_and_asks_leave_codes_out(self):
        self.wire()
        self.msg("c1", "2026-11-11T08:00", TEXT, sender=LARK[0], name=LARK[1], subject=SUBJ,
                 answer={"items": [cs.item("enter the Larkspur verification code", cs.when("", "", ""))],
                         "updates": []})
        self.msg("p1", "2026-11-11T08:05", "Can you send the cut list today?", sender="kai@friends.example",
                 name="Kai Moss", answer={"items": [cs.item("send the cut list", cs.when("", "", ""))], "updates": []})
        t = self.tools()
        for which in ("needs_you", "asks"):
            got = t.run("ledger_list", {"list": which})[0]
            text = json.dumps(got)
            self.assertNotIn("verification", text, which)
            self.assertIn("cut list", text, which)
        got = t.run("ledger_list", {"list": "cleanup"})[0]
        self.assertIn("verification", json.dumps(got))


class VoiceCodesAndStatus(V0.VoiceBase):
    def test_code_is_never_needs_you_and_never_first(self):
        self.app.messages.upsert_message({
            "source": "gmail", "account": "acct", "msg_id": "code-1", "thread": "code-1", "direction": "in",
            "sender": LARK[0], "sender_name": LARK[1], "recipients": [], "ts": self.now[0] - 60, "subject": SUBJ,
            "text": TEXT, "labels": [], "attachments": []})
        row = self.app.messages.message("gmail", "acct", "code-1")
        self.app.extractor.call = lambda body, info: {"items": [], "updates": []}
        self.app.extractor.read(row)
        st, _, body = self.req("/v1/ledger?kind=needs_you")
        self.assertEqual(st, 200)
        self.assertTrue(body["results"])
        self.assertFalse([r for r in body["results"] if "verification" in r.get("what", "").lower()])
        st, _, body = self.req("/v1/ledger")
        codes = [r for r in body["results"] if "verification" in r.get("what", "").lower()]
        self.assertEqual([c["kind"] for c in codes], ["cleanup"])
        self.assertNotEqual(body["results"][0]["id"], codes[0]["id"])
        st, _, body = self.req("/v1/brief/today?for=voice")
        self.assertNotIn("verification", json.dumps(body).lower())

    def test_status_fields(self):
        self.app.store.update_settings({"model_quiet_times": [["01:00", "04:00"], ["23:30", "00:10"]]})
        self.addCleanup(self.app.store.update_settings, {"model_quiet_times": []})
        st, _, body = self.req("/v1/status")
        self.assertEqual(st, 200)
        for k in ("now", "sources", "freshness", "store"):
            self.assertIn(k, body)
        self.assertIs(body["background_busy"], False)
        self.assertIsNone(body["background_since"])
        times = body["brief_times"]
        self.assertTrue(times and all(len(t) == 5 and t[2] == ":" for t in times))
        self.assertEqual(body["quiet_windows"], [["01:00", "04:00"], ["23:30", "00:10"]])
        self.assertTrue(body["next_brief"].startswith("2026-11-1"))
        self.assertTrue(body["next_brief"].endswith("-08:00"))
        self.assertEqual(body["suggested_engineer_quiet"],
                         voiceapi.engineer_quiet(times, body["quiet_windows"]))
        self.assertIn(["01:00", "04:00"], body["suggested_engineer_quiet"])
        with background.working(lambda: 1795000000.0):
            st, _, body = self.req("/v1/status")
        self.assertEqual((body["background_busy"], body["background_since"]), (True, 1795000000))
        self.assertIs(self.req("/v1/status")[2]["background_busy"], False)
        self.assertEqual(self.req("/v1/status", method="POST")[0], 405)


class EngineerQuiet(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(voiceapi.engineer_quiet([], []), [])
        self.assertEqual(voiceapi.engineer_quiet(["07:30", "12:00"], [["11:50", "12:30"]]),
                         [["07:10", "07:50"], ["11:40", "12:30"]])
        self.assertEqual(voiceapi.engineer_quiet(["00:10"], [["23:00", "23:55"]]), [["23:00", "00:30"]])
        self.assertEqual(voiceapi.engineer_quiet([], [["00:00", "23:59"], ["23:59", "00:00"]]), [["00:00", "00:00"]])

    def test_busy_nests(self):
        with background.working(lambda: 100.0):
            with background.working(lambda: 200.0):
                self.assertEqual(background.busy_view(), {"busy": True, "since": 100})
            self.assertTrue(background.busy_view()["busy"])
        self.assertEqual(background.busy_view(), {"busy": False, "since": None})


if __name__ == "__main__":
    unittest.main()
