"""0.16.0 (rework stage 2): clashes on any date, follow-ups, and the alert hooks.

Every new or changed time-bound ledger item is checked at once against the calendar (messages.db `events`) and the
other open ledger items, on any date. A clash is an overlap of two plausible claims on the owner's time (an ask for
the owner's time, an accepted commitment, an event), or a tight gap next to a job or a flight. Clashes stay open until a side
is cancelled, declined, done (without a yes that carries it on) or moved away.

Follow-ups: when the owner answers one side of a clash (the owner's yes closes an ask and starts a commitment), the other side's
person "hasn't heard from you" until an outgoing message to that person is seen, on any source.

Alerts (push to the owner's phones): a new clash, and a new ask from a VIP. Quiet hours hold an alert until they end
(VIPs come through by default); the hourly cap holds the rest. Alert text is names, sources and times only, never
message text.
"""
import datetime as dt
import json
import re

import ledger as L
import readrules

TIGHT_CALL_S = 60 * 60          # a job's wrap or call time this close to something else: tight
TIGHT_TRAVEL_S = 150 * 60       # a flight this close to something else: tight
NO_BLOCK = frozenset({"birthday", "bill", "deadline", "receivable", "delivery", "payment", "lead", "lodging", "hotel",
                      "document", "reply", "decision", "subscription", "package", "security"})
# 0.22.x: "security" (a sign-in alert's time is when it happened, not the owner's time: 9 of 9 extra clashes on the real-mail
# set). Chat items that can't make a clash (README R8: both sides must be real items; the local model's chat items that aren't
# are mostly "likely" or from the owner's own messages): see _chat_side.
# Measured on the local model's round-6 answers and not taken (README R8/M4 and the owner's tests keep these clashes): False
# would also stop two items of one chat from clashing (real Signal extra clashes 7 -> 0 of these, but the owner's question and
# their other time in one chat are a clash), and a "likely" chat item from clashing (synthetic oracle: 2 of 7 likely
# clashes lost; M4: a likely block still makes a likely grouped clash).
SAME_CHAT = True
CHAT_LIKELY = True
OWN_CHAT_KINDS = ("commitment",)   # what the owner's own chat message makes that can clash (None: anything), besides the
#                                    days away the owner announces (an all-day event of the owner's)
CHAT_SOURCES = ("signal", "slack", "sms", "sms_live")
CALL = frozenset({"job", "gig"})
TRAVEL = frozenset({"flight", "train", "trip"})   # a timed trip item is its departure
_SOFT_EVENT = re.compile(r"\b(birthday|bday|holiday|reminder|anniversary)\b", re.I)
# A whole-day side never makes a hard time clash. It makes a soft "away" clash with a timed ledger item only when it
# says the owner is away or off that day (owner's rule, 2026-11-11): a busy (opaque) all-day calendar entry for time
# off or out of office, a calendar hold, a trip of more than a day, or the owner's own "at the lab all day".
_OFF = re.compile(r"\b(out of (the )?office|ooo|time off|days? off|pto|vacation|annual leave|on leave|off work)\b", re.I)
_TRIP = re.compile(r"\b(trip|travel\w*|away|vacation|flight)\b", re.I)
_HOLD = re.compile(r"^\W*(hold|booked)\b", re.I)
OUT_SUBTYPES = frozenset({"time_off", "job", "gig", "travel"})   # M3: out blocks of the owner's that work like a trip
AWAY = {"off": "you're off that day", "away": "you're away then", "booked": "you're booked that day",
        "busy": "you're busy all day", "held": "your calendar holds that day"}
# 0.22.x: an ask for the owner's time or presence ("can someone be there Thursday 2:40?", "are you free Tuesday?") meets an all-day
# calendar entry that may take the day: several days long, or tentative (the owner's maybe, or the entry's own status)
_AVAIL = re.compile(r"\b(?:be (?:home|around|there|in town|available|free)|(?:anyone|someone|somebody)\b[^.?!\n]{0,30}"
                    r"\b(?:home|around|there)|are you (?:free|around|home|available|in town)|availab\w*)\b", re.I)
SWEEP_DAYS = 400


def blocks(it):
    """Does this item hold the owner's time?"""
    if not L.holds_time(it) or it["type"] == "cleanup":
        return False
    if it["type"] == "ask" and it["status"] == "waiting_on_them":
        return False
    sub = it.get("subtype") or ""
    if sub in NO_BLOCK:
        return sub == "birthday" and not it["all_day"] and it.get("start_ts") is not None   # a timed party does
    if it["type"] == "commitment" and sub == "deadline":
        return False
    return it.get("start_ts") is not None or bool(it.get("all_day") and it.get("start_date")) or bool(it["candidates"])


def _free(e):
    """A calendar entry marked free (transparency "transparent"): it never takes the owner's time."""
    return (e["transparency"] if "transparency" in e.keys() else "") == "transparent"


def _my_response(e):
    try:
        atts = json.loads(e["attendees"] or "[]") if isinstance(e["attendees"], str) else e["attendees"] or []
    except (ValueError, KeyError, IndexError):
        return ""
    return next((str(a.get("response") or "") for a in atts if isinstance(a, dict) and a.get("self")), "")


def tentative(e):
    """The entry is tentative: its own status, or the owner's maybe."""
    return e["status"] == "tentative" or _my_response(e) == "tentative"


def event_blocks(e):
    return e["status"] != "cancelled" and e["start_ts"] is not None and not _free(e) and not (
        e["all_day"] and _SOFT_EVENT.search(e["title"] or ""))


def away_of(kind, x, source=""):
    """Why a whole-day side takes the owner's day ("off", "away", "booked", "busy") or None. Holidays, birthdays,
    reminders, "this week" asks, dated commitments, other people's trips and plain all-day blocks don't."""
    if kind == "event":
        x = dict(x)
        if not x["all_day"] or (x.get("transparency") or "") == "transparent" or x["status"] == "cancelled":
            return None
        title = x["title"] or ""
        if (x.get("event_type") or "") == "outOfOffice" or _OFF.search(title):
            return "off"
        days = ((x["end_ts"] or x["start_ts"]) - x["start_ts"]) / 86400
        if days > 1.5 and _TRIP.search(title):
            return "away"
        return "booked" if _HOLD.search(title) else None
    if not x["all_day"] or x["candidates"] or not x.get("start_date"):
        return None
    multi = (x.get("end_date") or x["start_date"]) > x["start_date"]
    if multi and out_block(x) and (x["who"] == L.OWNER or source == "gmail"):
        return "away"                    # the owner's trip: the owner's own word or a booking by mail, not a friend's trip in a chat
    return "busy" if x["type"] == "event" and x["who"] == L.OWNER else None


def out_block(x):
    """A trip, or (acceptance rule M3, 2026-11-13) a time-off or "out" block that works like one: a gig, a
    job or travel of the owner's (an event or the owner's commitment by its subtype), or time off / out of office by its words. Of
    more than a day (away_of), it is one grouped clash with the busy timed entries and open invites during it."""
    sub = x.get("subtype") or ""
    if sub == "trip":
        return True
    if x["type"] not in ("event", "commitment"):
        return False
    return sub in OUT_SUBTYPES or _OFF.search(f"{x.get('what') or ''} {x.get('purpose') or ''}") is not None


def _held(e):
    """An all-day calendar entry that may take the owner's day: tentative, or more than a day long (free entries,
    holidays and reminders never get here: event_blocks)."""
    if not e["all_day"] or e["start_ts"] is None:
        return False
    return tentative(e) or ((e["end_ts"] or e["start_ts"]) - e["start_ts"]) / 86400 > 1.5


def _during_trip(e):
    """A timed calendar entry that clashes with the owner's trip or stay of more than a day (0.22.x, owner's rule 2026-11-13):
    any busy one inside it, whatever its attendees (a meeting, a dentist visit, a haircut), but not the trip's own
    travel entry. Free (transparent) and cancelled entries never get here (event_blocks); all-day entries and ones the owner
    declined don't count."""
    if e["all_day"] or _TRIP.search(e["title"] or ""):
        return False
    return _my_response(e) != "declined"


def _entry_of(db, o):
    """The calendar entry a timed ledger item stands for (its cal_ref: an invite, or a mail that repeats the entry), or
    None. In the owner's trip's grouped clash the item counts as that entry."""
    if o.get("cal_ref") is None or o.get("all_day") or o.get("candidates"):
        return None
    return db.execute("SELECT * FROM events WHERE id=?", (o["cal_ref"],)).fetchone()


