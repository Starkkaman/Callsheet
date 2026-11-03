"""0.6.2 days per call time: each call time (morning brief, midday, afternoon) has its own weekdays. Migration keeps
the scheduler's behaviour before 0.6.2, the scheduler runs a call time only on its days (in the settings' time
zone), at least one day per call time (turning it off is the "enabled" switch), the urgent light check still uses
workdays and skip_checkins_days_off, and the Setup page has day chips under each call time."""
import datetime as dt
import pathlib
import sys
import tempfile
import unittest
import zoneinfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import push  # noqa: E402
import rules  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from store import InputError, Store  # noqa: E402

WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WORK = ["mon", "tue", "wed", "thu", "fri"]
SAT = dt.date(2026, 10, 31)
SUN = dt.date(2026, 11, 1)
MON = dt.date(2026, 11, 2)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name)
        self.addCleanup(self.store.close)

    def days(self):
        return {c["id"]: c["days"] for c in self.store.settings()["call_times"]}

    def old(self, **settings):
        """Settings saved by 0.6.1 (no days on the call times)."""
        s = self.store.settings()
        s.update(settings)
        s["call_times"] = [{k: v for k, v in c.items() if k != "days"} for c in s["call_times"]]
        self.store._set_meta("settings", s)  # noqa: SLF001


class MigrationTest(Base):
    def test_defaults_and_old_settings(self):
        self.assertEqual(self.days(), {"morning": WEEK, "midday": WORK, "afternoon": WORK})
        self.old(skip_checkins_days_off=False)
        self.assertEqual(self.days(), {"morning": WEEK, "midday": WEEK, "afternoon": WEEK})
        self.old(skip_checkins_days_off=True, workdays=["mon", "wed", "sat"])
        self.assertEqual(self.days(), {"morning": WEEK, "midday": ["mon", "wed", "sat"],
                                       "afternoon": ["mon", "wed", "sat"]})

    def test_same_schedule_as_before_for_a_fortnight(self):
        for skip in (True, False):
            for work in (WORK, ["tue", "sun"]):
                self.old(timezone="Europe/Amsterdam", skip_checkins_days_off=skip, workdays=work)
                new = self.store.settings()
                legacy = dict(new, call_times=[{k: v for k, v in c.items() if k != "days"} for c in new["call_times"]])
                tz = zoneinfo.ZoneInfo("Europe/Amsterdam")
                t = dt.datetime(2026, 10, 20, 0, 0, tzinfo=tz)
                for _ in range(3 * 14):
                    a, b = rules.next_call(new, t), rules.next_call(legacy, t)
                    self.assertEqual((a[0], a[1]["id"]), (b[0], b[1]["id"]))
                    t = a[0]

    def test_a_save_stores_the_days_so_later_workday_changes_dont_move_them(self):
        self.old(skip_checkins_days_off=True, workdays=WORK)
        self.store.update_settings({"workdays": ["mon", "tue"]})
        self.assertEqual(self.days()["midday"], WORK)
        saved = self.store._get_meta("settings", {})  # noqa: SLF001
        self.assertTrue(all("days" in c for c in saved["call_times"]))


class ValidationTest(Base):
    def test_days_are_saved_in_week_order(self):
        self.store.update_settings({"call_times": [{"id": "afternoon", "days": ["sun", "mon", "mon"]}]})
        self.assertEqual(self.days()["afternoon"], ["mon", "sun"])

    def test_at_least_one_day_and_only_weekdays(self):
        for bad in ([], ["monday"], "mon", None, [1]):
            with self.assertRaises(InputError):
                self.store.update_settings({"call_times": [{"id": "morning", "days": bad}]})
        try:
            self.store.update_settings({"call_times": [{"id": "morning", "days": []}]})
        except InputError as e:
            self.assertEqual(str(e), "Pick at least one day for the Morning brief. To stop it, turn it off.")
        self.assertEqual(self.days()["morning"], WEEK)  # nothing saved on an error


