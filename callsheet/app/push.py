"""Phone notifications through Home Assistant (0.5.1): a short summary after each scheduled brief, and urgent
alerts between briefs from a light check every ~10 minutes.

The light check is rules only: it never runs a sync, the writer or any AI model. It reads, per connected mail
account, the INBOX threads newer than the last check (Gmail metadata only: From, Subject, labels, snippet) and,
per Slack workspace, one conversations.history per VIP DM. A message is urgent when (`urgent_reason`):
  - it is from a VIP, or
  - its subject or snippet says "emergency" or "asap", or says "urgent" / "today" together with a separate
    deadline ("due", "by Friday", "by 5pm", "today", "tonight", "end of day", "this afternoon", ...), or
  - the sender states a decision (writer.states_decision, the 0.4.1 inbound claim check: approved, paid, signed).
Bulk and automated mail, Gmail's category tabs and the owner's "doesn't need a reply" rules are skipped (VIPs
win over sender and domain rules, like the brief). Each message alerts at most once (seen ids, bounded); at
most `max_per_hour` alerts in any hour, extras grouped into one "N more urgent messages" alert.

Notification text is the sender's name and the subject (clipped), never body text; Slack alerts say only who
wrote. Tapping opens Callsheet's Ingress page in the Home Assistant app (`data.url` on iOS, `data.clickAction`
on Android, both "/hassio/ingress/<app slug>").

Logs counts only. Settings live in the meta key "push", state in "push_state" (store.py).
"""
import datetime as dt
import email.utils
import html
import os
import platform
import re
import sys
import threading
import time

import google
import ha
import rules
import slack
import writer  # states_decision only: a pure text check, no model

CHECK_EVERY_S = 10 * 60      # the light check's period
CALL_WINDOW_S = 10 * 60      # no light check this close to a call time (the brief covers it)
LOOKBACK_MAX_S = 60 * 60     # the light check never looks further back than this
FIRST_LOOKBACK_S = CHECK_EVERY_S
CHECK_DEADLINE_S = 25
MAX_THREADS = 10             # Gmail threads read per account per check
MAX_VIP_DMS = 10             # conversations.history per workspace per check (also the Slack request cap)
SEEN_KEEP = 500
MAX_TARGETS = 5
TITLE_MAX = 40
TEXT_MAX = 120
BRIEF_STALE_S = 15 * 60
QUERY = "in:inbox -category:promotions -category:social -category:forums"

URGENT_WORD = re.compile(r"\b(?:urgent(?:ly)?|today)\b", re.I)
STRONG_WORD = re.compile(r"\b(?:emergency|asap|a\.s\.a\.p\.?)(?=\W|$)", re.I)
DEADLINE_HINT = re.compile(r"\b(?:today|tonight|eod|end\s+of\s+(?:the\s+)?day|this\s+(?:morning|afternoon|evening)|"
                           r"within\s+the\s+hour|right\s+away|immediately|by\s+noon|"
                           r"by\s+\d{1,2}(?::\d\d)?\s*(?:am|pm)?)\b", re.I)
TITLES = {"vip": "VIP message", "urgent": "Looks urgent", "claim": "Sender says it's done"}


def defaults():
    return {"targets": [], "at_call_times": False, "urgent": False,
            "quiet_start": "22:00", "quiet_end": "07:00", "max_per_hour": 3}


class Invalid(ValueError):
    """A settings change the owner can fix; the message is a plain sentence."""


_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def config_from(saved):
    c = defaults()
    if isinstance(saved, dict):
        c.update({k: v for k, v in saved.items() if k in c})
    return c


def patch_config(cur, patch):
    """Validated copy of cur with patch applied."""
    if not isinstance(patch, dict):
        raise Invalid("Send the phone notification settings as a JSON object.")
    c = config_from(cur)
    for key, val in patch.items():
        if key == "targets":
            if not isinstance(val, list) or any(not isinstance(t, str) or not ha.SERVICE.fullmatch(t) for t in val):
                raise Invalid("Pick phones from the list Home Assistant shows (notify.mobile_app_…).")
            val = list(dict.fromkeys(val))
            if len(val) > MAX_TARGETS:
                raise Invalid(f"Pick at most {MAX_TARGETS} phones.")
            c["targets"] = val
        elif key in ("at_call_times", "urgent"):
            if not isinstance(val, bool):
                raise Invalid(f"{key} must be true or false.")
            c[key] = val
        elif key in ("quiet_start", "quiet_end"):
            if not isinstance(val, str) or not _HHMM.fullmatch(val):
                raise Invalid("Quiet hours must look like 22:00 (24-hour clock).")
            c[key] = val
        elif key == "max_per_hour":
            if not isinstance(val, int) or isinstance(val, bool) or not 1 <= val <= 10:
                raise Invalid("Alerts per hour must be a whole number from 1 to 10.")
            c[key] = val
        else:
            raise Invalid(f"Unknown phone notification setting: {str(key)[:40]}.")
    return c