def _set_group(db, it, item_id, group, now, message):
    """0.22.x (the acceptance rules for the owner's rule 2026-11-13): the owner's trip or stay of more than a day against the
    busy timed calendar entries during it is ONE likely clash that lists each entry by id (`entries`, in time order;
    b_ref is the first). group: {event id: (slot start, slot end, entry start, sure?)}. An entry that clears (declined,
    cancelled, moved out, made free) leaves the list; separate clashes are never opened for them. A clash per entry
    from 0.22.x before the ruling becomes the group (it keeps its id and alert) and the rest close as "grouped".
    M3 (2026-11-13): the owner's time-off or out block of more than a day groups the same way (away_of, out_block). M4: the
    grouped clash is likely unless every item in it is sure (the trip, not tentative, and each entry).
    Returns (conflict id, new?, ids closed as grouped)."""
    order = sorted(group, key=lambda r: (group[r][2], r))
    slot = (min(v[0] for v in group.values()), max(v[1] for v in group.values()))
    conf = "sure" if it["confidence"] == "sure" and it["status"] != "tentative" and all(
        v[3] for v in group.values()) else "likely"
    why = _why(it, f"{len(order)} calendar {'entry' if len(order) == 1 else 'entries'} during it", "away", "away")
    rows = db.execute("SELECT * FROM ledger_conflicts WHERE state='open' AND a_kind='item' AND a_ref=? AND "
                      "b_kind='event' AND kind='away' ORDER BY (entries = ''), id", (item_id,)).fetchall()
    rows = [r for r in rows if r["entries"] or r["b_ref"] in group]
    closed = []
    for r in rows[1:]:
        _resolve(db, r["id"], "grouped", now)
        closed.append(r["id"])
    if rows:
        db.execute("UPDATE ledger_conflicts SET b_ref=?, entries=?, slot_start=?, slot_end=?, confidence=?, "
                   "candidate=NULL, why=?, updated_at=? WHERE id=?",
                   (order[0], json.dumps(order), slot[0], slot[1], conf, why, now, rows[0]["id"]))
        return rows[0]["id"], False, closed
    cur = db.execute(
        "INSERT INTO ledger_conflicts (a_kind, a_ref, b_kind, b_ref, slot_start, slot_end, kind, confidence, why, "
        "state, message, created_at, updated_at, entries) VALUES ('item',?,'event',?,?,?,'away',?,?,'open',?,?,"
        "?,?)",
        (item_id, order[0], slot[0], slot[1], conf, why, message, now, now, json.dumps(order)))
    return cur.lastrowid, True, closed


def _groups_listing(db, event_id, but):
    """The trips whose open grouped clash lists this calendar entry."""
    return [r["a_ref"] for r in db.execute(
        "SELECT a_ref, entries FROM ledger_conflicts WHERE state='open' AND entries != '' AND a_kind='item' AND "
        "a_ref != ?", (but,)).fetchall() if event_id in L.group_entries(r)]


def regroup_event(db, event_id, now, tz):
    """0.22.x: re-check, at once, the trips whose grouped clash lists this calendar entry or whose days it falls on (an
    RSVP the owner pressed in Callsheet: a No takes the entry off the list, a Yes or Maybe puts it on), instead of waiting
    for the next sweep. Returns (new conflict ids, resolved ids)."""
    trips = set(_groups_listing(db, event_id, 0))
    e = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if e is not None and e["start_ts"] is not None and not e["all_day"]:
        d0 = dt.datetime.fromtimestamp(e["start_ts"], tz).date().isoformat()
        d1 = dt.datetime.fromtimestamp(max(e["start_ts"], (e["end_ts"] or e["start_ts"]) - 1), tz).date().isoformat()
        trips |= {r["id"] for r in db.execute(     # the owner's trips and (M3) time-off / out blocks on its days
            "SELECT * FROM ledger_items WHERE merged_into IS NULL AND all_day=1 AND status NOT IN "
            "('declined','done','cancelled') AND start_date <= ? AND coalesce(nullif(end_date, ''), start_date) >= ?",
            (d1, d0)).fetchall() if away_of("item", L.row_dict(r), _created_source(db, r["id"])) == "away"}
    new, resolved = [], []
    for t in sorted(trips):
        n, r = check_item(db, t, now, tz, regroup=False)
        new += n
        resolved += r
    return new, resolved


def _asks_presence(db, it, memo):
    """An open ask for the owner's time or presence ("can someone be there …", "are you free …"), in its words or its
    message's. memo: a one-element list that keeps the answer for one check_item."""
    if it["type"] != "ask" or it["who"] == L.OWNER:
        return False
    if not memo:
        text = f"{it.get('what') or ''} {it.get('purpose') or ''} {_source_text(db, it['id'], 'in')[0][:1500]}"
        memo.append(_AVAIL.search(text) is not None)
    return memo[0]


def _created_source(db, item_id):
    r = db.execute("SELECT m.source FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE s.item=? AND "
                   "s.role='created' LIMIT 1", (item_id,)).fetchone()
    return r["source"] if r else ""


def event_span(e):
    return e["start_ts"], (e["end_ts"] if e["end_ts"] and e["end_ts"] > e["start_ts"] else e["start_ts"] + 3600)


# 0.22.x (real Signal): "Wednesday night", "tonight", "this evening" with no clock time holds the evening, not the one
# hour the read starts it at (20:00): a clash with anything in that evening is likely, never sure.
_EVENING = re.compile(r"\b(?:tonight|evening|night)\b", re.I)
_CLOCK = re.compile(r"\d\s*(?:[ap]\.?m\b|[:.]\d{2})|\bat \d{1,2}\b|\b(?:noon|midnight|o'?clock)\b", re.I)
EVENING_HOURS = (18, 21)


def _vague_evening(it, tz):
    """(start, end) of the evening a vague "tonight" / "Friday night" item stands for, else None."""
    if it.get("all_day") or it.get("candidates") or it.get("end_ts") or it.get("start_ts") is None:
        return None
    ph = it.get("phrase") or ""
    if not _EVENING.search(ph) or _CLOCK.search(ph):
        return None
    z = L.zone(tz) if isinstance(tz, str) else tz
    t = dt.datetime.fromtimestamp(it["start_ts"], z)
    if not EVENING_HOURS[0] - 1 <= t.hour < EVENING_HOURS[1] + 2:
        return None
    a = t.replace(hour=EVENING_HOURS[0], minute=0, second=0, microsecond=0).timestamp()
    b = t.replace(hour=EVENING_HOURS[1], minute=0, second=0, microsecond=0).timestamp()
    s = it["start_ts"]
    return min(s, a), max(s + L.EVENT_MIN * 60, b)


def item_spans(it, tz, db=None):
    """[(start, end, candidate index or None)]. A whole-day item that repeats a calendar entry takes the entry's days
    (the message said "next week", the calendar has the exact days). 0.22.x: a vague evening holds the evening."""
    if db is not None and it.get("cal_ref") is not None and it.get("all_day") and not it["candidates"]:
        e = db.execute("SELECT start_ts, end_ts, all_day, status FROM events WHERE id=?", (it["cal_ref"],)).fetchone()
        if e and e["all_day"] and e["status"] != "cancelled" and e["start_ts"] is not None:
            return [(e["start_ts"], e["end_ts"] if (e["end_ts"] or 0) > e["start_ts"] else e["start_ts"] + 86400, None)]
    if it["candidates"]:
        out = []
        for i, c in enumerate(it["candidates"]):
            sp = L.span(it, tz, c)
            if sp:
                out.append((sp[0], sp[1], i))
        return out
    sp = _vague_evening(it, tz) or L.span(it, tz)
    return [(sp[0], sp[1], None)] if sp else []


def _firm(it, tz=None):
    return it["confidence"] == "sure" and not it["all_day"] and not it["approx"] and it["status"] != "tentative" and \
        (tz is None or _vague_evening(it, tz) is None)


def _relation(sa, sb, a_sub, b_sub):
    """("overlap", slot) / ("tight", slot) / None for two spans."""
    if sa[0] < sb[1] and sb[0] < sa[1]:
        return "overlap", (max(sa[0], sb[0]), min(sa[1], sb[1]))
    first, second = (sa, sb) if sa[1] <= sb[0] else (sb, sa)
    gap = second[0] - first[1]
    subs = {a_sub, b_sub}
    limit = TIGHT_TRAVEL_S if subs & TRAVEL else TIGHT_CALL_S if subs & CALL else -1
    if 0 <= gap <= limit:
        return "tight", (first[1], second[0])
    return None


def _chat_side(db, it):
    """(source, direction) of the message that made the item ("", "") when none."""
    r = db.execute("SELECT m.source, m.direction FROM ledger_sources s JOIN messages m ON m.id=s.message "
                   "WHERE s.item=? AND s.role='created' LIMIT 1", (it["id"],)).fetchone()
    return (r["source"], r["direction"]) if r else ("", "")


def chat_clashes(db, it):
    """0.22.x: can this item make a clash at all? Not a "likely" chat item (CHAT_LIKELY off), nor what the owner's own chat
    message made other than OWN_CHAT_KINDS and the days away the owner announces (an all-day event)."""
    src, direction = _chat_side(db, it)
    if src not in CHAT_SOURCES:
        return True
    if not CHAT_LIKELY and it.get("confidence") != "sure":
        return False
    return direction != "out" or OWN_CHAT_KINDS is None or it["type"] in OWN_CHAT_KINDS or (
        it["type"] == "event" and bool(it.get("all_day")))


