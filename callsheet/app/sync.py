"""One sync: read today's calendar events and recent mail of every connected account, apply the
rules, cache the brief and record last_sync. One account failing never stops the others.

When the AI writer is on, it runs after the rules brief is cached: a summary and suggested replies are
added, or Heads up says why the writer didn't run. A writer failure never breaks the brief.

Logs one line per sync (and one per writer run) with counts only: no subjects, names, addresses,
bodies or keys.
"""
import datetime as dt
import email.utils
import sys
import threading
import time

import facts
import google
import rules
import slack
import writer

MAIL_QUERY = "in:inbox -category:promotions -category:social -category:forums newer_than:14d"
DEADLINE_S = 50  # keeps POST api/sync under ~60 s
RECONNECT_CODES = ("invalid_grant", "invalid_client", "unauthorized_client")


def _stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()



def _addr(value):
    return email.utils.parseaddr(value or "")[1].lower()


def inbound_claim(row):
    """True when the row's latest incoming message (rules' `_inbound` text: mail subject and snippet, Slack the
    message) states that something was approved, paid, signed, released and so on (writer.states_decision:
    [bracketed] text, questions and conditions don't count). Pops `_inbound`, so no message text is stored."""
    return bool(writer.states_decision(row.pop("_inbound", "") or ""))

class _Stop(Exception):
    pass


