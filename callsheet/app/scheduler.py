"""Background thread that runs a sync at each enabled call time in the configured time zone
(check-ins skipped on days off when set), plus once at start-up if the last sync is older than 30 min.
"""
import datetime as dt
import sys
import threading
import time

import rules

STARTUP_MAX_AGE_S = 30 * 60
TICK_S = 30


class Scheduler:
    def __init__(self, store, syncer, clock=time.time, tick=TICK_S):
        self.store = store
        self.syncer = syncer
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
        if not self.store.has_accounts():
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
        settings = self.store.settings()
        tz = rules.tzinfo(settings)
        nxt, _ = rules.next_call(settings, dt.datetime.fromtimestamp(last_check, tz))
        return nxt is not None and nxt.timestamp() <= now

    def _sync(self):
        if not self.store.has_accounts():
            return
        try:
            self.syncer.run_now()
        except Exception as e:  # keep the scheduler alive; the message has no secrets (type only)
            sys.stderr.write(f"Callsheet sync failed: {type(e).__name__}\n")

    def _loop(self):
        if self.startup_due():
            self._sync()
        last = self.clock()
        while not self.stop_event.wait(self.tick):
            now = self.clock()
            if self.due(last, now):
                self._sync()
            last = now