def _related(db, a, b):
    """Two items that can't clash with each other: the same message made both, one is part of or follows the other."""
    if a["id"] == b["id"]:
        return True
    if not SAME_CHAT and a.get("thread") and a.get("thread") == b.get("thread") and \
            _chat_side(db, a)[0] in CHAT_SOURCES and _chat_side(db, b)[0] in CHAT_SOURCES:
        return True
    if a.get("parent") == b["id"] or b.get("parent") == a["id"]:
        return True
    if a.get("predecessor") == b["id"] or b.get("predecessor") == a["id"]:
        return True
    if a["who"] and a["who"] == b["who"] and a["who"] != L.OWNER and (a["thread"] == b["thread"] or (
            a["type"] == b["type"] and L.similar(a["what"], b["what"]) >= 0.5)):
        return True                      # one person's plan in one conversation doesn't clash with itself
    if a["thread"] and a["thread"] == b["thread"] and L.OWNER not in (a["who"], b["who"]) and \
            a.get("start_ts") is not None and b.get("start_ts") is not None and abs(a["start_ts"] - b["start_ts"]) <= 900:
        return True                      # two people in one conversation naming the same time: the same plan
    for x, y in ((a, b), (b, a)):        # "Ines mentioned you might do pickups Monday": the same plan
        name = (y.get("who_name") or "").split(" ")[0]
        if y["who"] != L.OWNER and len(name) > 1 and any(
                re.search(r"\b" + re.escape(name) + r"\b", r["text"] or "", re.I) for r in db.execute(
                    "SELECT m.text FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE s.item=? AND "
                    "s.role='created' AND m.direction='in'", (x["id"],))):
            return True
    for x, y in ((a, b), (b, a)):        # 0.22.0: the owner's yes in the thread names the time of the ask it answers
        # 0.22.x: and the owner's own question or offer of a time and theirs at that time are one plan being agreed
        if x["type"] in ("commitment", "ask") and x["who"] == L.OWNER and y["who"] not in ("", L.OWNER) and x["thread"] and \
                x["thread"] == y["thread"] and x.get("start_ts") is not None and y.get("start_ts") is not None and \
                abs(x["start_ts"] - y["start_ts"]) <= 900:
            return True
    if _made_together(db, a["id"], b["id"]):
        return True
    for x, y in ((a, b), (b, a)):        # 0.22.0: the owner's yes to an ask and what came with the ask (its flight)
        if x.get("predecessor") and _made_together(db, x["predecessor"], y["id"]):
            return True
    return False


def _made_together(db, a_id, b_id):
    return db.execute("SELECT 1 FROM ledger_sources x JOIN ledger_sources y ON x.message=y.message "
                      "WHERE x.item=? AND y.item=? AND x.role='created' AND y.role='created' LIMIT 1",
                      (a_id, b_id)).fetchone() is not None


def _pair(db, a_kind, a_ref, b_kind, b_ref):
    return db.execute("SELECT * FROM ledger_conflicts WHERE state='open' AND ((a_kind=? AND a_ref=? AND b_kind=? AND "
                      "b_ref=?) OR (a_kind=? AND a_ref=? AND b_kind=? AND b_ref=?))",
                      (a_kind, a_ref, b_kind, b_ref, b_kind, b_ref, a_kind, a_ref)).fetchone()


def _resolve(db, cid, why, now):
    db.execute("UPDATE ledger_conflicts SET state='resolved', resolved_at=?, resolved_why=?, updated_at=? WHERE id=?",
               (now, why, now, cid))


def _why(it, other_desc, kind, away=None):
    who = it["who_name"] or ("you" if it["who"] == L.OWNER else it["who"])
    if away:
        return f"{who}: {it['what']} and {other_desc}: {AWAY[away]}"[:300]
    return f"{who}: {it['what']} {'overlaps' if kind == 'overlap' else 'is right next to'} {other_desc}"[:300]


def _invite_events(db):
    """Calendar entries an open invite item stands for (heads.py: the clash is the invite's, not the entry's)."""
    return {r["cal_ref"] for r in db.execute(
        "SELECT cal_ref FROM ledger_items WHERE subtype='invite' AND cal_ref IS NOT NULL AND merged_into IS NULL AND "
        "status NOT IN ('declined','done','cancelled')").fetchall()}


def check_item(db, item_id, now, tz, message=None, regroup=True):
    """(new conflict ids, resolved ids) after item_id was created or changed. regroup: also re-check the trips whose
    grouped clash this item's calendar entry is (or should be) in."""
    it = L.get_item(db, item_id)
    new, resolved = [], []
    if it is None:
        return new, resolved
    mine = item_spans(it, tz, db) if blocks(it) and chat_clashes(db, it) else []
    keep = set()
    trips = set()                         # 0.22.x: trips whose grouped clash to re-check (regroup)
    if mine:
        lo = min(s for s, _, _ in mine) - TIGHT_TRAVEL_S - 86400
        hi = max(e for _, e, _ in mine) + TIGHT_TRAVEL_S + 86400
        others = [L.row_dict(r) for r in db.execute(
            "SELECT * FROM ledger_items WHERE id != ? AND merged_into IS NULL AND status NOT IN "
            "('declined','done','cancelled','postponed') AND ((start_ts BETWEEN ? AND ?) OR all_day=1 OR "
            "candidates != '[]')", (item_id, lo - 86400 * 30, hi))]
        sides = []
        stands = _invite_events(db)       # 0.21.0: an unanswered invite stands for its calendar entry (heads.py)
        for o in others:
            if not blocks(o) or _related(db, it, o) or (o.get("cal_ref") is not None and o["cal_ref"] == it.get(
                    "cal_ref")) or not chat_clashes(db, o):
                continue
            for s, e, _ in item_spans(o, tz, db):
                sides.append(("item", o["id"], (s, e), o.get("subtype") or "", o))
        for e in db.execute("SELECT * FROM events WHERE start_ts < ? AND coalesce(end_ts, start_ts) > ?", (hi, lo)):
            if not event_blocks(e) or e["id"] == it.get("cal_ref") or e["id"] in stands:
                continue
            if it.get("parent"):
                par = L.get_item(db, it["parent"])
                if par and par.get("cal_ref") == e["id"]:
                    continue
            sides.append(("event", e["id"], event_span(e), "", e))
        my_day = bool(it["all_day"]) and not it["candidates"]
        my_away = away_of("item", it, _created_source(db, item_id)) if my_day else None
        trip = my_day and my_away == "away"
        group = {}                        # 0.22.x: the owner's trip's busy timed calendar entries during it, one clash
        said_no = {r["cal_ref"] for r in db.execute(
            "SELECT cal_ref FROM ledger_items WHERE cal_ref IS NOT NULL AND merged_into IS NULL AND "
            "status='declined'")} if trip else set()
        mine_entry = _entry_of(db, it) if not my_day else None
        avail = []
        for kind, ref, osp, osub, other in sides:
            their_day = bool(other["all_day"]) and not (kind == "item" and other["candidates"])
            away = None
            if trip and not their_day:
                e = other if kind == "event" else _entry_of(db, other)
                if kind == "event" or e is not None:
                    # the owner's trip of more than a day against a busy timed calendar entry during it (0.22.x, owner's rule
                    # 2026-11-13): one grouped clash for the trip; an item that stands for its entry (an invite, a
                    # mail that repeats it) counts as the entry; not one the owner declined or the trip's own entry
                    if e["id"] not in said_no and e["id"] != it.get("cal_ref") and event_blocks(e) and \
                            _during_trip(e):
                        esp = event_span(e)
                        # M4: an entry is sure unless it is tentative, or only an item that isn't sure stands for it
                        sure = not tentative(e) and (kind == "event" or other.get("confidence") == "sure")
                        for s, en, _ in mine:
                            if s < esp[1] and esp[0] < en:
                                was = group.get(e["id"])
                                group[e["id"]] = (max(s, esp[0]), min(en, esp[1]), esp[0],
                                                  sure or bool(was and was[3]))
                                break
                    continue
            if my_day or their_day:
                if my_day == their_day:
                    continue                 # two whole days
                if my_day and kind == "event":
                    continue                 # a whole day against the calendar's own entries (the owner's trip: above)
                away = my_away if my_day else away_of(kind, other, _created_source(db, ref) if kind == "item" else "")
                if away == "away" and kind == "item" and mine_entry is not None:
                    trips.add(ref)           # this item stands for a calendar entry during the owner's trip: the trip's
                    continue                 # grouped clash lists it (regroup below), no clash of its own
                if not away and kind == "event" and _held(other) and _asks_presence(db, it, avail):
                    away = "held"            # 0.22.x: "can someone be there Thursday?" and a tentative or multi-day entry
                if not away:
                    continue
            for s, e, cand in mine:
                if away:
                    rel = ("away", (max(s, osp[0]), min(e, osp[1]))) if s < osp[1] and osp[0] < e else None
                else:
                    rel = _relation((s, e), osp, it.get("subtype") or "", osub)
                if rel is None:
                    continue
                k, slot = rel
                if k == "tight" and kind == "item" and it["thread"] and it["thread"] == other["thread"] and \
                        L.OWNER not in (it["who"], other["who"]):
                    break                    # 0.22.x: two people in one chat on times next to each other (a shoot
                    #                          and its call or wrap): one plan's logistics, not a tight squeeze
                if kind == "item":
                    firm = _firm(it, tz) and _firm(other, tz)
                    desc = f"{other['who_name'] or other['who']}: {other['what']}"
                else:
                    firm = _firm(it, tz) and not other["all_day"] and other["status"] != "tentative"
                    desc = f"calendar: {other['title']}"
                conf = "sure" if firm and k == "overlap" and cand is None else "likely"
                old = _pair(db, "item", item_id, kind, ref)
                if old is not None:
                    db.execute("UPDATE ledger_conflicts SET slot_start=?, slot_end=?, kind=?, confidence=?, candidate=?, "
                               "updated_at=? WHERE id=?", (slot[0], slot[1], k, conf, cand, now, old["id"]))
                    keep.add(old["id"])
                else:
                    cur = db.execute(
                        "INSERT INTO ledger_conflicts (a_kind, a_ref, b_kind, b_ref, slot_start, slot_end, kind, "
                        "confidence, candidate, why, state, message, created_at, updated_at) "
                        "VALUES ('item',?,?,?,?,?,?,?,?,?,'open',?,?,?)",
                        (item_id, kind, ref, slot[0], slot[1], k, conf, cand, _why(it, desc, k, away), message, now, now))
                    new.append(cur.lastrowid)
                    keep.add(cur.lastrowid)
                break
        if group:
            gid, fresh, closed = _set_group(db, it, item_id, group, now, message)
            keep.add(gid)
            new += [gid] if fresh else []
            resolved += closed
            keep |= set(closed)
    for c in db.execute("SELECT * FROM ledger_conflicts WHERE state='open' AND ((a_kind='item' AND a_ref=?) OR "
                        "(b_kind='item' AND b_ref=?))", (item_id, item_id)).fetchall():
        if c["id"] in keep:
            continue
        why = "moved away" if mine else f"{it['status']}" if not L.holds_time(it) else "no longer holds time"
        if mine and c["entries"] and c["a_kind"] == "item" and c["a_ref"] == item_id and away_of(
                "item", it, _created_source(db, item_id)) == "away":
            why = "entries cleared"          # the owner's trip stays; no calendar entry during it is left
        _resolve(db, c["id"], why, now)
        resolved.append(c["id"])
    if regroup:
        if it.get("cal_ref") is not None:   # its entry is in a trip's group: the trip re-checks (declined, moved)
            trips |= set(_groups_listing(db, it["cal_ref"], item_id))
        for t in sorted(trips - {item_id}):
            n, r = check_item(db, t, now, tz, message, regroup=False)
            new += n
            resolved += r
    return new, resolved


