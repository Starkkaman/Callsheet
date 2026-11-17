"""0.16.1 rework stage 7a: texts (approved 2026-11-11; docs/messages.md "Texts").

Two read-only ways into the message store (msgstore.py):

1. The nightly backup (source "sms", account "phone"). SMS Backup & Restore on the owner's phone writes
   `sms-<date>.xml` into a folder Home Assistant shows under /share or /media, picked in Setup → Connections → Texts.
   There is no default folder; reading is off until a folder is saved. `calls-*.xml` and anything else are ignored.
   The folder is read like a Reports
   folder (conn_reports.py): inside /share or /media, reached without links, files opened through the folder's
   descriptor with O_NOFOLLOW, regular single-link files only, capped in number and size, never changed. A file is
   read again only when its size or time changes. The XML is parsed with expat as a stream: no DTD (a DOCTYPE refuses
   the file, so no entities, external or internal), size and message caps, invalid character references (the app's
   UTF-16 emoji halves such as `&#55357;&#56832;`) repaired before parsing. SMS and MMS, both directions (1 inbox =
   in, 2 sent = out; drafts, outbox and failed are counted, not kept), contact name, address, time (ms), thread by
   address; MMS text parts are kept, attachments as type and file name only (their data is never kept). Idempotent:
   the same message in tomorrow's full backup changes nothing.

2. Live notifications (source "sms_live", account = the sensor). The Home Assistant Companion app's "Last
   notification" sensor for the owner's phone, read every minute through ha.py's two sensor GETs: its state, and its
   history since the last read. Only notifications from the messaging apps the owner allows are kept (Google Messages
   by default), with the title (the sender) and the text (bigText when longer).

When the backup later holds the same message (same sender, same text, within ±5 minutes) the two are linked: the live
row's `same_as` points at the backup row, and the backup copy is preferred (Chat's search and the ledger's queue skip
the live copy; a live copy the ledger already read hands its items to the backup copy). Nothing is deleted.
"""
import datetime as dt
import hashlib
import json
import os
import re
import stat
import time
import xml.parsers.expat

import conn_reports
import ha
from store import InputError

# 0.21.2: no default folder; texts are read only from a folder the user saved. FOLDER_HINT is only the empty
# field's placeholder.
FOLDER_HINT = "/share/<your folder>"
ACCOUNT = "phone"
APPS = (("com.google.android.apps.messaging", "Google Messages"),
        ("org.thoughtcrime.securesms", "Signal"),
        ("com.whatsapp", "WhatsApp"))
DEFAULT_APPS = ("com.google.android.apps.messaging",)
APP_NAMES = dict(APPS)
FILE_NAME = re.compile(r"sms-[A-Za-z0-9_.-]{1,100}\.xml")
MAX_FILE_BYTES = 512 * 1024 * 1024   # per backup file (media inside MMS can make them big)
MAX_MESSAGES = 500_000               # per backup file
MAX_TEXT = 50_000                    # characters of text per message
MAX_ATTACH = 50                      # attachments listed per MMS
MAX_DEPTH = 8                        # element nesting (smses > mms > parts > part is 4)
MAX_CARRY = 16 * 1024 * 1024         # bytes held back between chunks while repairing character references
MAX_ENTRIES = 5000                   # directory entries looked at per check
MAX_FILES_PASS = 3                   # backup files read per pass (the rest next pass)
KEEP_FILES = 400                     # remembered files
CHUNK = 1024 * 1024
STABLE_S = 60                        # a file younger than this may still be arriving
BATCH = 500
LINK_WINDOW_S = 300                  # ±5 minutes: a live notification and its backup copy
LINK_DAYS = 30                       # live notifications older than this are not looked at again
DEDUPE_S = 600                       # the same notification re-posted within 10 minutes is one message
BACKUP_OLD_S = 36 * 3600             # the Sources panel warns when the newest backup is older
LIVE_FIRST_S = 86400                 # the first live read looks back one day
LIVE_OVERLAP_S = 120
UNAVAILABLE = ("unavailable", "unknown", "")
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
BOX = {"1": "in", "2": "out"}        # SMS type / MMS msg_box: 1 inbox, 2 sent (3 draft, 4 outbox, 5 failed, 6 queued)
MMS_FROM = "137"


