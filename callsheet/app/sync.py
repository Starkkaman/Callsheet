"""One sync: read today's calendar events and recent mail of every connected account, apply the
rules, cache the brief and record last_sync. One account failing never stops the others.

When the AI writer is on, it runs after the rules brief is cached: a summary and suggested replies are
added, or Heads up says why the writer didn't run. A writer failure never breaks the brief.

Logs one line per sync (and one per writer run) with counts only: no subjects, names, addresses,
bodies or keys.
"""
import datetime as dt
import sys
import threading
import time

import google
import rules
import writer

MAIL_QUERY = "in:inbox -category:promotions -category:social -category:forums newer_than:14d"
DEADLINE_S = 50  # keeps POST api/sync under ~60 s
RECONNECT_CODES = ("invalid_grant", "invalid_client", "unauthorized_client")


def _stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class _Stop(Exception):
    pass


class Syncer:
    def __init__(self, store, clock=time.time, log=_stderr):
        self.store = store
        self.clock = clock
        self.log = log
        self.lock = threading.Lock()

    def run_now(self, wait=70):
        """Runs a sync and returns last_sync. If one is already running, waits for it and returns its result."""
        if self.lock.acquire(blocking=False):
            try:
                return self._run()
            finally:
                self.lock.release()
        if self.lock.acquire(timeout=wait):
            self.lock.release()
        return self.store.last_sync()

    # --- tokens -----------------------------------------------------------
    def _token(self, acc, client, force=False):
        now = self.clock()
        if not force and acc.get("access_token") and acc.get("expires_at", 0) - 60 > now:
            return acc["access_token"]
        if not client:
            raise google.GoogleError("The Google client ID and secret are missing. Add them in Settings.")
        if not acc.get("refresh_token"):
            raise google.GoogleError("Google asked to sign in again.", 400, "invalid_grant")
        res = google.refresh(client, acc["refresh_token"])
        if not res.get("access_token"):
            raise google.GoogleError("Google didn't send an access token.")
        acc["access_token"] = res["access_token"]
        acc["expires_at"] = now + float(res.get("expires_in", 3600))
        self.store.set_tokens(acc["id"], acc["access_token"], acc["expires_at"], res.get("refresh_token"))
        return acc["access_token"]

    def _call(self, acc, client, fn, *args):
        """fn(token, *args), refreshing the token once on 401."""
        if self.clock() > self._deadline:
            raise _Stop()
        try:
            return fn(self._token(acc, client), *args)
        except google.GoogleError as e:
            if e.status != 401:
                raise
        return fn(self._token(acc, client, force=True), *args)

    # --- one sync ---------------------------------------------------------
    def _run(self):
        t0 = self.clock()
        self._deadline = t0 + DEADLINE_S
        settings = self.store.settings()
        tz = rules.tzinfo(settings)
        now = dt.datetime.fromtimestamp(t0, tz)
        day = now.date()
        day_start, day_end = rules.day_bounds(day, tz)
        areas = {a["id"]: a for a in self.store.areas()}
        vips = {v["email"].lower(): v["name"] for v in self.store.vips()}
        client = self.store.google_client()

        events, threads, errors = [], [], []
        n = {"accounts": 0, "ok": 0, "failed": 0, "calendars": 0, "events": 0, "threads": 0}
        stopped = False
        for pub in self.store.accounts():
            n["accounts"] += 1
            acc = self.store.account_secret(pub["id"])
            area = areas.get(acc["area_id"])
            if area is None:
                continue
            if acc["status"] == "needs_reconnect":
                n["failed"] += 1
                errors.append(f"Couldn't read {acc['email']}: it needs to be connected again.")
                continue
            if not client:
                n["failed"] += 1
                msg = "the Google client ID and secret are missing. Add them again."
                self.store.set_status(acc["id"], "error", msg[0].upper() + msg[1:])
                errors.append(f"Couldn't read {acc['email']}: {msg}")
                continue
            problems, reconnect = [], False
            parts = []
            if area["reads"].get("calendar") and acc["services"].get("calendar"):
                parts.append("calendar")
            if area["reads"].get("mail") and acc["services"].get("mail"):
                parts.append("mail")
            for part in parts:
                try:
                    if part == "calendar":
                        for cal in acc["calendars"]:
                            if not cal.get("selected"):
                                continue
                            items = self._call(acc, client, google.calendar_events, cal["id"],
                                               day_start.isoformat(), day_end.isoformat(), str(tz))
                            n["calendars"] += 1
                            n["events"] += len(items)
                            events += [(ev, area["id"]) for ev in items]
                    else:
                        listing = self._call(acc, client, google.gmail_threads, MAIL_QUERY, 50)
                        for t in listing.get("threads") or []:
                            if not t.get("id"):
                                continue
                            full = self._call(acc, client, google.gmail_thread, t["id"])
                            n["threads"] += 1
                            threads.append((full, acc["email"], area["id"], acc["id"]))
                except _Stop:
                    stopped = True
                    break
                except google.GoogleError as e:
                    if e.code in RECONNECT_CODES:
                        reconnect = True
                        problems.append(str(e) + " Reconnect the account.")
                        break
                    problems.append(f"{part}: {e}")
            if reconnect:
                self.store.set_status(acc["id"], "needs_reconnect", problems[-1])
            elif problems:
                self.store.set_status(acc["id"], "error", "; ".join(problems))
            else:
                self.store.set_status(acc["id"], "connected", None)
            if problems:
                n["failed"] += 1
                errors.append(f"Couldn't read {acc['email']}: {'; '.join(problems)}")
            else:
                n["ok"] += 1
            if stopped:
                break
        if stopped:
            errors.append(f"Reading stopped after {DEADLINE_S} seconds, so some items were skipped.")

        schedule, all_day = rules.build_schedule(events, tz, day)
        replies = []
        for th, addr, area_id, acc_id in threads:
            row = rules.reply_row(th, addr, area_id, t0, settings["reply_age_days"], vips)
            if row:
                row["_ref"] = (acc_id, th.get("id"))
                replies.append(row)
        rules.sort_replies(replies)
        refs = {}
        for i, row in enumerate(replies, 1):
            row["id"] = f"r{i}"
            refs[row["id"]] = row.pop("_ref")
        heads_up = [f"All day: {title}" for title in all_day] + errors
        written = now.isoformat(timespec="seconds")
        mailboxes = sum(1 for a in self.store.accounts() if a["services"].get("mail"))
        brief = {
            "date": day.isoformat(), "written_at": written, "kind": rules.kind_for(settings, now),
            "schedule": schedule, "replies": replies, "heads_up": heads_up,
            "sources": f"Written {rules.hhmm(now)} from {mailboxes} mailbox{'es' if mailboxes != 1 else ''}, "
                       f"{n['calendars']} calendar{'s' if n['calendars'] != 1 else ''}"}
        self.store.set_cached_brief(brief)
        last = {"at": written, "ok": not errors, "errors": errors}
        self.store.set_last_sync(last)
        self.log(f"Callsheet sync: {n['accounts']} accounts ({n['ok']} ok, {n['failed']} failed), "
                 f"{n['calendars']} calendars, {n['events']} events, {n['threads']} threads, "
                 f"{len(schedule)} on schedule, {len(replies)} need a reply, {self.clock() - t0:.1f}s")
        wcfg = self.store.writer_config()
        if wcfg["provider"] != "off":
            self._write(wcfg, brief, refs, events, areas, settings, tz, day, client)
        return last

    # --- the AI writer ----------------------------------------------------
    def _writer_input(self, wcfg, brief, refs, events, areas, settings, tz, day, client):
        """The minimised writer input and the reply ids suggestions may be written for. Areas whose
        reads.writer is off are left out entirely (their events don't even show up as overlaps)."""
        allowed = {aid for aid, a in areas.items() if a["reads"].get("writer", True)}
        name = {aid: a["name"] for aid, a in areas.items()}
        sched, all_day = rules.build_schedule([e for e in events if e[1] in allowed], tz, day)
        rows = [r for r in brief["replies"] if r["area"] in allowed]
        bodies = {}
        if wcfg["suggest_replies"]:
            accs = {}
            for r in rows[:writer.MAX_SUGGEST_ROWS]:  # replies are sorted VIP first, then oldest
                acc_id, thread_id = refs[r["id"]]
                try:
                    if acc_id not in accs:
                        accs[acc_id] = self.store.account_secret(acc_id)
                    full = self._call(accs[acc_id], client, google.gmail_thread_full, thread_id)
                except _Stop:
                    break
                except Exception:  # noqa: BLE001 (no body: the row just gets no suggestion)
                    continue
                msgs = writer.thread_messages(full)
                if msgs:
                    bodies[r["id"]] = msgs
        data = {
            "brief": brief["kind"], "date": day.isoformat(), "weekday": f"{day:%A}",
            "timezone": str(tz), "owner": settings.get("name") or "",
            "schedule": [{"time": e.get("time"), "end": e.get("end"), "title": e.get("title"),
                          "area": name.get(e.get("area"), ""), "note": e.get("note") or "",
                          "flags": (e.get("flag") or {}).get("text", "")} for e in sched],
            "heads_up": [f"All day: {t}" for t in all_day],
            "replies": []}
        for r in rows:
            item = {"id": r["id"], "who": r["who"], "area": name.get(r["area"], ""), "subject": r["text"],
                    "reasons": r["reasons"], "age": r["age"], "suggest": r["id"] in bodies}
            if r["id"] in bodies:
                item["messages"] = bodies[r["id"]]
            data["replies"].append(item)
        return data, set(bodies)

    def _write(self, wcfg, brief, refs, events, areas, settings, tz, day, client):
        t0 = self.clock()
        provider, used, note = wcfg["provider"], wcfg["provider"], ""
        result, error, ids = None, None, set()
        try:
            data, ids = self._writer_input(wcfg, brief, refs, events, areas, settings, tz, day, client)
            payload = writer.build_payload(data)
            if provider == "local":
                loc = wcfg["local"]
                try:
                    result = writer.local_write(loc["base_url"], loc["model"], loc["api_key"], payload, ids)
                except writer.WriterError as e:
                    if not (wcfg["fallback_to_claude"] and wcfg["claude"]["api_key"]):
                        raise
                    used, note = "claude", "local failed"
                    try:
                        result = self._claude(wcfg, payload, ids)
                    except writer.Refused:
                        raise
                    except writer.WriterError as e2:
                        raise writer.WriterError(f"{e} Claude didn't run either: {e2}") from None
            else:
                result = self._claude(wcfg, payload, ids)
        except writer.Refused as e:
            error = str(e)
            note = f"refused ({e.category or 'no category'})"
        except writer.WriterError as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001 (a writer bug must never break the brief)
            error = "Something went wrong inside the writer. Check the app log."
            note = f"unexpected {type(e).__name__}"
        at = dt.datetime.fromtimestamp(self.clock(), tz).isoformat(timespec="seconds")
        if result:
            brief["summary"] = {"text": result["summary"], "by": result["by"], "at": at}
            for r in brief["replies"]:
                if r["id"] in result["replies"]:
                    r["suggestion"] = result["replies"][r["id"]]
            if result.get("beta") is False:
                note = (note + "; " if note else "") + "beta not accepted, sent without fallbacks"
        else:
            brief["heads_up"].append(f"The AI writer didn't run: {error}")
        self.store.set_cached_brief(brief)
        self.store.set_last_writer({
            "at": at, "ok": bool(result), "by": result["by"] if result else None,
            "input_tokens": result["input_tokens"] if result else 0,
            "output_tokens": result["output_tokens"] if result else 0, "error": None if result else error})
        self.log(f"Callsheet writer: {used}{' (fallback from local)' if used != provider else ''}, "
                 f"{'ok' if result else 'failed'}"
                 + (f", {result['input_tokens']} in / {result['output_tokens']} out tokens, "
                    f"{len(result['replies'])} of {len(ids)} suggestions" if result else "")
                 + (f", {note}" if note else "") + f", {self.clock() - t0:.1f}s")

    def _claude(self, wcfg, payload, ids):
        return writer.claude_write(wcfg["claude"]["api_key"], wcfg["claude"]["model"], payload, ids)
