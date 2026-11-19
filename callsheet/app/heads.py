"""0.21.0: what Today shows without the model (docs/today.md "Security, invites, channels"): security heads-ups and
invites to answer (docs/agenda.md: never hidden), and the Reports copied into messages.db so the ledger reads them too.

Plain code, deterministic, on every stored message:
- Security: the 0.8.0 rule (rules.security_alert: the SECURITY words in the subject or the start of the text, the
  look-alike sender check) on received mail and texts. Each alert is one ledger item (type event, subtype security,
  never_hide), shown at the top of Now and next until the owner marks it Done; never in Clean up, never snoozed. An
  alert older than SECURITY_WINDOW_S when Callsheet first sees it (the first read-back) is kept as done, with the
  reason in its history. A Clean up item the model made from the same message joins the alert (merged_into).
- Invites: Calendar events where the owner's own answer is needsAction (one item per series: its next time) and
  invites found in mail (a calendar file or Google's "Invitation:" subject; the time from that subject when it has
  one, else from the model's reading of the message). Each is an ask (subtype invite, never_hide) in Needs you, takes
  part in clash checks (conflicts.check_item; the invite stands for its calendar entry while it is unanswered) and
  closes by itself when it is answered in Calendar, cancelled or over. An invite in mail that is already on a
  calendar Callsheet reads is the calendar's.
- Reports (0.10.2a): each new report that doesn't read like instructions is copied into messages.db (source
  "reports"), so the extractor reads it like a message. Chat keeps reading reports from the index.
"""
import datetime as dt
import json
import re

import conflicts as C
import ledger as L
import need as N
import rules

SECURITY_WINDOW_S = 3 * 86400        # as the 0.8.0 brief's heads-ups: alerts newer than this when first seen
SEC_SOURCES = ("gmail", "sms", "sms_live")   # automated alerts arrive by mail and text; chats are people talking
SNIPPET = 240                        # the start of the text the rule reads (Gmail's snippet length, roughly)
PUSH_FRESH_S = 3600                  # a phone alert only for an alert this new (never for the read-back)
INVITE_DAYS = 60                     # calendar invites looked at this far ahead (gather.LOOKAHEAD_DAYS)
MAIL_INVITE_DAYS = 14                # a mail invite without a time stays open this long
SCAN_BATCH = 500
CAL_MIMES = ("text/calendar", "application/ics", "text/x-vcalendar", "application/x-ics", "text/x-calendar")
CAL_EXT = (".ics", ".vcs", ".ical", ".icalendar")
CAL_KEY = "invite:cal:"
LURE_MARK = "Not from {brand}: the address only looks like it"

_INV = re.compile(r"^\s*(?P<upd>updated\s+)?invitation(?:\s+with\s+note)?\s*:\s*(?P<rest>.+)$", re.I)
_CANCEL = re.compile(r"^\s*(?:cancel(?:l)?ed\s+(?:event|invitation)(?:\s+with\s+note)?|invitation\s+cancel(?:l)?ed)"
                     r"\s*:\s*(?P<rest>.+)$", re.I)
_ANSWER = re.compile(r"^\s*(?:accepted|declined|tentatively\s+accepted|tentative|proposed\s+new\s+time)\s*:", re.I)
_GTAIL = re.compile(r"^(?P<title>.+?)\s+@\s+(?P<when>.+?)(?:\s+\((?P<tz>[^()]*)\))?\s+\((?P<to>[^()]*@[^()]*)\)\s*$")
_DAY = r"[A-Za-z]{{3}}\s+(?P<{m}>[A-Za-z]{{3}})\s+(?P<{d}>\d{{1,2}}),\s+(?P<{y}>\d{{4}})"
_TIME = r"\d{1,2}(?::\d\d)?\s*[ap]m"
_GWHEN = re.compile("^" + _DAY.format(m="m1", d="d1", y="y1") +
                    r"(?:\s+(?P<t1>" + _TIME + r")\s*[-–]\s*(?:" + _DAY.format(m="m2", d="d2", y="y2") + r"\s+)?"
                    r"(?P<t2>" + _TIME + r"))?(?:\s*[-–]\s*" + _DAY.format(m="m3", d="d3", y="y3") + r")?$", re.I)
