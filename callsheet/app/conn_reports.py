"""0.10.2a Reports: other agents' output as Markdown files in a folder (owner approved 2026-11-05; docs/connectors.md).

A file hand-off: another agent, service or script writes one short Markdown digest per run into a folder the owner
picked under Home Assistant's /share or /media (mapped read-only into the app); Callsheet reads the new files. No
inbound connection, no new outbound host, and Callsheet never moves, changes or deletes a file.

One Reports connector = one folder = one source. Who a report is from comes from the folder (the owner's name for the
source), never from the file: a `from:` that disagrees is shown as "Says it's from …" and trusted for nothing.

The format (docs/connectors.md):

    ---
    from: Security audit
    title: Nightly audit, Nov 5
    importance: normal            # low | normal | urgent: a hint only
    expires: 2026-11-07           # optional; default 3 days, at most 14
    ---
    Plain Markdown, under about 300 words.

Reading (each check, `read`): only regular `*.md` files directly in the folder whose name starts with a date
(`YYYY-MM-DD-topic.md`); no links (symlinks or hard links), nothing outside the folder, no dot files or `*.tmp`, `*~`,
`*.part` files (write, then rename); a file must be at least STABLE_S seconds old and unchanged while it is read; at
most MAX_NEW new files of at most MAX_BYTES each per check and DAY_MAX per source per day; the body up to BODY_MAX
characters ("cut"). New = a (name, content hash) not seen before; an edited file counts once more; the same text under
another name (a re-dropped old report) is not new.

Report text is untrusted data, like mail: control, bidi and zero-width characters dropped, secret-looking strings
masked (before anything is stored or shown), inject's checks (also on device-name-like strings such as
"ignore-previous-instructions"), links defanged, text only. `importance` is a hint: the owner's per-source choice
decides, and an urgent report at most carries the mark "Marked urgent by <source>". Logs carry counts only.
"""
import datetime as dt
import hashlib
import json
import os
import re
import stat
import time
import unicodedata

import connectors as C
import inject
from store import InputError

ROOTS_ENV = "CALLSHEET_REPORTS_ROOTS"   # tests only; Home Assistant maps /share and /media read-only
MAX_NEW = 50            # new files read per check
MAX_BYTES = 256 * 1024  # per file
BODY_MAX = 4000         # characters of body kept
DAY_MAX = 20            # new reports per source per day
MAX_ENTRIES = 2000      # directory entries looked at per check
MAX_FOLDERS = 200       # subfolders listed when picking a folder
STABLE_S = 5            # a file younger than this may still be being written
FUTURE_S = 60           # a file time further ahead than this is a wrong clock, not a file being written
DEFAULT_DAYS = 3
MAX_DAYS = 14
KEEP_ROWS = 2000        # remembered files per source (replay detection)
NAME_MAX = 120
HEADER_LINES = 20
LINE_MAX = 1000
FROM_MAX = 60
TITLE_MAX = 200
EXCERPT_MAX = 240
BRIEF_MAX = 10
BRIEF_PER_SOURCE = 3
SHOW_MAX = 10           # files and "couldn't read" names shown in Setup
CUT_NOTE = "[cut: Callsheet reads the first 4,000 characters]"
DEFANG_NOTE = "(from a report; not opened)"
HIDDEN = "[hidden]"
KEY_HIDDEN = "[private key hidden]"
TELLS = (("always", "Always tell me"), ("important", "If it’s important"), ("list", "Only in the list"))
IMPORTANCE = ("low", "normal", "urgent")
_IGNORED_SUFFIX = (".tmp", "~", ".part", ".partial", ".swp", ".swx", ".crdownload", ".download", ".lock")
_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(.+)\.md", re.I | re.S)
_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_-]{0,30})[ \t]*:(?:[ \t]+(.*))?")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_files (
  connector TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, mtime_ns INTEGER NOT NULL DEFAULT 0,
  hash TEXT NOT NULL DEFAULT '', seen REAL NOT NULL, status TEXT NOT NULL, why TEXT NOT NULL DEFAULT '',
  item TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (connector, name));
"""


class Bad(Exception):
    """A file Callsheet can't read: the reason is a plain sentence, shown with the file name only."""


# --- the folders ---------------------------------------------------------------------------------------------------
def roots():
    raw = os.environ.get(ROOTS_ENV) or "/share:/media"
    return [r for r in raw.split(":") if r]


