"""Background thread that runs a sync at each enabled call time in the configured time zone
on that call time's days (0.6.2, rules.call_days), plus once at start-up if the last sync is older than 30 min.
0.5.1: after a scheduled brief the notifier sends the phone summary; between call times it runs the urgent light
check when push.check_due says so (every ~10 min, never in quiet hours, on days off or near a call time; it still
uses workdays and skip_checkins_days_off, not the call times' days).
0.6.1: a scheduled sync passes its call time's id (morning, midday, afternoon) so the writer uses that call's
preset; the start-up catch-up sync is a Refresh. The urgent light check never uses the writer.
0.6.3: the scheduler says where it stands. At start-up and after each scheduled brief it logs the next one
("Callsheet scheduler: next brief morning at 06:30 (Tue, America/Los_Angeles)", plus the quiet hours when urgent
alerts are on), so a quiet night in the log reads as one. The start-up time is taken before the catch-up sync, so a
call time that passes while a slow catch-up runs (Refresh on a slow preset, then the Fast reload up to its timeout)
still runs afterwards. A call time that finds another sync running says so. A failing tick is logged (type only) and
the loop goes on; a watchdog thread starts the loop again if it ever stops and logs a loop that hasn't ticked for
STALL_S (longer than the slowest brief).
"""
import datetime as dt
import sys
import threading
import time

import rules

STARTUP_MAX_AGE_S = 30 * 60
TICK_S = 30
WATCH_S = 60             # 0.6.3: the watchdog's period
STALL_S = 45 * 60        # 0.6.3: no tick for this long is logged (Careful 900 s + Fast 300 s + reload 300 s is 25 min)
FAIL_LOG_S = 10 * 60     # 0.6.3: the same failing tick is logged at most this often