_OFFSETS = {"utc": 0, "gmt": 0, "bst": 1, "cet": 1, "cest": 2, "eet": 2, "eest": 3, "est": -5, "edt": -4, "cst": -6,
            "cdt": -5, "mst": -7, "mdt": -6, "pst": -8, "pdt": -7, "akst": -9, "akdt": -8, "hst": -10}
_MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                    "dec"), 1)}


# --- security -----------------------------------------------------------------------------------------------------
def security_of(row):
    """The 0.8.0 rule on one stored message: {"marks", "brand", "lure"} or None."""
    if row.get("source") not in SEC_SOURCES or row.get("direction") != "in" or row.get("deleted_at") is not None:
        return None
    return rules.security_alert(None, str(row.get("sender") or ""), str(row.get("subject") or ""),
                                str(row.get("text") or "")[:SNIPPET])


def security_marks(alert):
    """[[code, words]] for the page: a look-alike sender is never named as the company it imitates."""
    if not alert:
        return []
    out = []
    if "lure" in alert["marks"]:
        out.append(["lure", LURE_MARK.format(brand=alert["lure"] if isinstance(alert["lure"], str) else
                                             "a known sender")])
    if "sender_fail" in alert["marks"]:
        out.append(["sender_fail", "Sender not verified"])
    return out


# --- invites in mail ---------------------------------------------------------------------------------------------
def _has_cal_file(row):
    try:
        files = json.loads(row.get("attachments") or "[]")
    except ValueError:
        return False
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or f.get("inline"):     # 0.24.2: an inline calendar part (its UID) isn't a file
            continue
        if str(f.get("type") or "").lower() in CAL_MIMES or str(f.get("name") or "").lower().endswith(CAL_EXT):
            return True
    return False


def _zone_of(word, tz):
    w = str(word or "").strip().lower()
    m = re.fullmatch(r"(?:gmt|utc)\s*([+-])\s*(\d{1,2})(?::?(\d\d))?", w)
    if m:
        off = int(m.group(2)) * 60 + int(m.group(3) or 0)
        return dt.timezone(dt.timedelta(minutes=off if m.group(1) == "+" else -off))
    if w in _OFFSETS:
        return dt.timezone(dt.timedelta(hours=_OFFSETS[w]))
    return tz


def _hm(t):
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?\s*([ap]m)", t.strip().lower())
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    return h, int(m.group(2) or 0)


def _date(mon, day, year):
    k = str(mon or "").lower()[:3]
    if k not in _MON:
        raise ValueError(mon)
    return dt.date(int(year), _MON[k], int(day))


def subject_when(when, zone_word, tz):
    """Google's invite subject time ("Wed Nov 18, 2026 2pm - 3pm", "Wed Nov 18, 2026", "Fri Nov 20, 2026 - Sun Nov
    22, 2026") -> ledger when fields, or None when it isn't that shape (a series, another calendar's words)."""
    m = _GWHEN.match(" ".join(str(when or "").split()))
    if not m:
        return None
    try:
        d1 = _date(m["m1"], m["d1"], m["y1"])
        if m["t1"]:
            z = _zone_of(zone_word, tz)
            h1, n1 = _hm(m["t1"])
            h2, n2 = _hm(m["t2"])
            d2 = _date(m["m2"], m["d2"], m["y2"]) if m["m2"] else d1
            s = dt.datetime.combine(d1, dt.time(h1, n1), tzinfo=z)
            e = dt.datetime.combine(d2, dt.time(h2, n2), tzinfo=z)
            if e <= s:
                e = s + dt.timedelta(hours=1)
            return {"start_ts": s.timestamp(), "end_ts": e.timestamp(), "all_day": 0, "start_date": None,
                    "end_date": None, "time": f"{h1:02d}:{n1:02d}", "date_resolved": 1, "approx": 0, "tz": "",
                    "phrase": " ".join(when.split())[:200], "candidates": []}
        d3 = _date(m["m3"], m["d3"], m["y3"]) if m["m3"] else d1
    except (ValueError, TypeError):
        return None
    return {"start_ts": None, "end_ts": None, "all_day": 1, "start_date": d1.isoformat(),
            "end_date": max(d1, d3).isoformat(), "time": "", "date_resolved": 1, "approx": 0, "tz": "",
            "phrase": " ".join(when.split())[:200], "candidates": []}