def carry(db, old_id, new_id, now):
    """The owner's yes turned ask old_id into commitment new_id: its open clashes follow (no new alert)."""
    moved = []
    for c in db.execute("SELECT * FROM ledger_conflicts WHERE state='open' AND ((a_kind='item' AND a_ref=?) OR "
                        "(b_kind='item' AND b_ref=?))", (old_id, old_id)).fetchall():
        col = "a_ref" if c["a_kind"] == "item" and c["a_ref"] == old_id else "b_ref"
        db.execute(f"UPDATE ledger_conflicts SET {col}=?, updated_at=? WHERE id=?", (new_id, now, c["id"]))
        moved.append(c["id"])
    return moved


def open_conflicts_of(db, item_id):
    return db.execute("SELECT * FROM ledger_conflicts WHERE state='open' AND ((a_kind='item' AND a_ref=?) OR "
                      "(b_kind='item' AND b_ref=?))", (item_id, item_id)).fetchall()


def after_message(db, msg, res, now, tz):
    """Clashes, carried clashes and follow-ups after one message's reading (ledger.apply's result)."""
    out = {"new": [], "resolved": [], "carried": [], "followups": list(res.get("followups") or []), "cleared": []}
    mid = msg.get("id")
    successors = {}
    for i in res.get("created") or []:
        it = L.get_item(db, i)
        if it and it.get("predecessor"):
            successors[it["predecessor"]] = i
            out["carried"] += carry(db, it["predecessor"], i, now)
    if msg.get("direction") == "out":   # before the checks: a yes without a new commitment closes its clash
        to = set(msg.get("to_keys") or ())
        answered = set(successors.values()) | {i for i in res.get("updated") or []
                                               if (L.get_item(db, i) or {}).get("status") in ("accepted", "done")}
        for i in answered:
            mine = L.get_item(db, i)
            for c in open_conflicts_of(db, i):
                kind, ref = (c["b_kind"], c["b_ref"]) if c["a_kind"] == "item" and c["a_ref"] == i else \
                    (c["a_kind"], c["a_ref"])
                if kind != "item":
                    continue
                other = L.get_item(db, ref)
                if not other or not L.is_active(other) or other["type"] not in ("ask", "commitment") or \
                        other["who"] in ("", L.OWNER) or other["who"] in to:
                    continue
                said = (mine.get("who_name") if mine.get("who") != L.OWNER else "") or \
                    ", ".join(sorted(n for n in msg.get("to_names") or () if n)) or "the other side"
                fid = L.add_followup(db, other["id"], {
                    "reason": f"You said yes to {said}; {other['who_name'] or other['who']} hasn't heard from you.",
                    "tell": other["who"], "tell_name": other["who_name"], "confidence": c["confidence"]},
                    mid, now, conflict=c["id"])
                if fid:
                    out["followups"].append(fid)
    if msg.get("direction") == "out":
        out["followups"] += _promise_followups(db, msg, res, now)
        out["followups"] += _decline_followups(db, msg, res, now, tz)      # 0.22.0 FUP-03
        out["followups"] += _stale_entry_followups(db, msg, res, now, tz)  # 0.22.0 FUP-04
        out["followups"] += _cross_thread_followups(db, msg, res, now, tz) # 0.22.0 FUP-09
    _paid(db, msg, res, now)
    _chased(db, msg, res, now)
    out["followups"] += _chase_followups(db, list(res.get("created") or []) + list(res.get("merged") or []) +
                                         list(res.get("updated") or []), msg.get("id"), now, tz)
    changed = list(dict.fromkeys(list(res.get("updated") or []) + list(res.get("created") or []) +
                                 list(res.get("merged") or [])))
    for i in changed:
        n, r = check_item(db, i, now, tz, mid)
        out["new"] += n
        out["resolved"] += r
    touched = set(res.get("updated") or []) | set(res.get("closed") or []) | set(res.get("merged") or []) | \
        set(successors)
    out["cleared"] = L.clear_followups(db, msg, now, touched)
    for i in res.get("closed") or []:
        it = L.get_item(db, i)
        if it and it["status"] in ("cancelled", "done", "declined"):
            # cancelled: nothing left to tell; 0.22.0 done (paid): the chase follow-up (nobody to tell) clears too
            only = "" if it["status"] == "cancelled" else " AND tell='' AND tell_name=''"
            for f in db.execute("SELECT id FROM ledger_followups WHERE state='open' AND item=? AND "
                                "coalesce(raised_message, -1) != ?" + only, (i, mid if mid is not None else -2)).fetchall():
                db.execute("UPDATE ledger_followups SET state='cleared', cleared_at=?, cleared_message=? WHERE id=?",
                           (now, mid, f["id"]))
                out["cleared"].append(f["id"])
    return out


PROMISE = re.compile(r"\b(get back to|let (?:you|him|her|them) know|circle back|(?:will|i'll|i will) confirm|"
                     r"confirm (?:by|tomorrow|later|tonight)|check (?:with|my|on) .{0,40}(?:and|then) (?:get back|"
                     r"let you know|confirm))", re.I)


def _promise_followups(db, msg, res, now):
    """0.22.0: the owner's own outgoing "let me check and get back to you tomorrow" is a promise to answer: a follow-up on the
    commitment it made, due when the commitment is, until the owner writes to them again (clear_followups)."""
    text = msg.get("text")
    if text is None and msg.get("id") is not None:
        r = db.execute("SELECT text FROM messages WHERE id=?", (msg["id"],)).fetchone()
        text = r["text"] if r else ""
    if not PROMISE.search(text or ""):
        return []
    keys = [k for k in (msg.get("to_keys") or ()) if k and k != L.OWNER]
    names = [n for n in (msg.get("to_names") or ()) if n]
    if not keys:
        return []
    made = []
    for i in res.get("created") or []:
        it = L.get_item(db, i)
        if not it or it["type"] != "commitment" or it["who"] != L.OWNER or not L.is_active(it):
            continue
        name = names[0] if names else keys[0]
        fid = L.add_followup(db, i, {"reason": f"You said you'd get back to {name}.", "tell": keys[0],
                                     "tell_name": name, "due_ts": it.get("start_ts"), "confidence": "likely"},
                             msg.get("id"), now)
        if fid:
            made.append(fid)
    return made