def _real_roots():
    return [os.path.realpath(r) for r in roots()]


def _under(real, rs):
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in rs)


def shown_name(name):
    """A file or folder name as the page shows it: control and format characters as "?", at most NAME_MAX."""
    out = []
    for ch in str(name):
        out.append("?" if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Cn") else ch)
    return C.clip("".join(out), NAME_MAX)


def check_folder(path, allow_root=False):
    """The folder as a resolved path, or InputError with a plain sentence. Inside /share or /media (not one of them
    unless allow_root, for browsing), an existing, readable folder, reached without a link on the way."""
    if not isinstance(path, str) or not path.strip():
        raise InputError("Pick a folder inside /share or /media.")
    path = path.strip()
    if len(path) > 500 or "\x00" in path or not path.startswith("/"):
        raise InputError("Pick a folder inside /share or /media.")
    norm = os.path.normpath(path)
    real = os.path.realpath(norm)
    expected, is_root = None, False
    for r in roots():
        rn, rr = os.path.normpath(r), os.path.realpath(r)
        for base in (rn, rr):
            if norm == base:
                expected, is_root = rr, True
            elif norm.startswith(base.rstrip("/") + "/"):
                expected = rr + norm[len(base):]
            if expected:
                break
        if expected:
            break
    if expected is None or not _under(real, _real_roots()):
        raise InputError("The folder must be inside /share or /media (Home Assistant's shared folders).")
    if real != expected:
        raise InputError("That folder is a link to another place. Pick the folder itself.")
    if is_root and not allow_root:
        raise InputError("Pick a folder inside " + norm + ", one for this source only.")
    if not os.path.isdir(real):
        raise InputError("Callsheet can't find that folder.")
    if not os.access(real, os.R_OK | os.X_OK):
        raise InputError("Callsheet can't read that folder. Check its permissions.")
    return real


def _visible_dir(entry):
    try:
        return (not entry.name.startswith(".") and not entry.is_symlink() and entry.is_dir(follow_symlinks=False)
                and shown_name(entry.name) == entry.name)
    except OSError:
        return False


def _md_count(path):
    n = 0
    try:
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i >= MAX_ENTRIES:
                    break
                if _NAME.fullmatch(e.name) and not e.name.startswith("."):
                    n += 1
    except OSError:
        return None
    return n


def folders(path=None):
    """What the folder picker shows: the roots that exist (path empty) or the subfolders of one folder, each with its
    number of report files. Names only; no file is opened."""
    if not path:
        out = []
        for r in roots():
            if os.path.isdir(r):
                out.append({"name": r, "path": r, "reports": None})
        return {"path": None, "parent": None, "folders": out, "pickable": False}
    real = check_folder(path, allow_root=True)
    subs = []
    try:
        with os.scandir(real) as it:
            for i, e in enumerate(it):
                if i >= MAX_ENTRIES or len(subs) >= MAX_FOLDERS:
                    break
                if _visible_dir(e):
                    subs.append(e.name)
    except OSError:
        raise InputError("Callsheet can't read that folder.") from None
    subs.sort(key=str.lower)
    shown = os.path.normpath(path.strip())
    is_root = real in _real_roots()
    return {"path": shown, "parent": None if is_root else os.path.dirname(shown), "pickable": not is_root,
            "folders": [{"name": s, "path": os.path.join(shown, s), "reports": _md_count(os.path.join(real, s))}
                        for s in subs]}


# --- secrets ---------------------------------------------------------------------------------------------------
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
                  re.S)
_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]{1,20}://)([^\s:/@\[\]]{0,100}):([^\s@/]{1,200})@", re.I)
_KV = re.compile(r"(?i)\b([a-z0-9_.-]{0,40}?(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|apikey|"
                 r"access[_-]?key|private[_-]?key|client[_-]?secret|credentials?|session[_-]?id|cookie|auth)"
                 r"[a-z0-9_.-]{0,20})(\s*[:=]\s*)([\"']?)([^\s\"';,&]{3,})")