def invite_of(row, tz):
    """An invite in a received mail: {"title", "when" (or None), "cancel"} or None. Answers (Accepted: …) are not
    invites."""
    if row.get("source") != "gmail" or row.get("direction") != "in" or row.get("deleted_at") is not None:
        return None
    subj = " ".join(str(row.get("subject") or "").split())
    if _ANSWER.match(subj):
        return None
    cancel = _CANCEL.match(subj)
    inv = _INV.match(subj)
    if not (cancel or inv or _has_cal_file(row)):
        return None
    rest = (cancel or inv)["rest"] if (cancel or inv) else subj
    title, when = rest, None
    g = _GTAIL.match(rest)
    if g:
        title = g["title"]
        when = subject_when(g["when"], g["tz"], tz)
    title = rules._clip(title.strip() or "an invite", 200)  # noqa: SLF001
    return {"title": title, "when": when, "cancel": bool(cancel), "updated": bool(inv and inv["upd"])}


def _span_of(when, tz):
    return L.span(dict(when, candidates=[]), tz) if when else None


def _on_calendar(db, title, when, now, tz):
    """A calendar entry (any answer, not cancelled) that is this invite: a similar title, and the same day when the
    mail gives one (else within the next INVITE_DAYS)."""
    sp = _span_of(when, tz)
    lo, hi = (sp[0] - 86400, sp[1] + 86400) if sp else (now - 86400, now + INVITE_DAYS * 86400)
    for e in db.execute("SELECT id, title FROM events WHERE status != 'cancelled' AND start_ts IS NOT NULL AND "
                        "start_ts < ? AND coalesce(end_ts, start_ts) > ?", (hi, lo)):
        if _same_title(title, e["title"]):
            return e["id"]
    return None


def _same_title(a, b):
    na, nb = " ".join(L.words(a)), " ".join(L.words(b))
    return bool(na and nb) and (na == nb or L.similar(a, b) >= 0.75)


# --- the items ------------------------------------------------------------------------------------------------------
def _item_of_message(db, message, subtype):
    r = db.execute("SELECT i.id FROM ledger_items i JOIN ledger_sources s ON s.item=i.id WHERE s.message=? AND "
                   "i.subtype=? ORDER BY i.id LIMIT 1", (message, subtype)).fetchone()
    return r["id"] if r else None


def _fold_cleanup(db, message, into, now):
    """A Clean up item the model already made from this message joins the alert or invite: never in Clean up."""
    for r in db.execute("SELECT i.id FROM ledger_items i JOIN ledger_sources s ON s.item=i.id WHERE s.message=? AND "
                        "i.type='cleanup' AND i.merged_into IS NULL AND i.id != ?", (message, into)).fetchall():
        db.execute("UPDATE ledger_items SET merged_into=?, updated_at=? WHERE id=?", (into, now, r["id"]))
        L.history(db, r["id"], "merged", now, message, note=f"kept as L{into} (security alerts and invites always "
                  "show)")


def add_security(db, row, alert, now):
    """The security item of one message (made once). Returns (item id, created?)."""
    have = _item_of_message(db, row["id"], "security")
    if have is not None:
        return have, False
    name = alert.get("brand") if alert.get("brand") and "lure" not in alert["marks"] else \
        (row.get("sender_name") or row.get("sender") or "")
    it = {"type": "event", "subtype": "security", "who": str(row.get("sender") or "").lower()[:300],
          "who_name": str(name)[:300], "what": rules._clip(row.get("subject") or "Security alert", 200),  # noqa: SLF001
          "purpose": "", "status": "open", "confidence": "sure", "never_hide": 1,
          "thread": f"{row['source']}:{row['account']}:{row.get('thread') or ''}", "anchor_ts": row["ts"],
          "phrase": "", "date_resolved": 1, "checks": []}
    item_id = L.insert_item(db, it, row["id"], now)
    if row["ts"] < now - SECURITY_WINDOW_S:
        L.set_status(db, item_id, "done", now, note="older than 3 days when Callsheet first saw it")
    _fold_cleanup(db, row["id"], item_id, now)
    return item_id, True