# --- 0.22.0 follow-ups the model doesn't have to see: one side not told -------------------------------------------------
DECLINE = re.compile(r"\bcan(?:'|’)?t\b|\bcannot\b|\bcan not\b|\bwon(?:'|’)?t be able\b|\bnot able to\b|\bunable\b|"
                     r"\bhave to pass\b|\bpass on\b|\bnot available\b|\bcount me out\b|\bdecline\b|\bno can do\b|"
                     r"\bnot going to make it\b|\bwon(?:'|’)?t make it\b", re.I)
MOVE_ASK = re.compile(r"\b(?:move|moving|moved|reschedul\w*|push(?:ed)? (?:it|back|to)|instead|swap|bump|shift|"
                      r"another (?:day|time))\b", re.I)
AVAIL_ASK = re.compile(r"\b(?:booked|book(?:ing)?|hold(?:ing)?|avail\w*|free|on (?:the|that) (?:job|shoot|doc|gig)|"
                       r"in for|confirm\w*)\b", re.I)
STALE_LEAD_S = 3 * 3600         # a stale calendar entry's follow-up is due this long before it starts
_OPEN_ASK = "type='ask' AND merged_into IS NULL AND status IN ('open','tentative')"


def _first(name):
    return (str(name or "").split(" ") or [""])[0]


def _thread_key(db, msg):
    r = db.execute("SELECT source, account, thread FROM messages WHERE id=?", (msg.get("id"),)).fetchone() \
        if msg.get("id") is not None else None
    return f"{r['source']}:{r['account']}:{r['thread']}" if r else None


def _source_text(db, item_id, direction=None):
    sql = ("SELECT m.subject, m.text, m.ts FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE s.item=? AND "
           "s.role='created'" + (" AND m.direction=?" if direction else ""))
    rows = db.execute(sql, (item_id, direction) if direction else (item_id,)).fetchall()
    return " \n".join(f"{r['subject'] or ''} {r['text'] or ''}" for r in rows), (rows[0]["ts"] if rows else None)


def _names(text, name):
    f = _first(name)
    return len(f) > 1 and re.search(r"\b" + re.escape(f) + r"\b", text or "", re.I) is not None


def _ask_days(db, it, tz):
    """The local dates an ask is about: its own time, or the dates its message names ("Mar 24-25")."""
    sp = L.span(it, tz)
    if sp is not None:
        return sp
    text, at = _source_text(db, it["id"])
    if not text or at is None:
        return None
    try:
        days = readrules.date_reading(text, dt.datetime.fromtimestamp(at, tz).date(), tz)[0]
    except Exception:  # noqa: BLE001 (the rules are a cross-check only)
        days = set()
    if not days:
        return None
    return L.day_bounds(min(days), tz)[0], L.day_bounds(max(days), tz)[1]


def _overlap(a, b):
    return a is not None and b is not None and a[0] < b[1] and b[0] < a[1]


def _clashing(db, a, b):
    return _pair(db, "item", a, "item", b) is not None


def _decline_followups(db, msg, res, now, tz):
    """FUP-03: when the owner says no to one person ("Can't Monday, sorry"), an open ask of someone else about the same plan
    (it names that person: "Ines mentioned you might do pickups Monday", or the same words, not a rival in a clash)
    at the same time is still waiting: a follow-up to tell them, until the owner writes to them about it."""
    text = _msg_text(db, msg)
    to = {k for k in msg.get("to_keys") or () if k}
    mine = set(res.get("created") or [])
    said_no = {}
    for i in res.get("updated") or []:
        it = L.get_item(db, i)
        if it and it["who"] not in ("", L.OWNER) and (it["status"] == "declined" or (
                it["status"] in ("moved", "postponed") and DECLINE.search(text))):
            said_no[i] = it
    tkey = _thread_key(db, msg)
    if DECLINE.search(text) and to and tkey:      # the model left the ask open: the owner's "can't" in its conversation
        marks = ",".join("?" * len(to))
        for r in db.execute(f"SELECT * FROM ledger_items WHERE {_OPEN_ASK} AND thread=? AND who IN ({marks})",
                            [tkey, *sorted(to)]).fetchall():
            if r["id"] not in mine:
                said_no.setdefault(r["id"], L.row_dict(r))
    made = []
    for d in said_no.values():
        dsp = L.span(d, tz)
        dtext, _ = _source_text(db, d["id"], "in")
        for r in db.execute(f"SELECT * FROM ledger_items WHERE {_OPEN_ASK} AND id != ? AND who NOT IN ('', ?, ?)",
                            (d["id"], L.OWNER, d["who"])).fetchall():
            o = L.row_dict(r)
            if o["id"] in mine or o["who"] in to or o["id"] in said_no:
                continue
            osp = L.span(o, tz)
            if dsp is not None and osp is not None and not _overlap(dsp, osp):
                continue
            otext, _ = _source_text(db, o["id"], "in")
            named = _names(otext, d["who_name"]) or _names(dtext, o["who_name"])
            if not (named or (dsp is not None and osp is not None and L.similar(d["what"], o["what"]) >= 0.5 and
                              not _clashing(db, d["id"], o["id"]))):
                continue
            when = f" for {L.when_label(d, tz)}" if L.when_label(d, tz) else ""
            fid = L.add_followup(db, o["id"], {
                "reason": f"You said no to {_first(d['who_name']) or 'them'}{when}; "
                          f"{_first(o['who_name']) or 'they'} still waits to hear about it.",
                "tell": o["who"], "tell_name": o["who_name"], "confidence": "likely"}, msg.get("id"), now)
            if fid:
                made.append(fid)
    return made


def _attendees(e):
    try:
        att = json.loads(e["attendees"] or "[]")
    except ValueError:
        att = []
    out = []
    for a in att:
        if isinstance(a, dict):
            if a.get("self"):
                continue
            key = str(a.get("address") or a.get("email") or "").lower()
            name = str(a.get("name") or a.get("displayName") or "")
        else:
            key = str(a or "").lower()
            name = key[2:].replace("_", " ").title() if key.startswith("p:") else ""
        if key or name:
            out.append((key, name or key.split("@")[0]))
    return out


def _stale_entry_followups(db, msg, res, now, tz):
    """FUP-04: the owner agrees to move a plan ("Thursday 3pm works. I'll update everyone") that a calendar entry still
    holds at the old time, and the entry has people the owner didn't write to: each gets a follow-up on the moved item ("the
    calendar still says Wednesday; Kofi hasn't heard"), due before the entry starts (Today arms a reminder). Cleared
    by the owner's message to them about it, or when the entry moves to the new time (sweep)."""
    to = {k for k in msg.get("to_keys") or () if k}
    to_first = {_first(n).lower() for n in msg.get("to_names") or () if n}
    if not to:
        return []
    tkey = _thread_key(db, msg)
    text = _msg_text(db, msg)
    thread_text = text
    knows = set()             # who was in the conversation, or on a message about the item: they know
    if tkey:
        src, acct, th = tkey.split(":", 2)
        for r in db.execute("SELECT subject, text FROM messages WHERE source=? AND account=? AND thread=? AND ts <= ? "
                            "AND ts >= ? AND id != ? ORDER BY ts DESC LIMIT 3",
                            (src, acct, th, msg.get("ts") or now, (msg.get("ts") or now) - 7 * 86400,
                             msg.get("id") or -1)).fetchall():
            thread_text += f" \n{r['subject'] or ''} {r['text'] or ''}"
        knows |= _people_in(db, "SELECT sender, sender_name, recipients FROM messages WHERE source=? AND account=? AND "
                                "thread=? AND ts >= ?", (src, acct, th, (msg.get("ts") or now) - 30 * 86400))
    if not MOVE_ASK.search(thread_text) or DECLINE.search(text):
        return []
    items = [L.get_item(db, i) for i in dict.fromkeys(list(res.get("created") or []) + list(res.get("updated") or []))]
    items = [it for it in items if it and L.is_active(it) and it["type"] != "cleanup"]
    items.sort(key=lambda it: (not (it["type"] == "commitment" and it["who"] == L.OWNER), L.span(it, tz) is None))
    if not items:
        return []
    made = []
    for e in db.execute("SELECT * FROM events WHERE status != 'cancelled' AND start_ts > ? AND start_ts < ? "
                        "ORDER BY start_ts", (now, now + 30 * 86400)).fetchall():
        people = _attendees(e)
        title = e["title"] or ""
        with_them = any(k in to or _first(n).lower() in to_first for k, n in people) or any(
            re.search(r"\b" + re.escape(f) + r"\b", title, re.I) for f in to_first if len(f) > 1)
        if not with_them:
            continue
        names_out = {f for f in to_first} | {_first(n).lower() for k, n in people}
        tw = L.words(title) - names_out - {"w"}
        if not tw & (L.words(thread_text) | {w for it in items for w in L.words(it["what"] + " " + it["purpose"])}):
            continue
        espan = event_span(e)
        it = next((x for x in items if not _overlap(L.span(x, tz), espan)), None)
        if it is None:
            continue                     # the calendar already has the new time
        told = knows | _people_in(db, "SELECT m.sender, m.sender_name, m.recipients FROM ledger_sources s JOIN messages "
                                      "m ON m.id=s.message WHERE s.item=?", (it["id"],))
        others = [(k, n) for k, n in people if k not in to and _first(n).lower() not in to_first and
                  _first(n).lower() not in {"me", "owner"} and k not in told and _first(n).lower() not in told]
        if not others:
            continue
        isp = L.span(it, tz)
        due = max(now, min(espan[0], isp[0] if isp else espan[0]) - STALE_LEAD_S)
        d = dt.datetime.fromtimestamp(espan[0], tz)
        for k, n in others[:4]:
            fid = L.add_followup(db, it["id"], {
                "reason": f"The calendar still has {title or 'it'} on {d:%a} {L.clock12(d)}; "
                          f"{_first(n)} hasn't heard about the change.",
                "tell": k, "tell_name": n, "due_ts": due, "confidence": "likely", "event": e["id"]}, msg.get("id"), now)
            if fid:
                made.append(fid)
    return made