# --- deep link -----------------------------------------------------------------------
def app_slug():
    """The app's Supervisor slug, e.g. "a1b2c3d4_callsheet": the container's hostname is the slug with "_"
    written as "-". None when Callsheet doesn't run as a Home Assistant app."""
    s = os.environ.get("CALLSHEET_APP_SLUG", "").strip()
    if s:
        return s if re.fullmatch(r"[a-z0-9]+_callsheet", s) else None
    host = (os.environ.get("HOSTNAME") or platform.node() or "").strip().lower()
    m = re.fullmatch(r"([a-z0-9]+)-callsheet", host)
    return f"{m.group(1)}_callsheet" if m else None


def ingress_path():
    slug = app_slug()
    return f"/hassio/ingress/{slug}" if slug else None


def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _payload(title, message, tag=None, urgent=False):
    data = {"group": "callsheet"}
    link = ingress_path()
    if link:
        data["url"] = link          # iOS companion app
        data["clickAction"] = link  # Android companion app
    if tag:
        data["tag"] = tag           # a newer summary replaces the older one on the phone
    if urgent:
        data["priority"] = "high"   # Android: deliver now
        data["ttl"] = 0
        data["push"] = {"interruption-level": "time-sensitive"}  # iOS
    return {"title": _clip(title, TITLE_MAX), "message": _clip(message, TEXT_MAX), "data": data}


def test_payload():
    return _payload("Callsheet", "Test notification: phone notifications work. Tap to open Callsheet.",
                    tag="callsheet-test")


def _subject_of(row):
    """A mail row's subject (the row text is a quoted snippet when there is no subject: not used)."""
    if row.get("via") != "mail":
        return ""
    t = str(row.get("text") or "")
    return "" if t.startswith('"') else t


def brief_payload(brief):
    """After a scheduled brief: counts and the top item's who/what (subject or event title), no message text."""
    replies, schedule = brief.get("replies") or [], brief.get("schedule") or []
    n_r, n_e = len(replies), len(schedule)
    parts = [f"{n_r} to reply" if n_r else "Nothing to reply to",
             f"{n_e} event{'s' if n_e != 1 else ''} today"]
    msg = ", ".join(parts) + "."
    top = rules.rules_top(schedule, replies)
    if top:
        ref = top[0]["ref"]
        row = next((r for r in replies if r.get("id") == ref), None)
        if row is not None:
            subj = _subject_of(row)
            what = subj or ("Slack" if row.get("via") != "mail" else "")
            msg += " Top: " + _clip(row.get("who") or "Someone", 40) + (f", {what}" if what else "")
        else:
            ev = next((e for e in schedule if e.get("id") == ref), None)
            if ev is not None:
                msg += f" Top: clash at {ev.get('time') or ''} {ev.get('title') or ''}".rstrip()
    return _payload(brief.get("kind") or "Callsheet", msg, tag="callsheet-brief")


def alert_payload(item):
    who = _clip(item.get("who") or "Someone", 50)
    if item.get("source") == "slack":
        msg = f"{who} sent you a Slack message"
    else:
        subj = item.get("subject") or "(no subject)"
        msg = f"{who}: {subj}"
    return _payload(TITLES.get(item.get("reason"), "Looks urgent"), msg, urgent=True)


def batch_payload(n):
    return _payload("Looks urgent", f"{n} more urgent message{'s' if n != 1 else ''}. Open Callsheet to see them.",
                    tag="callsheet-more", urgent=True)