def add_mail_invite(db, row, inv, now, tz):
    """The invite item of one mail (or the change it makes). Returns (item id or None, created?)."""
    have = _item_of_message(db, row["id"], "invite")
    if have is not None:
        return have, False
    opens = [L.row_dict(r) for r in db.execute(
        "SELECT * FROM ledger_items WHERE subtype='invite' AND merged_into IS NULL AND status NOT IN "
        "('declined','done','cancelled') ORDER BY id DESC LIMIT 200")]
    same = [o for o in opens if _same_title(inv["title"], o["what"])]
    if inv["cancel"]:
        for o in same:
            L.set_status(db, o["id"], "cancelled", now, row["id"], note="the invite was cancelled by mail")
            C.check_item(db, o["id"], now, tz, row["id"])
        return (same[0]["id"] if same else None), False
    when = inv["when"]
    sp = _span_of(when, tz)
    if sp and sp[1] <= now:
        return None, False                         # over already
    if not sp and row["ts"] < now - MAIL_INVITE_DAYS * 86400:
        return None, False                         # an old invite without a time
    mail_same = [o for o in same if not str(o["thread"]).startswith(CAL_KEY)]
    if mail_same:                                  # "Updated invitation": the same invite, maybe a new time
        o = mail_same[0]
        L.add_source(db, o["id"], row["id"], "updated", now)
        if when and _span_of(when, tz) != L.span(o, tz):
            L.update_fields(db, o["id"], L.when_fields(dict(when, candidates=json.dumps([]))), now)
            L.history(db, o["id"], "changed", now, row["id"], fields={"when": when["phrase"]},
                      note="updated invitation")
        return o["id"], False
    if any(str(o["thread"]).startswith(CAL_KEY) for o in same) or _on_calendar(db, inv["title"], when, now, tz) or \
            L.uid_match(db, dict(when or {}), tz, row["id"]) is not None:   # 0.24.2: its calendar part names the entry
        return None, False                         # on a calendar Callsheet reads: the calendar's invite
    if inv.get("updated"):
        return None, False                         # a change to an invite the owner already dealt with: the model reads it
    it = {"type": "ask", "subtype": "invite", "who": str(row.get("sender") or "").lower()[:300],
          "who_name": str(row.get("sender_name") or row.get("sender") or "")[:300], "what": inv["title"],
          "purpose": "", "status": "open", "confidence": "sure" if when else "likely", "never_hide": 1,
          "thread": f"{row['source']}:{row['account']}:{row.get('thread') or ''}", "anchor_ts": row["ts"],
          "checks": [] if when else [{"field": "when", "value": "", "why": "the time is in the email"}]}
    it.update(when or {"phrase": "", "date_resolved": 1})
    item_id = L.insert_item(db, it, row["id"], now)
    _fold_cleanup(db, row["id"], item_id, now)
    C.check_item(db, item_id, now, tz, row["id"])
    return item_id, True


def on_message(db, row, now, tz, security=True):
    """Both rules on one stored message, inside a messages.db write. {"security": id or None, "invite": id or None,
    "created": [ids]}. The extractor passes security=False and calls adopt_security after the model's read."""
    row = dict(row)
    out = {"security": None, "invite": None, "created": []}
    alert = security_of(row)
    if alert:
        if security:
            out["security"], new = add_security(db, row, alert, now)
            if new:
                out["created"].append(out["security"])
        return out
    inv = invite_of(row, tz)
    if inv:
        out["invite"], new = add_mail_invite(db, row, inv, now, tz)
        if new:
            out["created"].append(out["invite"])
    return out


def _merge_into(db, into, res, message, now, note):
    for i in list(res.get("created") or []):
        if i == into:
            continue
        db.execute("UPDATE ledger_items SET merged_into=?, updated_at=? WHERE id=?", (into, now, i))
        L.history(db, i, "merged", now, message, note=note)
        res["created"].remove(i)
        res["items"] = [into if x == i else x for x in res.get("items") or []]
    if into not in res.setdefault("items", []):
        res["items"].append(into)


def adopt_security(db, row, alert, res, now):
    """After the model read a security alert (extract.Extractor.read): the alert is one item, always shown. The rule's
    item when Heads.scan made it first (the model's new items join it); else the model's first new item becomes the
    security item (its words kept); else the rule makes one. res (ledger.apply's result) is changed to match.
    Returns (item id, created?)."""
    row = dict(row)
    have = _item_of_message(db, row["id"], "security")
    if have is not None:
        _merge_into(db, have, res, row["id"], now, f"kept as the security alert L{have}")
        return have, False
    made = [i for i in res.get("created") or [] if L.get_item(db, i) is not None]
    if not made:
        item_id, new = add_security(db, row, alert, now)
        if item_id not in res.setdefault("items", []):
            res["items"].append(item_id)
        if new:
            res.setdefault("created", []).append(item_id)
        return item_id, new
    keep = made[0]
    L.update_fields(db, keep, {"subtype": "security", "never_hide": 1, "confidence": "sure"}, now)
    L.history(db, keep, "changed", now, row["id"], fields={"subtype": "security"},
              note="a security alert (always shown until marked Done)")
    _merge_into(db, keep, res, row["id"], now, f"kept as the security alert L{keep}")
    if row["ts"] < now - SECURITY_WINDOW_S:
        L.set_status(db, keep, "done", now, note="older than 3 days when Callsheet first saw it")
    _fold_cleanup(db, row["id"], keep, now)
    return keep, False


