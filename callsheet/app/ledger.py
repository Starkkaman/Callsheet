"""0.16.0 (rework stage 2): the ledger, in messages.db.

Every message is read once (extract.py) into ledger items: an **ask** (someone wants something from the owner), a
**commitment** (the owner agreed to something, or someone agreed to something for the owner), an **event** (trip, job,
gig, birthday, appointment, deadline, bill, delivery ...) or **cleanup** (newsletters, receipts, notifications).
Later messages update items (done, moved, cancelled ...); every change is kept in `ledger_history`. Nothing is
deleted and nothing is silently dropped: an uncertain item is kept with its source message and a lower confidence,
and every field the checks rejected is listed on the item (`checks`).

All writes run inside one MessageStore.write transaction (the functions here take its `db`); the Ledger class is the
read side (views for the API and the replay runner).
"""
import datetime as dt
import json
import re
import zoneinfo

TYPES = ("ask", "commitment", "event", "cleanup")
STATUSES = ("open", "accepted", "tentative", "claimed", "waiting_on_them", "autopay", "moved", "postponed",
            "declined", "done", "cancelled")
CLOSED = frozenset({"declined", "done", "cancelled"})
QUIET = frozenset({"postponed"})              # still open, but holds no time
HISTORY_ONLY = ("changed", "updated")         # an update that changes fields, not the status
CONFIDENCE = ("sure", "likely", "unsure")
OWNER = "owner"
MERGE_DAYS = 45                               # an item can only merge into one created this recently
NOW_NEXT_S = 90 * 60                          # Now and next: starting within 90 minutes (or all-day today)
EVENT_MIN = 60                                # an item without an end lasts an hour (as dates.EVENT_MIN)
TRIP_PARTS = frozenset({"lodging", "hotel", "flight", "car", "rental", "train", "transport", "ferry", "bus"})
# 0.22.0 (acceptance Rule C): a trip's legs live inside the trip item (merged into it, kept in its history), never a
# second item next to it; a hotel check-in with its time stays its own item under the trip (parent)
TRIP_LEGS = frozenset({"flight", "car", "rental", "train", "transport", "ferry", "bus"})
LEG_WORDS = re.compile(r"\b(?:flights?|flying|fly|depart(?:s|ure)?|return flight|rental car|car rental|car pick ?up|"
                       r"pick ?up (?:the )?car|train|ferry)\b|→", re.I)
