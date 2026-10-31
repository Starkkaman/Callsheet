"""0.6.0 "Know me": the background scan that fills the local index, and its settings.

One low-priority thread reads one bounded step at a time from each source in turn (a queue per source, round
robin), stores the step's items and resume point together (index.commit_step), and paces itself. It never runs
within 10 minutes of a call time or while a sync is running, so briefs are never slowed; Google's and Slack's
"slow down" answers make it wait (with growing pauses) and try the same step again. The owner can pause, resume
and stop (stop keeps what was read). A scan that was running when the app stopped resumes after a restart.

After a finished scan, a light refresh every REFRESH_EVERY_S reads what is new since the last read (with an overlap),
through the same steps, so the index keeps up cheaply.

Estimates before a scan come from cheap counts (Gmail's profile and resultSizeEstimate, Calendar and Drive id lists,
the vault's file list) and give plain-word warnings: "About 48,000 emails. May take about 6 hours and use about
900 MB on this box."
"""
import datetime as dt
import os
import secrets
import shutil
import sys
import threading
import time

import rules
import sources as srcs
from store import Conflict, InputError

WINDOWS = (  # key, label, days (None = the whole history)
    ("30d", "30 days", 30),
    ("90d", "90 days", 90),
    ("1y", "1 year", 365),
    ("5y", "5 years", 5 * 365 + 1),
    ("all", "Entire history", None),
)
SOURCE_KEYS = ("gmail", "calendar", "slack", "drive", "obsidian", "signal")
SECONDS_PER = {"gmail": 0.45, "calendar": 0.02, "slack": 0.03, "drive": 1.0, "obsidian": 0.005, "signal": 0.001}
BYTES_PER = {"gmail": 19_000, "calendar": 3_000, "slack": 1_500, "drive": 20_000, "obsidian": 12_000, "signal": 800}
CALL_PAUSE_S = 10 * 60        # no scanning this close to a call time (before or after)
REFRESH_EVERY_S = 30 * 60
REFRESH_OVERLAP_S = 2 * 86400
BACKOFF_MAX_S = 3600
TICK_S = 1.0
IDLE_S = 30.0
ESTIMATE_TTL_S = 15 * 60
UNIT_WORDS = {"gmail": "emails", "calendar": "events", "slack": "messages", "drive": "files", "obsidian": "notes", "signal": "messages"}


def window(key):
    for k, label, days in WINDOWS:
        if k == key:
            return k, label, days
    raise InputError("Pick how far back to read: 30 days, 90 days, 1 year, 5 years or the entire history.")


def default_settings():
    return {"vault": "", "sources": {k: True for k in SOURCE_KEYS}}


# --- plain words --------------------------------------------------------------------------------------------------
def about_count(n):
    n = int(n or 0)
    if n < 1000:
        return f"{n:,}"
    step = 100 if n < 10_000 else 1000
    return f"{int(round(n / step) * step):,}"


def about_time(seconds):
    s = max(0, float(seconds or 0))
    if s < 90:
        return "a minute or two"
    if s < 3600:
        m = int(round(s / 60))
        if m > 10:
            m = int(round(m / 5) * 5)
        return f"about {m} minutes"
    if s < 2 * 86400:
        hrs = int(round(s / 3600))
        return "about an hour" if hrs <= 1 else f"about {hrs} hours"
    return f"about {int(round(s / 86400))} days"


def about_size(b):
    mb = max(0, float(b or 0)) / 1e6
    if mb < 1:
        return "under 1 MB"
    if mb < 100:
        return f"about {int(round(mb))} MB"
    if mb < 1000:
        return f"about {int(round(mb / 100) * 100)} MB"
    return f"about {mb / 1000:.1f} GB".replace(".0 GB", " GB")