def _people_in(db, sql, args):
    """Lower-cased addresses and first names of the senders and recipients of the messages sql selects."""
    out = set()
    for r in db.execute(sql, args).fetchall():
        try:
            rec = json.loads(r["recipients"] or "[]")
        except ValueError:
            rec = []
        for k, n in [(r["sender"], r["sender_name"])] + [(x.get("address"), x.get("name")) if isinstance(x, dict)
                                                         else (x, "") for x in rec]:
            if k and k != "me":
                out.add(str(k).lower())
            f = _first(n).lower()
            if len(f) > 1:
                out.add(f)
    out -= {L.OWNER, "owner"}
    return out


def _cross_thread_followups(db, msg, res, now, tz):
    """FUP-09: the owner's yes in one conversation ("Confirming Mar 24-25" to Dana) is what someone else asked about in
    another ("Are you booked on the Brightwater doc Mar 24-25?" from Theo): the same days, and the ask is about the owner's
    booking or shares the job's words. A follow-up on that ask, until the owner writes to them about it."""
    to = {k for k in msg.get("to_keys") or () if k}
    tkey = _thread_key(db, msg)
    text = _msg_text(db, msg)
    mine = set(res.get("created") or [])
    commits = []
    for i in dict.fromkeys(list(res.get("created") or []) + list(res.get("updated") or [])):
        it = L.get_item(db, i)
        if it and it["type"] == "commitment" and it["who"] == L.OWNER and it["status"] == "accepted" and \
                L.is_active(it) and L.span(it, tz) is not None:
            commits.append(it)
    made = []
    for c in commits:
        csp = L.span(c, tz)
        cwords = L.words(f"{c['what']} {c['purpose']} {text}")
        for r in db.execute(f"SELECT * FROM ledger_items WHERE {_OPEN_ASK} AND who NOT IN ('', ?) AND "
                            "coalesce(thread, '') != ?", (L.OWNER, tkey or "")).fetchall():
            o = L.row_dict(r)
            if o["id"] in mine or o["who"] in to or o["id"] == c.get("predecessor") or _clashing(db, c["id"], o["id"]):
                continue
            if not _overlap(csp, _ask_days(db, o, tz)):
                continue
            otext, _ = _source_text(db, o["id"])
            ow = L.words(f"{o['what']} {o['purpose']} {otext}")
            shared = {w for w in cwords & ow if len(w) >= 4 and not w.isdigit()} - L.words(
                "mar march apr april day days confirm confirming booked booking")
            if not (shared or AVAIL_ASK.search(otext)):
                continue
            who_to = ", ".join(sorted(_first(n) for n in msg.get("to_names") or () if n)) or "someone else"
            fid = L.add_followup(db, o["id"], {
                "reason": f"You confirmed {c['what']} ({L.when_label(c, tz)}) with {who_to}; "
                          f"{_first(o['who_name']) or 'they'} asked and hasn't heard.",
                "tell": o["who"], "tell_name": o["who_name"], "confidence": "likely"}, msg.get("id"), now)
            if fid:
                made.append(fid)
    return made


def _msg_text(db, msg):
    if msg.get("text") is not None or msg.get("id") is None:
        return f"{msg.get('subject') or ''} \n{msg.get('text') or ''}"
    r = db.execute("SELECT subject, text FROM messages WHERE id=?", (msg["id"],)).fetchone()
    return f"{r['subject'] or ''} \n{r['text'] or ''}" if r else ""


def chase_line(it, now, tz):
    """"15 days late: chase it ($2,400)." for a late receivable (Today counts the days at each view), else ""."""
    late = L.late_of(it, now, tz)
    if not late:
        return ""
    amt = f" ({it['amount']})" if it.get("amount") and it["amount"] not in it["what"] else ""
    return f"{L.late_word(late)}: chase it{amt}."


def _chase_followups(db, ids, mid, now, tz):
    """0.22.0: money owed to the owner that is past due (a receivable ask, late) is a follow-up for the owner: chase it. Nobody to
    tell (tell empty), so a message doesn't clear it; it clears when the item is done (a later message says it's paid)."""
    made = []
    for i in dict.fromkeys(ids):
        it = L.get_item(db, i)
        if not it or it.get("merged_into") is not None or it["type"] != "ask" or it.get("subtype") != "receivable":
            continue
        line = chase_line(it, now, tz)
        if not line:
            continue
        fid = L.add_followup(db, i, {"reason": line, "tell": "", "tell_name": "",
                                     "confidence": "sure" if it["confidence"] == "sure" else "likely"}, mid, now)
        if fid:
            made.append(fid)
    return made


PAID = re.compile(r"\b(?:payment (?:received|confirmed|processed|complete|successful)|(?:has|have|was|were|been) paid|"
                  r"paid in full|thank(?:s| you) for (?:your )?pay(?:ment|ing)|received (?:your|the|a) payment|"
                  r"got (?:the|your) payment|(?:it's|it is|is now) paid|payment (?:has|was) (?:received|made))\b", re.I)
_NOT_PAID = re.compile(r"(?:\bnot|\bnever|n't|n’t)\s+(?:yet\s+)?(?:been\s+)?paid\b|\bunpaid\b|\bpast[- ]due\b|\boverdue\b", re.I)
_REF = re.compile(r"#\s?([A-Za-z0-9-]*\d[A-Za-z0-9-]*)|\binvoice\s+(?:no\.?|number)?\s*([A-Za-z0-9-]*\d[A-Za-z0-9-]*)",
                  re.I)
_AMT = re.compile(r"[$€£¥₹]\s?(\d[\d,]*(?:\.\d{2})?)")


def _refs(text):
    return {(a or b).lower() for a, b in _REF.findall(text or "")}


def _amounts(text):
    return {x.replace(",", "").removesuffix(".00") for x in _AMT.findall(text or "")}


def _paid(db, msg, res, now):
    """0.22.0: a message that says a bill or invoice is paid ("Payment received for invoice #1043") closes the open
    one it names (the same invoice number or amount, or the same conversation), when the model left it open."""
    text = _msg_text(db, msg)
    if not PAID.search(text) or _NOT_PAID.search(text):
        return []
    refs, amts = _refs(text), _amounts(text)
    thread = db.execute("SELECT source, account, thread FROM messages WHERE id=?", (msg.get("id"),)).fetchone() \
        if msg.get("id") is not None else None
    tkey = f"{thread['source']}:{thread['account']}:{thread['thread']}" if thread else None
    mine = set(res.get("created") or [])
    done = []
    for r in db.execute("SELECT * FROM ledger_items WHERE merged_into IS NULL AND status NOT IN ('declined','done',"
                        "'cancelled','autopay') AND subtype IN ('bill','receivable','payment','invoice') AND "
                        "type IN ('ask','event')").fetchall():
        it = L.row_dict(r)
        if it["id"] in mine:
            continue
        hay = " ".join([it["what"], it["purpose"], it.get("amount") or ""] + [
            x["text"] or "" for x in db.execute("SELECT m.subject || ' ' || m.text AS text FROM ledger_sources s JOIN "
                                                "messages m ON m.id=s.message WHERE s.item=?", (it["id"],))])
        theirs = _refs(hay)
        if refs and theirs:
            if not refs & theirs:
                continue                 # another invoice
        elif not ((amts and amts & _amounts(hay)) or (tkey and it["thread"] == tkey)):
            continue
        L.set_status(db, it["id"], "done", now, msg.get("id"), note="the message says it's paid")
        res.setdefault("updated", []).append(it["id"])
        res.setdefault("closed", []).append(it["id"])
        done.append(it["id"])
    return done