# 0.20.0: structured facts for the visual blocks, set at extraction (extract.py) and never by the page or a model alone:
# amount (as written, copied from the sentence the model cited by the 0.12.2 amount rules) with its currency, and stage
# (a package's progress, in order; "due": a task with a deadline, its date in when)
PACKAGE_STAGES = ("ordered", "shipped", "out_for_delivery", "delivered")
STAGES = PACKAGE_STAGES + ("due",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_items (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  subtype TEXT NOT NULL DEFAULT '',
  who TEXT NOT NULL DEFAULT '',
  who_name TEXT NOT NULL DEFAULT '',
  owed TEXT NOT NULL DEFAULT '',
  what TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  start_ts REAL,
  end_ts REAL,
  all_day INTEGER NOT NULL DEFAULT 0,
  start_date TEXT,
  end_date TEXT,
  time TEXT NOT NULL DEFAULT '',
  date_resolved INTEGER NOT NULL DEFAULT 1,
  approx INTEGER NOT NULL DEFAULT 0,
  tz TEXT NOT NULL DEFAULT '',
  phrase TEXT NOT NULL DEFAULT '',
  anchor_ts REAL,
  candidates TEXT NOT NULL DEFAULT '[]',
  says TEXT NOT NULL DEFAULT '',
  says_name TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT 'likely',
  never_hide INTEGER NOT NULL DEFAULT 0,
  thread TEXT NOT NULL DEFAULT '',
  cal_ref INTEGER,
  parent INTEGER,
  predecessor INTEGER,
  merged_into INTEGER,
  checks TEXT NOT NULL DEFAULT '[]',
  note TEXT NOT NULL DEFAULT '',
  alert TEXT NOT NULL DEFAULT '',
  amount TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_items_status ON ledger_items (status, type);
CREATE INDEX IF NOT EXISTS ledger_items_who ON ledger_items (who);
CREATE INDEX IF NOT EXISTS ledger_items_start ON ledger_items (start_ts);
CREATE TABLE IF NOT EXISTS ledger_sources (
  item INTEGER NOT NULL, message INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'created', at REAL NOT NULL,
  PRIMARY KEY (item, message, role));
CREATE INDEX IF NOT EXISTS ledger_sources_message ON ledger_sources (message);
CREATE TABLE IF NOT EXISTS ledger_history (
  id INTEGER PRIMARY KEY, item INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
  status_from TEXT NOT NULL DEFAULT '', status_to TEXT NOT NULL DEFAULT '', message INTEGER,
  fields TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ledger_history_item ON ledger_history (item);
CREATE TABLE IF NOT EXISTS ledger_conflicts (
  id INTEGER PRIMARY KEY,
  a_kind TEXT NOT NULL, a_ref INTEGER NOT NULL, b_kind TEXT NOT NULL, b_ref INTEGER NOT NULL,
  slot_start REAL, slot_end REAL, kind TEXT NOT NULL DEFAULT 'overlap', confidence TEXT NOT NULL DEFAULT 'likely',
  candidate INTEGER, why TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'open',
  message INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL, resolved_at REAL,
  resolved_why TEXT NOT NULL DEFAULT '', alert TEXT NOT NULL DEFAULT '', alerted_at REAL,
  entries TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ledger_conflicts_state ON ledger_conflicts (state);
CREATE TABLE IF NOT EXISTS ledger_followups (
  id INTEGER PRIMARY KEY, item INTEGER NOT NULL, conflict INTEGER, reason TEXT NOT NULL DEFAULT '',
  tell TEXT NOT NULL DEFAULT '', tell_name TEXT NOT NULL DEFAULT '', says TEXT NOT NULL DEFAULT '',
  due_ts REAL, state TEXT NOT NULL DEFAULT 'open', confidence TEXT NOT NULL DEFAULT 'likely',
  raised_at REAL NOT NULL, raised_message INTEGER, cleared_at REAL, cleared_message INTEGER, event INTEGER);
CREATE INDEX IF NOT EXISTS ledger_followups_state ON ledger_followups (state);
CREATE TABLE IF NOT EXISTS ledger_reads (
  message INTEGER PRIMARY KEY, state TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL,
  seconds REAL, items TEXT NOT NULL DEFAULT '[]', drops TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS ledger_reads_state ON ledger_reads (state)
"""

_ITEM_COLS = ("type", "subtype", "who", "who_name", "owed", "what", "purpose", "status", "start_ts", "end_ts",
              "all_day", "start_date", "end_date", "time", "date_resolved", "approx", "tz", "phrase", "anchor_ts",
              "candidates", "says", "says_name", "confidence", "never_hide", "thread", "cal_ref", "parent",
              "predecessor", "checks", "note", "amount", "currency", "stage")
_FACT_COLS = (("amount", "TEXT NOT NULL DEFAULT ''"), ("currency", "TEXT NOT NULL DEFAULT ''"),
              ("stage", "TEXT NOT NULL DEFAULT ''"))     # 0.20.0, added to an older messages.db by ensure()
_WHEN_COLS = ("start_ts", "end_ts", "all_day", "start_date", "end_date", "time", "date_resolved", "approx", "tz",
              "phrase", "candidates")


def ensure(ms):
    """Create the ledger tables in messages.db (idempotent)."""
    def run(db):
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
        have = {r[1] for r in db.execute("PRAGMA table_info(ledger_items)")}
        for col, decl in _FACT_COLS:
            if col not in have:
                db.execute(f"ALTER TABLE ledger_items ADD COLUMN {col} {decl}")
        if "event" not in {r[1] for r in db.execute("PRAGMA table_info(ledger_followups)")}:
            db.execute("ALTER TABLE ledger_followups ADD COLUMN event INTEGER")   # 0.22.0: the stale calendar entry
        if "entries" not in {r[1] for r in db.execute("PRAGMA table_info(ledger_conflicts)")}:
            # 0.22.x: the owner's trip's grouped clash lists the calendar entries during it (conflicts.check_item)
            db.execute("ALTER TABLE ledger_conflicts ADD COLUMN entries TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE ledger_items SET subtype='gig' WHERE subtype='shoot'")   # 0.21.2: the subtype's new name
    ms.write(run)


def stage_after(old, new):
    """The stage an item moves to: a package only moves forward (a late "shipped" mail doesn't undo "delivered")."""
    if not new or new not in STAGES:
        return old or ""
    if old in PACKAGE_STAGES and new in PACKAGE_STAGES and PACKAGE_STAGES.index(new) < PACKAGE_STAGES.index(old):
        return old
    return new


# --- words (merging, matching) -------------------------------------------------------------------------------------
_STOP = frozenset("a an the to for of and or in on at with my your our his her is are be me you it this that from "
                  "about can could would will please help do does did get go going i we us".split())


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def words(s):
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if w not in _STOP and len(w) > 1}


def similar(a, b):
    """Word overlap of two short texts (0..1, by the shorter one)."""
    A, B = words(a), words(b)
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def norm_who(v):
    """A who/handle for comparing: lower case, without the name:/p:/p_ prefixes."""
    v = str(v or "").strip().lower()
    changed = True
    while changed:
        changed = False
        for p in ("name:", "p:", "p_"):
            if v.startswith(p):
                v, changed = v[len(p):], True
    return v


# --- time helpers --------------------------------------------------------------------------------------------------
def zone(name):
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def day_bounds(d, tz):
    """(start_ts, end_ts) of local date d (or a YYYY-MM-DD string)."""
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    a = dt.datetime.combine(d, dt.time(), tzinfo=tz)
    b = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), tzinfo=tz)
    return a.timestamp(), b.timestamp()


def span(it, tz, cand=None):
    """(start_ts, end_ts) an item holds (all-day: the whole local days), or None without a resolved time."""
    if cand is not None:
        s, e = cand.get("start_ts"), cand.get("end_ts")
        if cand.get("start_date"):
            a, _ = day_bounds(cand["start_date"], tz)
            _, b = day_bounds(cand.get("end_date") or cand["start_date"], tz)
            return a, b
        return (s, e if e and e > s else s + EVENT_MIN * 60) if s is not None else None
    if it.get("all_day") and it.get("start_date"):
        a, _ = day_bounds(it["start_date"], tz)
        _, b = day_bounds(it.get("end_date") or it["start_date"], tz)
        return a, b
    s, e = it.get("start_ts"), it.get("end_ts")
    if s is None:
        return None
    return s, (e if e and e > s else s + EVENT_MIN * 60)


def clock12(t):
    h, m = t.hour, t.minute
    ap = "am" if h < 12 else "pm"
    h = h % 12 or 12
    return f"{h}:{m:02d} {ap}" if m else f"{h} {ap}"


def when_label(it, tz):
    """A plain 12-hour label: "Wed Mar 3, 10 am – 12 pm", "Sat Mar 18 – Mon Mar 20", "10 am (date not known)"."""
    def day(d):
        return f"{d:%a} {d:%b} {d.day}"
    if it.get("all_day") and it.get("start_date"):
        a = dt.date.fromisoformat(it["start_date"])
        b = dt.date.fromisoformat(it.get("end_date") or it["start_date"])
        return day(a) if a == b else f"{day(a)} – {day(b)}"
    if it.get("start_ts") is not None:
        a = dt.datetime.fromtimestamp(it["start_ts"], tz)
        out = f"{day(a)}, {clock12(a)}"
        if it.get("end_ts"):
            b = dt.datetime.fromtimestamp(it["end_ts"], tz)
            out += f" – {clock12(b)}" if b.date() == a.date() else f" – {day(b)}, {clock12(b)}"
        return ("about " if it.get("approx") else "") + out
    if it.get("time"):
        h, m = (int(x) for x in it["time"].split(":"))
        return f"{clock12(dt.time(h, m))} (date not known)"
    return ""


def iso(ts, tz):
    return dt.datetime.fromtimestamp(ts, tz).isoformat(timespec="minutes") if ts is not None else None


# --- rows ------------------------------------------------------------------------------------------------------------
def _loads(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def group_entries(c):
    """0.22.x: the calendar entry ids (events.id) a grouped clash lists, in time order: the owner's trip or stay of more than a
    day against the busy timed entries during it is one clash (conflicts.check_item). [] for a clash between two
    sides."""
    try:
        v = c["entries"]
    except (KeyError, IndexError):
        return []
    got = _loads(v, []) if isinstance(v, str) else v or []
    return [int(x) for x in got if isinstance(x, int) and not isinstance(x, bool)] if isinstance(got, list) else []


def row_dict(r):
    d = dict(r)
    d["candidates"] = _loads(d.get("candidates"), [])
    d["checks"] = _loads(d.get("checks"), [])
    return d


def get_item(db, item_id):
    r = db.execute("SELECT * FROM ledger_items WHERE id=?", (item_id,)).fetchone()
    return row_dict(r) if r else None


def item_sources(db, item_id):
    return [r["message"] for r in db.execute("SELECT DISTINCT message FROM ledger_sources WHERE item=? ORDER BY at",
                                             (item_id,))]


def is_active(it):
    return it["status"] not in CLOSED and it.get("merged_into") is None


def holds_time(it):
    return is_active(it) and it["status"] not in QUIET


def history(db, item_id, kind, now, message=None, frm="", to="", fields=None, note=""):
    db.execute("INSERT INTO ledger_history (item, at, kind, status_from, status_to, message, fields, note) "
               "VALUES (?,?,?,?,?,?,?,?)", (item_id, now, kind, frm or "", to or "", message,
                                            json.dumps(fields or {}, default=str), str(note or "")[:500]))


def add_source(db, item_id, message, role, now):
    if message is None:
        return
    db.execute("INSERT OR IGNORE INTO ledger_sources (item, message, role, at) VALUES (?,?,?,?)",
               (item_id, message, role, now))


def owed_of(it):
    if it["type"] == "commitment":
        return "by_owner" if it.get("who") == OWNER else "to_owner"
    if it["type"] == "ask":
        return "to_owner" if it.get("status") == "waiting_on_them" or it.get("subtype") == "receivable" else "by_owner"
    return ""


def insert_item(db, it, message, now):
    row = {c: it.get(c) for c in _ITEM_COLS}
    for c, dflt in (("subtype", ""), ("who", ""), ("who_name", ""), ("what", ""), ("purpose", ""), ("status", "open"),
                    ("all_day", 0), ("time", ""), ("date_resolved", 1), ("approx", 0), ("tz", ""), ("phrase", ""),
                    ("says", ""), ("says_name", ""), ("confidence", "likely"), ("never_hide", 0), ("thread", ""),
                    ("note", ""), ("amount", ""), ("currency", ""), ("stage", "")):
        if row[c] is None:
            row[c] = dflt
    row["candidates"] = json.dumps(it.get("candidates") or [])
    row["checks"] = json.dumps(it.get("checks") or [])
    row["owed"] = owed_of(it)
    cols = list(row) + ["created_at", "updated_at"]
    cur = db.execute(f"INSERT INTO ledger_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row[c] for c in row] + [now, now])
    item_id = cur.lastrowid
    add_source(db, item_id, message, "created", now)
    history(db, item_id, "created", now, message, to=row["status"])
    return item_id


def update_fields(db, item_id, fields, now):
    if not fields:
        return
    vals = dict(fields)
    for k in ("candidates", "checks"):
        if k in vals and not isinstance(vals[k], str):
            vals[k] = json.dumps(vals[k])
    db.execute(f"UPDATE ledger_items SET {', '.join(k + '=?' for k in vals)}, updated_at=? WHERE id=?",
               list(vals.values()) + [now, item_id])


def set_status(db, item_id, status, now, message=None, note="", kind=None, fields=None):
    it = get_item(db, item_id)
    if it is None:
        return None
    upd = dict(fields or {})
    if status and status != it["status"]:
        upd["status"] = status
        if it["type"] == "ask":
            upd["owed"] = owed_of(dict(it, status=status))
    update_fields(db, item_id, upd, now)
    add_source(db, item_id, message, "updated", now)
    history(db, item_id, kind or "status", now, message, frm=it["status"], to=status or it["status"],
            fields={k: v for k, v in (fields or {}).items() if k != "checks"}, note=note)
    return it


def when_fields(it):
    return {c: it.get(c) for c in _WHEN_COLS}


# --- merging -----------------------------------------------------------------------------------------------------------
def _overlap(a, b, slack=0):
    return a is not None and b is not None and a[0] < b[1] + slack and b[0] < a[1] + slack


DELIVERY = frozenset({"delivery", "package"})
# words every delivery mail shares: they never tell two orders apart (the item's own words do)
_DELIVERY_WORDS = None
_ORDER_REF = re.compile(r"\b(?:order|tracking|shipment|package)\s*(?:#|no\.?|number|id)?\s*:?\s*#?\s*"
                        r"([a-z0-9][a-z0-9-]*\d[a-z0-9-]*)|#\s?([a-z0-9-]*\d[a-z0-9-]{3,})", re.I)


def delivery_words():
    global _DELIVERY_WORDS
    if _DELIVERY_WORDS is None:
        _DELIVERY_WORDS = words("delivery delivered delivering deliver package packages parcel order ordered orders "
                                "shipment shipped shipping ship ships arriving arrives arrive arrival expected estimated "
                                "estimate tomorrow today tonight out way item items now new update status track "
                                "tracking")
    return _DELIVERY_WORDS


def order_refs(text):
    """Order and tracking numbers a delivery mail names ("Order #112-4471", "Tracking number 1Z99…")."""
    return {(a or b).lower().strip("-") for a, b in _ORDER_REF.findall(str(text or ""))} - {""}


def _texts_of(db, item_id=None, message=None):
    if message is not None:
        rows = db.execute("SELECT subject, text FROM messages WHERE id=?", (message,)).fetchall()
    else:
        rows = db.execute("SELECT m.subject, m.text FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE "
                          "s.item=?", (item_id,)).fetchall()
    return " \n".join(f"{r['subject'] or ''} {r['text'] or ''}" for r in rows)


def same_delivery(db, old, it, tz, message=None):
    """0.22.x: two delivery items are one package when they name the same order or tracking number, or, when the mails
    give none to compare, the same item (its own words, not the shop's or the delivery words) on dates that meet.
    Never by the thread or the sender alone: one shop's thread can carry two orders. Returns a score or None."""
    mine = order_refs(f"{it.get('what') or ''} {it.get('purpose') or ''} " +
                      (_texts_of(db, message=message) if message is not None else ""))
    theirs = order_refs(f"{old.get('what') or ''} {old.get('purpose') or ''} " + _texts_of(db, item_id=old["id"]))
    if mine and theirs:
        return 2.0 if mine & theirs else None
    shop = words(old.get("who_name")) | words(it.get("who_name")) | delivery_words()
    A = words(f"{old.get('what') or ''} {old.get('purpose') or ''}") - shop
    B = words(f"{it.get('what') or ''} {it.get('purpose') or ''}") - shop
    if not A or not B:
        return None
    sim = len(A & B) / min(len(A), len(B))
    if sim < 0.5:
        return None
    a, b = span(old, tz), span(it, tz)
    if a and b and not _overlap(a, b, slack=86400):
        return None                      # the same thing ordered again for another day
    return 1.0 + sim


def find_same(db, it, tz, anchor, message=None, among=None):
    """An open item the new one repeats (same type and person, same time or the same thing), else None.
    Trips merge with any overlapping open trip (the hotel mail and the airline mail are one trip). Deliveries merge
    only as one package (same_delivery). among: only these item ids, whatever their type now (0.22.1: tidied items,
    Clean up lines that were this type)."""
    trip = it["type"] == "event" and it.get("subtype") == "trip"
    if not it.get("who") and not trip:
        return None
    args = [it["type"], anchor - MERGE_DAYS * 86400]
    sql = ("SELECT * FROM ledger_items WHERE type=? AND merged_into IS NULL AND created_at >= ? "
           "AND status NOT IN ('declined','done','cancelled')")
    if among is not None:
        if not among:
            return None
        args = [anchor - MERGE_DAYS * 86400] + sorted(among)
        sql = ("SELECT * FROM ledger_items WHERE merged_into IS NULL AND created_at >= ? AND status NOT IN "
               f"('declined','done','cancelled') AND id IN ({','.join('?' * len(among))})")
    if trip:
        sql += " AND subtype='trip'"
    else:
        sql += " AND who=?"
        args.append(it["who"])
    best, best_score = None, 0.0
    mine = span(it, tz)
    for r in db.execute(sql + " ORDER BY id DESC LIMIT 50", args):
        old = row_dict(r)
        if message is not None and db.execute("SELECT 1 FROM ledger_sources WHERE item=? AND message=?",
                                              (old["id"], message)).fetchone():
            continue                     # two items of one answer are two things
        if it.get("subtype") in DELIVERY and old.get("subtype") in DELIVERY:
            score = same_delivery(db, old, it, tz, message)
            if score is not None and score > best_score:
                best, best_score = old, score
            continue
        theirs = span(old, tz)
        sim = similar(old["what"] + " " + old["purpose"], (it.get("what") or "") + " " + (it.get("purpose") or ""))
        if mine and theirs and bool(it.get("all_day")) != bool(old.get("all_day")):
            continue                     # a trip's days and its 4 pm departure are two items
        if mine and theirs:
            same_start = abs((mine[0] or 0) - (theirs[0] or 0)) <= 15 * 60
            if not _overlap(mine, theirs):
                continue
            whole = bool(it.get("all_day")) and bool(old.get("all_day"))   # "next week": many things share it
            if trip or (sim >= 0.5 if whole else same_start and (it["type"] != "event" or sim >= 0.2) or sim >= 0.3):
                score = 1.0 + sim + (0.5 if same_start else 0)
            else:
                continue
        elif not mine and not theirs:
            if sim < 0.5:
                continue
            score = sim
        else:
            if sim < 0.5:
                continue
            score = sim
        if score > best_score:
            best, best_score = old, score
    return best


def merge(db, old, it, message, now):
    """The new reading joins the old item: a source more, missing fields filled, a firmer confidence kept."""
    upd = {}
    for c in ("subtype", "purpose", "says", "says_name", "thread"):
        if not old.get(c) and it.get(c):
            upd[c] = it[c]
    if not old.get("amount") and it.get("amount"):        # 0.20.0: the first amount the ledger found stays
        upd["amount"], upd["currency"] = it["amount"], it.get("currency") or ""
    st = stage_after(old.get("stage"), it.get("stage"))
    if st != (old.get("stage") or ""):
        upd["stage"] = st
    if old.get("start_ts") is None and not old.get("all_day") and (it.get("start_ts") is not None or it.get("all_day")):
        upd.update(when_fields(it))
    elif old.get("end_ts") is None and it.get("end_ts") and it.get("start_ts") == old.get("start_ts"):
        upd["end_ts"] = it["end_ts"]
    if CONFIDENCE.index(it.get("confidence") or "likely") < CONFIDENCE.index(old.get("confidence") or "likely"):
        upd["confidence"] = it["confidence"]
    if it.get("never_hide") and not old.get("never_hide"):
        upd["never_hide"] = 1
    if it.get("checks"):
        upd["checks"] = (old.get("checks") or []) + it["checks"]
    update_fields(db, old["id"], upd, now)
    add_source(db, old["id"], message, "merged", now)
    history(db, old["id"], "merged", now, message, fields={k: v for k, v in upd.items() if k != "checks"})
    return old["id"]


APPOINTMENT = frozenset({"appointment"})
# words that name a time, not a thing: never the word an appointment and an entry share
_TIME_TOKENS = frozenset("am pm mon tue tues wed thu thur thurs fri sat sun monday tuesday wednesday thursday friday "
                         "saturday sunday jan feb mar apr may jun jul aug sep sept oct nov dec january february march "
                         "april june july august september october november december today tomorrow tonight morning "
                         "afternoon evening noon week next pst pdt est edt utc gmt reminder".split())


def _appointment_match(db, it, sp, message=None):
    """0.22.x: the calendar entry an appointment mail ("Reminder: your visit Thu 1:00 PM") repeats: a timed entry
    that holds the appointment's start (from 15 minutes before it begins to its end) and shares a word with it: its
    title against the item's words, the provider (who_name) or the mail's subject. The entry may block more time
    than the visit ("12:30-1:30" for a 1:00 visit). Returns (event id, score) or None."""
    pool = words(f"{it.get('what') or ''} {it.get('purpose') or ''} {it.get('who_name') or ''}")
    if message is not None:
        r = db.execute("SELECT subject FROM messages WHERE id=?", (message,)).fetchone()
        pool |= words(r["subject"]) if r else set()
    best = None
    for e in db.execute("SELECT * FROM events WHERE all_day=0 AND status != 'cancelled' AND start_ts IS NOT NULL AND "
                        "start_ts <= ? AND coalesce(end_ts, start_ts + 3600) > ?", (sp[0] + 900, sp[0])).fetchall():
        common = {w for w in words(e["title"]) & pool if w not in _TIME_TOKENS and not w.isdigit()}
        if not common:
            continue
        score = len(common) - abs(e["start_ts"] - sp[0]) / 86400
        if best is None or score > best[1]:
            best = (e["id"], score)
    return best


def cal_match(db, it, tz, message=None):
    """The calendar event this item repeats (start within 15 minutes or the same all-day date, and a similar title,
    or exactly the same times; an appointment also by _appointment_match), else None."""
    sp = span(it, tz)
    if sp is None or it["type"] not in ("event", "commitment"):
        return None
    if it.get("subtype") in APPOINTMENT and not it.get("all_day") and it.get("start_ts") is not None:
        hit = _appointment_match(db, it, sp, message)
        if hit:
            return hit[0]
    rows = db.execute("SELECT * FROM events WHERE start_ts < ? AND coalesce(end_ts, start_ts) > ?",
                      (sp[1] + 900, sp[0] - 900)).fetchall()
    best, best_score = None, 0.0
    for e in rows:
        if e["all_day"] != (1 if it.get("all_day") else 0) and not it.get("all_day"):
            if e["all_day"]:
                continue
        sim = similar(e["title"], (it.get("what") or "") + " " + (it.get("purpose") or ""))
        if it.get("all_day") and e["all_day"] and it.get("start_date") and sim > 0 and e["end_ts"]:
            z = zone(tz) if isinstance(tz, str) else tz
            first = dt.datetime.fromtimestamp(e["start_ts"], z).date().isoformat()
            last = (dt.datetime.fromtimestamp(e["end_ts"], z) - dt.timedelta(days=1)).date()
            if first == it["start_date"] or last.isoformat() == (it.get("end_date") or it["start_date"]):
                if sim + 0.5 > best_score:      # the same days (one end in common) and a word in common
                    best, best_score = e["id"], sim + 0.5
                continue
        exact = abs((e["start_ts"] or 0) - sp[0]) <= 900
        same_times = exact and (e["end_ts"] is None or it.get("end_ts") is None or abs(e["end_ts"] - sp[1]) <= 900)
        if exact and (sim >= 0.3 or same_times and sim > 0):
            score = sim + (1 if same_times else 0)
            if score > best_score:
                best, best_score = e["id"], score
    return best


def trip_parent(db, it, tz):
    """An open trip this event falls inside (a hotel, a flight, a car on the trip's dates), else None."""
    if it["type"] != "event" or it.get("subtype") not in TRIP_PARTS:
        return None
    sp = span(it, tz)
    if sp is None:
        return None
    for r in db.execute("SELECT * FROM ledger_items WHERE type='event' AND subtype='trip' AND merged_into IS NULL "
                        "AND status NOT IN ('declined','done','cancelled') ORDER BY id DESC LIMIT 20"):
        t = row_dict(r)
        tsp = span(t, tz)
        if tsp and tsp[0] - 86400 <= sp[0] < tsp[1]:
            return t["id"]
    return None


# ---- 0.22.1: what the owner tidied stays tidied ---------------------------------------------------------------------------
# Tidy up (tidy.py) moves an item to Clean up (type cleanup) and keeps the move in tidy_moves (state 'moved'). The item
# is the one place its message lands: a re-read or a second copy of the message, or a later message that repeats it
# (find_same by its type before the move), joins it in Clean up. It comes back only on a real new signal (msg "signal":
# a person or the owner writing about it, never machine mail, promo or a channel post not addressed to the owner), with
# the reason in a "back" check, a "tidy_back" history line and the move's state 'back' (Tidy's Undo leaves it alone).
COPY_S = 600          # a second copy of a mail (another account, an import): same sender and subject within this


def _tidy_on(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tidy_moves'").fetchone() is not None


def tidied_moves(db):
    """{item id: (tidy_moves row id, type before the move)} for the items Tidy up moved that are still moved."""
    if not _tidy_on(db):
        return {}
    out = {}
    for r in db.execute("SELECT t.id, t.item, t.before FROM tidy_moves t JOIN ledger_items i ON i.id=t.item WHERE "
                        "t.state='moved' AND i.type='cleanup' ORDER BY t.id"):
        try:
            was = (json.loads(r["before"]) or {}).get("type") or ""
        except ValueError:
            was = ""
        out[r["item"]] = (r["id"], was)
    return out


def copies(db, message):
    """This message's id and its copies' (Gmail: the same sender and subject within COPY_S)."""
    m = db.execute("SELECT id, source, sender, subject, ts FROM messages WHERE id=?", (message,)).fetchone()
    if m is None or m["source"] != "gmail" or not (m["subject"] or "").strip() or m["ts"] is None:
        return [message]
    return [r["id"] for r in db.execute(
        "SELECT id FROM messages WHERE source='gmail' AND lower(sender)=lower(?) AND subject=? AND ts BETWEEN ? AND ? "
        "AND deleted_at IS NULL", (m["sender"], m["subject"], m["ts"] - COPY_S, m["ts"] + COPY_S))] or [message]


def find_tidied(db, it, tz, anchor, message=None, moves=None):
    """The tidied item a new item repeats: (item, from this message or a copy?) or (None, False). Never for a Clean up
    line, a security alert, an invite or a never-hidden item (those keep their own way)."""
    if it["type"] == "cleanup" or it.get("subtype") in ("security", "invite") or it.get("never_hide"):
        return None, False
    moves = tidied_moves(db) if moves is None else moves
    mine = {i for i, (_m, was) in moves.items() if was == it["type"]}
    if not mine:
        return None, False
    if message is not None:
        ids = copies(db, message)
        got = {r["item"] for r in db.execute(f"SELECT DISTINCT item FROM ledger_sources WHERE message IN "
                                             f"({','.join('?' * len(ids))})", ids)} & mine
        if got:
            best = max((get_item(db, i) for i in got), key=lambda o: (similar(
                o["what"] + " " + o["purpose"], (it.get("what") or "") + " " + (it.get("purpose") or "")), o["id"]))
            return best, True
    return find_same(db, it, tz, anchor, message, among=mine), False


def bring_back(db, item_id, why, now, message=None):
    """A tidied item comes back where it was (its type before Tidy up), saying why. True when it came back."""
    if not _tidy_on(db):
        return False
    m = db.execute("SELECT * FROM tidy_moves WHERE item=? AND state='moved' ORDER BY id DESC LIMIT 1",
                   (item_id,)).fetchone()
    it = get_item(db, item_id)
    if m is None or it is None or it["type"] != "cleanup":
        return False
    was = (json.loads(m["before"]) or {}).get("type") or "ask"
    checks = [c for c in it.get("checks") or [] if not (isinstance(c, dict) and c.get("field") in ("tidy", "back"))]
    checks.append({"field": "back", "value": m["grp"], "why": why[:200]})
    db.execute("UPDATE ledger_items SET type=?, checks=?, updated_at=? WHERE id=?",
               (was, json.dumps(checks), now, item_id))
    history(db, item_id, "tidy_back", now, message, note=why[:300], fields={"type": ["cleanup", was]})
    db.execute("UPDATE tidy_moves SET state='back' WHERE id=?", (m["id"],))
    return True


def _sources_of(db, item_id):
    return {r["message"] for r in db.execute("SELECT message FROM ledger_sources WHERE item=?", (item_id,))}


def back_why(msg):
    return f"Back from Clean up: {msg['signal']}" if msg.get("signal") else ""


# --- one message's reading -------------------------------------------------------------------------------------------
def apply(db, msg, parsed, now, tz):
    """Apply one message's normalised reading (extract.normalize): updates first, then the new items (merged into an
    existing item when they repeat it). Returns {"created", "merged", "updated", "closed", "items", "drops",
    "followups"}: `items` holds the resulting item id for each new item, in order."""
    out = {"created": [], "merged": [], "updated": [], "closed": [], "items": [], "drops": list(parsed.get("drops") or []),
           "followups": []}
    mid = msg.get("id")
    later = []
    for u in parsed.get("updates") or []:
        if u.get("new") is not None:
            later.append(u)
            continue
        it = get_item(db, u["id"])
        if it is None:
            out["drops"].append({"what": "update", "id": u.get("ref"), "why": "no such item"})
            continue
        if it.get("subtype") == "security" and u.get("status"):     # 0.21.0: only the owner's Done closes one
            out["drops"].append({"what": "update", "id": u.get("ref"), "why": "a security alert closes only by your Done"})
            continue
        if msg.get("signal") and it["type"] == "cleanup" and u.get("status") not in CLOSED and \
                mid not in _sources_of(db, it["id"]):
            bring_back(db, it["id"], back_why(msg), now, mid)    # 0.22.1: a person writes about a tidied item
            it = get_item(db, it["id"])
        _update(db, it, u, mid, now, out)
    closed_now = set(out["closed"])
    moves = tidied_moves(db)
    for it in parsed.get("items") or []:
        same = find_same(db, it, tz, msg.get("ts") or now, mid)
        if same is None and moves:
            # 0.22.1: a tidied item stays the one place its message lands; a real new signal brings it back
            same, again = find_tidied(db, it, tz, msg.get("ts") or now, mid, moves)
            if same is not None and msg.get("signal") and not again and bring_back(db, same["id"], back_why(msg),
                                                                                    now, mid):
                moves.pop(same["id"], None)
                same = get_item(db, same["id"])
            elif same is not None and not any(isinstance(c, dict) and c.get("field") == "tidy" and
                                              c.get("value") == "again" for c in same.get("checks") or []):
                it = dict(it, checks=list(it.get("checks") or []) + [
                    {"field": "tidy", "value": "again", "why": "already in Clean up (Tidy up): " +
                     ("the same message read again" if again else "more about it, from no one writing to you")}])
        if same is not None:
            out["items"].append(merge(db, same, it, mid, now))
            out["merged"].append(same["id"])
            continue
        it = dict(it)
        it["cal_ref"] = cal_match(db, it, tz, mid)
        _end_from_calendar(db, it)
        it["parent"] = trip_parent(db, it, tz)
        if it["type"] == "commitment" and it.get("who") == OWNER:
            it["predecessor"] = _predecessor(db, it, closed_now, tz)
        item_id = insert_item(db, it, mid, now)
        out["items"].append(item_id)
        out["created"].append(item_id)
    _fold_ask_plans(db, out, mid, now, tz)
    _fold_trip_legs(db, out, mid, now, tz)
    for u in later:                      # updates and follow-ups on this answer's own new items ("N1")
        k = u["new"]
        it = get_item(db, out["items"][k]) if 0 <= k < len(out["items"]) else None
        if it is None:
            out["drops"].append({"what": "update", "id": u.get("ref"), "why": "no such new item"})
            continue
        _update(db, it, u, mid, now, out)
    return out


def _update(db, it, u, mid, now, out):
    if it.get("merged_into"):
        it = get_item(db, it["merged_into"])
    if True:
        fields = {}
        if u.get("when"):
            fields.update(u["when"])
            if fields.get("start_ts") is not None and fields.get("end_ts") is None and it.get("start_ts") is not None \
                    and it.get("end_ts") and not fields.get("all_day"):
                fields["end_ts"] = fields["start_ts"] + (it["end_ts"] - it["start_ts"])   # a move keeps its length
        if u.get("checks"):
            fields["checks"] = (it.get("checks") or []) + u["checks"]
        dated = (fields.get("start_ts") if "start_ts" in fields else it.get("start_ts")) is not None or \
            bool(fields.get("start_date") if "start_date" in fields else it.get("start_date"))
        if u.get("stage") and (u["stage"] != "due" or dated):   # 0.20.0: a package moves on; "due" needs a date
            sg = stage_after(it.get("stage"), u["stage"])
            if sg != (it.get("stage") or ""):
                fields["stage"] = sg
        if u.get("amount"):                                  # 0.20.0: a changed amount ("the total is now $310")
            fields["amount"], fields["currency"] = u["amount"], u.get("currency") or ""
        st = u.get("status") or ""
        kind = "status"
        if st in HISTORY_ONLY:
            kind, st = st, ""
        elif st == "moved":
            kind = "moved"
            if it["type"] == "ask" and it["who"] == OWNER and it["status"] == "waiting_on_them":
                st = ""          # 0.22.x: the owner's question with another time offered still waits on them (their offer
                #                  is their own ask); the new time is kept, but it holds none of the owner's time
            if not u.get("when"):
                fields["checks"] = fields.get("checks", it.get("checks") or []) + [
                    {"field": "when", "value": "", "why": "moved without a new time; the old time is kept"}]
        elif st == "postponed":
            kind = "postponed"
        if st or fields or u.get("note") or kind in HISTORY_ONLY:     # 0.22.0: a bare "changed" is kept too
            before = when_fields(it)
            set_status(db, it["id"], st, now, mid, note=u.get("note") or "", kind=kind, fields=fields)
            if fields and kind in ("moved", "changed", "updated", "status"):
                history(db, it["id"], "when_from", now, mid, fields={k: before[k] for k in ("start_ts", "end_ts",
                                                                                            "start_date", "end_date")})
            out["updated"].append(it["id"])
            if st in CLOSED:
                out["closed"].append(it["id"])
        if u.get("follow_up"):
            fid = add_followup(db, it["id"], u["follow_up"], mid, now)
            if fid:
                out["followups"].append(fid)


def _end_from_calendar(db, it):
    """0.22.0: a timed item that repeats a calendar entry (cal_ref) and gives no end ("Kickoff is Tue 10am, invite
    sent") lasts as long as the entry (listed in checks)."""
    if it.get("cal_ref") is None or it.get("all_day") or it.get("start_ts") is None or it.get("end_ts") is not None \
            or it.get("candidates"):
        return
    e = db.execute("SELECT start_ts, end_ts, all_day FROM events WHERE id=?", (it["cal_ref"],)).fetchone()
    if e and not e["all_day"] and e["start_ts"] is not None and (e["end_ts"] or 0) > e["start_ts"] and \
            abs(e["start_ts"] - it["start_ts"]) <= 900:
        it["end_ts"] = it["start_ts"] + (e["end_ts"] - e["start_ts"])
        it["checks"] = list(it.get("checks") or []) + [{"field": "when.end", "value": "",
                                                         "why": "end from the calendar entry it repeats"}]


ASKING = re.compile(r"\?|\b(?:are you|you're|you are|if you're|if you are) (?:free|around|available|up for)\b|"
                    r"\b(?:can|could|would|will) you\b|\bany chance\b|\bwork for you\b|\bup for\b", re.I)
NO_FOLD = frozenset({"birthday", "bill", "receivable", "security", "invite", "delivery"})


def is_leg(it):
    """A flight, car or train of a trip (by its subtype, or an event without one whose words say so), or a hotel stay
    given as whole days (the trip's own days; a check-in at 3 pm stays its own item under the trip)."""
    if it["type"] != "event" or it.get("subtype") == "trip":
        return False
    sub = it.get("subtype") or ""
    if sub in ("lodging", "hotel"):
        return bool(it.get("all_day"))
    return sub in TRIP_LEGS or (sub in ("", "travel") and LEG_WORDS.search(it.get("what") or "") is not None)


def _trip_dates(trip, legs, tz):
    """A trip the model gave a time ("7:05 am", its first flight's) as the whole local days it covers, from its own
    start and end and its legs; None when only one day is known (nothing to go on)."""
    pts = [trip["start_ts"]] + ([trip["end_ts"]] if trip.get("end_ts") else [])
    for g in legs:
        sp = span(g, tz)
        if sp is not None:
            pts.append(sp[0])
    days = sorted({dt.datetime.fromtimestamp(t, tz).date() for t in pts if t is not None})
    if len(days) < 2:
        return None
    return {"all_day": 1, "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(), "start_ts": None,
            "end_ts": None, "time": "", "approx": 0}


def _fold_trip_legs(db, out, mid, now, tz):
    """0.22.0: each flight, car or train this message made that falls inside an open trip (made by this message or
    before) is merged into the trip: the leg is kept in the trip's history ("flight YHZ→TOS, Thu Mar 18, 7:05 am"),
    never shown as a second item. A trip made now also takes in the open legs already on its days. A trip that came
    with a time (its first flight's) becomes the whole days from its start to its last leg."""
    made = [get_item(db, i) for i in out["created"]]
    new_trips = [t for t in made if t and t["type"] == "event" and t.get("subtype") == "trip" and is_active(t)]
    legs = [g for g in made if g and is_leg(g) and is_active(g)]
    for t in new_trips:
        if t.get("all_day") or t.get("start_ts") is None:
            continue
        inside = [g for g in legs if span(g, tz) and t["start_ts"] - 86400 <= span(g, tz)[0] <= t["start_ts"] +
                  30 * 86400]           # this message's legs: the same booking
        fields = _trip_dates(t, inside, tz)
        if fields:
            update_fields(db, t["id"], fields, now)
            history(db, t["id"], "changed", now, mid, fields=dict(fields), note="a trip is the whole days it covers")
    if new_trips:                        # the legs already in the ledger on a new trip's days
        ids = {g["id"] for g in legs}
        for r in db.execute("SELECT * FROM ledger_items WHERE type='event' AND merged_into IS NULL AND status NOT IN "
                            "('declined','done','cancelled') AND (start_ts IS NOT NULL OR start_date IS NOT NULL) "
                            "ORDER BY id DESC LIMIT 200").fetchall():
            g = row_dict(r)
            if g["id"] not in ids and is_leg(g):
                legs.append(g)
    for g in legs:
        sp = span(g, tz)
        if sp is None:
            continue
        trip = None
        for r in db.execute("SELECT * FROM ledger_items WHERE type='event' AND subtype='trip' AND merged_into IS NULL "
                            "AND status NOT IN ('declined','done','cancelled') AND id != ? ORDER BY id DESC LIMIT 20",
                            (g["id"],)):
            t = row_dict(r)
            tsp = span(t, tz)
            if tsp and tsp[0] - 86400 <= sp[0] < tsp[1]:
                trip = t
                break
        if trip is None:
            continue
        label = when_label(g, tz)
        db.execute("UPDATE ledger_items SET merged_into=?, parent=?, updated_at=? WHERE id=?",
                   (trip["id"], trip["id"], now, g["id"]))
        history(db, g["id"], "merged", now, mid, note=f"a leg of the trip L{trip['id']}")
        if mid is not None:
            add_source(db, trip["id"], mid, "merged", now)
        history(db, trip["id"], "merged", now, mid, note=f"{g['what']}" + (f", {label}" if label else ""))
        if g["id"] in out["created"]:
            out["created"].remove(g["id"])
            out["merged"].append(trip["id"])
            out["items"] = [trip["id"] if x == g["id"] else x for x in out["items"]]


def _fold_ask_plans(db, out, mid, now, tz):
    """0.22.0: one message's ask and an event of the same person for the same time are one thing: the plan the ask is
    about ("Dinner at Mom's Sunday at 6?" read as an ask and a dinner event). The event joins the ask (kept, merged,
    with a history line; its time fills a missing one), so it neither shows twice nor clashes with the ask's rivals."""
    r = db.execute("SELECT text, subject FROM messages WHERE id=?", (mid,)).fetchone() if mid is not None else None
    if not r or not ASKING.search(f"{r['subject'] or ''}\n{r['text'] or ''}"):
        return                            # "Prepro Tue 2pm. Please bring a lens list": a plan and a task, both kept
    made = [get_item(db, i) for i in out["created"]]
    asks = [a for a in made if a and a["type"] == "ask" and a["who"] not in ("", OWNER)]
    for ev in made:
        if not ev or ev["type"] != "event" or ev["who"] in ("", OWNER) or (ev.get("subtype") or "") in NO_FOLD:
            continue
        esp = span(ev, tz)
        best = None
        for a in asks:
            if a["who"] != ev["who"] or a.get("merged_into") is not None:
                continue
            asp = span(a, tz)
            sim = similar(a["what"] + " " + a["purpose"], ev["what"] + " " + ev["purpose"])
            if asp and esp:
                near = _overlap(asp, esp) or abs(asp[0] - esp[0]) <= 3 * 3600
                if near and not (bool(a["all_day"]) != bool(ev["all_day"]) and sim < 0.3):
                    best = a
                    break
            elif esp and not asp and sim >= 0.3:
                best = a
                break
        if best is None:
            continue
        upd = {}
        if span(best, tz) is None and esp:
            upd.update(when_fields(ev))
            upd["candidates"] = json.dumps(ev.get("candidates") or [])
        elif best.get("end_ts") is None and ev.get("end_ts") and best.get("start_ts") == ev.get("start_ts"):
            upd["end_ts"] = ev["end_ts"]
        if not best.get("subtype") and ev.get("subtype"):
            upd["subtype"] = ev["subtype"]
        if best.get("cal_ref") is None and ev.get("cal_ref") is not None:
            upd["cal_ref"] = ev["cal_ref"]
        if upd:
            update_fields(db, best["id"], upd, now)
        db.execute("UPDATE ledger_items SET merged_into=?, updated_at=? WHERE id=?", (best["id"], now, ev["id"]))
        history(db, ev["id"], "merged", now, mid, note=f"the plan the ask L{best['id']} is about (one thing)")
        history(db, best["id"], "merged", now, mid, fields={k: v for k, v in upd.items() if k != "candidates"},
                note=f"L{ev['id']} is this ask's plan")
        out["created"].remove(ev["id"])
        out["merged"].append(best["id"])
        out["items"] = [best["id"] if x == ev["id"] else x for x in out["items"]]


def _predecessor(db, it, closed_now, tz):
    """The ask this message closed by saying yes to it (its slot overlaps the new commitment), else None."""
    sp = span(it, tz)
    for i in closed_now:
        old = get_item(db, i)
        if old and old["type"] == "ask" and old["status"] == "done":
            osp = span(old, tz)
            if sp is None or osp is None or _overlap(sp, osp):
                return i
    return None


# --- follow-ups --------------------------------------------------------------------------------------------------------
def add_followup(db, item_id, fu, message, now, conflict=None):
    """A follow-up on an item ("Marco hasn't heard from you"); one open follow-up per item and person."""
    tell = fu.get("tell") or ""
    if db.execute("SELECT 1 FROM ledger_followups WHERE item=? AND state='open' AND tell=?",
                  (item_id, tell)).fetchone():
        return None
    cur = db.execute("INSERT INTO ledger_followups (item, conflict, reason, tell, tell_name, says, due_ts, state, "
                     "confidence, raised_at, raised_message, event) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (item_id, conflict, str(fu.get("reason") or "")[:300], tell, str(fu.get("tell_name") or "")[:100],
                      fu.get("says") or "", fu.get("due_ts"), "open", fu.get("confidence") or "likely", now, message,
                      fu.get("event")))
    history(db, item_id, "follow_up", now, message, note=fu.get("reason") or "")
    return cur.lastrowid


def clear_followups(db, msg, now, touched=None):
    """An outgoing message to a person clears the open follow-ups that wait on telling that person (any source) about
    an item this message answers, updates or closes (touched). A message to them about something else doesn't."""
    if msg.get("direction") != "out":
        return []
    touched = None if touched is None else set(touched)
    keys = {k for k in msg.get("to_keys") or () if k}
    names = {str(n).lower() for n in msg.get("to_names") or () if n}
    out = []
    for f in db.execute("SELECT * FROM ledger_followups WHERE state='open' AND coalesce(raised_message, -1) != ?",
                        (msg.get("id") if msg.get("id") is not None else -2,)).fetchall():
        if touched is not None:
            i, seen = f["item"], set()
            while i is not None and i not in touched and i not in seen:
                seen.add(i)
                r = db.execute("SELECT merged_into FROM ledger_items WHERE id=?", (i,)).fetchone()
                i = r["merged_into"] if r else None
            if i is None or i not in touched:
                if not _answers_promise(db, f, touched):
                    continue
        if (f["tell"] and f["tell"] in keys) or (f["tell_name"] and f["tell_name"].lower() in names):
            db.execute("UPDATE ledger_followups SET state='cleared', cleared_at=?, cleared_message=? WHERE id=?",
                       (now, msg.get("id"), f["id"]))
            history(db, f["item"], "follow_up_cleared", now, msg.get("id"))
            out.append(f["id"])
    return out


def _answers_promise(db, f, touched):
    """0.22.0: the owner's promise to get back to someone (a follow-up on the owner's own commitment) is kept by the owner's answer to the ask
    it was about, or by a change to an item of that conversation in the owner's message to them."""
    r = db.execute("SELECT predecessor, thread, who FROM ledger_items WHERE id=?", (f["item"],)).fetchone()
    if not r:
        return False
    if r["predecessor"] in touched:
        return True
    if r["who"] != OWNER or f["conflict"] is not None or not r["thread"] or not touched:
        return False
    marks = ",".join("?" * len(touched))
    return db.execute(f"SELECT 1 FROM ledger_items WHERE thread=? AND id IN ({marks}) LIMIT 1",
                      [r["thread"], *touched]).fetchone() is not None


def mark_read(db, message, state, now, seconds=None, items=(), drops=(), note="", tries=None):
    old = db.execute("SELECT tries FROM ledger_reads WHERE message=?", (message,)).fetchone()
    n = tries if tries is not None else (old["tries"] if old else 0) + 1
    db.execute("INSERT OR REPLACE INTO ledger_reads (message, state, tries, at, seconds, items, drops, note) "
               "VALUES (?,?,?,?,?,?,?,?)", (message, state, n, now, seconds, json.dumps(list(items)),
                                            json.dumps(list(drops), default=str)[:20000], str(note or "")[:300]))


# --- late (0.22.0) -----------------------------------------------------------------------------------------------------
# A deadline in the past is never dropped from Today: the item shows as late (Needs you), with how many days.
DUE_SUBTYPES = frozenset({"bill", "payment", "receivable", "invoice", "deadline", "document", "reply", "decision"})
ATTEND_SUBTYPES = frozenset({"job", "gig", "meeting", "appointment", "party", "birthday", "trip", "flight", "lodging",
                             "hotel", "train", "availability", "invite", "security"})
PAST_DUE = re.compile(r"\b(?:past[- ]due|overdue|was due|were due|final notice|late (?:fee|charge) (?:has been|was) "
                      r"(?:applied|charged|added)|\d{1,3}\s+days?\s+(?:late|past[- ]due|overdue))\b", re.I)
_N_DAYS_LATE = re.compile(r"\b(\d{1,3})\s+days?\s+(?:late|past[- ]due|overdue)\b", re.I)
_DUE_WORD = re.compile(r"\b(?:by|due|deadline|before|no later than|eod|end of (?:the )?day|get back)\b", re.I)


def past_due_marked(it):
    """extract.normalize marks a bill or invoice its message calls past due (a check with field "past_due")."""
    return any(isinstance(c, dict) and c.get("field") == "past_due" for c in it.get("checks") or [])


def has_deadline(it):
    """Is the item's time a deadline (pay, send, answer by then) rather than a time to be somewhere?"""
    sub = it.get("subtype") or ""
    if it["type"] == "cleanup" or sub in ATTEND_SUBTYPES or it.get("status") == "autopay":
        return False
    if sub in DUE_SUBTYPES or it.get("stage") == "due" or past_due_marked(it):
        return True
    return it["type"] == "commitment" and bool(_DUE_WORD.search(it.get("phrase") or ""))


def late_of(it, now, tz):
    """{"days": n or None, "since": ts or None} when the item's deadline has passed and it is still open; else None.
    Days count local dates ("was due March 3", read on March 8: 5). An undated "15 days past due" counts from its
    message."""
    if not is_active(it) or it.get("status") in ("autopay", "postponed") or it["type"] == "cleanup":
        return None
    sp = span(it, tz)
    if sp is not None:
        if sp[1] > now or not has_deadline(it):
            return None
        due_day = dt.datetime.fromtimestamp(sp[1] - 1, tz).date()
        return {"days": max(0, (dt.datetime.fromtimestamp(now, tz).date() - due_day).days), "since": sp[1]}
    if not (past_due_marked(it) or (it.get("subtype") in DUE_SUBTYPES and PAST_DUE.search(
            f"{it.get('phrase') or ''} {it.get('what') or ''}"))):
        return None
    m = _N_DAYS_LATE.search(it.get("phrase") or "") or _N_DAYS_LATE.search(it.get("what") or "")
    if m and it.get("anchor_ts"):
        extra = (dt.datetime.fromtimestamp(now, tz).date() - dt.datetime.fromtimestamp(it["anchor_ts"], tz).date()).days
        return {"days": int(m.group(1)) + max(0, extra), "since": None}
    return {"days": None, "since": None}


def late_word(late):
    """"5 days late", "1 day late", "Late" (due earlier today), "Past due" (days not known)."""
    if not late:
        return ""
    n = late.get("days")
    if n is None:
        return "Past due"
    return "Late" if n == 0 else f"{n} day{'' if n == 1 else 's'} late"


# --- the read side ---------------------------------------------------------------------------------------------------
def section_of(it, now, tz):
    """Where an item shows: security, needs_you, now_next, coming_up, clean_up, or none (closed, merged, waiting on them,
    cancelled or past)."""
    if it.get("merged_into") is not None or it["status"] in CLOSED:
        return "none"
    if it.get("subtype") == "security":
        return "security"                # 0.21.0 (heads.py): at the top of Now and next until the owner marks it Done
    t = it["type"]
    if t == "cleanup":
        return "clean_up"
    if t == "ask":
        return "none" if it["status"] == "waiting_on_them" else "needs_you"
    if t == "commitment" and it["status"] == "claimed":
        return "needs_you"
    sp = span(it, tz)
    if sp is None and t == "commitment" and it.get("who") == OWNER and not it.get("time"):
        return "needs_you"               # the owner said they'd do something, no time at all: it's on them
    if sp is None:
        return "coming_up"
    if sp[1] <= now:
        # 0.22.0: a deadline that has passed (a bill, "send it by Friday") is on the owner, shown as late; a past plan isn't
        return "needs_you" if late_of(it, now, tz) else "none"
    if it.get("all_day"):
        a, b = sp
        return "now_next" if a <= now < b else "coming_up"
    return "now_next" if sp[0] - NOW_NEXT_S <= now else "coming_up"


def urgency(start_ts, now):
    """(word, seconds until): now (under 3 h or under way), today (under 24 h), soon (a week), later."""
    if start_ts is None:
        return "later", None
    left = start_ts - now
    word = "now" if left < 3 * 3600 else "today" if left < 86400 else "soon" if left < 7 * 86400 else "later"
    return word, int(left)


class Ledger:
    """The read side: views for the API (stage 4 UI) and the replay runner."""

    def __init__(self, ms, tz_fn, clock):
        self.ms = ms
        self.tz_fn = tz_fn
        self.clock = clock
        # 0.17.0 (set by api.App): snoozed(now) -> {item: until} (prefs.Prefs.snoozed: hidden until then, in "snoozed");
        # vip_of(row) -> bool (a VIP's asks go to the top of Needs you)
        self.snoozed = None
        self.vip_of = None
        # cleanup_rule(handle, names, text) -> the owner's newest matching clean-up rule (prefs.Rules.cleanup_rule) or
        # None: each Clean up item gets its decision ("clean_up" or "keep"); nothing moves or hides
        self.cleanup_rule = None
        ensure(ms)

    def tz(self):
        return zone(self.tz_fn()) if isinstance(self.tz_fn(), str) else self.tz_fn()

    def _rows(self, sql, args=()):
        return [row_dict(r) for r in self.ms._read(sql, args)]  # noqa: SLF001

    def item(self, item_id, detail=True):
        rows = self._rows("SELECT * FROM ledger_items WHERE id=?", (item_id,))
        return self.present(rows[0], detail=detail) if rows else None

    def sources(self, item_id):
        return [dict(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT s.role, s.at, m.id, m.source, m.thread, m.msg_id, m.direction, m.sender, m.sender_name, m.ts, "
            "m.subject, m.link FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE s.item=? "
            "AND m.same_as IS NULL ORDER BY m.ts",   # 0.16.1: a linked live text shows as its backup copy
            (item_id,))]

    def present(self, it, now=None, detail=False):
        tz = self.tz()
        now = self.clock() if now is None else now
        d = {k: it[k] for k in ("id", "type", "subtype", "who", "who_name", "owed", "what", "purpose", "status",
                                "says", "says_name", "confidence", "cal_ref", "parent", "predecessor", "merged_into",
                                "note")}
        d["amount"], d["currency"], d["stage"] = it.get("amount") or "", it.get("currency") or "", it.get("stage") or ""
        d["never_hide"] = bool(it["never_hide"])
        d["when"] = {"start": iso(it["start_ts"], tz), "end": iso(it["end_ts"], tz), "all_day": bool(it["all_day"]),
                     "start_date": it["start_date"], "end_date": it["end_date"], "time": it["time"] or None,
                     "date_resolved": bool(it["date_resolved"]), "approx": bool(it["approx"]),
                     "phrase": it["phrase"], "tz": it["tz"], "label": when_label(it, tz),
                     "candidates": [{"start": iso(c.get("start_ts"), tz), "end": iso(c.get("end_ts"), tz),
                                     "start_date": c.get("start_date"), "end_date": c.get("end_date")}
                                    for c in it["candidates"]]}
        sp = span(it, tz)
        d["urgency"], d["seconds_until"] = urgency(sp[0] if sp else None, now)
        d["section"] = section_of(it, now, tz)
        late = late_of(it, now, tz)                  # 0.22.0: a passed deadline, shown as late
        d["late"] = dict(late, word=late_word(late)) if late else None
        d["checks"] = it["checks"]
        d["created_at"], d["updated_at"] = it["created_at"], it["updated_at"]
        src = self.sources(it["id"])
        d["from"] = [{"message": s["id"], "source": s["source"], "sender_name": s["sender_name"] or s["sender"],
                      "direction": s["direction"], "ts": s["ts"], "subject": s["subject"], "link": s["link"],
                      "role": s["role"]} for s in src]
        if detail:
            d["history"] = [dict(r) for r in self.ms._read(  # noqa: SLF001
                "SELECT at, kind, status_from, status_to, message, fields, note FROM ledger_history WHERE item=? "
                "ORDER BY id", (it["id"],))]
            d["follow_ups"] = self.followups(item=it["id"], state=None)
        return d

    def _cleanup_decision(self, row):
        """The owner's clean-up rule for one Clean up item: matched on its senders (address and name) or on its words
        (what, purpose, subtype, subjects). {decision: clean_up|keep, rule, label} or None."""
        text = " ".join(str(x or "") for x in (row.get("what"), row.get("purpose"), row.get("subtype")))
        text += " " + " ".join(str(f.get("subject") or "") for f in row.get("from") or [])
        for s in self.sources(row["id"]) or [{}]:
            names = [n for n in (s.get("sender_name"), row.get("who_name")) if n]
            hit = self.cleanup_rule(s.get("sender") or "", names, text)
            if hit:
                return {"decision": "clean_up" if hit["fields"].get("counts", True) else "keep", "rule": hit["id"],
                        "label": hit["label"]}
        return None

    def items(self, where="1=1", args=()):
        return self._rows(f"SELECT * FROM ledger_items WHERE {where} ORDER BY id", args)

    def conflicts(self, state="open", now=None):
        tz = self.tz()
        now = self.clock() if now is None else now
        sql = "SELECT * FROM ledger_conflicts" + (" WHERE state=?" if state else "") + " ORDER BY slot_start"
        out = []
        for c in self.ms._read(sql, (state,) if state else ()):  # noqa: SLF001
            c = dict(c)
            c["sides"] = [self._side(c["a_kind"], c["a_ref"], now), self._side(c["b_kind"], c["b_ref"], now)]
            # 0.22.x: the owner's trip's one grouped clash lists each calendar entry during it (group_entries)
            c["entries"] = [self._side("event", e, now) for e in group_entries(c)]
            c["slot"] = {"start": iso(c["slot_start"], tz), "end": iso(c["slot_end"], tz),
                         "label": when_label({"start_ts": c["slot_start"], "end_ts": c["slot_end"]}, tz)}
            c["urgency"], c["seconds_until"] = urgency(c["slot_start"], now)
            out.append(c)
        order = {"now": 0, "today": 1, "soon": 2, "later": 3}
        return sorted(out, key=lambda c: (order[c["urgency"]], c["slot_start"] or 0))

    def _side(self, kind, ref, now):
        if kind == "item":
            rows = self._rows("SELECT * FROM ledger_items WHERE id=?", (ref,))
            if rows:
                it = rows[0]
                return {"kind": "item", "id": ref, "type": it["type"], "who_name": it["who_name"] or it["who"],
                        "what": it["what"], "for": it["purpose"], "status": it["status"],
                        "sources": sorted({s["source"] for s in self.sources(ref)})}
            return {"kind": "item", "id": ref}
        r = self.ms._read("SELECT * FROM events WHERE id=?", (ref,))  # noqa: SLF001
        e = dict(r[0]) if r else {}
        return {"kind": "event", "id": ref, "event_id": e.get("event_id"), "title": e.get("title", ""),
                "status": e.get("status", ""), "sources": ["calendar"]}

    def followups(self, state="open", item=None):
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if item is not None:
            where.append("item=?")
            args.append(item)
        return [dict(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM ledger_followups" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id",
            args)]

    def reads(self):
        rows = self.ms._read("SELECT state, count(*) n FROM ledger_reads GROUP BY state")  # noqa: SLF001
        drops = 0
        for r in self.ms._read("SELECT drops FROM ledger_reads WHERE drops != '[]'"):  # noqa: SLF001
            drops += len(_loads(r["drops"], []))
        return {"by_state": {r["state"]: r["n"] for r in rows}, "drops": drops}

    def rejected(self, limit=50):
        """The last readings with dropped parts (updates for unknown items, invalid types) and their reasons."""
        return [{"message": r["message"], "at": r["at"], "drops": _loads(r["drops"], [])} for r in self.ms._read(  # noqa: SLF001
            "SELECT message, at, drops FROM ledger_reads WHERE drops != '[]' ORDER BY at DESC LIMIT ?", (int(limit),))]

    def view(self, now=None, horizon_days=30):
        """GET api/ledger: {security, now_next, needs_you, coming_up, clean_up, conflicts, follow_ups, waiting, counts}."""
        tz = self.tz()
        now = self.clock() if now is None else now
        groups = {"security": [], "now_next": [], "needs_you": [], "coming_up": [], "clean_up": [], "waiting": [],
                  "snoozed": []}
        try:
            snoozed = self.snoozed(now) if self.snoozed else {}
        except Exception:  # noqa: BLE001 (nothing hidden when the snoozes can't be read)
            snoozed = {}
        for it in self.items("merged_into IS NULL AND status NOT IN ('declined','done','cancelled')"):
            if it["id"] in snoozed and it.get("subtype") != "security":   # 0.21.0: a security alert never snoozes
                groups["snoozed"].append(dict(self.present(it, now), snoozed_until=snoozed[it["id"]]))
                continue
            sec = section_of(it, now, tz)
            if sec == "none":
                if it["type"] == "ask" and it["status"] == "waiting_on_them":
                    groups["waiting"].append(self.present(it, now))
                continue
            row = self.present(it, now)
            if sec == "coming_up" and row["seconds_until"] is not None and row["seconds_until"] > horizon_days * 86400:
                continue
            groups[sec].append(row)
        if self.vip_of:
            for r in groups["needs_you"]:
                try:
                    r["vip"] = bool(self.vip_of(r))
                except Exception:  # noqa: BLE001
                    r["vip"] = False
        groups["needs_you"].sort(key=lambda r: (not r.get("vip"), not r.get("late"), r["created_at"]))
        if self.cleanup_rule:
            for r in groups["clean_up"]:
                try:
                    r["cleanup"] = self._cleanup_decision(r)
                except Exception:  # noqa: BLE001 (no decision when the rules can't be read)
                    r["cleanup"] = None
        groups["coming_up"].sort(key=lambda r: (r["seconds_until"] is None, r["seconds_until"] or 0))
        conf = self.conflicts("open", now)
        fus = self.followups("open")
        return dict(groups, conflicts=conf, follow_ups=fus, reads=self.reads(),
                    counts={k: len(v) for k, v in groups.items()} | {"conflicts": len(conf), "follow_ups": len(fus)})
