"""0.22.0 one reminder store: Chat's snooze and remind (actions.py item.snooze / item.remind) go through Today's
reminders (today_reminders, Today.set_aside, Today.tick), the same store as Today's Remind me buttons; the rows Chat
kept in prefs' owner_actions (0.17.0) move there once. A snooze hides the item and brings it back with no phone
reminder; a reminder hides it and then reminds. Undo cancels; reminders carry names, never message text."""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import actions  # noqa: E402
import conflicts  # noqa: E402
import prefs  # noqa: E402
import push  # noqa: E402
import test_chat_cards_0170 as cc  # noqa: E402
import today  # noqa: E402
from test_chat_store_0170 import T0, UTC  # noqa: E402


class OneReminderStore(cc.CardBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(push.defaults(), targets=["notify.mobile_app_x"], quiet_start="22:00", quiet_end="07:00")
        self.send = cc.FakeSend()
        self.al = conflicts.Alerter(self.ms, self.lv, lambda: self.cfg, self.send, cc.payload, clock=self.clock,
                                    prefs=self.prefs)
        self.td = today.Today(self.ms, self.lv, self.clock, alerter=self.al)
        self.A = actions.Actions(self.ms, self.clock, today=self.td, prefs=self.prefs, tz=lambda: UTC)

    def rows(self):
        return [dict(r) for r in self.ms._read("SELECT * FROM today_reminders ORDER BY id")]  # noqa: SLF001

    def test_snooze_and_remind_land_in_todays_store(self):
        s = self.A.run("item.snooze", [self.truck], {"until_ts": T0 + 3600}, via="chat", ref="c_x:1")
        r = self.A.run("item.remind", [self.dinner], {"until_ts": T0 + 3600}, via="chat", ref="c_x:2")
        rows = self.rows()
        self.assertEqual([(x["item"], x["hide"], x["push"], x["state"]) for x in rows],
                         [(self.truck, 1, 0, "open"), (self.dinner, 1, 1, "open")])
        self.assertEqual(self.prefs.snoozed(), self.td.snoozed())                     # one read, one store
        self.assertEqual(set(self.td.snoozed()), {self.truck, self.dinner})
        self.assertEqual(self.ms._read("SELECT COUNT(*) FROM owner_actions")[0][0], 0)  # noqa: SLF001
        self.now[0] = T0 + 3601
        out = dict(self.td.tick())
        self.assertEqual(out[rows[0]["id"]], "ended")                                 # a snooze: back, no phone
        self.assertEqual(out[rows[1]["id"]], "sent")                                  # a reminder: to the phone
        self.assertEqual([k for k, _ in self.send.sent], ["reminder"])
        self.assertNotIn("dinner", json.dumps(self.send.sent).lower())                # names, never message text
        self.assertEqual(self.td.snoozed(), {})
        with self.assertRaises(actions.Invalid):                                      # already due: nothing to undo
            self.A.undo(s["id"])
        with self.assertRaises(actions.Invalid):
            self.A.undo(r["id"])

    def test_undo_cancels_and_a_new_one_replaces(self):
        self.td.note({"items": [self.truck], "chips": ["remind_tomorrow"]})          # Today's button
        s = self.A.run("item.snooze", [self.truck], {"until_ts": T0 + 7200}, via="chat")
        self.assertEqual([x["state"] for x in self.rows()], ["replaced", "open"])     # one open per item
        self.A.undo(s["id"])
        self.assertEqual(self.rows()[-1]["state"], "cancelled")
        self.assertEqual(self.td.snoozed(), {})
        self.assertIn(self.truck, [r["id"] for r in self.lv.view(now=T0)["needs_you"]])

    def test_old_owner_actions_rows_move_once(self):
        def legacy(item, action, until, undone=None, fired=None):
            return self.ms.write(lambda db: db.execute(
                "INSERT INTO owner_actions (item, action, fields, label, before, until_ts, ref, at, undone_at, fired_at) "
                "VALUES (?,?,'{}','x','{}',?,'c_x:1',?,?,?)", (item, action, until, T0 - 60, undone, fired)).lastrowid)
        a = legacy(self.truck, "snooze", T0 + 3600)
        b = legacy(self.dinner, "remind", T0 + 7200)
        c = legacy(self.dinner, "remind", T0 - 10, undone=T0 - 5)                    # undone: stays put
        d = legacy(self.truck, "snooze", T0 - 10)                                     # over: nothing to carry
        e = legacy(self.dinner, "remind", T0 - 30)                                    # due, never fired: fires now
        td2 = today.Today(self.ms, self.lv, self.clock, alerter=self.al)              # the next start migrates
        today.Today(self.ms, self.lv, self.clock)                                     # and only once
        rows = self.rows()   # in due order, one open per item: the dinner's later reminder replaces the due one
        self.assertEqual([(x["item"], x["push"], x["due_ts"], x["state"]) for x in rows],
                         [(self.dinner, 1, T0 - 30, "replaced"), (self.truck, 0, T0 + 3600, "open"),
                          (self.dinner, 1, T0 + 7200, "open")])
        moved = {r["id"]: json.loads(r["fields"]).get("moved", "-") for r in self.ms._read(  # noqa: SLF001
            "SELECT id, fields FROM owner_actions")}
        self.assertEqual(moved, {a: rows[1]["id"], b: rows[2]["id"], c: "-", d: None, e: rows[0]["id"]})
        self.assertEqual(set(self.td.snoozed()), {self.truck, self.dinner})
        self.prefs.undo_action(a)                                                     # an old card's Undo
        self.assertEqual(self.rows()[1]["state"], "cancelled")
        self.assertEqual(self.td.snoozed(), {self.dinner: T0 + 7200})
        self.now[0] = T0 + 7201
        self.assertEqual(td2.tick(), [(rows[2]["id"], "sent")])
        with self.assertRaises(prefs.Invalid):
            self.prefs.act("remind", [self.truck], {"until_ts": T0 + 99})


if __name__ == "__main__":
    unittest.main()