def absorb(db, invite_id, res, message, now, tz):
    """After the model read an invite mail (extract.Extractor.read): its new items for the same thing join the invite
    (their time fills a missing one); res (ledger.apply's result) is changed to match."""
    inv = L.get_item(db, invite_id)
    if inv is None or inv.get("merged_into") is not None:
        return
    for i in list(res.get("created") or []):
        it = L.get_item(db, i)
        if it is None or it["type"] == "commitment" or i == invite_id:
            continue
        if L.span(inv, tz) is None and L.span(it, tz) is not None and not it["candidates"]:
            L.update_fields(db, invite_id, {k: it.get(k) for k in L._WHEN_COLS}, now)  # noqa: SLF001
            L.update_fields(db, invite_id, {"confidence": it["confidence"]}, now)
            inv = L.get_item(db, invite_id)
        db.execute("UPDATE ledger_items SET merged_into=?, updated_at=? WHERE id=?", (invite_id, now, i))
        L.history(db, i, "merged", now, message, note=f"kept as the invite L{invite_id}")
        res["created"].remove(i)
        res["items"] = [invite_id if x == i else x for x in res.get("items") or []]
        if invite_id not in res.setdefault("updated", []):
            res["updated"].append(invite_id)


# --- calendar invites --------------------------------------------------------------------------------------------------
def _my_answer(e):
    try:
        att = json.loads(e["attendees"] or "[]")
    except ValueError:
        return ""
    me = next((a for a in att if isinstance(a, dict) and a.get("self")), None)
    return str(me.get("response") or "") if me else ""


def _event_when(e):
    if e["all_day"] and e["start"]:
        d0 = str(e["start"])[:10]
        try:
            d1 = (dt.date.fromisoformat(str(e["end"])[:10]) - dt.timedelta(days=1)).isoformat() if e["end"] else d0
        except ValueError:
            d1 = d0
        return {"start_ts": None, "end_ts": None, "all_day": 1, "start_date": d0, "end_date": max(d0, d1), "time": "",
                "date_resolved": 1, "approx": 0, "tz": "", "phrase": "", "candidates": "[]"}
    return {"start_ts": e["start_ts"], "end_ts": e["end_ts"] or e["start_ts"] + 3600, "all_day": 0, "start_date": None,
            "end_date": None, "time": "", "date_resolved": 1, "approx": 0, "tz": "", "phrase": "", "candidates": "[]"}


