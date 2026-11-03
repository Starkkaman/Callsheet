"""Background thread that runs a sync at each enabled call time in the configured time zone
on that call time's days (0.6.2, rules.call_days), plus once at start-up if the last sync is older than 30 min.
0.5.1: after a scheduled brief the notifier sends the phone summary; between call times it runs the urgent light
check when push.check_due says so (every ~10 min, never in quiet hours, on days off or near a call time; it still
uses workdays and skip_checkins_days_off, not the call times' days).
0.6.1: a scheduled sync passes its call time's id (morning, midday, afternoon) so the writer uses that call's
preset; the start-up catch-up sync is a Refresh. The urgent light check never uses the writer.
"""
import datetime as dt
import sys
import threading
import time

import rules

STARTUP_MAX_AGE_S = 30 * 60
TICK_S = 30


class Scheduler:
    def __init__(self, store, syncer, clock=time.time, tick=TICK_S, notifier=None):
        self.store = store
        self.syncer = syncer
        self.notifier = notifier
        self.projects = None  # 0.7.0, set by api.App
        self.clock = clock
        self.tick = tick
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="callsheet-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def startup_due(self):
        if not self.store.has_sources():
            return False
        at = self.store.last_sync().get("at")
        if not at:
            return True
        try:
            age = self.clock() - dt.datetime.fromisoformat(at).timestamp()
        except ValueError:
            return True
        return age > STARTUP_MAX_AGE_S

    def due(self, last_check, now):
        """True when an enabled call time falls in (last_check, now]."""
        return self.due_call(last_check, now) is not None

    def due_call(self, last_check, now):
        """The id of the enabled call time in (last_check, now] (morning, midday or afternoon), else None."""
        settings = self.store.settings()
        tz = rules.tzinfo(settings)
        nxt, call = rules.next_call(settings, dt.datetime.fromtimestamp(last_check, tz))
        return call["id"] if nxt is not None and nxt.timestamp() <= now else None

    def _sync(self, scheduled=False, kind=None):
        if not self.store.has_sources():
            return
        try:
            self.syncer.run_now(kind=kind)
        except Exception as e:  # keep the scheduler alive; the message has no secrets (type only)
            sys.stderr.write(f"Callsheet sync failed: {type(e).__name__}\n")
        if scheduled and self.notifier:
            try:
                self.notifier.after_brief()
            except Exception as e:  # noqa: BLE001 (type only, no secrets)
                sys.stderr.write(f"Callsheet push failed: {type(e).__name__}\n")

    def _urgent(self, now):
        if not self.notifier or not self.store.has_sources():
            return
        try:
            if self.notifier.due(now):
                self.notifier.check()
        except Exception as e:  # noqa: BLE001 (type only, no secrets)
            sys.stderr.write(f"Callsheet urgent check failed: {type(e).__name__}\n")

    def _projects(self, now):
        """0.7.0: the weekly review's phone notification (projects.py; counts only, once a week)."""
        pj = getattr(self, "projects", None)
        if pj is None:
            return
        try:
            pj.tick(now)
        except Exception as e:  # noqa: BLE001 (type only, no secrets)
            sys.stderr.write(f"Callsheet review check failed: {type(e).__name__}\n")

    def _loop(self):
        if self.startup_due():
            self._sync()
        last = self.clock()
        while not self.stop_event.wait(self.tick):
            now = self.clock()
            kind = self.due_call(last, now)
            if kind is not None:
                self._sync(scheduled=True, kind=kind)
            else:
                self._urgent(now)
            self._projects(now)
            last = now
