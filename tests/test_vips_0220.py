"""0.22.0 VIPs in one store (owner 2026-11-16): Setup's VIP list (store.Store.vips) is the only VIP home. Chat's VIP
(propose_setting kind vip) goes on that list (by name when it has no address, with its options); VIPs earlier versions
kept as Chat rules move onto it once, keeping who's a VIP; alerts, quiet hours, Needs you and Setup all read it."""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import conflicts  # noqa: E402
import people  # noqa: E402
import prefs  # noqa: E402
import push  # noqa: E402
import test_chat_cards_0170 as cc  # noqa: E402
from store import InputError  # noqa: E402


class VipStoreTest(cc.CardBase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(push.defaults(), targets=["notify.mobile_app_x"], quiet_start="22:00", quiet_end="07:00")
        self.send = cc.FakeSend()
        self.al = conflicts.Alerter(self.ms, self.lv, lambda: self.cfg, self.send, cc.payload, clock=self.clock,
                                    prefs=self.prefs)
        self.ex.alerter = self.al
        self.lv.vip_of = self.al.needs_you_vip
        self.n = 0

    ask_from = cc.AlertTest.ask_from

    def legacy_rule(self, fields):
        """A VIP as 0.17.0 to 0.21 kept it: a preferences row only (before NULL), not on Setup's list."""
        return self.ms.write(lambda db: db.execute(
            "INSERT INTO preferences (kind, fields, label, ref, created_at) VALUES ('vip', ?, 'legacy', 'c_x:1', ?)",
            (json.dumps(prefs.check("vip", fields)), self.now[0])).lastrowid)

    def test_setup_needs_an_address_chat_adds_by_name(self):
        with self.assertRaises(InputError):
            self.store.add_vip("Lena Park", "", "personal")
        v = self.store.add_vip("Lena", None, "personal", opts={"alerts": False}, by_name=True)
        self.assertEqual((v["email"], self.store.vip(v["id"])["opts"]), ("", {"alerts": False}))
        self.assertNotIn("", people.vip_map(self.store))                    # no address: never matches a blank one
        self.assertEqual(prefs.vip_words(self.store.vip(v["id"])), "no alerts")

    def test_chat_vip_goes_on_setups_list_and_undo_takes_it_off(self):
        row = self.prefs.apply("vip", {"person": "Lena", "through_quiet": False})
        vs = self.store.vips()
        self.assertEqual([(v["name"], v["email"], v["area_id"]) for v in vs], [("Lena", "", "personal")])
        self.assertIs(vs[0]["opts"]["through_quiet"], False)
        self.assertEqual(self.prefs.rules().of("vip"), [])                   # the row is the journal, not a second store
        self.assertEqual(self.prefs.listing()["rules"][0]["kind"], "vip")    # still in Your rules with Undo
        self.prefs.undo(row["id"])
        self.assertEqual(self.store.vips(), [])

    def test_chat_vip_on_a_setup_vip_sets_its_options(self):
        v = self.store.add_vip("Lena Park", "lena@park.example", "personal")
        row = self.prefs.apply("vip", {"person": "Lena", "sources": ["gmail"], "needs_you": False})
        self.assertEqual(len(self.store.vips()), 1)
        self.assertEqual(self.store.vip(v["id"])["opts"]["needs_you"], False)
        self.prefs.undo(row["id"])
        self.assertEqual(self.store.vip(v["id"])["opts"], {})
        self.assertEqual(self.store.vip(v["id"])["email"], "lena@park.example")   # Undo never removes a Setup VIP

    def test_legacy_rules_move_onto_the_list_once(self):
        self.store.add_vip("Dana Ruiz", "dana@ruiz.example", "personal")
        a = self.legacy_rule({"person": "Lena", "alerts": True})
        b = self.legacy_rule({"person": "Dana", "sources": ["slack"], "dm_only": True})
        c = self.legacy_rule({"person": "Sam", "alerts": False, "needs_you": False})
        self.assertEqual(self.prefs.migrate_vips(), 3)
        self.assertEqual(self.prefs.migrate_vips(), 0)                       # once
        by = {v["name"]: v for v in self.store.vips()}
        self.assertEqual(set(by), {"Dana Ruiz", "Lena", "Sam"})              # everyone who was a VIP still is
        self.assertTrue(by["Dana Ruiz"]["opts"]["dm_only"])                  # the rule's options on the Setup VIP
        self.assertEqual(by["Dana Ruiz"]["email"], "dana@ruiz.example")
        self.assertFalse(by["Sam"]["opts"]["alerts"])
        for rid in (a, b, c):
            self.assertTrue(self.prefs.get(rid)["before"]["migrated"])
        self.prefs.undo(c)                                                   # Undo of a moved rule still works
        self.assertNotIn("Sam", {v["name"] for v in self.store.vips()})
        self.prefs.undo(b)
        self.assertEqual(self.store.vips()[0]["opts"], {})

    def test_alerts_quiet_hours_and_needs_you_read_the_list(self):
        self.assertEqual(self.ask_from("2026-11-12T12:00"), [])                      # not a VIP
        v = self.store.add_vip("Lena Park", "lena@park.example", "personal")         # added in Setup
        self.assertEqual(self.ask_from("2026-11-12T12:05"), ["sent"])
        self.assertEqual(self.ask_from("2026-11-12T23:00"), ["sent"])                # through quiet hours (setting)
        row = self.prefs.apply("vip", {"person": "Lena Park", "through_quiet": False})
        self.assertEqual(self.ask_from("2026-11-12T23:05"), ["held"])                # waits: her option on the list
        self.prefs.undo(row["id"])
        self.prefs.apply("vip", {"person": "Lena", "alerts": False})
        self.assertEqual(self.ask_from("2026-11-13T12:00"), [])                      # no alerts for her
        self.assertTrue(self.lv.view(now=self.now[0])["needs_you"][0]["vip"])        # still at the top of Needs you
        self.store.delete_vip(v["id"])                                               # removed in Setup
        self.assertEqual(self.ask_from("2026-11-13T12:10"), [])
        self.assertFalse(any(r["kind"] == "vip" for r in self.prefs.listing()["rules"]))   # gone from Your rules too


if __name__ == "__main__":
    unittest.main()