def _chased(db, msg, res, now):
    """0.22.0: the owner's own message that names an open receivable's invoice number ("a nudge on invoice #1043") chases it:
    the item waits on them (the chase follow-up stays until it's paid). The model may not see that item: another
    thread and person."""
    if msg.get("direction") != "out":
        return []
    text = _msg_text(db, msg)
    refs = _refs(text)
    if not refs or PAID.search(text):
        return []
    touched = set(res.get("updated") or []) | set(res.get("created") or [])
    out = []
    for r in db.execute("SELECT * FROM ledger_items WHERE merged_into IS NULL AND status='open' AND type='ask' AND "
                        "subtype='receivable'").fetchall():
        it = L.row_dict(r)
        if it["id"] in touched:
            continue
        hay = " ".join([it["what"], it["purpose"]] + [x["text"] or "" for x in db.execute(
            "SELECT m.subject || ' ' || m.text AS text FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE "
            "s.item=?", (it["id"],))])
        if refs & _refs(hay):
            L.set_status(db, it["id"], "waiting_on_them", now, msg.get("id"), note="you chased it")
            res.setdefault("updated", []).append(it["id"])
            out.append(it["id"])
    return out


def sweep(db, now, tz):
    """Re-check every open, time-bound item from yesterday on (calendar changes: new, moved or cancelled events)."""
    new, resolved = [], []
    for r in db.execute("SELECT id FROM ledger_items WHERE merged_into IS NULL AND status NOT IN "
                        "('declined','done','cancelled') AND (coalesce(end_ts, start_ts) >= ? OR all_day=1 OR "
                        "candidates != '[]')", (now - 86400,)).fetchall():
        n, rs = check_item(db, r["id"], now, tz)
        new += n
        resolved += rs
    for c in db.execute("SELECT c.id FROM ledger_conflicts c JOIN events e ON e.id=c.b_ref WHERE c.state='open' AND "
                        "c.b_kind='event' AND c.entries = '' AND e.status='cancelled'").fetchall():
        _resolve(db, c["id"], "event cancelled", now)
        resolved.append(c["id"])
    for c in db.execute("SELECT * FROM ledger_conflicts WHERE state='open' AND entries != ''").fetchall():
        # 0.22.x: a grouped clash whose trip wasn't re-checked (past): cancelled or deleted entries leave its list
        ids = L.group_entries(c)
        left = []
        for i in ids:
            e = db.execute("SELECT status FROM events WHERE id=?", (i,)).fetchone()
            if e is not None and e["status"] != "cancelled":
                left.append(i)
        if not left:
            _resolve(db, c["id"], "entries cleared", now)
            resolved.append(c["id"])
        elif left != ids:
            db.execute("UPDATE ledger_conflicts SET entries=?, b_ref=?, updated_at=? WHERE id=?",
                       (json.dumps(left), left[0], now, c["id"]))
    # 0.22.0 (FUP-04): a stale calendar entry's follow-up clears when the entry moves to the item's time or goes
    cleared = []
    for f in db.execute("SELECT * FROM ledger_followups WHERE state='open' AND event IS NOT NULL").fetchall():
        e = db.execute("SELECT * FROM events WHERE id=?", (f["event"],)).fetchone()
        it = L.get_item(db, f["item"])
        gone = e is None or e["status"] == "cancelled" or e["start_ts"] is None or it is None or not L.is_active(it)
        if gone or _overlap(event_span(e), L.span(it, tz)):
            db.execute("UPDATE ledger_followups SET state='cleared', cleared_at=? WHERE id=?", (now, f["id"]))
            if it is not None:
                L.history(db, it["id"], "follow_up_cleared", now, note="the calendar entry changed")
            cleared.append(f["id"])
    # 0.22.0: an invoice that has become past due since it was read is chased too
    chase = _chase_followups(db, [r["id"] for r in db.execute(
        "SELECT id FROM ledger_items WHERE merged_into IS NULL AND type='ask' AND subtype='receivable' AND status NOT IN "
        "('declined','done','cancelled')").fetchall()], None, now, tz)
    return {"new": new, "resolved": resolved, "followups": chase, "cleared": cleared}


# --- alerts ------------------------------------------------------------------------------------------------------------
SECURITY_FRESH_S = 3600       # 0.21.0: a phone alert only for a security alert this new (the "urgent" setting)
SOURCE_WORD = {"gmail": "email", "signal": "Signal", "slack": "Slack", "sms": "text", "sms_live": "text",
               "calendar": "calendar", "reports": "report"}


def _until(seconds):
    if seconds is None:
        return ""
    if seconds < 3600:
        return "within the hour" if seconds > 0 else "now"
    if seconds < 86400:
        return f"in {int(seconds // 3600)} h"
    return f"in {int(seconds // 86400)} days" if seconds >= 2 * 86400 else "tomorrow"


