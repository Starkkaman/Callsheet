"""0.6.3 scheduler against the fakes (fake Google, a fake llama.cpp router, the fake Home Assistant API): the real
Scheduler, Syncer, Notifier and Store through a night like 2026-11-03's (a start-up Refresh mapped to Careful, then
the Fast reload against a server that doesn't answer or doesn't know the model), a call time during the catch-up, a
page Refresh's background writer, chat waiting for LOCAL_LOCK, the heartbeat line, call times at their time on their
days across the DST change, the tick guard and the watchdog. Never contacts real services."""
import datetime as dt
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import zoneinfo
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import fake_ha  # noqa: E402
import presets  # noqa: E402
import writer  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from store import Store  # noqa: E402
from test_presets_061 import FAST_MODEL, SWIFT, FakeRouter, PresetBase  # noqa: E402
from test_writer import LOCAL_KEY  # noqa: E402

LA = zoneinfo.ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
TZ_NOTE = "(Tue, America/Los_Angeles)"


def la(month, day, h, m=0, s=0):
    return dt.datetime(2026, month, day, h, m, s, tzinfo=LA).timestamp()


class Clock:
    """A wall clock the test moves (or the monotonic clock of a watchdog test)."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = t


def wait_for(cond, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Router(FakeRouter):
    """The overnight test router: models in .unknown get 404 "model not found"."""

    def __init__(self):
        super().__init__()
        self.unknown = set()

    def answer(self, req):
        if req["method"] == "POST" and (req["body"] or {}).get("model") in self.unknown:
            return 404, {"error": {"code": 404, "message": "model not found", "type": "not_found_error"}}, None
        return super().answer(req)


class NightBase(PresetBase):
    def setUp(self):
        super().setUp()
        self.loc.close()
        self.loc = Router()
        self.ha = fake_ha.FakeHA()
        env = mock.patch.dict(os.environ, dict(self.ha.env(), HOSTNAME="a1b2c3d4-callsheet"))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.ha.close)
        self.ok("PATCH", "/api/settings", {"timezone": "America/Los_Angeles"})
        self.seed()
        self.local_on()
        self.pid = self.careful(refresh=1)  # the owner's mapping: Refresh (and so the start-up catch-up) on Careful
        self.ok("PATCH", "/api/push", {"targets": ["mobile_app_iphone"], "urgent": True, "at_call_times": True})
        real = presets.options  # Fast's 300 s timeout becomes 1 s
        p = mock.patch.object(presets, "options", lambda loc, pr: dict(
            real(loc, pr), timeout=1 if pr["id"] == presets.FAST else 30))
        p.start()
        self.addCleanup(p.stop)
        self.loc.hang_s = 3.0
        self.clock = Clock(la(11, 3, 0, 2, 40))  # Tuesday 00:02:40 PST: the restart into 0.6.2
        self.app.syncer.clock = self.app.notifier.clock = self.clock
        self.app.notifier.log = self.logs.append
        self.sch = Scheduler(self.app.store, self.app.syncer, clock=self.clock, tick=0.02, notifier=self.app.notifier,
                             log=self.logs.append, watch=0.05)

    def tearDown(self):
        self.sch.stop()
        wait_for(lambda: not self.app.syncer.lock.locked(), 10)
        super().tearDown()

    def text(self):
        return "\n".join(self.logs)

    def logged(self, part, timeout=20):
        return wait_for(lambda: part in self.text(), timeout)

    def assert_free(self):
        self.assertTrue(wait_for(lambda: not self.app.syncer.lock.locked()))
        self.assertIsNone(self.app.syncer.writing)
        self.assertFalse(writer.LOCAL_LOCK.locked())

    def tick_at(self, t):
        """Moves the clock and waits until the scheduler has ticked at it."""
        self.clock.set(t)
        self.assertTrue(wait_for(lambda: self.sch.last_at == t), f"no tick at {t}")


class NightTest(NightBase):
    def test_slow_refresh_then_reload_timeout_and_the_scheduler_goes_on(self):
        self.loc.hang.add(FAST_MODEL)  # the router never answers for the Fast model (until its timeout)
        self.sch.start()
        self.assertTrue(self.logged("loading the fast model again failed"))
        self.assert_free()
        self.assertTrue(self.sch.thread.is_alive())
        self.assertIn(f"Callsheet scheduler: next brief morning at 06:30 {TZ_NOTE}; urgent checks paused in quiet "
                      "hours 22:00-07:00", self.logs[0])  # at start-up, before the catch-up sync
        self.assertIn(f"Callsheet writer: preset {self.pid} for refresh", self.text())
        # the night: quiet hours, nothing due, but the loop ticks
        self.tick_at(la(11, 3, 3, 0))
        self.tick_at(la(11, 3, 6, 29, 50))
        self.assertNotIn("urgent check:", self.text())
        self.assertEqual(self.text().count("Callsheet sync:"), 1)
        # 05:51 the live server is back; 06:30 the morning brief runs on Fast, sends the summary, says what's next
        self.loc.hang.discard(FAST_MODEL)
        self.tick_at(la(11, 3, 6, 30, 5))
        self.assertTrue(self.logged(f"next brief midday at 12:30 {TZ_NOTE}"))
        self.assertEqual(self.text().count("Callsheet sync:"), 2)
        self.assertIn("Callsheet writer: preset fast for morning", self.text())
        self.assertIn("Callsheet push: brief, 1 of 1 phones", self.text())
        self.assert_free()
        # after quiet hours the urgent check runs again
        self.tick_at(la(11, 3, 7, 20))
        self.assertTrue(self.logged("Callsheet urgent check:"))
        self.assertTrue(self.sch.thread.is_alive())
        self.assertEqual(self.sch.restarts, 0)

    def test_reload_failures_release_every_lock_and_are_logged(self):
        for fail in ("unknown", "broken", "hang"):
            getattr(self.loc, fail).add(FAST_MODEL)
            self.app.syncer.run_now(kind="refresh")
            self.assertIn("Callsheet writer: loading the fast model again failed", self.logs[-1], fail)
            self.assert_free()
            getattr(self.loc, fail).discard(FAST_MODEL)
        with mock.patch.object(presets, "reload_body", side_effect=ValueError("bug")):  # not a WriterError
            self.app.syncer.run_now(kind="refresh")
        self.assertIn("Callsheet writer: failed unexpectedly (ValueError)", self.logs[-1])
        self.assert_free()
        self.app.syncer.run_now(kind="refresh")  # and the next sync runs as usual
        self.assertIn("Callsheet writer: fast model loaded again", self.logs[-1])

    def test_page_refresh_in_the_background_doesnt_stop_the_ticks(self):
        self.app.store.set_last_sync({"at": dt.datetime.fromtimestamp(self.clock(), LA).isoformat(), "ok": True,
                                      "errors": []})  # no catch-up sync
        self.loc.swap_s = 0.5
        self.loc.hang.add(FAST_MODEL)
        self.sch.start()
        self.assertTrue(self.logged("next brief morning"))
        t0 = time.monotonic()
        self.ok("POST", "/api/sync")  # Refresh on Careful: answers with the rules brief, the writer goes on
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(self.app.syncer.lock.locked())
        seen = {self.sch.ticked}
        self.assertTrue(wait_for(lambda: seen.add(self.sch.ticked) or len(seen) > 3 or not self.app.syncer.lock.locked()))
        self.assertGreater(len(seen), 3)  # the loop ticked while the writer ran
        self.tick_at(la(11, 3, 6, 30, 5))  # a call time while the Refresh's writer or reload still runs
        self.assertIn("the morning call time found a sync already running, so it uses that brief", self.text())
        self.assertTrue(self.logged(f"next brief midday at 12:30 {TZ_NOTE}"))
        self.assertTrue(self.logged("loading the fast model again failed"))
        self.assert_free()
        self.loc.hang.discard(FAST_MODEL)
        self.tick_at(la(11, 3, 7, 20))
        self.assertTrue(self.logged("Callsheet urgent check:"))

    def test_chat_waits_for_the_reload_then_runs(self):
        self.loc.hang.add(FAST_MODEL)
        t = threading.Thread(target=self.app.syncer.run_now, kwargs={"kind": "refresh"})
        t.start()
        self.assertTrue(wait_for(lambda: "Callsheet writer: local, ok" in self.text() and writer.LOCAL_LOCK.locked()))
        events, timing = [], {}
        body = {"model": SWIFT, "stream": False, "max_tokens": 16, "messages": [{"role": "user", "content": "Hi"}]}
        res = writer._post(self.loc.base, LOCAL_KEY, body, {"timeout": 30}, None, time.monotonic,  # noqa: SLF001
                           notify=events.append, timing=timing)
        self.assertEqual((events[0], events[-1]), ("lock", "sent"))  # waited for the reload, then went out
        self.assertGreater(timing["lock_s"], 0)
        self.assertTrue(res["choices"])
        t.join(10)
        self.assertIn("loading the fast model again failed", self.text())
        self.assert_free()


class CatchUpTest(unittest.TestCase):
    def test_a_call_time_during_the_catch_up_sync_still_runs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        self.addCleanup(store.close)
        store.update_settings({"timezone": "America/Los_Angeles"})
        clock, kinds, logs = Clock(la(11, 3, 6, 25)), [], []

        class S:
            def run_now(self, kind=None):
                kinds.append(kind)
                if kind is None:  # Refresh on Careful, then the Fast reload until its timeout: 8 minutes
                    clock.set(clock.t + 8 * 60)

        sch = Scheduler(store, S(), clock=clock, tick=0, log=logs.append)
        sch.startup_due = lambda: True
        ticks = iter([False, True])
        sch.stop_event.wait = lambda t: next(ticks)
        with mock.patch.object(store, "has_sources", return_value=True):
            sch._loop()
        self.assertEqual(kinds, [None, "morning"])  # 0.6.2: [None], the 06:30 brief was skipped
        self.assertEqual(logs, [f"Callsheet scheduler: next brief morning at 06:30 {TZ_NOTE}",
                                f"Callsheet scheduler: next brief midday at 12:30 {TZ_NOTE}"])


class StoreBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name)
        self.addCleanup(self.store.close)
        self.store.update_settings({"timezone": "America/Los_Angeles"})
        self.logs = []


class HeartbeatTest(StoreBase):
    def test_lines(self):
        sch = Scheduler(self.store, None, log=self.logs.append)
        line = sch.heartbeat_line
        self.assertEqual(line(la(11, 2, 23, 0)), f"Callsheet scheduler: next brief morning at 06:30 {TZ_NOTE}")
        self.assertEqual(line(la(11, 3, 6, 30, 5)), f"Callsheet scheduler: next brief midday at 12:30 {TZ_NOTE}")
        # Saturday after the morning brief: the check-ins keep to the working days, so Sunday's morning brief
        self.assertEqual(line(la(10, 31, 6, 31)),
                         "Callsheet scheduler: next brief morning at 06:30 (Sun, America/Los_Angeles)")
        sch.notifier = object()
        self.store.update_push({"targets": ["mobile_app_iphone"], "urgent": True})
        self.assertTrue(line(la(11, 2, 23, 0)).endswith("; urgent checks paused in quiet hours 22:00-07:00"))
        self.store.update_push({"quiet_start": "00:00", "quiet_end": "00:00"})
        self.assertNotIn("quiet", line(la(11, 2, 23, 0)))
        self.store.update_settings({"call_times": [{"id": k, "enabled": False} for k in ("morning", "midday",
                                                                                        "afternoon")]})
        self.assertEqual(line(la(11, 2, 23, 0)), "Callsheet scheduler: no brief scheduled (every call time is off)")
        with mock.patch.object(self.store, "settings", side_effect=RuntimeError("x")):
            sch._heartbeat(0)
        self.assertEqual(self.logs, ["Callsheet scheduler: next brief unknown (RuntimeError)"])

    def test_call_times_run_at_their_time_on_their_days_across_the_dst_change(self):
        s = self.store.settings()  # saved by 0.6.1: no days (morning every day, check-ins on working days)
        s["call_times"] = [{k: v for k, v in c.items() if k != "days"} for c in s["call_times"]]
        self.store._set_meta("settings", s)  # noqa: SLF001
        times = iter(range(int(la(10, 31, 22, 0)), int(la(11, 3, 7, 0)) + 1, 60))  # Sat 22:00 PDT to Tue 07:00 PST
        now, runs = {}, []

        def clock():
            now["t"] = next(times)
            return now["t"]

        class S:
            def run_now(self, kind=None):
                runs.append((kind, now["t"]))

        sch = Scheduler(self.store, S(), clock=clock, tick=0, log=self.logs.append)
        sch.startup_due = lambda: False
        n = (int(la(11, 3, 7, 0)) - int(la(10, 31, 22, 0))) // 60
        ticks = iter([False] * n + [True])
        sch.stop_event.wait = lambda t: next(ticks)
        with mock.patch.object(self.store, "has_sources", return_value=True):
            sch._loop()
        self.assertEqual(runs, [("morning", la(11, 1, 6, 30)), ("morning", la(11, 2, 6, 30)),
                                ("midday", la(11, 2, 12, 30)), ("afternoon", la(11, 2, 17, 0)),
                                ("morning", la(11, 3, 6, 30))])
        self.assertEqual(dt.datetime.fromtimestamp(runs[-1][1], UTC).strftime("%H:%M"), "14:30")  # 06:30 PST
        self.assertEqual(self.logs[-1], f"Callsheet scheduler: next brief midday at 12:30 {TZ_NOTE}")
        self.assertEqual(len(self.logs), 6)  # start-up and after each brief


class WatchdogTest(StoreBase):
    def test_a_failing_tick_is_logged_once_and_the_loop_goes_on(self):
        runs = []

        class S:
            def run_now(self, kind=None):
                runs.append(kind)

        times = iter([la(11, 3, 6, 0)] * 5)
        sch = Scheduler(self.store, S(), clock=lambda: next(times), tick=0, log=self.logs.append)
        sch.startup_due = lambda: False
        sch.due_call = mock.Mock(side_effect=[RuntimeError("boom secret"), RuntimeError("again"), "morning", None])
        ticks = iter([False] * 4 + [True])
        sch.stop_event.wait = lambda t: next(ticks)
        with mock.patch.object(self.store, "has_sources", return_value=True):
            sch._loop()
        self.assertEqual(runs, ["morning"])
        self.assertEqual(self.logs.count("Callsheet scheduler: a tick failed (RuntimeError); trying again in 0 s"), 1)
        self.assertNotIn("secret", "\n".join(self.logs))

    def test_the_watchdog_starts_a_stopped_loop_again(self):
        class Boom(BaseException):
            pass

        calls = []

        def due(last, now):
            calls.append(now)
            if len(calls) == 1:
                raise Boom()
            return None

        sch = Scheduler(self.store, None, tick=0.01, log=self.logs.append, watch=0.02)
        sch.startup_due = lambda: False
        sch.due_call = due
        sch.start()
        self.addCleanup(sch.stop)
        self.assertTrue(wait_for(lambda: len(calls) > 3))
        self.assertIn("Callsheet scheduler: the loop stopped (Boom)", self.logs)
        self.assertIn("Callsheet scheduler: the loop wasn't running; starting it again", self.logs)
        self.assertEqual(sch.restarts, 1)
        self.assertTrue(sch.thread.is_alive())
        sch.stop()
        self.assertFalse(sch.thread.is_alive() or sch.watchdog.is_alive())

    def test_a_loop_that_stopped_ticking_is_logged_once(self):
        mono = Clock(1000.0)
        sch = Scheduler(self.store, None, log=self.logs.append, stall=60, monotonic=mono)
        sch.thread = mock.Mock(is_alive=lambda: True)
        sch.ticked, sch.busy = 1000.0, "the morning sync"
        sch.check_loop()
        mono.set(1061)
        sch.check_loop()
        sch.check_loop()
        self.assertEqual(self.logs, ["Callsheet scheduler: no tick for 1 min (still in the morning sync)"])
        sch.ticked, sch.busy = 1061, None
        sch.check_loop()
        mono.set(1200)
        sch.check_loop()
        self.assertEqual(self.logs[-1], "Callsheet scheduler: no tick for 2 min")
        self.assertEqual(sch.restarts, 0)


if __name__ == "__main__":
    unittest.main()