class TextsError(Exception):
    """A plain sentence for the Sources panel (never a path outside the folder or a message's text)."""


class BadFile(Exception):
    """A backup file Callsheet can't read; the reason is a plain sentence."""


# --- settings (messages.db meta "texts") -------------------------------------------------------------------------------
def settings(ms):
    v = ms.meta("texts", {}) or {}
    apps = [a for a in v.get("apps", DEFAULT_APPS) if a in APP_NAMES]
    sensor = v.get("sensor") if isinstance(v.get("sensor"), str) and ha.NOTIFY_SENSOR.fullmatch(v["sensor"]) else ""
    # a folder counts only when the owner saved it (folder_set); "" means texts aren't set up (nothing is read)
    folder = v.get("folder") if v.get("folder_set") and isinstance(v.get("folder"), str) else ""
    return {"folder": folder, "folder_set": bool(folder), "sensor": sensor, "apps": apps}


def update_settings(ms, body):
    """PATCH api/texts: {folder?, sensor?, apps?}. The folder must be a real folder inside /share or /media (the
    Reports rules); the sensor a `sensor.*_last_notification` id (or "" to stop live reading); apps from APPS."""
    if not isinstance(body, dict) or not body or set(body) - {"folder", "sensor", "apps"}:
        raise InputError("Send folder, sensor or apps.")
    cur = dict(ms.meta("texts", {}) or {})
    if "folder" in body:
        f = body["folder"]
        if f in ("", None):
            cur.update(folder="", folder_set=False)      # texts off: no folder is read
        else:
            conn_reports.check_folder(f)
            cur.update(folder=os.path.normpath(f.strip()), folder_set=True)
    if "sensor" in body:
        s = body["sensor"]
        if s in ("", None):
            cur["sensor"] = ""
        elif not isinstance(s, str) or not ha.NOTIFY_SENSOR.fullmatch(s):
            raise InputError("Pick the phone's Last notification sensor (sensor.…_last_notification).")
        else:
            cur["sensor"] = s
    if "apps" in body:
        a = body["apps"]
        if not isinstance(a, list) or any(x not in APP_NAMES for x in a):
            raise InputError("Pick apps from the list.")
        cur["apps"] = [x for x, _ in APPS if x in a]
    ms.set_meta("texts", cur)
    return settings(ms)


def sensors():
    """The phones' Last notification sensors Home Assistant has: for each notify.mobile_app_<phone> the sensor
    sensor.<phone>_last_notification, asked for by its own id (GET states/<sensor>; a 404 means the sensor is off in
    the Companion app). Returns [{id, name, state}]."""
    out = []
    for t in ha.notify_targets():
        sid = "sensor." + t["id"][len("mobile_app_"):] + "_last_notification"
        if not ha.NOTIFY_SENSOR.fullmatch(sid):
            continue
        try:
            st = ha.notification_state(sid)
        except ha.HAError as e:
            if e.code == "missing":
                continue
            raise
        state = str((st or {}).get("state") or "")
        name = str(((st or {}).get("attributes") or {}).get("friendly_name") or t["name"] + " Last notification")
        out.append({"id": sid, "name": name[:120], "available": state not in UNAVAILABLE})
    return out


# --- the backup XML ----------------------------------------------------------------------------------------------------
_REF = re.compile(rb"&#(?:x([0-9A-Fa-f]{1,8})|([0-9]{1,10}));")


def _cp(m):
    return int(m.group(1), 16) if m.group(1) is not None else int(m.group(2))


def _xml_ok(cp):
    return cp in (9, 10, 13) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF


def fix_refs(buf):
    """Character references XML forbids, made readable: a UTF-16 surrogate pair (`&#55357;&#56832;`, how SMS Backup &
    Restore writes emoji) becomes the one character; any other invalid reference becomes U+FFFD."""
    if b"&#" not in buf:
        return buf
    out, pos, ms = [], 0, list(_REF.finditer(buf))
    i = 0
    while i < len(ms):
        m, cp = ms[i], _cp(ms[i])
        if _xml_ok(cp):
            i += 1
            continue
        out.append(buf[pos:m.start()])
        if 0xD800 <= cp <= 0xDBFF and i + 1 < len(ms) and ms[i + 1].start() == m.end():
            lo = _cp(ms[i + 1])
            if 0xDC00 <= lo <= 0xDFFF:
                out.append(b"&#%d;" % (0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)))
                pos = ms[i + 1].end()
                i += 2
                continue
        out.append(b"&#65533;")
        pos = m.end()
        i += 1
    out.append(buf[pos:])
    return b"".join(out)