class Syncer:
    def __init__(self, store, clock=time.time, log=_stderr, sleep=time.sleep):
        self.store = store
        self.clock = clock
        self.log = log
        self.sleep = sleep  # Slack rate-limit waits (tests replace it)
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
    def _read(self, live=True):
        """The Google read path and the rules: returns the brief and what the writer input needs. With
        live=False (the owner-run writer evaluation) account status is not changed; refreshed access
        tokens are still saved, as in a sync."""
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
                if live:
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
            if live:  # the evaluation's read leaves account status as it is
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

        slack_rows, slack_heads, n["slack"] = self._read_slack(areas, vips, t0, tz, errors, live)
        fmt = rules.time_format(settings)
        schedule, all_day = rules.build_schedule(events, tz, day, fmt)
        for i, row in enumerate(schedule, 1):
            row["id"], row["key"] = f"s{i}", rules.row_key(row.pop("_key"))
        suppress = self.store.suppress_rules()
        replies, hidden = [], 0
        for th, addr, area_id, acc_id in threads:
            row = rules.reply_row(th, addr, area_id, t0, settings["reply_age_days"], vips)
            if row:
                row["_ref"] = {"source": "mail", "acc": acc_id, "thread_id": th.get("id")}
                replies.append(row)
        replies += slack_rows
        shown = []
        for row in replies:
            if rules.suppressed(row["_sup"], row["vip"], suppress):
                hidden += 1
            else:
                shown.append(row)
        replies = rules.sort_replies(shown)
        refs = {}
        for i, row in enumerate(replies, 1):
            row["id"], row["key"] = f"r{i}", rules.row_key(row.pop("_key"))
            ref = row.pop("_ref", None) or {"source": "slack"}
            ref.update(row.pop("_sup"), key=row["key"], vip=row["vip"])
            if "_messages" in row:
                ref["messages"] = row.pop("_messages")
            if "_facts" in row:  # for the writer's facts block only; never stored
                ref["facts"] = row.pop("_facts")
            row["claim"] = inbound_claim(row)
            # the wait wording for the page (0.4.1), from the same facts and rule as the writer's: no message text
            wait = facts.row_wait(ref.get("facts"), day, tz)
            if wait:
                row["wait"] = wait
            # done: mail by account + thread, Slack by team + channel + thread (or message) ts
            done_key = row.pop("_done", None)
            if ref.get("source") == "mail":
                done_key = f"mail:{ref.get('acc')}:{ref.get('thread_id')}"
            ref.update(done_key=done_key, last=float(row.pop("_last", 0) or 0))
            row["gist"] = rules.fallback_gist(row)
            refs[row["id"]] = ref
        heads_up = [f"All day: {title}" for title in all_day] + slack_heads + errors
        written = now.isoformat(timespec="seconds")
        mailboxes = sum(1 for a in self.store.accounts() if a["services"].get("mail"))
        tail = (f"from {mailboxes} mailbox{'es' if mailboxes != 1 else ''}, "
                f"{n['calendars']} calendar{'s' if n['calendars'] != 1 else ''}")
        if n["slack"]:
            tail += f", {n['slack']} Slack workspace{'s' if n['slack'] != 1 else ''}"
        sources = f"Written {rules.clock(now, fmt)} {tail}"
        brief = {
            "date": day.isoformat(), "written_at": written, "kind": rules.kind_for(settings, now),
            "schedule": schedule, "replies": replies, "heads_up": heads_up, "sources": sources,
            "sources_tail": tail, "hidden": hidden, "done": []}
        self._apply_done(brief, refs, tz, live)
        return {"t0": t0, "brief": brief, "refs": refs, "events": events, "areas": areas, "settings": settings,
                "tz": tz, "day": day, "client": client, "errors": errors, "n": n, "schedule": schedule}

    def _read_slack(self, areas, vips, t0, tz, errors, live):
        """Every connected Slack workspace whose area reads chat. Returns (rows, heads_up, workspaces read).
        With live=False (the writer evaluation) neither the saved state nor the status changes."""
        rows, heads, count = [], [], 0
        for pub in self.store.slack_workspaces():
            area = areas.get(pub["area_id"])
            if area is None or not area["reads"].get("chat", True):
                continue
            ws = self.store.slack_secret(pub["id"])
            name = ws["team_name"] or ws["domain"] or "workspace"
            if ws["status"] == "needs_reconnect":
                errors.append(f"Couldn't read Slack {name}: it needs to be connected again.")
                continue
            cl = slack.Client(ws["token"], clock=self.clock, sleep=self.sleep, deadline=self._deadline)
            try:
                data, state, problems = slack.read_workspace(cl, ws, self.clock(), self.log)
            except (slack.SlackError, slack.Budget) as e:
                reconnect = isinstance(e, slack.SlackError) and e.code in slack.RECONNECT_CODES
                msg = str(e) if isinstance(e, slack.SlackError) else "Slack reading stopped before it started."
                if live:
                    self.store.set_slack_state(ws["id"], status="needs_reconnect" if reconnect else "error", error=msg)
                errors.append(f"Couldn't read Slack {name}: {msg}")
                continue
            count += 1
            if live:
                self.store.set_slack_state(ws["id"], state=state, status="connected", error=None)
            errors += [f"Slack {name}: {p}" for p in problems]
            r, h = rules.slack_rows(data, ws, area["id"], t0, vips, tz, rules.time_format(self.store.settings()))
            for row in r:
                row["_ref"] = {"source": "slack", "ws": ws["id"]}
            rows += r
            heads += h
        return rows, heads, count

    def _apply_done(self, brief, refs, tz, live=True):
        """Splits the brief's reply rows (and rows already under "done") by the stored done keys: a done row
        leaves replies; it is listed under "done" (with done_at) when it was marked done on the brief's date.
        A row whose last message is newer than the one seen when it was marked done reopens: its key is
        deleted (live only) and it stays in replies. Live runs also prune keys older than 30 days."""
        done = self.store.done_keys()
        rows = list(brief.get("replies") or []) + [{k: v for k, v in r.items() if k != "done_at"}
                                                    for r in brief.get("done") or []]
        rows.sort(key=lambda r: int(str(r.get("id") or "r0")[1:] or 0))
        keep, out, reopened = [], [], []
        for r in rows:
            ref = refs.get(r.get("id")) or {}
            entry = done.get(ref.get("done_key")) if ref.get("done_key") else None
            if entry is None:
                keep.append(r)
            elif float(ref.get("last") or 0) > float(entry.get("last") or 0):
                reopened.append(ref["done_key"])
                keep.append(r)
            else:
                try:
                    day = dt.datetime.fromisoformat(entry["done_at"]).astimezone(tz).date().isoformat()
                except (KeyError, TypeError, ValueError):
                    day = None
                if day == brief.get("date"):
                    out.append(dict(r, done_at=entry["done_at"]))
        brief["replies"], brief["done"] = keep, out
        if live:
            self.store.clear_done(reopened, self.clock())

    def _mark_new(self, brief):
        """New since: rows whose key wasn't in any earlier brief today get new: true; previous_at is the
        latest earlier brief's time (None for the day's first)."""
        seen = self.store.seen_day()
        rows = brief["schedule"] + brief["replies"]
        keys = [r["key"] for r in rows]
        if seen.get("date") == brief["date"] and seen.get("briefs"):
            known = set(seen.get("keys") or [])
            for r in rows:
                if r["key"] not in known:
                    r["new"] = True
            brief["previous_at"] = seen["briefs"][-1]
            keys = list(dict.fromkeys((seen.get("keys") or []) + keys))[-3000:]
            briefs = (seen["briefs"] + [brief["written_at"]])[-20:]
        else:
            brief["previous_at"] = None
            briefs = [brief["written_at"]]
        self.store.set_seen_day({"date": brief["date"], "keys": keys, "briefs": briefs})

    def _run(self):
        r = self._read()
        t0, brief, errors, n, schedule, replies = r["t0"], r["brief"], r["errors"], r["n"], r["schedule"], \
            r["brief"]["replies"]
        written = brief["written_at"]
        self._mark_new(brief)
        self.store.set_brief_refs({rid: {k: v for k, v in ref.items() if k not in ("messages", "facts")}
                                   for rid, ref in r["refs"].items()})
        self.store.set_cached_brief(brief)
        last = {"at": written, "ok": not errors, "errors": errors}
        self.store.set_last_sync(last)
        self.log(f"Callsheet sync: {n['accounts']} accounts ({n['ok']} ok, {n['failed']} failed), "
                 f"{n['calendars']} calendars, {n['events']} events, {n['threads']} threads, {n['slack']} Slack, "
                 f"{len(schedule)} on schedule, {len(replies)} need a reply, {self.clock() - t0:.1f}s")
        wcfg = self.store.writer_config()
        if wcfg["provider"] != "off":
            self._write(wcfg, brief, r["refs"], r["events"], r["areas"], r["settings"], r["tz"], r["day"], r["client"])
        return last

    def writer_data(self, wcfg):
        """For the owner-run writer evaluation (evaluate.py): the writer input a sync would build right now
        (same Google reads, rules, stripping and row ids) without storing anything: no cached brief, last
        sync, account status or last writer. The caller holds self.lock. Returns (data, ids, ask, read_errors):
        ask is what a live brief would ask for (areas, the rules' top refs, the active profile, time format)."""
        r = self._read(live=False)
        data, ids, ask = self._writer_input(wcfg, r["brief"], r["refs"], r["events"], r["areas"], r["settings"],
                                            r["tz"], r["day"], r["client"])
        return data, ids, ask, len(r["errors"])

    # --- the AI writer ----------------------------------------------------
    def _writer_input(self, wcfg, brief, refs, events, areas, settings, tz, day, client):
        """The minimised writer input, the reply ids suggestions may be written for, and the ask (area ids in
        the input, the rows the rules put in Top of the day among the rows sent, the owner's writing
        instructions). Areas whose reads.writer is off are left
        out entirely (their events don't even show up as overlaps)."""
        allowed = {aid for aid, a in areas.items() if a["reads"].get("writer", True)}
        name = {aid: a["name"] for aid, a in areas.items()}
        fmt = rules.time_format(settings)
        sched, all_day = rules.build_schedule([e for e in events if e[1] in allowed], tz, day, fmt)
        sid = {e["key"]: e["id"] for e in brief["schedule"] if e.get("key")}
        for e in sched:
            e["id"] = sid.get(rules.row_key(e.pop("_key")))
        sched = [e for e in sched if e["id"]]
        rows = [r for r in brief["replies"] if r["area"] in allowed]
        bodies = {}
        if wcfg["suggest_replies"]:
            accs = {}
            for r in rows[:writer.MAX_SUGGEST_ROWS]:  # replies are sorted VIP first, then oldest
                ref = refs[r["id"]]
                if ref.get("source") == "slack":
                    if ref.get("messages"):
                        bodies[r["id"]] = [dict(m, text=writer.cap_text(m.get("text") or "")) for m in ref["messages"]]
                    continue
                acc_id, thread_id = ref["acc"], ref["thread_id"]
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
            "areas": [{"id": aid, "name": name.get(aid, "")} for aid in dict.fromkeys(
                [e["area"] for e in sched] + [r["area"] for r in rows])],
            "schedule": [{"id": e["id"], "time": e.get("time"), "end": e.get("end"), "title": e.get("title"),
                          "area": name.get(e.get("area"), ""), "note": e.get("note") or "",
                          "flags": (e.get("flag") or {}).get("text", "")} for e in sched],
            "heads_up": [f"All day: {t}" for t in all_day],
            "replies": []}
        for r in rows:
            item = {"id": r["id"], "who": r["who"], "area": name.get(r["area"], ""), "source": r.get("source", ""),
                    "subject": r["text"],
                    "reasons": r["reasons"], "age": r["age"], "suggest": r["id"] in bodies}
            if r["id"] in bodies:
                item["messages"] = bodies[r["id"]]
            data["replies"].append(item)
        # the facts block (0.4.1): the rules' numbers for the rows and events sent; the deadline is looked for in
        # the last message's text when its body was read, else in the subject and snippet (Slack: the message)
        f_rows = []
        for r in rows:
            f = dict(refs.get(r["id"], {}).get("facts") or {})
            if f and refs[r["id"]].get("source") == "mail" and bodies.get(r["id"]):
                f["text"] = (bodies[r["id"]][-1].get("text") or "") + "\n" + (f.get("text") or "")
                # the sender's-claim check also reads the body (quotes and signature stripped) when the last
                # message read is still the sender's; only the flag is kept (stored with the brief after _write)
                if not r.get("claim") and _addr(bodies[r["id"]][-1].get("from")) == refs[r["id"]].get("sender"):
                    r["claim"] = bool(writer.states_decision(bodies[r["id"]][-1].get("text") or ""))
            f_rows.append({"id": r["id"], "who": r["who"], "text": r["text"], "vip": r.get("vip"), "facts": f})
        f_sched = [{"start_hhmm": e.get("start_hhmm"), "end_hhmm": e.get("end_hhmm"), "title": e.get("title"),
                    "area": name.get(e.get("area"), "")} for e in sched]
        data[writer.FACTS_KEY] = facts.build(f_rows, f_sched, day, tz, fmt, settings.get("reply_age_days") or 0)
        sent = {e["id"] for e in data["schedule"]} | {r["id"] for r in data["replies"]}
        top = rules.rules_top([e for e in brief["schedule"] if e.get("id") in sent],
                              [r for r in brief["replies"] if r.get("id") in sent])
        ask = writer.make_ask([a["id"] for a in data["areas"]], [t["ref"] for t in top],
                              self.store.active_prompts(wcfg), fmt)
        return data, list(bodies), ask  # ids in row order (VIP first, then oldest)

    def _write(self, wcfg, brief, refs, events, areas, settings, tz, day, client):
        t0 = self.clock()
        provider, used, note = wcfg["provider"], wcfg["provider"], ""
        result, error, ids = None, None, set()
        try:
            data, ids, ask = self._writer_input(wcfg, brief, refs, events, areas, settings, tz, day, client)
            payload = writer.build_payload(data)
            if provider == "local":
                loc = wcfg["local"]
                opts = writer.local_options(loc)
                try:
                    local_payload = writer.fit_local(data, ids, opts, self.log, ask=ask)
                    result = writer.local_write(loc["base_url"], loc["model"], loc["api_key"], local_payload, ids,
                                                opts=opts, log=self.log, ask=ask)
                except writer.WriterError as e:
                    if not (wcfg["fallback_to_claude"] and wcfg["claude"]["api_key"]):
                        raise
                    used, note = "claude", "local failed"
                    try:
                        result = self._claude(wcfg, payload, ids, ask)
                    except writer.Refused:
                        raise
                    except writer.WriterError as e2:
                        raise writer.WriterError(f"{e} Claude didn't run either: {e2}") from None
            else:
                result = self._claude(wcfg, payload, ids, ask)
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
            brief["summary"] = {"text": result["summary"], "by": result["by"], "at": at,
                                "areas": result.get("areas") or {}, "top": result.get("top") or []}
            flagged = set(result.get("flagged") or ())
            gists = result.get("gists") or {}
            for r in brief["replies"]:
                if r["id"] in gists:
                    r["gist"] = gists[r["id"]]
                if r["id"] in result["replies"]:
                    r["suggestion"] = result["replies"][r["id"]]
                    if r["id"] in flagged:
                        r["suggestion_warning"] = writer.WARNING
            if flagged:
                n = len(flagged)
                note = (note + "; " if note else "") + f"{n} suggestion{'s' if n != 1 else ''} flagged"
            if result.get("beta") is False:
                note = (note + "; " if note else "") + "beta not accepted, sent without fallbacks"
        else:
            brief["heads_up"].append(f"The AI writer didn't run: {error}")
        self._apply_done(brief, refs, tz)  # rows marked done (or undone) while the writer ran
        self.store.set_cached_brief(brief)
        self.store.set_last_writer({
            "at": at, "ok": bool(result), "by": result["by"] if result else None,
            "input_tokens": result["input_tokens"] if result else 0,
            "output_tokens": result["output_tokens"] if result else 0,
            "empty_suggestions": result.get("empty", 0) if result else 0, "error": None if result else error})
        self.log(f"Callsheet writer: {used}{' (fallback from local)' if used != provider else ''}, "
                 f"{'ok' if result else 'failed'}"
                 + (f", {result['input_tokens']} in / {result['output_tokens']} out tokens, "
                    f"{len(result['replies'])} of {len(ids)} suggestions, {result.get('empty', 0)} empty"
                    if result else "")
                 + (f", {note}" if note else "") + f", {self.clock() - t0:.1f}s")

    def _claude(self, wcfg, payload, ids, ask=None):
        return writer.claude_write(wcfg["claude"]["api_key"], wcfg["claude"]["model"], payload, ids, ask=ask)