class SchedulingTest(Base):
    def sch(self, tz):
        self.store.update_settings({"timezone": tz})
        return Scheduler(self.store, None, clock=lambda: 0)

    def test_runs_only_on_its_days(self):
        self.store.update_settings({"call_times": [{"id": "morning", "days": ["mon", "tue", "wed", "thu", "fri"]},
                                                   {"id": "midday", "days": ["sat"]}]})
        sch = self.sch("UTC")

        def ts(day, h, m=0):
            return dt.datetime.combine(day, dt.time(h, m), tzinfo=dt.timezone.utc).timestamp()
        self.assertIsNone(sch.due_call(ts(SAT, 6, 29), ts(SAT, 6, 31)))      # no morning brief on Saturday
        self.assertEqual(sch.due_call(ts(SAT, 12, 29), ts(SAT, 12, 31)), "midday")
        self.assertIsNone(sch.due_call(ts(SAT, 16, 59), ts(SAT, 17, 1)))     # afternoon: workdays (migrated)
        self.assertIsNone(sch.due_call(ts(SUN, 6, 0), ts(SUN, 23, 0)))       # nothing at all on Sunday
        self.assertEqual(sch.due_call(ts(MON, 6, 29), ts(MON, 6, 31)), "morning")
        self.assertIsNone(sch.due_call(ts(MON, 12, 29), ts(MON, 12, 31)))    # midday only on Saturday
        nxt, call = rules.next_call(self.store.settings(), dt.datetime.combine(SAT, dt.time(13), tzinfo=dt.timezone.utc))
        self.assertEqual((nxt.date(), nxt.hour, call["id"]), (MON, 6, "morning"))

    def test_the_day_is_the_settings_time_zone_day(self):
        # Morning brief on Mondays only, at 06:30 in Auckland = Sunday 17:30 UTC
        self.store.update_settings({"call_times": [{"id": "morning", "days": ["mon"]},
                                                   {"id": "midday", "enabled": False},
                                                   {"id": "afternoon", "enabled": False}]})
        sch = self.sch("Pacific/Auckland")
        akl = zoneinfo.ZoneInfo("Pacific/Auckland")
        run = dt.datetime.combine(MON, dt.time(6, 30), tzinfo=akl)
        self.assertEqual(run.astimezone(dt.timezone.utc).date(), SUN)
        self.assertEqual(sch.due_call(run.timestamp() - 60, run.timestamp()), "morning")
        tue = run + dt.timedelta(days=1)
        self.assertIsNone(sch.due_call(tue.timestamp() - 60, tue.timestamp()))
        nxt, _ = rules.next_call(self.store.settings(), tue)
        self.assertEqual(nxt, run + dt.timedelta(days=7))

    def test_urgent_check_keeps_workdays(self):
        # call days don't change the urgent light check: still off on days off with skip_checkins_days_off
        self.store.update_settings({"timezone": "UTC", "call_times": [{"id": "midday", "days": WEEK}]})
        s = self.store.settings()
        cfg = {"urgent": True, "targets": ["mobile_app_x"], "quiet_start": "22:00", "quiet_end": "07:00"}
        sat = dt.datetime.combine(SAT, dt.time(10), tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(push.check_due(s, cfg, {}, sat), (False, "day off"))
        self.store.update_settings({"skip_checkins_days_off": False})
        self.assertNotEqual(push.check_due(self.store.settings(), cfg, {}, sat)[1], "day off")


class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        static = ROOT / "callsheet" / "app" / "static"
        js = (static / "app.js").read_text()
        cls.times = js[js.index("function stepTimes()"):js.index("function stepDelivery()")]
        cls.review = js[js.index("function stepReview()"):]
        cls.css = (static / "app.css").read_text()

    def test_day_chips_under_each_call_time(self):
        self.assertIn("var days = h('div', { class: 'chips ctdays', role: 'group', 'aria-label': label + ' days' }, "
                      "DAYS.map(", self.times)
        self.assertIn("'aria-pressed': String((ct.days || DAYS).indexOf(d) >= 0)", self.times)
        self.assertIn("if (!want.length) { err.textContent = 'Pick at least one day. To stop the ' + label + ', turn it "
                      "off.'; return; }", self.times)
        self.assertIn("save(ct, { days: DAYS.filter(", self.times)
        self.assertIn("inp, t]), days, err]);", self.times)
        self.assertIn("'No urgent checks on days off'", self.times)
        self.assertNotIn("innerHTML", self.times)
        self.assertIn("return c.time + onDays(c);", self.review)
        self.assertIn(".tb .ctdays{display:flex;flex-wrap:wrap;gap:4px}", self.css)


if __name__ == "__main__":
    unittest.main()