class _Refs:
    """fix_refs over a stream: a chunk is cut where no reference (or pair) can straddle the cut."""

    def __init__(self):
        self.carry = b""

    def feed(self, chunk):
        buf = self.carry + chunk
        cut = len(buf) - 14
        while cut > 0:
            j = buf.rfind(b"&", max(0, cut - 14), cut)
            if j == -1:
                break
            cut = j
        cut = max(cut, 0)
        self.carry = buf[cut:]
        if len(self.carry) > MAX_CARRY:
            raise BadFile("It has an unreadable run of character codes.")
        return fix_refs(buf[:cut])

    def flush(self):
        out, self.carry = fix_refs(self.carry), b""
        return out


def _attr(a, k):
    v = a.get(k)
    return "" if v is None or v == "null" else str(v)


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def addr_key(address):
    """One address as a thread key: a phone number by its last 10 digits ("+1 (555) 123-4567" = "5551234567"),
    a short code or name (e.g. "GOOGLE") lowercased."""
    a = str(address or "").strip()
    d = _digits(a)
    if d and len(d) >= len(re.sub(r"[\s()+.\-]", "", a)):
        return d[-10:]
    return a.lower()[:100]


def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n]


def _names(contact, n):
    """A thread's contact_name split per address ("Dana, Sam" for "a~b") when the counts agree."""
    c = "" if contact in ("", "(Unknown)") else contact
    parts = [p.strip() for p in c.split(",")] if c else []
    return parts if len(parts) == n else ([c] if n == 1 and c else [""] * n)


def _mid(kind, key, date, box, text):
    return kind[0] + hashlib.sha256(f"{kind}|{key}|{date}|{box}|{text}".encode()).hexdigest()[:24]


def sms_record(a):
    """One <sms> element's attributes -> a store record, or (None, why)."""
    direction = BOX.get(_attr(a, "type"))
    if direction is None:
        return None, "draft or unsent"
    address = _attr(a, "address")
    try:
        date = int(_attr(a, "date"))
    except ValueError:
        return None, "no time"
    text = _clip(_attr(a, "body"), MAX_TEXT)
    key = addr_key(address)
    name = _names(_attr(a, "contact_name"), 1)[0]
    peer = {"name": name, "address": address}
    rec = {"source": "sms", "account": ACCOUNT, "msg_id": _mid("sms", key, date, direction, text),
           "thread": "sms:" + key, "direction": direction, "sender": address if direction == "in" else "me",
           "sender_name": name if direction == "in" else "", "recipients": [peer] if direction == "out" else [],
           "ts": date / 1000.0, "subject": _clip(_attr(a, "subject"), 300), "text": text, "labels": ["sms"],
           "channel": "Texts with " + (name or address or "someone"), "link": "", "attachments": []}
    return rec, None