class Alerter:
    """Push alerts for new clashes, VIP asks and (0.21.0) new security alerts. send(payload, targets, kind) is push.Notifier._send; cfg() the push
    settings (push.config_from); vip(source, handle) -> bool; payload(title, message, tag, urgent) is push._payload."""

    def __init__(self, ms, ledger_ro, cfg, send, payload, vip=None, clock=None, tz=None, log=None, prefs=None,
                 names=None):
        self.ms, self.lv, self.cfg, self.send, self.payload = ms, ledger_ro, cfg, send, payload
        self.vip = vip or (lambda source, handle: False)
        # 0.17.0: prefs.Prefs (the owner's rules set by chat: VIPs, alert rules, reminders) and names(source, handle)
        # -> the sender's names from People (for matching a rule's person)
        self.prefs = prefs
        self.names = names or (lambda source, handle: [])
        self.clock = clock or ledger_ro.clock
        self.tz = tz or ledger_ro.tz
        self.log = log or (lambda line: None)

    def _state(self):
        try:
            return json.loads(self.ms.meta("ledger_push", "{}") or "{}")
        except ValueError:
            return {}

    def _room(self, cfg, now):
        sent = [t for t in self._state().get("sent", []) if now - t < 3600]
        return len(sent) < int(cfg.get("max_per_hour") or 3)

    def _note_sent(self, now):
        st = self._state()
        st["sent"] = [t for t in st.get("sent", []) if now - t < 3600] + [now]
        self.ms.set_meta("ledger_push", json.dumps(st))

    def _quiet(self, cfg, now):
        import push  # noqa: PLC0415 (push imports ha; the ledger works without it)
        return push.in_quiet(cfg, dt.datetime.fromtimestamp(now, self.tz()))

    def _decide(self, cfg, now, vip, enabled, through=None, rule=None):
        """send, held or off. 0.17.0: `through` (a VIP gets through quiet hours: their rule, else the setting) and
        `rule` (the newest matching alert rule: off, quiet, through or None)."""
        if rule == "off" or not enabled or not cfg.get("targets"):
            return "off"
        if rule == "quiet":   # a rule's own quiet time (like "Slack after 9 pm, except Dana") holds VIPs too
            return "held"
        if through is None:
            through = vip and cfg.get("vip_through_quiet", True)
        if self._quiet(cfg, now) and not (through or rule == "through"):
            return "held"
        return "send" if self._room(cfg, now) else "held"

    def _rules(self):
        try:
            return self.prefs.rules() if self.prefs is not None else None
        except Exception:  # noqa: BLE001 (the usual settings still apply)
            return None

    def _senders(self, item_id):
        """[(source, handle, names, slack DM?)] of the item's received messages."""
        rows = self.ms._read("SELECT m.source, m.sender, m.sender_name, m.channel FROM ledger_sources s JOIN messages m "  # noqa: SLF001
                             "ON m.id=s.message WHERE s.item=? AND m.direction='in'", (item_id,))
        out = []
        for r in rows:
            try:
                extra = [n for n in self.names(r["source"], r["sender"]) if n]
            except Exception:  # noqa: BLE001
                extra = []
            out.append((r["source"], r["sender"], [r["sender_name"]] + extra,
                        r["source"] == "slack" and str(r["channel"] or "").startswith("DM with ")))
        return out

    def _vip_of(self, senders, rules, cfg, need="alerts"):
        """(VIP?, through quiet hours?) for these senders: the newest VIP rule set by chat wins over the VIP list."""
        vip = through = False
        dflt = cfg.get("vip_through_quiet", True)
        for src, handle, names, dm in senders:
            f = rules.vip(src, handle, names, dm) if rules is not None else None
            if f is not None:
                if f[need]:
                    vip = True
                    through = through or (dflt if f["through_quiet"] is None else f["through_quiet"])
            elif self.vip(src, handle):
                vip, through = True, through or dflt
        return vip, through

    def _vip_side(self, side):
        if side.get("kind") != "item":
            return False
        return self._vip_of(self._senders(side["id"]), self._rules(), self.cfg())[0]

    def needs_you_vip(self, row):
        """0.17.0 (ledger.Ledger.vip_of): an ask from someone who is VIP for Needs you goes to the top."""
        return self._vip_of(self._senders(row["id"]), self._rules(), self.cfg(), "needs_you")[0]

    def _clash_ctx(self, c, rules, cfg, now):
        senders = [s for side in c["sides"] if side.get("kind") == "item" for s in self._senders(side["id"])]
        vip, through = self._vip_of(senders, rules, cfg)
        local = dt.datetime.fromtimestamp(now, self.tz())
        rule = rules.alert("clashes", [s[:3] for s in senders], local) if rules is not None else None
        return vip, through, rule

    def _ask_ctx(self, item_id, rules, cfg, now):
        senders = self._senders(item_id)
        vip, through = self._vip_of(senders, rules, cfg)
        local = dt.datetime.fromtimestamp(now, self.tz())
        rule = rules.alert("asks", [s[:3] for s in senders], local) if rules is not None and vip else None
        return vip, through, rule

    def remind_payload(self, it, a):
        """A reminder the owner asked for: who and where from, never message text."""
        src = it["from"][0] if it.get("from") else {}
        who = it.get("who_name") or "An item"
        return self.payload("Reminder", f"{who} ({SOURCE_WORD.get(src.get('source'), 'a message')}), as you asked",
                            tag=f"callsheet-remind-{a['id']}", urgent=False)

    def clash_payload(self, c):
        names = []
        for s in c["sides"]:
            if s["kind"] == "event":
                names.append("your calendar")
            else:
                src = "/".join(SOURCE_WORD.get(x, x) for x in s.get("sources") or []) or "a message"
                names.append(f"{s.get('who_name') or 'you'} ({src})")
        title = f"Clash {_until(c['seconds_until'])}: {c['slot']['label']}".strip()
        msg = f"{names[0]} and {names[1]} both want this time."
        n = len(c.get("entries") or [])
        if n:                            # 0.22.x: the owner's trip's grouped clash
            title = f"Trip clash {_until(c['seconds_until'])}: {c['slot']['label']}".strip()
            msg = f"{names[0]}: your trip overlaps {n} calendar {'entry' if n == 1 else 'entries'}."
        elif c.get("kind") == "away":
            title = f"Away-day clash {_until(c['seconds_until'])}: {c['slot']['label']}".strip()
            msg = f"{names[0]} and {names[1]}: you may be away or off that day."
        return self.payload(title, msg, tag=f"callsheet-clash-{c['id']}",
                            urgent=c["seconds_until"] is not None and c["seconds_until"] < 86400)

    def ask_payload(self, it, cfg):
        src = it["from"][0] if it["from"] else {}
        who = it["who_name"] or "Someone"
        if cfg.get("show_subject", True) and src.get("subject"):
            msg = f"{who}: {src['subject']}"
        else:
            msg = f"{who} asked you something ({SOURCE_WORD.get(src.get('source'), 'a message')})"
        return self.payload("VIP ask", msg, tag=f"callsheet-ask-{it['id']}", urgent=True)

    def security_payload(self, it):
        """A security alert: who and where from, never its words (a subject can hold a code)."""
        src = it["from"][0] if it.get("from") else {}
        who = it.get("who_name") or "a sender"
        return self.payload("Security alert", f"From {who} ({SOURCE_WORD.get(src.get('source'), 'a message')}). "
                            "Open Callsheet to see it.", tag=f"callsheet-security-{it['id']}", urgent=True)

    def _mark(self, table, ref, state, now):
        self.ms.write(lambda db: db.execute(f"UPDATE {table} SET alert=?, alerted_at=? WHERE id=?", (state, now, ref))
                      if table == "ledger_conflicts" else
                      db.execute("UPDATE ledger_items SET alert=? WHERE id=?", (state, ref)))

    def _go(self, cfg, kind, payload, now):
        sent, _errors = self.send(payload, cfg["targets"], kind)
        if sent:
            self._note_sent(now)
        return "sent" if sent else "failed"

    def notify(self, res):
        """After a message's reading: alert its new clashes (not carried ones) and its new VIP asks."""
        cfg, now = self.cfg(), self.clock()
        rules = self._rules()
        out = []
        new = set((res.get("conflicts") or {}).get("new") or [])
        if new:
            for c in self.lv.conflicts(state="open", now=now):
                if c["id"] not in new:
                    continue
                vip, through, rule = self._clash_ctx(c, rules, cfg, now)
                d = self._decide(cfg, now, vip, cfg.get("clashes", True), through, rule)
                state = self._go(cfg, "clash", self.clash_payload(c), now) if d == "send" else d
                self._mark("ledger_conflicts", c["id"], state, now)
                out.append(("clash", c["id"], state))
        for i in res.get("created") or []:
            it = self.lv.item(i, detail=False)
            if it and it["section"] == "security":       # 0.21.0: a new security alert (heads.py)
                src = it["from"][0] if it["from"] else {}
                if (src.get("ts") or 0) < now - SECURITY_FRESH_S:
                    continue                             # the read-back: shown on Today, no phone alert
                d = self._decide(cfg, now, False, cfg.get("urgent", False))
                state = self._go(cfg, "security", self.security_payload(it), now) if d == "send" else d
                self._mark("ledger_items", i, state, now)
                out.append(("security", i, state))
                continue
            if not it or it["type"] != "ask" or it["section"] != "needs_you":
                continue
            vip, through, rule = self._ask_ctx(i, rules, cfg, now)
            if not vip:
                continue
            d = self._decide(cfg, now, True, cfg.get("vip_asks", True), through, rule)
            state = self._go(cfg, "vip_ask", self.ask_payload(it, cfg), now) if d == "send" else d
            self._mark("ledger_items", i, state, now)
            out.append(("vip_ask", i, state))
        return out

    def _vip_handle(self, message_id):
        r = self.ms._read("SELECT source, sender FROM messages WHERE id=?", (message_id,))  # noqa: SLF001
        return bool(r) and self.vip(r[0]["source"], r[0]["sender"])

    def remind(self, title, message, tag, vip=False):
        """0.18.0: a reminder the owner set on Today ("Remind me"), to the owner's own phones: names, sources and times only.
        Returns sent | failed | held | off (a held reminder isn't retried: the card on Today still shows it)."""
        cfg, now = self.cfg(), self.clock()
        d = self._decide(cfg, now, vip, cfg.get("reminders", True))
        return self._go(cfg, "reminder", self.payload(title, message, tag=tag, urgent=True), now) if d == "send" else d

    def flush(self):
        """Send held alerts once quiet hours are over and there's room, if the clash is still open."""
        cfg, now = self.cfg(), self.clock()
        rules = self._rules()
        out = []
        held = {r["id"] for r in self.ms._read("SELECT id FROM ledger_conflicts WHERE state='open' AND alert='held'")}  # noqa: SLF001
        if held:
            for c in self.lv.conflicts(state="open", now=now):
                if c["id"] in held:
                    vip, through, rule = self._clash_ctx(c, rules, cfg, now)
                    if self._decide(cfg, now, vip, cfg.get("clashes", True), through, rule) == "send":
                        state = self._go(cfg, "clash", self.clash_payload(c), now)
                        self._mark("ledger_conflicts", c["id"], state, now)
                        out.append(("clash", c["id"], state))
        for r in self.ms._read("SELECT id FROM ledger_items WHERE alert='held' AND status='open'"):  # noqa: SLF001
            it = self.lv.item(r["id"], detail=False)
            if it and it["section"] == "security":
                if self._decide(cfg, now, False, cfg.get("urgent", False)) == "send":
                    state = self._go(cfg, "security", self.security_payload(it), now)
                    self._mark("ledger_items", r["id"], state, now)
                    out.append(("security", r["id"], state))
                continue
            vip, through, rule = self._ask_ctx(r["id"], rules, cfg, now) if it else (False, False, None)
            if it and it["section"] == "needs_you" and \
                    self._decide(cfg, now, True, cfg.get("vip_asks", True), through, rule) == "send":
                state = self._go(cfg, "vip_ask", self.ask_payload(it, cfg), now)
                self._mark("ledger_items", r["id"], state, now)
                out.append(("vip_ask", r["id"], state))
        for a in (self.prefs.due_reminders(now) if self.prefs is not None else []):   # 0.17.0: "remind me at …"
            it = self.lv.item(a["item"], detail=False)
            state = self._go(cfg, "reminder", self.remind_payload(it, a), now) if it and cfg.get("targets") else "off"
            self.prefs.fired(a["id"], now)
            out.append(("reminder", a["id"], state))
        return out