def _stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class Scheduler:
    def __init__(self, store, syncer, clock=time.time, tick=TICK_S, notifier=None, log=None, watch=WATCH_S,
                 stall=STALL_S, monotonic=time.monotonic):
        self.store = store
        self.syncer = syncer
        self.notifier = notifier
        self.projects = None  # 0.7.0, set by api.App
        self.clock = clock
        self.tick = tick
        self.log = log or _stderr
        self.watch, self.stall, self.monotonic = watch, stall, monotonic
        self.stop_event = threading.Event()
        self.thread = None
        self.watchdog = None
        self.ticked = None    # 0.6.3: monotonic time of the loop's start or last tick (the watchdog reads it)
        self.last_at = None   # 0.6.3: clock() of the last tick done, so a restarted loop still runs a call time missed
        self.busy = None      # 0.6.3: "the morning sync" while the loop runs one (for the watchdog's line)
        self.restarts = 0
        self._stalled = False
        self._fail = (None, 0.0)

    def start(self):
        self._start_loop()
        self.watchdog = threading.Thread(target=self._watch, name="callsheet-scheduler-watchdog", daemon=True)
        self.watchdog.start()

    def _start_loop(self, catch_up=True, last=None):
        self.ticked = self.monotonic()
        self.thread = threading.Thread(target=self._run, args=(catch_up, last), name="callsheet-scheduler",
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        for t in (self.thread, self.watchdog):
            if t:
                t.join(timeout=5)

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
        lock = getattr(self.syncer, "lock", None)
        if scheduled and (getattr(self.syncer, "writing", None) is not None or (lock is not None and lock.locked())):
            # 0.6.3: run_now doesn't start a second sync (it returns at once while a writer works, else waits for the
            # running one); say so instead of passing silently
            self.log(f"Callsheet scheduler: the {kind} call time found a sync already running, so it uses that brief")
        self.busy = f"the {kind or 'start-up'} sync"
        try:
            self.syncer.run_now(kind=kind)
        except Exception as e:  # keep the scheduler alive; the message has no secrets (type only)
            self.log(f"Callsheet sync failed: {type(e).__name__}")
        finally:
            self.busy = None
        if scheduled and self.notifier:
            try:
                self.notifier.after_brief()
            except Exception as e:  # noqa: BLE001 (type only, no secrets)
                self.log(f"Callsheet push failed: {type(e).__name__}")

    def _urgent(self, now):
        if not self.notifier or not self.store.has_sources():
            return
        try:
            if self.notifier.due(now):
                self.notifier.check()
        except Exception as e:  # noqa: BLE001 (type only, no secrets)
            self.log(f"Callsheet urgent check failed: {type(e).__name__}")

    def _projects(self, now):
        """0.7.0: the weekly review's phone notification (projects.py; counts only, once a week)."""
        pj = getattr(self, "projects", None)
        if pj is None:
            return
        try:
            pj.tick(now)
        except Exception as e:  # noqa: BLE001 (type only, no secrets)
            sys.stderr.write(f"Callsheet review check failed: {type(e).__name__}\n")

    def heartbeat_line(self, at):
        """0.6.3: when the next brief runs after `at` (a timestamp), in the settings' time zone, with the quiet hours
        when urgent alerts are on."""
        s = self.store.settings()
        tz = rules.tzinfo(s)
        nxt, call = rules.next_call(s, dt.datetime.fromtimestamp(at, tz))
        if nxt is None:
            line = "Callsheet scheduler: no brief scheduled (every call time is off)"
        else:
            line = f"Callsheet scheduler: next brief {call['id']} at {nxt:%H:%M} ({nxt:%a}, {getattr(tz, 'key', tz)})"
        cfg = self.store.push_config() if self.notifier else {}
        if cfg.get("urgent") and cfg.get("targets") and cfg.get("quiet_start") != cfg.get("quiet_end"):
            line += f"; urgent checks paused in quiet hours {cfg['quiet_start']}-{cfg['quiet_end']}"
        return line

    def _heartbeat(self, at):
        try:
            self.log(self.heartbeat_line(at))
        except Exception as e:  # noqa: BLE001 (a log line never stops the scheduler; type only)
            self.log(f"Callsheet scheduler: next brief unknown ({type(e).__name__})")

    def _failed(self, e):
        name, at = self._fail
        now = self.monotonic()
        if name != type(e).__name__ or now - at > FAIL_LOG_S:
            self._fail = (type(e).__name__, now)
            self.log(f"Callsheet scheduler: a tick failed ({type(e).__name__}); trying again in {self.tick} s")

    def _run(self, catch_up=True, last=None):
        try:
            self._loop(catch_up, last)
        except BaseException as e:  # noqa: BLE001 (type only; the watchdog starts the loop again)
            self.log(f"Callsheet scheduler: the loop stopped ({type(e).__name__})")

    def _loop(self, catch_up=True, last=None):
        if last is None:
            last = self.clock()  # 0.6.3: before the catch-up sync, so a call time that passes while it runs still runs
        self.last_at = last
        self._heartbeat(last)
        if catch_up and self.startup_due():
            self._sync()
        while not self.stop_event.wait(self.tick):
            self.ticked = self.monotonic()
            try:
                now = self.clock()
                kind = self.due_call(last, now)
                if kind is not None:
                    self._sync(scheduled=True, kind=kind)
                    self._heartbeat(now)
                else:
                    self._urgent(now)
                self._projects(now)
                last = self.last_at = now
            except Exception as e:  # noqa: BLE001 (a failing tick never stops the scheduler; type only)
                self._failed(e)

    # --- 0.6.3 watchdog -------------------------------------------------------------------------------------------
    def _watch(self):
        while not self.stop_event.wait(self.watch):
            try:
                self.check_loop()
            except Exception as e:  # noqa: BLE001 (type only)
                self.log(f"Callsheet scheduler: watchdog failed ({type(e).__name__})")

    def check_loop(self):
        """Starts the loop again when its thread has stopped (from the last tick it did, without a catch-up sync);
        logs once when it hasn't ticked for self.stall seconds (it may still be in a sync)."""
        t = self.thread
        if self.stop_event.is_set() or t is None:
            return
        if not t.is_alive():
            self.restarts += 1
            self.log("Callsheet scheduler: the loop wasn't running; starting it again")
            self._start_loop(catch_up=False, last=self.last_at)
            return
        idle = self.monotonic() - (self.ticked if self.ticked is not None else self.monotonic())
        if idle <= self.stall:
            self._stalled = False
        elif not self._stalled:
            self._stalled = True
            busy = self.busy
            self.log(f"Callsheet scheduler: no tick for {idle / 60:.0f} min" + (f" (still in {busy})" if busy else ""))