# --- the urgent rules (pure) -------------------------------------------------------------
def urgent_words(text):
    """True for "emergency"/"asap", or "urgent"/"today" with a separate deadline in the same text."""
    text = str(text or "")
    if STRONG_WORD.search(text):
        return True
    for m in URGENT_WORD.finditer(text):
        for d in list(DEADLINE_HINT.finditer(text)) + list(rules.DEADLINE.finditer(text)):
            if m.group().lower() == "today" and d.group().lower() == "today":
                continue  # "today" twice is not urgent
            if d.end() <= m.start() or d.start() >= m.end():
                return True
    return False


def urgent_reason(vip, subject, snippet, inbound):
    """"vip", "urgent", "claim" or None. inbound: the text the claim check reads (the subject unless it is a
    reply or forward, then the snippet), as in the brief."""
    if vip:
        return "vip"
    if urgent_words(subject + "\n" + snippet):
        return "urgent"
    if writer.states_decision(inbound or ""):
        return "claim"
    return None


def _hdr(msg):
    return {h.get("name", "").lower(): h.get("value", "") for h in (msg.get("payload") or {}).get("headers", [])}


def mail_item(thread, account_email, acc_id, vips, suppress, since_ms):
    """The urgent item for a thread's latest message (threads.get format=metadata), or None."""
    msgs = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
    if not msgs:
        return None
    last = max(msgs, key=lambda m: int(m.get("internalDate") or 0))
    if int(last.get("internalDate") or 0) <= since_ms:
        return None
    labels = last.get("labelIds") or []
    hdr = _hdr(last)
    name, addr = email.utils.parseaddr(hdr.get("from", ""))
    addr = addr.lower()
    if "SENT" in labels or "INBOX" not in labels or not addr or addr == account_email.lower():
        return None
    vip = addr in vips
    if not vip and (rules.is_automated(hdr, addr) or any(lb in rules.NO_REPLY_CATEGORIES for lb in labels)):
        return None
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    sup = {"source": "mail", "thread": str(thread.get("id", "")).lower(), "sender": addr, "domain": domain or None}
    if rules.suppressed(sup, vip, suppress):
        return None
    subject = " ".join(hdr.get("subject", "").split())
    snippet = " ".join(html.unescape(last.get("snippet") or "").split())
    inbound = ("" if rules._REPLY_SUBJECT.match(subject) else subject) + "\n" + snippet
    reason = urgent_reason(vip, subject, snippet, inbound)
    if not reason:
        return None
    return {"id": f"m:{acc_id}:{last.get('id')}", "source": "mail", "reason": reason,
            "who": name.strip() or vips.get(addr) or addr, "subject": _clip(subject, 100),
            "at": int(last.get("internalDate") or 0) / 1000}


def vip_slack_users(users, vips):
    """Slack user ids in the saved workspace state that are VIPs (by email, else by name, as rules.slack_rows)."""
    names = {n.strip().lower() for n in vips.values() if n}
    out = []
    for uid, u in (users or {}).items():
        if not isinstance(u, dict):
            continue
        if u.get("email"):
            if u["email"].lower() in vips:
                out.append(uid)
        elif any(n and n.strip().lower() in names for n in (u.get("name"), u.get("real_name"))):
            out.append(uid)
    return sorted(out)


ORDER = {"vip": 0, "urgent": 1, "claim": 2}


def plan(items, sent, held, now, limit):
    """Rate limit: (singles, batch, held, sent). At most `limit` alerts in any hour (sent: their times). When
    there is no room the items are held (counted) for the next check with room; otherwise items beyond room-1
    and the held count go into one batch alert."""
    recent = [t for t in sent if now - t < 3600]
    room = max(0, limit - len(recent))
    items = sorted(items, key=lambda i: (ORDER.get(i.get("reason"), 9), -float(i.get("at") or 0)))
    if room == 0:
        return [], 0, held + len(items), recent
    if len(items) + (1 if held else 0) <= room:
        singles, batch = items, held
    else:
        singles = items[:room - 1]
        batch = held + len(items) - len(singles)
    n = len(singles) + (1 if batch else 0)
    return singles, batch, 0, recent + [now] * n