def mms_record(a, parts, addrs):
    """One <mms> element (its attributes, text and attachment parts, addresses) -> a store record, or (None, why)."""
    direction = BOX.get(_attr(a, "msg_box"))
    if direction is None:
        return None, "draft or unsent"
    try:
        date = int(_attr(a, "date"))
    except ValueError:
        return None, "no time"
    if date < 100_000_000_000:   # some phones keep MMS times in seconds
        date *= 1000
    others = [x for x in _attr(a, "address").split("~") if x]
    names = _names(_attr(a, "contact_name"), len(others))
    peers = [{"name": n, "address": x} for n, x in zip(names, others)]
    key = "~".join(sorted({addr_key(x) for x in others})) or "unknown"
    texts, files = [], []
    for p in parts:
        ct = p.get("ct", "").lower()
        if ct == "application/smil":
            continue
        if ct == "text/plain" and not p.get("fn"):
            if p.get("text"):
                texts.append(p["text"])
        elif len(files) < MAX_ATTACH:
            files.append({"name": _clip(p.get("fn") or p.get("cl") or p.get("name") or "", 200), "type": _clip(ct, 100)})
    text = _clip("\n".join(texts), MAX_TEXT)
    sender = "me"
    sender_name = ""
    if direction == "in":
        frm = next((x["address"] for x in addrs if x["type"] == MMS_FROM and x["address"]), others[0] if others else "")
        sender = frm
        k = addr_key(frm)
        sender_name = next((p["name"] for p in peers if addr_key(p["address"]) == k), "")
    group = len(others) > 1
    label = ", ".join(p["name"] or p["address"] for p in peers) or "someone"
    recips = peers if direction == "out" else [p for p in peers if addr_key(p["address"]) != addr_key(sender)]
    rec = {"source": "sms", "account": ACCOUNT, "msg_id": _mid("mms", key, date, direction, text + "|" + json.dumps(files)),
           "thread": "sms:" + key, "direction": direction, "sender": sender, "sender_name": sender_name,
           "recipients": recips, "ts": date / 1000.0, "subject": _clip(_attr(a, "sub"), 300), "text": text,
           "labels": ["mms"] + (["group"] if group else []),
           "channel": ("Group text: " if group else "Texts with ") + label, "link": "", "attachments": files}
    return rec, None


class _Parser:
    """expat, as a stream; no DTD, no entities; calls emit(record) for each kept message."""

    def __init__(self, emit):
        self.emit = emit
        self.depth = 0
        self.root = None
        self.backup_date = None
        self.mms = None
        self.seen = 0
        self.skipped = {}
        p = xml.parsers.expat.ParserCreate()
        p.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
        p.StartDoctypeDeclHandler = self._refuse
        p.EntityDeclHandler = self._refuse
        p.UnparsedEntityDeclHandler = self._refuse
        p.ExternalEntityRefHandler = self._refuse
        p.StartElementHandler = self._start
        p.EndElementHandler = self._end
        self.p = p

    @staticmethod
    def _refuse(*_a):
        raise BadFile("It declares a DTD or entities, which a phone backup never has.")

    def _skip(self, why):
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def _start(self, name, a):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise BadFile("It is nested deeper than a phone backup.")
        if self.depth == 1:
            if name != "smses":
                raise BadFile("It isn't an SMS Backup & Restore file (calls and other files are ignored).")
            self.root = name
            try:
                self.backup_date = int(_attr(a, "backup_date")) / 1000.0 or None
            except ValueError:
                self.backup_date = None
            return
        if self.depth == 2:
            if name in ("sms", "mms"):
                self.seen += 1
                if self.seen > MAX_MESSAGES:
                    raise BadFile(f"It holds more than {MAX_MESSAGES:,} messages.")
            if name == "sms":
                rec, why = sms_record(a)
                if rec is not None:
                    self.emit(rec)
                else:
                    self._skip(why)
            elif name == "mms":
                self.mms = (dict((k, a[k]) for k in a if k != "data"), [], [])
            return
        if self.mms is not None:
            if name == "part" and len(self.mms[1]) < MAX_ATTACH + 10:
                self.mms[1].append({"ct": _attr(a, "ct"), "text": _clip(_attr(a, "text"), MAX_TEXT),
                                    "fn": _attr(a, "fn"), "cl": _attr(a, "cl"), "name": _attr(a, "name")})
            elif name == "addr" and len(self.mms[2]) < 200:
                self.mms[2].append({"address": _attr(a, "address"), "type": _attr(a, "type")})

    def _end(self, name):
        if self.depth == 2 and name == "mms" and self.mms is not None:
            rec, why = mms_record(*self.mms)
            self.mms = None
            if rec is not None:
                self.emit(rec)
            else:
                self._skip(why)
        self.depth -= 1

    def feed(self, data, final=False):
        try:
            self.p.Parse(data, final)
        except xml.parsers.expat.ExpatError as e:
            raise BadFile(f"It isn't well-formed XML (line {e.lineno}).") from None
        if final and self.root is None:
            raise BadFile("It is empty.")


def parse_bytes(raw, emit=None):
    """Parse a whole backup held in memory (tests and tools): (records, parser)."""
    out = []
    ps = _Parser(emit or out.append)
    refs = _Refs()
    for i in range(0, len(raw), CHUNK):
        ps.feed(refs.feed(raw[i:i + CHUNK]))
    ps.feed(refs.flush(), True)
    return out, ps