def sync_invites(db, now, tz):
    """Calendar invites to answer -> open invite items; answered, cancelled or past ones close. Returns {"created",
    "closed", "new_clashes"}."""
    out = {"created": [], "closed": [], "new_clashes": []}
    need = {}
    nd = N.Need(db)
    for e in db.execute("SELECT * FROM events WHERE status != 'cancelled' AND start_ts IS NOT NULL AND "
                        "coalesce(end_ts, start_ts) > ? AND start_ts < ? ORDER BY start_ts, id",
                        (now, now + INVITE_DAYS * 86400)).fetchall():
        if str((nd.me_of(e) or {}).get("response") or "") != "needsAction" or nd.shared(e):
            continue                                  # 0.24.0: a shared calendar's `self` is the calendar, not him
        key = CAL_KEY + (e["ical_uid"] or f"{e['account']}:{e['event_id']}")
        if key in need:
            need[key][1] += 1
        else:
            need[key] = [e, 1]
    have = {}
    for r in db.execute("SELECT * FROM ledger_items WHERE thread LIKE ? AND merged_into IS NULL ORDER BY id",
                        (CAL_KEY + "%",)).fetchall():
        have.setdefault(r["thread"], []).append(L.row_dict(r))
    for key, (e, n) in need.items():
        mine = have.get(key) or []
        cur = next((o for o in mine if L.is_active(o)), None)
        title = rules._clip((e["title"] or "").strip() or "(No title)", 200)  # noqa: SLF001
        purpose = "repeats" if n > 1 else ""
        when = _event_when(e)
        if cur is None:
            if any(o["cal_ref"] == e["id"] for o in mine):
                continue                              # the owner marked this one done here: it stays done
            it = dict(when, type="ask", subtype="invite", who="", who_name="", what=title, purpose=purpose,
                      status="open", confidence="sure", never_hide=1, thread=key, anchor_ts=now, checks=[])
            it["candidates"] = []
            item_id = L.insert_item(db, it, None, now)
            L.update_fields(db, item_id, {"cal_ref": e["id"]}, now)
            out["created"].append(item_id)
        else:
            item_id = cur["id"]
            upd = {k: v for k, v in dict(when, what=title, purpose=purpose, cal_ref=e["id"]).items()
                   if k != "candidates" and cur.get(k) != v}
            if upd:
                L.update_fields(db, item_id, upd, now)
                L.history(db, item_id, "changed", now, fields={k: v for k, v in upd.items() if k != "cal_ref"},
                          note="the calendar changed")
        _fold_mail(db, item_id, title, e, now, tz)
        out["new_clashes"] += C.check_item(db, item_id, now, tz)[0]
    for key, mine in have.items():
        for o in mine:
            if not L.is_active(o) or key in need:
                continue
            e = db.execute("SELECT * FROM events WHERE id=?", (o["cal_ref"],)).fetchone() if o["cal_ref"] else None
            ans = str((nd.me_of(e) or {}).get("response") or "") if e else ""
            if e is None:
                st, note = "cancelled", "no longer on the calendar"
            elif e["status"] == "cancelled":
                st, note = "cancelled", "the event was cancelled"
            elif (nd.shared(e) or nd.me_of(e) is None) and (e["end_ts"] or e["start_ts"] or 0) > now:
                # 0.24.0 (B1): an invite on a shared calendar stays open (never closed or hidden: the rules); Today shows
                # it as a heads-up from its calendar entry, never as an RSVP ask (shared_invite_item)
                continue
            elif nd.shared(e) or nd.me_of(e) is None:
                st, note = "done", "the time has passed"
            elif ans == "declined":
                st, note = "declined", "you said no in Calendar"
            elif ans in ("accepted", "tentative"):
                st, note = "done", "you answered in Calendar" + (" (maybe)" if ans == "tentative" else "")
            else:
                st, note = "done", "the time has passed"
            L.set_status(db, o["id"], st, now, note=note)
            C.check_item(db, o["id"], now, tz)
            out["closed"].append(o["id"])
    for r in db.execute("SELECT * FROM ledger_items WHERE subtype='invite' AND merged_into IS NULL AND status NOT IN "
                        "('declined','done','cancelled') AND thread NOT LIKE ?", (CAL_KEY + "%",)).fetchall():
        o = L.row_dict(r)                             # a mail invite: answered on a calendar, or over
        sp = L.span(o, tz)
        if sp and sp[1] <= now:
            L.set_status(db, o["id"], "done", now, note="the time has passed")
            C.check_item(db, o["id"], now, tz)
            out["closed"].append(o["id"])
        elif _on_calendar(db, o["what"], L.when_fields(o) if sp else None, now, tz):
            L.set_status(db, o["id"], "done", now, note="it is on your calendar")
            C.check_item(db, o["id"], now, tz)
            out["closed"].append(o["id"])
    return out


def shared_invite_item(nd, db, it):
    """0.24.0 (B1): an invite item whose calendar entry is on a shared calendar he isn't a guest on: shown as that
    entry's heads-up, never as an RSVP ask in Needs you."""
    if it.get("subtype") != "invite" or it.get("cal_ref") is None:
        return False
    e = db.execute("SELECT * FROM events WHERE id=?", (it["cal_ref"],)).fetchone()
    return e is not None and e["status"] != "cancelled" and (nd.shared(e) or nd.me_of(e) is None)