def _list_words(parts):
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def estimate_words(per_source, free_bytes):
    """per_source: {source: (count or None, plus)} -> (seconds, bytes, summary sentence, [warnings])."""
    seconds = sum(SECONDS_PER.get(s, 0.1) * (c or 0) for s, (c, _) in per_source.items())
    size = sum(BYTES_PER.get(s, 5000) * (c or 0) for s, (c, _) in per_source.items())
    parts = []
    for s in SOURCE_KEYS:
        if s in per_source and per_source[s][0]:
            c, plus = per_source[s]
            unit = UNIT_WORDS[s][:-1] if c == 1 and not plus else UNIT_WORDS[s]
            parts.append(f"{about_count(c)}{'+' if plus else ''} {unit}")
    summary = ("About " + _list_words(parts) + ". " if parts else "") + \
        f"May take {about_time(seconds)} and use {about_size(size)} on this box."
    warnings = []
    if seconds >= 3600:
        warnings.append(("This can take hours." if seconds >= 2 * 3600 else "This can take an hour or more.") + " Briefs still run on time: the scan pauses around call times. "
                        "You can pause, stop and resume.")
    if "slack" in per_source and per_source["slack"][0] is None:
        warnings.append("Slack doesn't say how many messages there are, so Slack isn't in this estimate.")
    if free_bytes is not None and size > 0.8 * free_bytes:
        warnings.append(f"This box has only {about_size(free_bytes).replace('about ', '')} free; the index may need "
                        f"{about_size(size)}.")
    return seconds, size, summary, warnings