# --- timing (pure) ---------------------------------------------------------------------
def _minutes(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def in_quiet(cfg, local):
    """Quiet hours (start == end: none). Crosses midnight when start > end."""
    s, e = _minutes(cfg["quiet_start"]), _minutes(cfg["quiet_end"])
    t = local.hour * 60 + local.minute
    if s == e:
        return False
    return s <= t < e if s < e else (t >= s or t < e)


def check_due(settings, cfg, state, now_ts):
    """(due, why). Off, no phones, a day off (when check-ins skip days off), quiet hours, within 10 min of a call
    time, or less than 10 min since the last check (or brief) -> not due."""
    if not cfg.get("urgent") or not cfg.get("targets"):
        return False, "off"
    tz = rules.tzinfo(settings)
    local = dt.datetime.fromtimestamp(now_ts, tz)
    if settings.get("skip_checkins_days_off") and rules.is_day_off(settings, local.date()):
        return False, "day off"
    if in_quiet(cfg, local):
        return False, "quiet hours"
    nxt, _ = rules.next_call(settings, local - dt.timedelta(seconds=CALL_WINDOW_S + 1))
    if nxt is not None and nxt.timestamp() <= now_ts + CALL_WINDOW_S:
        return False, "call time"
    last = float((state or {}).get("last_check") or 0)
    if last and now_ts - last < CHECK_EVERY_S:
        return False, "not yet"
    return True, "due"


def _stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class Notifier:
    def __init__(self, store, syncer, clock=time.time, log=_stderr, sleep=time.sleep):
        self.store = store
        self.syncer = syncer
        self.clock = clock
        self.log = log
        self.sleep = sleep
        self.lock = threading.Lock()

    # --- sending -----------------------------------------------------------
    def _send(self, payload, targets, kind):
        """One notification to every target. Returns (sent count, plain errors). Records last_sent/last_error."""
        sent, errors = 0, []
        for t in targets:
            try:
                ha.notify(t, payload)
                sent += 1
            except ha.HAError as e:
                errors.append(str(e))
        st = self.store.push_state()
        at = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="seconds")
        if sent:
            st["last_sent"] = {"at": at, "kind": kind}
        st["last_error"] = {"at": at, "text": " ".join(dict.fromkeys(errors))} if errors else None
        self.store.set_push_state(st)
        self.log(f"Callsheet push: {kind}, {sent} of {len(targets)} phones" + (f", {len(errors)} failed" if errors else ""))
        return sent, errors

    def test(self):
        cfg = self.store.push_config()
        if not cfg["targets"]:
            raise ha.HAError("Pick a phone first.", 0, "none")
        if not ha.available():
            raise ha.HAError(ha.NOT_IN_HA, 0, "unavailable")
        return self._send(test_payload(), cfg["targets"], "test")

    def after_brief(self):
        """After a scheduled brief: moves the light check's marker to now (the brief covers what came before),
        clears held alerts, and sends the summary when "At call times" is on."""
        now = self.clock()
        with self.lock:
            st = self.store.push_state()
            st["last_check"], st["held"] = now, 0
            self.store.set_push_state(st)
        cfg = self.store.push_config()
        if not cfg["at_call_times"] or not cfg["targets"] or not ha.available():
            return None
        brief = self.store.cached_brief()
        if not brief or not self.store.has_sources():
            return None
        try:
            age = now - dt.datetime.fromisoformat(brief.get("written_at") or "").timestamp()
        except ValueError:
            return None
        if age > BRIEF_STALE_S:
            return None
        return self._send(brief_payload(brief), cfg["targets"], "brief")

    # --- the light check ---------------------------------------------------
    def due(self, now=None):
        now = self.clock() if now is None else now
        return check_due(self.store.settings(), self.store.push_config(), self.store.push_state(), now)[0]

    def check(self):
        """One light check. Skipped (returns None) while a sync or evaluation holds the sync lock; it doesn't take
        that lock itself, so Refresh never waits for it. Never calls the writer or a model.
        Returns {"found", "sent", "held"}."""
        if self.syncer.lock.locked():
            return None
        with self.lock:
            return self._check()

    def _check(self):
        now = self.clock()
        cfg, st = self.store.push_config(), self.store.push_state()
        last = float(st.get("last_check") or 0)
        since = max(last, now - LOOKBACK_MAX_S) if last else now - FIRST_LOOKBACK_S
        deadline = now + CHECK_DEADLINE_S
        seen = list(st.get("seen") or [])
        known = set(seen)
        vips = {v["email"].lower(): v["name"] for v in self.store.vips()}
        areas = {a["id"]: a for a in self.store.areas()}
        items, n_mail, n_slack, problems = [], 0, 0, 0
        new_ids = []
        # mail: INBOX threads newer than the last check, metadata only
        client = self.store.google_client()
        suppress = self.store.suppress_rules()
        q = f"{QUERY} after:{int(since)}"
        for pub in self.store.accounts():
            area = areas.get(pub["area_id"])
            if not client or area is None or not area["reads"].get("mail") or not pub["services"].get("mail") \
                    or pub["status"] == "needs_reconnect" or self.clock() > deadline:
                continue
            acc = self.store.account_secret(pub["id"])
            try:
                listing = self._gcall(acc, client, google.gmail_threads, q, MAX_THREADS)
                for t in (listing.get("threads") or [])[:MAX_THREADS]:
                    if not t.get("id") or self.clock() > deadline:
                        continue
                    full = self._gcall(acc, client, google.gmail_thread, t["id"])
                    n_mail += 1
                    it = mail_item(full, acc["email"], acc["id"], vips, suppress, int(since * 1000))
                    if it and it["id"] not in known:
                        known.add(it["id"])
                        new_ids.append(it["id"])
                        items.append(it)
            except google.GoogleError:
                problems += 1
        # Slack: one conversations.history per VIP DM (ids from the brief's saved state)
        for pub in self.store.slack_workspaces():
            area = areas.get(pub["area_id"])
            if area is None or not area["reads"].get("chat", True) or pub["status"] == "needs_reconnect" or not vips:
                continue
            ws = self.store.slack_secret(pub["id"])
            state = ws.get("state") or {}
            dms = state.get("dms") or {}
            users = state.get("users") or {}
            uids = [u for u in vip_slack_users(users, vips) if u in dms and u != ws.get("user_id")][:MAX_VIP_DMS]
            if not uids:
                continue
            cl = slack.Client(ws["token"], clock=self.clock, sleep=self.sleep, deadline=deadline,
                              max_calls=min(MAX_VIP_DMS, slack.MAX_CALLS))
            for uid in uids:
                ch = dms[uid]
                try:
                    msgs = cl.get("conversations.history", channel=ch, oldest=f"{since:.6f}", limit=5).get("messages") or []
                except (slack.SlackError, slack.Budget):
                    problems += 1
                    break
                n_slack += 1
                theirs = [m for m in msgs if slack.is_real(m) and m.get("user") == uid
                          and float(m.get("ts") or 0) > since]
                fresh = [m for m in theirs if f"s:{ws['id']}:{ch}:{m['ts']}" not in known]
                for m in fresh:
                    known.add(f"s:{ws['id']}:{ch}:{m['ts']}")
                    new_ids.append(f"s:{ws['id']}:{ch}:{m['ts']}")
                if fresh:  # one alert per DM per check
                    u = users.get(uid) or {}
                    items.append({"id": f"s:{ws['id']}:{ch}:{fresh[-1]['ts']}", "source": "slack", "reason": "vip",
                                  "who": u.get("name") or u.get("real_name") or "A VIP",
                                  "at": max(float(m["ts"]) for m in fresh)})
        singles, batch, held, sent = plan(items, st.get("sent") or [], int(st.get("held") or 0), now,
                                          cfg["max_per_hour"])
        st.update(last_check=now, seen=(seen + new_ids)[-SEEN_KEEP:], sent=sent, held=held)
        self.store.set_push_state(st)
        for it in singles:
            self._send(alert_payload(it), cfg["targets"], "urgent")
        if batch:
            self._send(batch_payload(batch), cfg["targets"], "urgent batch")
        self.log(f"Callsheet urgent check: {n_mail} threads, {n_slack} Slack DMs, {len(items)} urgent, "
                 f"{len(singles) + (1 if batch else 0)} sent, {held} held, {problems} problems")
        return {"found": len(items), "sent": len(singles) + (1 if batch else 0), "held": held}

    def _gcall(self, acc, client, fn, *args):
        """fn(token, *args) with the sync's token handling (refresh when expired, once more on 401)."""
        try:
            return fn(self.syncer._token(acc, client), *args)
        except google.GoogleError as e:
            if e.status != 401:
                raise
        return fn(self.syncer._token(acc, client, force=True), *args)