# --- the folder --------------------------------------------------------------------------------------------------------
class Folder:
    """One look at the backup folder through a directory descriptor (it can't be swapped for a link mid-read)."""

    def __init__(self, real):
        self.real = real
        self.fd = os.open(real, _DIR_FLAGS)

    def close(self):
        os.close(self.fd)

    def entries(self, wall):
        files, skipped = [], []
        with os.scandir(self.fd) as it:
            for i, e in enumerate(it):
                if i >= MAX_ENTRIES:
                    break
                name = e.name
                if name.startswith(".") or not name.lower().startswith("sms-") or not name.lower().endswith(".xml"):
                    continue                    # calls-*.xml, Syncthing's temporary files, anything else
                if not FILE_NAME.fullmatch(name):
                    skipped.append((conn_reports.shown_name(name), "The file name has odd characters or is too long."))
                    continue
                try:
                    st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    skipped.append((name, "It's a link. Callsheet reads only files in the folder itself."))
                    continue
                if not stat.S_ISREG(st.st_mode):
                    skipped.append((name, "It isn't a regular file."))
                    continue
                if st.st_nlink > 1:
                    skipped.append((name, "It's linked from another place. Callsheet reads only plain files."))
                    continue
                files.append({"name": name, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino,
                              "young": -STABLE_S < wall - st.st_mtime_ns / 1e9 < STABLE_S})
        return files, skipped

    def read(self, f, emit):
        """Stream the file into the parser. Returns the parser, or None when the file changed while it was read
        (still arriving: read next pass). Raises BadFile."""
        if f["size"] > MAX_FILE_BYTES:
            raise BadFile(f"It's bigger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        try:
            fd = os.open(f["name"], _OPEN_FLAGS, dir_fd=self.fd)
        except OSError:
            raise BadFile("Callsheet couldn't open it.") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_ino != f["ino"] or st.st_nlink > 1:
                raise BadFile("It changed into something that isn't a plain file.")
            if st.st_size > MAX_FILE_BYTES:
                raise BadFile(f"It's bigger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            ps, refs, n = _Parser(emit), _Refs(), 0
            while True:
                b = os.read(fd, CHUNK)
                if not b:
                    break
                n += len(b)
                if n > MAX_FILE_BYTES:
                    raise BadFile(f"It's bigger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
                ps.feed(refs.feed(b))
            ps.feed(refs.flush(), True)
            st2 = os.fstat(fd)
            if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns or st.st_mtime_ns != f["mtime_ns"]:
                return None
            return ps
        finally:
            os.close(fd)


def _same(old, f):
    return old is not None and old.get("size") == f["size"] and old.get("mtime_ns") == f["mtime_ns"]


class BackupReader:
    """live.Live's reader for the nightly backup (kind "sms"): one target, the folder."""
    key = "sms"

    def __init__(self, ms, clock=time.time):
        self.ms, self.clock = ms, clock

    def targets(self):
        cfg = settings(self.ms)
        if cfg["folder"]:                  # 0.21.2: only a folder the owner saved
            return [(ACCOUNT, "Texts")]
        return []

    def tick(self, _account):
        cfg = settings(self.ms)
        if not cfg["folder"]:
            raise TextsError("No backup folder is set. Pick one in Setup › Connections › Texts.")
        try:
            real = conn_reports.check_folder(cfg["folder"])
        except InputError as e:
            raise TextsError(f"{cfg['folder']}: {e}") from None
        now = self.clock()
        try:
            folder = Folder(real)
        except OSError:
            raise TextsError(f"Callsheet can't open {cfg['folder']}.") from None
        try:
            files, skipped = folder.entries(now)
            seen = self.ms.cursor("sms", ACCOUNT, "files") or {}
            todo = sorted((f for f in files if not _same(seen.get(f["name"]), f)), key=lambda f: -f["mtime_ns"])
            ready = [f for f in todo if not f["young"]]
            new, newest, done = 0, None, 0
            for f in ready[:MAX_FILES_PASS]:
                res = self._read_file(folder, f)
                if res is None:      # changed while read: next pass
                    continue
                seen[f["name"]] = res
                new += res.get("new", 0)
                if res.get("newest"):
                    newest = max(newest or 0, res["newest"])
                done += 1
                self._save_seen(seen, files)
            self._save_seen(seen, files)
            self.ms.set_cursor("sms", ACCOUNT, "skipped", skipped[:20])
        finally:
            folder.close()
        link(self.ms, self.clock())
        return {"new": new, "newest": newest, "backlog": len(todo) - done}

    def _save_seen(self, seen, files):
        names = {f["name"] for f in files}
        keep = dict(sorted(((k, v) for k, v in seen.items() if k in names), key=lambda kv: -kv[1].get("mtime_ns", 0))
                    [:KEEP_FILES])
        self.ms.set_cursor("sms", ACCOUNT, "files", keep)

    def _read_file(self, folder, f):
        batch, out = [], {"new": 0, "same": 0, "updated": 0, "newest": None}

        def flush():
            if batch:
                r = self.ms.upsert_messages(batch)
                for k in ("new", "same", "updated"):
                    out[k] += r[k]
                if r["newest"]:
                    out["newest"] = max(out["newest"] or 0, r["newest"])
                batch.clear()

        def emit(rec):
            batch.append(rec)
            if len(batch) >= BATCH:
                flush()

        base = {"size": f["size"], "mtime_ns": f["mtime_ns"], "ino": f["ino"], "at": self.clock()}
        try:
            ps = folder.read(f, emit)
            if ps is None:
                flush()
                return None
            flush()
        except BadFile as e:
            flush()   # what was read before the problem is kept (the same messages again are harmless)
            kept = out["new"] + out["same"] + out["updated"]
            return dict(base, status="bad", why=str(e), messages=kept, new=out["new"], newest=out["newest"])
        kept = out["new"] + out["same"] + out["updated"]
        return dict(base, status="read", why="", messages=kept, new=out["new"], newest=out["newest"],
                    backup_at=ps.backup_date or f["mtime_ns"] / 1e9, skipped=ps.skipped)


# --- live notifications ------------------------------------------------------------------------------------------------
def _when(v):
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(_s(x) for x in v)
    return str(v).strip()


def notification_record(state, sensor, apps):
    """One state of the Last notification sensor -> a store record (source "sms_live"), or None (another app, no
    text, an ongoing notification, the sensor unavailable)."""
    if not isinstance(state, dict) or str(state.get("state") or "") in UNAVAILABLE:
        return None
    a = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    pkg = _s(a.get("package"))
    if pkg not in apps:
        return None
    if a.get("is_ongoing") in (True, "true", "True") or a.get("android.isGroupSummary") in (True, "true"):
        return None
    title = _clip(_s(a.get("android.title")), 300)
    short, big = _s(a.get("android.text")), _s(a.get("android.bigText"))
    text = big if len(big) > len(short) else short
    text = text or _s(state.get("state"))
    if not text or not title:
        return None
    post = a.get("post_time")
    try:
        ts = float(post) / 1000.0 if post not in (None, "") else None
    except (TypeError, ValueError):
        ts = None
    ts = ts or _when(state.get("last_changed")) or _when(state.get("last_updated"))
    if not ts:
        return None
    conv = _clip(_s(a.get("android.conversationTitle")), 300)
    group = a.get("android.isGroupConversation") in (True, "true", "True") and bool(conv)
    app = APP_NAMES.get(pkg, pkg)
    text = _clip(text, MAX_TEXT)
    mid = "n" + hashlib.sha256(f"{pkg}|{title}|{conv}|{text}|{int(ts)}".encode()).hexdigest()[:24]
    return {"source": "sms_live", "account": sensor, "msg_id": mid, "thread": f"live:{pkg}:{conv if group else title}",
            "direction": "in", "sender": title, "sender_name": title, "recipients": [], "ts": ts, "subject": "",
            "text": text, "labels": ["notification", app], "channel": app + (" · " + conv if group else ""),
            "link": "", "attachments": []}


class NotificationReader:
    """live.Live's reader for the phone's Last notification sensor (kind "sms_live"): one target, the sensor."""
    key = "sms_live"

    def __init__(self, ms, clock=time.time, api=ha):
        self.ms, self.clock, self.api = ms, clock, api

    def targets(self):
        s = settings(self.ms)["sensor"]
        return [(s, "Texts (live)")] if s else []

    def tick(self, sensor):
        cfg, now = settings(self.ms), self.clock()
        if sensor != cfg["sensor"]:
            return {"new": 0, "newest": None, "backlog": 0}
        st = self.api.notification_state(sensor) or {}
        state = str(st.get("state") or "")
        upd = _when(st.get("last_updated")) or _when(st.get("last_changed"))
        prev = self.ms.cursor("sms_live", sensor, "sensor") or {}
        avail = state not in UNAVAILABLE
        self.ms.set_cursor("sms_live", sensor, "sensor", {
            "available": avail, "checked": now, "changed": upd,
            "since": prev.get("since") if (not avail and prev.get("available") is False) else (None if avail else now)})
        cur = self.ms.cursor("sms_live", sensor, "since") or {"t": now - LIVE_FIRST_S}
        new, newest = 0, None
        if upd is None or upd > cur["t"]:
            hist = self.api.notification_history(sensor, max(0, cur["t"] - LIVE_OVERLAP_S))
            recs, top = [], cur["t"]
            for s in hist:
                t = _when(s.get("last_updated")) or _when(s.get("last_changed"))
                if t:
                    top = max(top, t)
                r = notification_record(s, sensor, cfg["apps"])
                if r is not None and not self._repeat(r, recs):
                    recs.append(r)
            if recs:
                res = self.ms.upsert_messages(recs)
                new, newest = res["new"], res["newest"]
            self.ms.set_cursor("sms_live", sensor, "since", {"t": max(top, upd or 0)})
        if new:
            link(self.ms, now)
        return {"new": new, "newest": newest, "backlog": 0}

    def _repeat(self, r, pending):
        """The same sender and text again within DEDUPE_S (an app re-posting its notification) is one message."""
        for p in pending:
            if (p["sender"], p["text"]) == (r["sender"], r["text"]) and abs(p["ts"] - r["ts"]) <= DEDUPE_S \
                    and p["msg_id"] != r["msg_id"]:
                return True
        rows = self.ms._read(  # noqa: SLF001
            "SELECT msg_id FROM messages WHERE source='sms_live' AND account=? AND sender=? AND text=? AND ts BETWEEN ? "
            "AND ?", (r["account"], r["sender"], r["text"], r["ts"] - DEDUPE_S, r["ts"] + DEDUPE_S))
        return any(x["msg_id"] != r["msg_id"] for x in rows)


# --- linking a live notification to its backup copy ---------------------------------------------------------------------
def _norm(s):
    return " ".join(str(s or "").split()).casefold().rstrip("…").rstrip(".").strip()


def _who(s):
    return re.sub(r"[\W_]+", "", str(s or "").casefold())


def _same_sender(live, bak):
    w = _who(live["sender_name"] or live["sender"])
    if w and w in (_who(bak["sender_name"]), _who(bak["sender"])):
        return True
    d = _digits(live["sender"])
    return len(d) >= 7 and d[-10:] == _digits(bak["sender"])[-10:]


def _same_text(live, bak):
    a, b = _norm(live["text"]), _norm(bak["text"])
    if not a or not b:
        return False
    if a == b or (len(a) >= 20 and b.startswith(a)):   # a notification may cut a long text
        return True
    return False


def _match(live, bak):
    if _same_sender(live, bak) and _same_text(live, bak):
        return True
    # a group notification: "Sam: see you at 6" from the group's title
    m = re.match(r"([^:\n]{1,60}):\s+(.+)", str(live["text"] or ""), re.S)
    if m and _who(m.group(1)) in (_who(bak["sender_name"]), _who(bak["sender"])):
        return _same_text(dict(live, text=m.group(2)), bak)
    return False


def link(ms, now):
    """Link each unlinked live notification (last LINK_DAYS) to the backup copy of the same message. Returns how many
    were linked."""
    lives = [dict(r) for r in ms._read(  # noqa: SLF001
        "SELECT id, sender, sender_name, text, ts FROM messages WHERE source='sms_live' AND same_as IS NULL AND ts>=? "
        "ORDER BY ts", (now - LINK_DAYS * 86400,))]
    pairs, used = [], set()
    for lv in lives:
        cands = [dict(r) for r in ms._read(  # noqa: SLF001
            "SELECT id, sender, sender_name, text, ts FROM messages WHERE source='sms' AND direction='in' AND "
            "deleted_at IS NULL AND ts BETWEEN ? AND ? ORDER BY abs(ts - ?)",
            (lv["ts"] - LINK_WINDOW_S, lv["ts"] + LINK_WINDOW_S, lv["ts"]))]
        for c in cands:
            if c["id"] not in used and _match(lv, c):
                pairs.append((lv["id"], c["id"]))
                used.add(c["id"])
                break
    if not pairs:
        return 0

    def run(db):
        ledger = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_reads'").fetchone())
        for lid, bid in pairs:
            db.execute("UPDATE messages SET same_as=? WHERE id=? AND same_as IS NULL", (bid, lid))
            if not ledger:
                continue
            got = db.execute("SELECT state, items FROM ledger_reads WHERE message=?", (lid,)).fetchone()
            if got is None or got["state"] == "failed":
                continue   # not read yet: the ledger reads the backup copy instead
            if db.execute("SELECT 1 FROM ledger_reads WHERE message=?", (bid,)).fetchone() is None:
                db.execute("INSERT INTO ledger_reads (message, state, tries, at, items, drops, note) VALUES "
                           "(?, 'skipped', 0, ?, ?, '[]', ?)",
                           (bid, now, got["items"], f"the same text as live notification {lid}, read earlier"))
            db.execute("INSERT OR IGNORE INTO ledger_sources (item, message, role, at) SELECT item, ?, role, at FROM "
                       "ledger_sources WHERE message=?", (bid, lid))
        return len(pairs)
    return ms.write(run)


# --- the Sources panel ---------------------------------------------------------------------------------------------------
def _clock_time(ts, now, tz):
    d = dt.datetime.fromtimestamp(ts, tz)
    hm = d.strftime("%H:%M")
    if now - ts < 86400 and d.date() == dt.datetime.fromtimestamp(now, tz).date():
        return hm
    if now - ts < 86400:
        return "yesterday " + hm
    return d.strftime("%a %b ") + str(d.day) + " " + hm


def backup_status(ms, now, tz):
    """Extra facts for the backup row: (line, warning or None, stale)."""
    files = ms.cursor("sms", ACCOUNT, "files") or {}
    good = [v for v in files.values() if v.get("status") == "read"]
    n = ms.count("sms")
    warns, stale = [], False
    if good:
        last = max(good, key=lambda v: v.get("backup_at") or 0)
        at = last.get("backup_at") or 0
        line = f"Nightly backup {_clock_time(at, now, tz)} · {n:,} message{'s' if n != 1 else ''}"
        if now - at > BACKUP_OLD_S:
            stale = True
            hours = int((now - at) // 3600)
            age = f"{hours // 24} days" if hours >= 48 else f"{hours} hours"
            warns.append(f"The newest text backup is {age} old. Check SMS Backup & Restore and Syncthing on the phone.")
    else:
        line = "No backup read yet" + (f" · {n:,} messages" if n else "")
    newest = max(files.items(), key=lambda kv: kv[1].get("mtime_ns", 0), default=None)
    if newest and newest[1].get("status") == "bad":
        stale = True
        warns.append(f"Couldn't read {conn_reports.shown_name(newest[0])}: {newest[1].get('why') or 'no reason given'}")
    for name, why in (ms.cursor("sms", ACCOUNT, "skipped") or [])[:3]:
        warns.append(f"Skipped {name}: {why}")
    return line, " ".join(warns) or None, stale


def live_status(ms, sensor, now, tz):
    """Extra facts for the live row: (line, warning or None, unavailable)."""
    from live import when   # noqa: PLC0415 (live imports this module)
    newest = ms.newest_ts("sms_live", sensor)
    line = ("Last notification " + when(newest, now, tz)) if newest else "No text notifications yet"
    s = ms.cursor("sms_live", sensor, "sensor") or {}
    if s and s.get("available") is False:
        since = s.get("since")
        return line, (f"The phone's Last notification sensor ({sensor}) is unavailable"
                      + (f" since {when(since, now, tz)}" if since else "")
                      + ". Open the Home Assistant app on the phone; the nightly backup still brings the texts."), True
    return line, None, False