def _fold_mail(db, item_id, title, e, now, tz):
    """Open mail invites for this calendar invite join it (the mail is one of its sources)."""
    for r in db.execute("SELECT * FROM ledger_items WHERE subtype='invite' AND merged_into IS NULL AND status NOT IN "
                        "('declined','done','cancelled') AND thread NOT LIKE ? AND id != ?",
                        (CAL_KEY + "%", item_id)).fetchall():
        o = L.row_dict(r)
        sp = L.span(o, tz)
        if not _same_title(title, o["what"]) or (sp and abs(sp[0] - (e["start_ts"] or 0)) > 86400):
            continue
        db.execute("UPDATE ledger_items SET merged_into=?, updated_at=? WHERE id=?", (item_id, now, o["id"]))
        for s in db.execute("SELECT message FROM ledger_sources WHERE item=?", (o["id"],)).fetchall():
            L.add_source(db, item_id, s["message"], "merged", now)
        L.history(db, o["id"], "merged", now, note=f"on your calendar: L{item_id}")
        C.check_item(db, o["id"], now, tz)


# --- reports into messages.db -------------------------------------------------------------------------------------------
def report_message(x):
    """One Reports row (connectors.Connectors.report_items) -> a messages.db record."""
    return {"source": "reports", "account": str(x["connector"]), "msg_id": str(x["source_id"]),
            "thread": str(x["source_id"]), "direction": "in", "sender": f"report:{x['connector']}",
            "sender_name": str(x.get("label") or "Report"), "recipients": [], "ts": float(x["ts"] or 0),
            "subject": str(x.get("title") or ""), "text": str(x.get("body") or ""), "labels": ["report"],
            "channel": "", "link": "", "attachments": []}


# --- the loop -------------------------------------------------------------------------------------------------------
class Heads:
    """The deterministic pass (extract.Runner's hooks, every minute, model or not): new messages through both rules,
    the calendar's invites, new Reports into the store, and the phone alerts for what's new."""

    def __init__(self, ms, tz, clock, alerter=None, reports=None, log=None):
        self.ms, self.tz, self.clock = ms, tz, clock
        self.alerter = alerter
        self.reports = reports                 # reports(after_id) -> [rows] (connectors.report_items) or None
        self.log = log or (lambda line: None)
        L.ensure(ms)

    def scan(self, now=None):
        """Messages not looked at yet (by id) that are new enough to matter. Returns the created item ids."""
        now = self.clock() if now is None else now
        tz = self.tz()
        try:
            after = int(self.ms.meta("heads_cursor", "0") or 0)
        except (TypeError, ValueError):
            after = 0
        created = []
        while True:
            rows = self.ms._read("SELECT * FROM messages WHERE id > ? AND same_as IS NULL ORDER BY id LIMIT ?",  # noqa: SLF001
                                 (after, SCAN_BATCH))
            if not rows:
                break
            top = rows[-1]["id"]

            def run(db, rows=rows):
                got = []
                for r in rows:
                    if r["ts"] < now - max(SECURITY_WINDOW_S, MAIL_INVITE_DAYS * 86400) or r["deleted_at"] is not None:
                        continue                  # the extractor's own read-back takes the older ones
                    got += on_message(db, r, now, tz)["created"]
                return got
            created += self.ms.write(run)
            after = top
            self.ms.set_meta("heads_cursor", str(after))
            if len(rows) < SCAN_BATCH:
                break
        return created

    def copy_reports(self):
        if self.reports is None:
            return 0
        try:
            after = int(self.ms.meta("heads_reports", "0") or 0)
        except (TypeError, ValueError):
            after = 0
        rows = self.reports(after) or []
        keep = [report_message(x) for x in rows if not x.get("flagged") and x.get("connector") is not None]
        if keep:
            self.ms.upsert_messages(keep)
        if rows:
            self.ms.set_meta("heads_reports", str(max(x["id"] for x in rows)))
        return len(keep)

    def tick(self, now=None):
        now = self.clock() if now is None else now
        tz = self.tz()
        try:
            self.copy_reports()
        except Exception as e:  # noqa: BLE001 (reports never stop the rest)
            self.log(f"Callsheet heads: reports copy failed ({type(e).__name__})")
        created = self.scan(now)
        inv = self.ms.write(lambda db: sync_invites(db, now, tz))
        if self.alerter is not None and (created or inv["new_clashes"]):
            try:
                self.alerter.notify({"created": created, "conflicts": {"new": inv["new_clashes"]}})
            except Exception as e:  # noqa: BLE001 (alerts never stop the reading)
                self.log(f"Callsheet heads: alerts failed ({type(e).__name__})")
        return {"created": created, "invites": inv}