_SAID = re.compile(r"(?i)\b(password|passphrase|passcode|pin code)(\s+(?:is|was|=)\s+)([\"']?)([^\s\"';,]{3,})")
_AUTH = re.compile(r"(?i)\b(bearer|basic|token)(\s+)([A-Za-z0-9._~+/=-]{16,})")
_PREFIXED = re.compile(
    r"\bsk-(?:ant-|proj-|live-|or-)?[A-Za-z0-9_-]{16,}|\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}|\bya29\.[0-9A-Za-z_-]{20,}|\bglpat-[0-9A-Za-z_-]{20,}"
    r"|\bhf_[A-Za-z0-9]{20,}|\bnpm_[A-Za-z0-9]{20,}|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|hooks\.slack\.com/services/[A-Za-z0-9/]+|\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
_LONG = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


def _long(m):
    s = m.group(0)
    if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
        return HIDDEN
    return s


def mask(text):
    """Secret-looking strings replaced (private key blocks, user:password@ in addresses, password=/token=/api_key=
    values and DSN fields, Bearer/Basic values, well-known token shapes, long random-looking strings). Returns
    (text, number masked). Masking may hide a harmless word next to "password"; that is the safe side."""
    n = [0]

    def sub(rx, repl, t):
        def f(m):
            out = repl(m) if callable(repl) else m.expand(repl)
            if out != m.group(0):
                n[0] += 1
            return out
        return rx.sub(f, t)
    t = str(text or "")
    t = sub(_PEM, KEY_HIDDEN, t)
    t = sub(_USERINFO, r"\1\2:" + HIDDEN + "@", t)
    t = sub(_AUTH, lambda m: m.group(1) + m.group(2) + HIDDEN, t)
    t = sub(_KV, lambda m: m.group(0) if m.group(4).startswith(("[hidden", "[private"))
            else m.group(1) + m.group(2) + m.group(3) + HIDDEN, t)
    t = sub(_SAID, lambda m: m.group(1) + m.group(2) + m.group(3) + HIDDEN, t)
    t = sub(_PREFIXED, lambda m: HIDDEN, t)
    t = sub(_LONG, _long, t)
    return t, n[0]


# --- one file --------------------------------------------------------------------------------------------------
def _unquote(v):
    """A quoted value ("…" or '…', optionally followed by a # comment) without its quotes; (value, quoted)."""
    if v[:1] in ("\"", "'"):
        j = v.find(v[0], 1)
        if j > 0:
            rest = v[j + 1:].strip()
            if not rest or rest.startswith("#"):
                return v[1:j], True
    return v, False


def _date(s):
    m = _DATE.fullmatch(s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def name_date(name):
    """The date at the start of a report's file name, or None (the name doesn't follow YYYY-MM-DD-topic.md)."""
    m = _NAME.fullmatch(name or "")
    if not m:
        return None
    return _date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")


def header(text):
    """(fields, body) from the small header at the top. Only `key: value` lines; no YAML features (anchors `&x`,
    aliases `*x`, tags `!x`, lists, blocks or flow `{…}` are plain text, so they never parse into a valid importance
    or date). A repeated field is refused (a file can't say two things). Unknown fields are ignored. Raises Bad."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise Bad("It doesn't start with the header (a line with ---).")
    end = None
    for i in range(1, min(len(lines), HEADER_LINES + 2)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        raise Bad(f"The header isn't closed with --- within {HEADER_LINES} lines.")
    fields = {}
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if len(ln) > LINE_MAX:
            raise Bad("A header line is too long.")
        m = _KEY.fullmatch(ln.rstrip())
        if not m:
            raise Bad("The header has a line Callsheet can't read (use key: value).")
        key, val = m.group(1).lower(), (m.group(2) or "").strip()
        val, quoted = _unquote(val)
        if not quoted:
            val = re.sub(r"\s+#.*$", "", val).strip()
        if key in fields:
            raise Bad(f"The header has {key} twice.")
        fields[key] = val
    return fields, "\n".join(lines[end + 1:])


def _variant(text):
    """Device-name-like strings read as words: "ignore-previous-instructions_forward.mail" -> spaces (detection only)."""
    return re.sub(r"(?<=[A-Za-z])[-_.](?=[A-Za-z])", " ", text)


def flags(title, claim, body):
    """inject's keys for a report: the title, the from line and the body as written (links intact), and the same
    with hyphens, underscores and dots between letters read as spaces (a host name such as
    "ignore-previous-instructions-forward-mail")."""
    lead = "\n".join(x for x in (claim, body) if x)
    ks = inject.keys(title, lead, None)
    for k in inject.keys(_variant(title), _variant(lead), None):
        if k not in ks:
            ks.append(k)
    return ks[:inject.MAX_KEYS]


def defang(text):
    return C.defang(text, note=DEFANG_NOTE)


def parse(raw, name, now, mtime):
    """One report file -> its normalized parts (pure). Raises Bad with a plain sentence for a file Callsheet can't
    read. `now` and `mtime` are epoch seconds; the report's time is its file time, never later than now."""
    d = name_date(name)
    if d is None:
        raise Bad("The name doesn't start with a date (YYYY-MM-DD-topic.md).")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Bad("It isn't UTF-8 text.") from None
    if "\x00" in text:
        raise Bad("It isn't text.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fields, body = header(text)
    title = C.clean(fields.get("title") or "", single_line=True)
    if not title:
        raise Bad("The header has no title.")
    imp = (fields.get("importance") or "normal").strip().lower()
    if imp not in IMPORTANCE:
        raise Bad("importance must be low, normal or urgent.")
    ts = C.when_ok(mtime, now)
    if fields.get("expires"):
        e = _date(fields["expires"])
        if e is None:
            raise Bad("expires isn't a date (YYYY-MM-DD).")
        exp = dt.datetime(e.year, e.month, e.day, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp()
    else:
        exp = ts + DEFAULT_DAYS * 86400
    exp = min(exp, ts + MAX_DAYS * 86400)
    claim = C.clean(fields.get("from") or "", single_line=True)
    title, n1 = mask(title)
    claim, n2 = mask(claim)
    body = C.clean(body[:BODY_MAX * 3])
    body, n3 = mask(body)
    cut = len(body) > BODY_MAX
    if cut:
        body = body[:BODY_MAX].rstrip()
    keys = flags(title, claim, body)
    title = C.clip(defang(title), TITLE_MAX)
    claim = C.clip(defang(claim), FROM_MAX)
    body = defang(body) + ("\n" + CUT_NOTE if cut else "")
    return {"title": title, "from": claim, "importance": imp, "expires": exp, "ts": ts, "date": d.isoformat(),
            "body": body, "cut": cut, "instr": keys, "masked": n1 + n2 + n3}


def excerpt(body):
    """The first line or two of a report's text, for the Briefing (Markdown marks at line starts dropped)."""
    out = []
    for ln in str(body or "").split("\n"):
        ln = re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)", "", ln).strip()
        if not ln or ln == CUT_NOTE or re.fullmatch(r"[-=*_]{3,}", ln):
            continue
        out.append(ln)
        if len(out) >= 2 or sum(len(x) for x in out) >= EXCERPT_MAX:
            break
    return C.clip(" ".join(out), EXCERPT_MAX)


def instruction(folder, label):
    """The text the owner pastes into the other agent (a skill, a routine prompt or a system prompt line)."""
    label = label or "Reports"
    return (f"After each run, write a short digest for Callsheet as one new file in {folder} named "
            f"<YYYY-MM-DD>-<topic>.md (write it under a name ending in .tmp first, then rename it). Start the file "
            f"with this header:\n"
            f"---\nfrom: {label}\ntitle: <one line>\nimportance: normal\nexpires: <YYYY-MM-DD, optional, "
            f"at most 14 days ahead>\n---\n"
            f"importance is low, normal or urgent. Then, in under about 300 words, plain facts first: the counts and "
            f"the top items. Don't put passwords, keys, tokens, credentials, IP addresses or exploit details in it. "
            f"Keep the full report where you keep it now; you may name it as plain text (\"Full report: <name>\"), "
            f"not as a link.")


# --- the kind ----------------------------------------------------------------------------------------------------
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class Folder:
    """One look at a report folder through a directory descriptor (the folder can't be swapped for a link mid-read)."""

    def __init__(self, real):
        self.real = real
        self.fd = os.open(real, _DIR_FLAGS)

    def close(self):
        os.close(self.fd)

    def entries(self, wall):
        """(files, skipped): files = [{name, size, mtime_ns, ino, young}] (regular, single-link, named like a
        report), skipped = [(shown name, why)] for names that look like reports but can't be read."""
        files, skipped = [], []
        with os.scandir(self.fd) as it:
            for i, e in enumerate(it):
                if i >= MAX_ENTRIES:
                    break
                name = e.name
                low = name.lower()
                if name.startswith(".") or low.endswith(_IGNORED_SUFFIX):
                    continue                    # dot files and files still being written
                if not low.endswith(".md"):
                    continue                    # only Markdown files
                if shown_name(name) != name or len(name) > NAME_MAX:
                    skipped.append((shown_name(name), "The file name has odd characters or is too long."))
                    continue
                try:
                    st = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    skipped.append((name, "It's a link. Callsheet reads only files in the folder itself."))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    continue
                if not stat.S_ISREG(st.st_mode):
                    skipped.append((name, "It isn't a regular file."))
                    continue
                if st.st_nlink > 1:
                    skipped.append((name, "It's linked from another place. Callsheet reads only plain files."))
                    continue
                files.append({"name": name, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino,
                              "young": -FUTURE_S < wall - st.st_mtime_ns / 1e9 < STABLE_S})
        return files, skipped

    def read(self, f):
        """The file's bytes, or Bad. Opened without following links, checked to be the same regular file that was
        listed, at most MAX_BYTES, and unchanged while read (otherwise it is still being written: None)."""
        if f["size"] > MAX_BYTES:
            raise Bad(f"It's bigger than {MAX_BYTES // 1024} KB.")
        try:
            fd = os.open(f["name"], _OPEN_FLAGS, dir_fd=self.fd)
        except OSError:
            raise Bad("Callsheet couldn't open it.") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_ino != f["ino"] or st.st_nlink > 1:
                raise Bad("It changed into something that isn't a plain file.")
            if st.st_size > MAX_BYTES:
                raise Bad(f"It's bigger than {MAX_BYTES // 1024} KB.")
            chunks, n = [], 0
            while n <= MAX_BYTES:
                b = os.read(fd, min(65536, MAX_BYTES + 1 - n))
                if not b:
                    break
                chunks.append(b)
                n += len(b)
            if n > MAX_BYTES:
                raise Bad(f"It's bigger than {MAX_BYTES // 1024} KB.")
            st2 = os.fstat(fd)
            if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns or st.st_mtime_ns != f["mtime_ns"]:
                return None
            return b"".join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def digest(raw):
        return _sha(raw)

    @staticmethod
    def same(old, f):
        """Already seen: the same size and file time as the row Callsheet kept."""
        return old is not None and old["size"] == f["size"] and old["mtime_ns"] == f["mtime_ns"]


def _sha(b):
    return hashlib.sha256(b).hexdigest()


class Reports:
    kind = "reports"
    noun = "Reports folder"
    item_kind = "report"
    every_s = 900           # a local folder: looked at every 15 minutes
    feed_cap = MAX_BYTES
    local = True            # no host, no network: read() below instead of net.py
    tells = TELLS
    schema = _SCHEMA

    def __init__(self, wall=time.time, access=None):
        self.wall = wall    # file times are real times (tests move them, not this)
        # 0.11.2 Drive folders (conn_reports_drive.py): the owner's Google accounts (tests pass a fake), and what the
        # previous look at each Drive source saw (a young file is read once it looked the same twice)
        self.access = access
        self.drive_seen = {}

    def _access(self, conns):
        import conn_reports_drive as D
        return self.access(conns) if self.access else D.access(conns)

    @staticmethod
    def is_drive(cfg):
        return cfg.get("where") == "drive"

    # Setup -----------------------------------------------------------------------------------------------------
    @staticmethod
    def feed_url(cfg):
        return cfg.get("folder", "")

    def key(self, cfg):
        if self.is_drive(cfg):      # 0.11.2: a Drive source is the account and the folder id
            return "drive:" + str(cfg.get("account")) + ":" + str(cfg.get("folder_id"))
        return cfg.get("folder")

    def hostline(self, folder, every_s):
        return (f"Callsheet will read the report files in {folder} {C.every_label(every_s)}. "
                "It never changes or deletes them.")

    def check(self, ctx, address, via=None, where=None, account=None):
        """The owner's Check: the folder's report files as they are now (nothing is recorded). 0.11.2: where="drive"
        with the account id and the Drive folder id as the address (conn_reports_drive.py)."""
        if where == "drive":
            import conn_reports_drive as D
            return D.check_draft(ctx.c, self._access(ctx.c), account, address, ctx.c.clock(), self.wall())
        if where not in (None, "", "folder"):
            raise InputError("Choose a folder on this box or a Google Drive folder.")
        real = check_folder(address)
        shown = os.path.normpath(address.strip())
        now = ctx.c.clock()
        found, bad = [], []
        try:
            fo = Folder(real)
        except OSError:
            raise InputError("Callsheet can't read that folder.") from None
        try:
            files, skipped = fo.entries(self.wall())
            bad += skipped
            files.sort(key=lambda f: -f["mtime_ns"])
            for f in files[:MAX_NEW]:
                try:
                    raw = fo.read(f)
                    if raw is None:
                        continue
                    p = parse(raw, f["name"], now, f["mtime_ns"] / 1e9)
                except Bad as e:
                    bad.append((f["name"], str(e)))
                    continue
                found.append({"name": f["name"], "title": p["title"], "date": p["date"], "when": C._iso(p["ts"]),
                              "flagged": bool(p["instr"]), "expired": p["expires"] < now})
        finally:
            fo.close()
        title = os.path.basename(shown)
        label = C.clip(C.clean(title.replace("-", " ").replace("_", " "), True), C.LABEL_MAX) or "Reports"
        label = label[:1].upper() + label[1:]
        n = len(found)
        return {"kind": self.kind, "title": title, "label": label, "host": "", "folder": shown,
                "config": {"folder": real, "shown": shown}, "items": None,
                "latest": found[:SHOW_MAX], "files": n, "unreadable": [{"name": a, "why": b} for a, b in bad[:SHOW_MAX]],
                "unreadable_n": len(bad), "instruction": instruction(shown, label),
                "note": None if n or bad else "No reports in this folder yet. Callsheet will read them when they appear."}

    # reading ---------------------------------------------------------------------------------------------------
    def _rows(self, store, cid):
        with store.lock:
            return {r["name"]: r for r in store.db.execute(
                "SELECT * FROM report_files WHERE connector=?", (cid,)).fetchall()}

    def _put(self, store, cid, f, h, seen, status, why="", item="", meta=None):
        with store.lock:
            store.db.execute(
                "INSERT OR REPLACE INTO report_files (connector, name, size, mtime_ns, hash, seen, status, why, item, "
                "meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, f["name"], f["size"], f["mtime_ns"], h, seen, status, why[:200], item,
                 json.dumps(meta or {}, ensure_ascii=False)))

    def forget(self, store, cid):
        self.drive_seen.pop(cid, None)
        with store.lock:
            store.db.execute("DELETE FROM report_files WHERE connector=?", (cid,))

    def read(self, conns, r):
        """One look at the folder: new and edited report files into the index. Returns (new, state changes)."""
        cid, cfg = r["id"], conns._cfg(r)
        if self.is_drive(cfg):      # 0.11.2: a Drive folder, read through google.py's allowlist
            import conn_reports_drive as D
            try:
                fo = D.DriveFolder(self._access(conns), cfg.get("account"), str(cfg.get("folder_id") or ""),
                                   self.drive_seen.setdefault(cid, {}))
            except D.Trouble as e:
                return 0, {"error": str(e)}
            return self._look(conns, r, fo, D.Trouble)
        try:
            real = check_folder(cfg.get("shown") or cfg.get("folder") or "")
        except InputError as e:
            return 0, {"error": str(e)}
        if real != cfg.get("folder"):
            return 0, {"error": "The folder moved or became a link. Remove this source and add the folder again."}
        try:
            fo = Folder(real)
        except OSError:
            return 0, {"error": "Callsheet can't read the folder."}
        return self._look(conns, r, fo, ())

    def _look(self, conns, r, fo, trouble):
        """The reading both folder kinds share: `fo` lists (entries), reads one file (read: bytes, None = later, or
        Bad), says whether a file was seen already (same) and fingerprints it (digest). `trouble`: the exception that
        stops a look (Google can't be asked now; () for a local folder); what was read before it is kept."""
        store, index, now = conns.store, conns.index, conns.clock()
        cid = r["id"]
        rows = self._rows(store, cid)
        have = set()
        if index is not None:
            with index.lock:
                have = {x[0] for x in index.db.execute("SELECT source_id FROM items WHERE source=? AND account=?",
                                                       (self.kind, cid)).fetchall()}
        keep_since = now - C.PRUNE_DAYS * 86400     # older reports aren't put back after the index pruned them
        day = sum(1 for x in rows.values() if x["status"] == "ok" and x["seen"] >= now - 86400)
        new = restored = later = 0
        out = []
        files, skipped, stop = [], [], None
        try:
            files, skipped = fo.entries(self.wall())
            todo = []
            for f in files:
                old = rows.get(f["name"])
                same = fo.same(old, f)
                if same and (old["status"] != "ok" or old["item"] in have or old["seen"] < keep_since):
                    continue
                if f["young"]:
                    later += 1
                    continue
                todo.append((f, old, same))
            todo.sort(key=lambda x: x[0]["mtime_ns"])
            later += max(0, len(todo) - MAX_NEW)
            for n, (f, old, same) in enumerate(todo[:MAX_NEW]):
                try:
                    raw = fo.read(f)
                except Bad as e:
                    self._put(store, cid, f, "", now, "bad", str(e))
                    continue
                except trouble as e:
                    stop = str(e)
                    later += len(todo[:MAX_NEW]) - n
                    break
                if raw is None:
                    later += 1
                    continue
                h = fo.digest(raw)
                if old is not None and old["hash"] == h and old["status"] == "ok" and old["item"] in have:
                    self._put(store, cid, f, h, old["seen"], "ok", "", old["item"], json.loads(old["meta"] or "{}"))
                    continue
                restore = old is not None and old["hash"] == h and old["status"] == "ok"
                if restore and old["seen"] < keep_since:
                    self._put(store, cid, f, h, old["seen"], "ok", "", old["item"], json.loads(old["meta"] or "{}"))
                    continue
                if not restore:
                    with store.lock:
                        dup = store.db.execute("SELECT name FROM report_files WHERE connector=? AND hash=? AND name<>?",
                                               (cid, h, f["name"])).fetchone()
                    if dup is not None:
                        self._put(store, cid, f, h, now, "dup", "The same text as " + shown_name(dup["name"]) + ".")
                        continue
                    if old is not None and old["hash"] == h:      # unchanged text that was refused before
                        self._put(store, cid, f, h, old["seen"], old["status"], old["why"])
                        continue
                    if day >= DAY_MAX:
                        self._put(store, cid, f, h, now, "over",
                                  f"More than {DAY_MAX} reports in a day from this source; skipped.")
                        continue
                try:
                    p = parse(raw, f["name"], now, f["mtime_ns"] / 1e9)
                except Bad as e:
                    self._put(store, cid, f, h, now, "bad", str(e))
                    continue
                sid = C.item_id("rp", cid, f["name"])
                ts = p["ts"] if not restore else min(p["ts"], old["seen"])
                meta = {k: p[k] for k in ("from", "importance", "expires", "date", "cut")}
                out.append({"source": self.kind, "account": cid, "source_id": sid, "kind": "report",
                            "title": p["title"], "people": r["label"], "ts": ts, "body": p["body"],
                            "instr": p["instr"]})
                self._put(store, cid, f, h, old["seen"] if restore else now, "ok", "", sid, meta)
                if restore:
                    restored += 1
                else:
                    new += 1
                    day += 1
        except trouble as e:            # the listing itself failed: nothing was read
            stop = str(e)
        finally:
            fo.close()
        for name, why in skipped:
            if name not in rows or rows[name]["why"] != why:
                self._put(store, cid, {"name": name, "size": 0, "mtime_ns": 0}, "", now, "bad", why)
        if out and index is not None:
            index.commit_step(out)
            with index.lock:
                for it in out:
                    if it["instr"]:
                        index.db.execute("UPDATE items SET instr=? WHERE source=? AND account=? AND source_id=?",
                                         (json.dumps(it["instr"]), self.kind, cid, it["source_id"]))
            conns._prune(self.kind, cid)
        self._trim(store, cid)
        conns.log(f"Callsheet reports: {cid} {len(files)} files, {new} new, {restored} restored, {later} later, "
                  f"{len(skipped)} skipped" + (", stopped" if stop else ""))
        return new, {"error": stop, "later": later}

    def _trim(self, store, cid):
        with store.lock:
            store.db.execute("DELETE FROM report_files WHERE connector=? AND name NOT IN (SELECT name FROM "
                             "report_files WHERE connector=? ORDER BY seen DESC LIMIT ?)", (cid, cid, KEEP_ROWS))

    def public(self, conns, r, out):
        """Setup's extras for one source: the folder, the owner's choice, the files Callsheet couldn't read, the
        instruction for the other agent."""
        cfg = conns._cfg(r)
        with conns.store.lock:
            bad = conns.store.db.execute(
                "SELECT name, why FROM report_files WHERE connector=? AND status IN ('bad','over') ORDER BY seen DESC "
                "LIMIT ?", (r["id"], SHOW_MAX)).fetchall()
        shown = cfg.get("shown") or cfg.get("folder") or ""
        tell = cfg.get("tell") if cfg.get("tell") in dict(TELLS) else "important"
        if self.is_drive(cfg):      # 0.11.2
            import conn_reports_drive as D
            email = str(cfg.get("email") or "")
            ins, where = D.instruction(shown, email, r["label"]), D.shown(shown, email)
            out.update(where="drive", email=email)
        else:
            ins, where = instruction(shown, r["label"]), shown
            out.update(where="folder")
        out.update(folder=where, tell=tell, tell_label=dict(TELLS)[tell], tells=[list(x) for x in TELLS],
                   unreadable=[{"name": shown_name(x["name"]), "why": x["why"]} for x in bad],
                   instruction=ins, host="", hosts=[], every="every 15 minutes")
        return out

    # the Briefing ----------------------------------------------------------------------------------------------
    def brief_list(self, conns, rows, limit=BRIEF_MAX, per_source=BRIEF_PER_SOURCE):
        """"From your reports": unexpired reports of the active sources, newest first (by when Callsheet saw the
        file, never by a date the file claims), at most `per_source` from one source so a flood can't push the
        others out. importance never changes the order: at most the mark "Marked urgent by <source>"."""
        if not rows:
            return None
        now = conns.clock()
        found, more = [], 0
        for r in rows:
            if r["paused"] or conns.index is None:
                continue
            cfg = conns._cfg(r)
            tell = cfg.get("tell") if cfg.get("tell") in dict(TELLS) else "important"
            with conns.store.lock:
                meta = {x["item"]: x["meta"] for x in conns.store.db.execute(
                    "SELECT item, meta FROM report_files WHERE connector=? AND status='ok'", (r["id"],)).fetchall()}
            with conns.index.lock:
                got = conns.index.db.execute(
                    "SELECT id, source_id, title, ts, body, instr, instr_v FROM items WHERE source=? AND account=? "
                    "ORDER BY ts DESC, id DESC LIMIT 200", (self.kind, r["id"])).fetchall()
            mine = []
            for x in got:
                try:
                    m = json.loads(meta.get(x["source_id"]) or "null")
                except ValueError:
                    m = None
                if not isinstance(m, dict) or float(m.get("expires") or 0) < now:
                    continue
                mine.append((r, tell, x, m))
            more += max(0, len(mine) - per_source)
            found += mine[:per_source]
        found.sort(key=lambda p: (-p[2]["ts"], p[2]["id"]))
        more += max(0, len(found) - limit)
        out = []
        for r, tell, x, m in found[:limit]:
            stored = bool(x["instr"]) if x["instr_v"] == inject.VERSION else C.flagged(x["title"], x["body"])
            flag = stored or bool(flags(x["title"], m.get("from") or "", x["body"] or ""))
            claim = m.get("from") or ""
            marks = [["instructions", C.FLAG_MARK]] if flag else []
            if claim and _norm(claim) != _norm(r["label"]):
                marks.append(["unverified", "Says it’s from " + claim])
            if m.get("importance") == "urgent" and tell != "list":
                marks.append(["urgent", "Marked urgent by " + r["label"]])
            out.append({"id": x["id"], "connector": r["id"], "label": r["label"], "title": x["title"],
                        "excerpt": excerpt(x["body"]), "when": C._iso(x["ts"]), "date": m.get("date"),
                        "flagged": flag, "tell": tell, "marks": marks})
        return {"items": out, "more": more, "sources": len(rows)}


def _norm(s):
    return re.sub(r"\W+", " ", unicodedata.normalize("NFKC", str(s or "")).casefold()).strip()