class Scanner:
    def __init__(self, store, index, syncer=None, clock=time.time, sleep=time.sleep, sources=None, tick=TICK_S,
                 log=None):
        self.store, self.index, self.syncer = store, index, syncer
        self.clock, self.sleep, self.tick = clock, sleep, tick
        self.sources = {s.key: s for s in (sources or srcs.all_sources())}
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.wipe_token = secrets.token_urlsafe(9)
        self._estimates = {}
        self._session = {}   # target key -> (start time, done at start) for rates in this run
        self._stop_step = False

    # --- settings ---------------------------------------------------------------------------------------------
    def settings(self):
        s = default_settings()
        saved = self.index.meta("settings") or {}
        s["vault"] = saved.get("vault") or ""
        s["sources"].update({k: bool(v) for k, v in (saved.get("sources") or {}).items() if k in s["sources"]})
        return s

    def update_settings(self, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the change as a JSON object.")
        s = self.settings()
        removed = []
        for key, val in patch.items():
            if key == "vault":
                if val in (None, ""):
                    new = ""
                else:
                    srcs.check_vault(val)
                    new = val.strip()
                if new != s["vault"]:
                    removed.append(("obsidian", "vault"))
                s["vault"] = new
            elif key == "sources":
                if not isinstance(val, dict) or any(k not in SOURCE_KEYS or not isinstance(v, bool)
                                                    for k, v in val.items()):
                    raise InputError("sources takes true/false for " + ", ".join(SOURCE_KEYS) + ".")
                for k, v in val.items():
                    if s["sources"][k] and not v:
                        removed.append((k, None))
                s["sources"].update(val)
            else:
                raise InputError(f"Know me settings can't change {str(key)[:40]}.")
        with self.lock:
            self.index.set_meta("settings", s)
            for source, account in removed:   # turning a source off (or changing the vault) removes what it read
                self.index.delete(source, account)
        return s

    def _ctx(self):
        return srcs.Ctx(self.store, self.index, self.clock, self.sleep, self.settings(), stop=self._should_stop)

    def _should_stop(self):
        return self._stop_step or self.stop_event.is_set()

    # --- targets and estimates -------------------------------------------------------------------------------
    def _wanted(self, sources):
        on = self.settings()["sources"]
        if sources is None:
            sources = [k for k in SOURCE_KEYS if on.get(k)]
        if not isinstance(sources, list) or any(s not in self.sources for s in sources):
            raise InputError("Pick which sources to read: " + ", ".join(SOURCE_KEYS) + ".")
        return [s for s in sources if on.get(s, True)]

    def targets(self, sources=None):
        ctx = self._ctx()
        out = []
        for key in self._wanted(sources):
            for t in self.sources[key].targets(ctx):
                t["key"] = f"{t['source']}:{t['account']}"
                out.append(t)
        return out

    def _since(self, days):
        return None if days is None else self.clock() - days * 86400

    def free_bytes(self):
        try:
            return shutil.disk_usage(str(self.index.dir)).free
        except OSError:
            return None

    def estimate(self, win, sources=None):
        """Counts per target (cheap requests; cached 15 minutes) and the plain-word summary and warnings."""
        key, label, days = window(win)
        wanted = self._wanted(sources)
        cache_key = (key, tuple(wanted))
        hit = self._estimates.get(cache_key)
        if hit and self.clock() - hit["at"] < ESTIMATE_TTL_S:
            return hit["out"]
        ctx, since = self._ctx(), self._since(days)
        rows, per = [], {}
        for t in self.targets(wanted):
            count, note = None, t["blocked"]
            if not note:
                try:
                    count = self.sources[t["source"]].estimate(ctx, t, since)
                except (srcs.SourceError, srcs.Retry) as e:
                    note = str(e)
                if count is None and not note and t["source"] == "slack":
                    note = "Counted while reading"
            plus = t["source"] in ("calendar", "drive") and count is not None and count >= 2500
            rows.append({"key": t["key"], "source": t["source"], "account": t["account"], "label": t["label"],
                         "unit": t["unit"], "count": count, "plus": plus, "note": note})
            c, p = per.get(t["source"], (0, False))
            if count is None and not t["blocked"] and t["source"] == "slack":
                per[t["source"]] = (None, False) if c in (0, None) else (c, p)
            elif count is not None:
                per[t["source"]] = ((c or 0) + count, p or plus)
        free = self.free_bytes()
        seconds, size, summary, warnings = estimate_words(per, free)
        out = {"window": key, "label": label, "targets": rows, "seconds": round(seconds), "bytes": int(size),
               "free_bytes": free, "summary": summary, "warnings": warnings, "time": about_time(seconds),
               "size": about_size(size)}
        self._estimates[cache_key] = {"at": self.clock(), "out": out}
        return out

    # --- jobs -------------------------------------------------------------------------------------------------
    def job(self):
        return self.index.meta("job")

    def _save(self, job, name="job"):
        self.index.set_meta(name, job)

    def start(self, win, sources=None):
        key, label, days = window(win)
        with self.lock:
            cur = self.job()
            if cur and cur["state"] in ("running", "paused"):
                raise Conflict("A scan is already going. Pause or stop it first.")
            now = self.clock()
            wanted = self._wanted(sources)
            if not wanted:
                raise InputError("Turn on at least one source.")
            est = self._estimates.get((key, tuple(wanted)))
            counts = {r["key"]: r["count"] for r in est["out"]["targets"]} if est else {}
            job = {"id": secrets.token_hex(4), "kind": "scan", "window": key, "label": label,
                   "since": self._since(days), "until": None, "sources": wanted, "state": "running",
                   "started_at": now, "finished_at": None, "active_s": 0.0, "items": 0, "targets": {}, "order": [],
                   "next": 0}
            for t in self.targets(wanted):
                job["targets"][t["key"]] = {
                    "source": t["source"], "account": t["account"], "label": t["label"], "unit": t["unit"],
                    "area": t["area"], "done": 0, "total": counts.get(t["key"]), "status": "skipped" if t["blocked"] else "queued",
                    "error": t["blocked"], "retry_at": 0, "fails": 0, "added": 0}
                job["order"].append(t["key"])
            self.index.clear_cursors("scan")
            self._session = {}
            self._save(job)
            self.index.set_meta("refresh", None)
        return self.status()

    def pause(self):
        with self.lock:
            job = self.job()
            if not job or job["state"] != "running":
                raise Conflict("There's no scan going to pause.")
            job["state"] = "paused"
            self._stop_step = True
            self._save(job)
        return self.status()

    def resume(self):
        with self.lock:
            job = self.job()
            if not job or job["state"] not in ("paused", "cancelled"):
                raise Conflict("There's no paused or stopped scan to resume.")
            if job["state"] == "cancelled":
                raise Conflict("That scan was stopped. Start a new scan; what was read is kept.")
            job["state"] = "running"
            self._stop_step = False
            self._session = {}
            self._save(job)
        return self.status()

    def cancel(self):
        """Stop: keeps everything read so far; the resume points are dropped."""
        with self.lock:
            job = self.job()
            if not job or job["state"] not in ("running", "paused"):
                raise Conflict("There's no scan going to stop.")
            job["state"] = "cancelled"
            job["finished_at"] = self.clock()
            self._stop_step = True
            self._save(job)
            self.index.clear_cursors("scan")
        return self.status()

    def wipe(self, token):
        if not isinstance(token, str) or not secrets.compare_digest(token, self.wipe_token):
            raise InputError("Confirm deleting the index first.")
        with self.lock:
            self._stop_step = True
            self.index.wipe()
            self.wipe_token = secrets.token_urlsafe(9)
            self._estimates = {}
        return self.status()

    # --- when it may run --------------------------------------------------------------------------------------
    def blocked(self, now=None):
        """(reason, sentence) when the scan must wait now, else None: near a call time, or a sync is running."""
        now = self.clock() if now is None else now
        s = self.store.settings()
        tz = rules.tzinfo(s)
        local = dt.datetime.fromtimestamp(now, tz)
        nxt, call = rules.next_call(s, local - dt.timedelta(seconds=CALL_PAUSE_S + 1))
        if nxt is not None and nxt.timestamp() <= now + CALL_PAUSE_S:
            at = rules.clock(rules.hhmm(nxt), rules.time_format(s))
            name = (call or {}).get("label") or "brief"
            return "call", f"Waiting for the {at} {name}: the scan pauses 10 minutes before and after each call time."
        if self.syncer is not None and getattr(self.syncer, "lock", None) is not None and self.syncer.lock.locked():
            return "sync", "Waiting while Callsheet refreshes the brief."
        return None

    # --- the work ---------------------------------------------------------------------------------------------
    def _active(self):
        """(job, meta name, cursor key) of the work to do now, or (None, None, None)."""
        job = self.job()
        if job and job["state"] == "running":
            return job, "job", "scan"
        if job and job["state"] in ("paused",):
            return None, None, None
        ref = self.index.meta("refresh")
        if ref and ref["state"] == "running":
            return ref, "refresh", "refresh"
        synced = self.index.meta("synced") or {}
        last = self.index.meta("refresh_at") or 0
        if synced and self.clock() - last >= REFRESH_EVERY_S:
            return self._new_refresh(synced), "refresh", "refresh"
        return None, None, None

    def _new_refresh(self, synced):
        now = self.clock()
        ref = {"id": secrets.token_hex(4), "kind": "refresh", "state": "running", "started_at": now,
               "finished_at": None, "active_s": 0.0, "items": 0, "targets": {}, "order": [], "next": 0,
               "since": None, "until": None, "sources": []}
        on = self.settings()["sources"]
        live = {t["key"]: t for t in self.targets([k for k in SOURCE_KEYS if on.get(k)])}
        for key, upto in synced.items():
            t = live.get(key)
            if not t or t["blocked"]:
                continue
            ref["targets"][key] = {"source": t["source"], "account": t["account"], "label": t["label"],
                                   "unit": t["unit"], "area": t["area"], "done": 0, "total": None, "status": "queued", "error": None,
                                   "retry_at": 0, "fails": 0, "added": 0, "since": float(upto) - REFRESH_OVERLAP_S}
            ref["order"].append(key)
        self.index.clear_cursors("refresh")
        self.index.set_meta("refresh_at", now)
        self._save(ref, "refresh")
        return ref

    def run_steps(self, max_steps=1):
        """Runs up to max_steps steps now (the thread calls this with 1; tests with more). Returns steps done."""
        done = 0
        while done < max_steps and not self.stop_event.is_set():
            with self.lock:
                self._stop_step = False
                job, name, ckey = self._active()
                if job is None:
                    return done
                if self.blocked():
                    return done
                key = self._pick(job)
                if key is None:
                    if self._all_finished(job):
                        self._finish(job, name)
                    else:
                        self._save(job, name)
                    return done
                t = job["targets"][key]
                cursor = self.index.cursor(t["source"], t["account"], ckey)
            t0 = self.clock()
            self._step(job, name, ckey, key, t, cursor)
            with self.lock:
                job2 = self.index.meta(name)
                if job2 and job2.get("id") == job["id"]:
                    tt = job2["targets"].get(key)
                    if tt is not None:
                        tt.update({k: t[k] for k in ("done", "total", "status", "error", "retry_at", "fails", "added")})
                    job2["active_s"] = float(job2.get("active_s") or 0) + max(0.0, self.clock() - t0)
                    job2["items"] = sum(x["added"] for x in job2["targets"].values())
                    job2["next"] = job["next"]
                    if job2["state"] == "running" and self._all_finished(job2):
                        self._finish(job2, name)
                    else:
                        self._save(job2, name)
            done += 1
        return done

    def _pick(self, job):
        order = job["order"]
        now = self.clock()
        for i in range(len(order)):
            k = order[(job["next"] + i) % len(order)]
            t = job["targets"][k]
            if t["status"] in ("queued", "running") or (t["status"] == "waiting" and t["retry_at"] <= now):
                job["next"] = (job["next"] + i + 1) % len(order)
                return k
        return None

    @staticmethod
    def _all_finished(job):
        return all(t["status"] in ("done", "error", "skipped") for t in job["targets"].values())

    def _finish(self, job, name):
        job["state"] = "done"
        job["finished_at"] = self.clock()
        synced = self.index.meta("synced") or {}
        upto = job["started_at"]
        for key, t in job["targets"].items():
            if t["status"] == "done":
                synced[key] = upto
        self.index.set_meta("synced", synced)
        if name == "job":
            self.index.set_meta("refresh_at", self.clock())
        self._save(job, name)

    def _step(self, job, name, ckey, key, t, cursor):
        src = self.sources.get(t["source"])
        ctx = self._ctx()
        since = t.get("since", job.get("since"))
        if src is None:
            t.update(status="error", error="This source isn't available in this version.")
            return
        try:
            if t["total"] is None and job["kind"] == "scan" and cursor is None:
                try:
                    t["total"] = src.estimate(ctx, t, since)
                except srcs.SourceError:
                    t["total"] = None
            if key not in self._session:
                self._session[key] = (self.clock(), t["done"])
            items, new_cursor = src.step(ctx, t, since, job.get("until"), cursor)
            n = self.index.commit_step(items, t["source"], t["account"], ckey, new_cursor,
                                       clear_cursor=new_cursor is None)
            t["done"] += len(items)
            t["added"] += n["added"]
            if isinstance(new_cursor, dict) and new_cursor.get("listed") and new_cursor.get("last") and t["source"] == "gmail":
                t["total"] = new_cursor["listed"]
            if isinstance(new_cursor, dict) and "all" in new_cursor:
                t["total"] = len(new_cursor["all"])
            t["status"] = "done" if new_cursor is None else "running"
            t["error"] = None
            t["fails"] = 0
            if t["status"] == "done" and t["total"] is not None:
                t["total"] = max(t["total"], t["done"]) if t["source"] not in ("gmail", "obsidian") else t["done"]
        except srcs.Retry as e:
            t["fails"] += 1
            wait = min(BACKOFF_MAX_S, max(float(e.wait), 30.0 * (2 ** (t["fails"] - 1))))
            t.update(status="waiting", error=str(e), retry_at=self.clock() + wait)
        except srcs.SourceError as e:
            t.update(status="error", error=str(e))
        except Exception as e:  # noqa: BLE001 (type only, never data)
            self.log(f"Callsheet index: {t['source']} step failed: {type(e).__name__}")
            t.update(status="error", error="Something went wrong reading this source, so it was skipped. "
                                           "The app log has the details.")

    # --- status for the page and later stages -----------------------------------------------------------------
    def _view(self, job):
        if not job:
            return None
        now = self.clock()
        rows, remaining_total, rate_total = [], 0, 0.0
        active = max(1.0, float(job.get("active_s") or 0))
        for key in job["order"]:
            t = job["targets"][key]
            start = self._session.get(key)
            rate = None
            if t["done"] and active > 0:
                rate = t["done"] / active * 60.0
            if start and now - start[0] > 5 and t["done"] > start[1]:
                rate = (t["done"] - start[1]) / max(1.0, now - start[0]) * 60.0
            left = max(0, (t["total"] or 0) - t["done"]) if t["status"] not in ("done", "error", "skipped") else 0
            eta = round(left / rate * 60) if rate and left else (0 if t["status"] == "done" else None)
            remaining_total += left
            rows.append({"key": key, "source": t["source"], "account": t["account"], "label": t["label"],
                         "unit": t["unit"], "done": t["done"], "total": t["total"], "status": t["status"],
                         "error": t["error"], "rate_per_min": round(rate, 1) if rate else None, "eta_s": eta,
                         "retry_at": t["retry_at"] or None})
        done_items = sum(r["done"] for r in rows)
        if done_items:
            rate_total = done_items / active
        eta = round(remaining_total / rate_total) if rate_total and remaining_total else None
        return {"id": job["id"], "kind": job["kind"], "state": job["state"], "window": job.get("window"),
                "label": job.get("label"), "started_at": job["started_at"], "finished_at": job["finished_at"],
                "elapsed_s": round((job["finished_at"] or now) - job["started_at"]), "active_s": round(active),
                "eta_s": eta, "read": done_items, "added": job.get("items", 0), "targets": rows}

    def status(self):
        with self.lock:
            job = self.job()
            view = self._view(job)
            wait = self.blocked() if job and job["state"] == "running" else None
            ref = self.index.meta("refresh")
        return {"job": view, "waiting": {"reason": wait[0], "text": wait[1]} if wait else None,
                "refresh": {"state": ref["state"], "at": ref["started_at"], "added": ref.get("items", 0)} if ref else None,
                "synced": sorted((self.index.meta("synced") or {}).keys()),
                "windows": [{"key": k, "label": label} for k, label, _ in WINDOWS],
                "settings": self.settings(), "wipe_token": self.wipe_token}

    # --- housekeeping -----------------------------------------------------------------------------------------
    def prune_orphans(self):
        """Items of Google accounts or Slack workspaces that were removed from Callsheet leave the index."""
        accs = {a["id"] for a in self.store.accounts()}
        wss = {w["id"] for w in self.store.slack_workspaces()}
        removed = 0
        for source, account in self.index.accounts():
            if (source in ("gmail", "calendar", "drive") and account not in accs) or \
                    (source == "slack" and account not in wss):
                removed += self.index.delete(source, account)
        if removed:
            synced = self.index.meta("synced") or {}
            keep = {k: v for k, v in synced.items() if k.split(":", 1)[0] == "obsidian" or k.split(":", 1)[1] in accs | wss}
            self.index.set_meta("synced", keep)
        return removed

    # --- the thread -------------------------------------------------------------------------------------------
    def start_thread(self):
        self.thread = threading.Thread(target=self._loop, name="callsheet-index", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        try:  # low priority: Linux lets a thread lower its own priority
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError):
            pass
        last_prune = 0.0
        while not self.stop_event.is_set():
            did = 0
            try:
                if self.clock() - last_prune > 600:
                    self.prune_orphans()
                    last_prune = self.clock()
                did = self.run_steps(1)
            except Exception as e:  # noqa: BLE001 (type only)
                self.log(f"Callsheet index: scan loop error: {type(e).__name__}")
            self.stop_event.wait(self.tick if did else IDLE_S)
