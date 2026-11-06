"""0.12.1 Reports from a Google Drive folder (CLAUDE.md "Reports": the existing drive.readonly access, no new host).

The same Reports kind as conn_reports.py, with the folder on Google Drive instead of under /share or /media, for agents
that run in the cloud and have Drive access of their own. One source = one of the owner's connected Google accounts and
one folder; who a report is from is the source (account + folder id), never the file.

Requests (all through google.py's allowlist, GET only, with the account's own token; nothing else is contacted):
    picker      GET drive/v3/files   q: folders in a folder ('<id>' in parents), "Shared with me", or a name search
                GET drive/v3/files/<id>   (fields: the opened folder's name and kind)
    each look   GET drive/v3/files/<folder id>   (still a folder, not in the bin)
                GET drive/v3/files   q: '<folder id>' in parents and trashed = false   (paged, at most MAX_ENTRIES)
                GET drive/v3/files/<file id>?alt=media   (only new or changed report files, at most MAX_NEW a look)
Google's token endpoint is used as before (sources.GoogleAccess refreshes an expired token).

What is read: files directly in the folder (no subfolders), named like a report (`YYYY-MM-DD-topic.md`) whose type is
text/markdown, text/x-markdown or text/plain. Google Docs aren't read (not exported: a Doc has no checksum to tell an
edit from a re-save; the agent uploads a .md file instead); a Doc or shortcut named like a report is listed under
"Couldn't read" with the reason. Dot files and *.tmp, *~, *.part … are skipped like in a local folder. Two files with
the same name: the newest is read. A file must be at least STABLE_S old or look the same (size, time, checksum) at two
looks in a row (Callsheet looks again a minute later); its bytes must match the checksum Google listed, otherwise it
changed while read and is tried later. Everything after the download is conn_reports.py's: the same parser, masking,
inject flags, defanged links, caps, replay and flood rules.
"""
import datetime as dt
import hashlib
import re

import conn_reports as R
import connectors as C
import google
from store import InputError, NotFound

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
TEXT_TYPES = ("text/markdown", "text/x-markdown", "text/plain")
STABLE_S = 120          # younger than this: read only when it looked the same at the previous look
PAGE = 200              # files listed per request on a look
PICK_PAGE = 100         # folders per page in the picker
SEARCH_MAX = 100        # characters of a folder search
TOKEN_MAX = 1000        # a picker page token from Google, passed back as it came
ROOTS = {"root": "My Drive", "shared": "Shared with me"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_ACC = re.compile(r"[A-Za-z0-9_-]{1,40}")
LIST_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum,headRevisionId)"
FOLDER_FIELDS = "id,name,mimeType,trashed"


class Trouble(Exception):
    """Google couldn't be asked now (a plain sentence): the look stops and Setup shows it."""


def access(conns):
    """The owner's Google accounts as the Know me index uses them (tokens, refresh on 401, the scope cache)."""
    import sources
    return sources.GoogleAccess(conns.store, conns.index, conns.clock)


def _plain(e, email=""):
    """A Google error during a look or a pick, in plain words (never the token or the request)."""
    import sources
    if isinstance(e, NotFound):
        return "The Google account for this folder isn't connected any more. Connect it again, or remove this source."
    if isinstance(e, sources.SourceError):
        if e.status == 404:
            return "Callsheet can't find the Drive folder any more (deleted, or no longer shared with this account)."
        if e.status == 403:
            return (f"Google said {email or 'this account'} hasn't given Callsheet read-only Drive access. Give it in "
                    "Setup → Get to know me.")
        return str(e)
    if isinstance(e, sources.Retry):
        return str(e)
    return "Callsheet couldn't ask Google just now."


def _call(acc, account, fn, *args):
    """One allowlisted Drive GET through the account's token; Google's errors become Trouble (plain words) except a
    missing file (404), which the caller handles."""
    import sources
    try:
        return acc.call(account, fn, *args)
    except (sources.SourceError, sources.Retry, NotFound) as e:
        raise Trouble(_plain(e)) from e


def _q(s):
    """A value inside a Drive query string: backslash and quote escaped."""
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def _ns(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp() * 1_000_000) * 1000


def _account(conns, account):
    if not isinstance(account, str) or not _ACC.fullmatch(account):
        raise InputError("Pick one of your Google accounts.")
    for a in conns.store.accounts():
        if a["id"] == account:
            return a
    raise InputError("That Google account isn't connected any more. Pick another one.")


def _granted(acc, a):
    try:
        return acc.has_drive(a["id"])
    except Exception:  # noqa: BLE001 (the picker still lists the account)
        return None


def _name(n):
    return R.shown_name(C.clean(str(n or ""), single_line=True)) or "(no name)"


# --- the picker --------------------------------------------------------------------------------------------------
def folders(conns, acc, body):
    """What the Drive folder picker shows. Without an account: the owner's Google accounts (with or without Drive
    access). With one: the folders in `parent` ("root" = My Drive, "shared" = Shared with me, or a folder id), or the
    folders whose name contains `q`; a page at a time (`page`: Google's token for the next one). Names only."""
    account = body.get("account")
    if not account:
        out = [{"id": a["id"], "email": a["email"], "granted": acc.cached_drive(a["id"]) if conns.index else None}
               for a in conns.store.accounts()]
        return {"where": "drive", "accounts": out, "account": None, "folders": [], "pickable": False}
    a = _account(conns, account)
    if _granted(acc, a) is False:
        return {"where": "drive", "account": a["id"], "email": a["email"], "need_access": True, "folders": [],
                "pickable": False, "note": "Give Callsheet read-only Drive access for " + a["email"] +
                " first, in Setup → Get to know me."}
    parent, q, page = body.get("parent") or "root", body.get("q"), body.get("page")
    if not isinstance(parent, str) or not (parent in ROOTS or _ID.fullmatch(parent)):
        raise InputError("Pick a folder from the list.")
    if q is not None and not isinstance(q, str) or page is not None and (not isinstance(page, str) or len(page) > TOKEN_MAX):
        raise InputError("Send {account, parent, q, page}.")
    q = R.shown_name(C.clean(q or "", single_line=True)).replace("?", " ").strip()[:SEARCH_MAX]
    here = {"id": parent, "name": ROOTS.get(parent)}
    if q:
        query = f"name contains '{_q(q)}' and mimeType = '{FOLDER}' and trashed = false"
        here = {"id": None, "name": None}
    elif parent == "shared":
        query = f"sharedWithMe and mimeType = '{FOLDER}' and trashed = false"
    else:
        if parent != "root":
            meta = _call(acc, a["id"], google.drive_file, parent, FOLDER_FIELDS)
            if meta.get("mimeType") != FOLDER or meta.get("trashed"):
                raise InputError("That isn't a folder Callsheet can open. Pick another one.")
            here["name"] = _name(meta.get("name"))
        query = f"'{_q(parent)}' in parents and mimeType = '{FOLDER}' and trashed = false"
    res = _call(acc, a["id"], google.drive_files, query, page or None, PICK_PAGE,
                "nextPageToken,files(id,name,mimeType)", "name")
    fs = [{"id": f["id"], "name": _name(f.get("name"))} for f in res.get("files") or []
          if isinstance(f, dict) and isinstance(f.get("id"), str) and _ID.fullmatch(f["id"])
          and f.get("mimeType") == FOLDER][:PICK_PAGE]
    nxt = res.get("nextPageToken")
    return {"where": "drive", "account": a["id"], "email": a["email"], "parent": here["id"], "name": here["name"],
            "search": q or None, "folders": fs, "next": nxt if isinstance(nxt, str) and len(nxt) <= TOKEN_MAX else None,
            "pickable": bool(here["id"]) and here["id"] not in ROOTS}


# --- one look --------------------------------------------------------------------------------------------------
def instruction(folder, email, label):
    """The text the owner pastes into the other agent (it uses its own Drive access to the folder)."""
    label = label or "Reports"
    return (f"After each run, upload one new file to the Google Drive folder \"{folder}\" ({email}) named "
            f"<YYYY-MM-DD>-<topic>.md, as a Markdown or plain text file (not a Google Doc), uploaded whole in one go. "
            f"Start the file with this header:\n"
            f"---\nfrom: {label}\ntitle: <one line>\nimportance: normal\nexpires: <YYYY-MM-DD, optional, "
            f"at most 14 days ahead>\n---\n"
            f"importance is low, normal or urgent. Then, in under about 300 words, plain facts first: the counts and "
            f"the top items. Don't put passwords, keys, tokens, credentials, IP addresses or exploit details in it. "
            f"Keep the full report where you keep it now; you may name it as plain text (\"Full report: <name>\"), "
            f"not as a link.")


def shown(name, email):
    return f"{name} (Google Drive, {email})"


class DriveFolder:
    """One look at a Drive folder: the same interface as conn_reports.Folder (entries, read, close) plus same() and
    digest(), so conn_reports.Reports reads both the same way. `seen`: what the previous look saw ({name: (size,
    mtime_ns, md5)}), for files younger than STABLE_S."""

    def __init__(self, acc, account, folder_id, seen=None):
        self.acc, self.account, self.folder_id = acc, account, folder_id
        self.seen = seen if seen is not None else {}
        meta = _call(acc, account, google.drive_file, folder_id, FOLDER_FIELDS)
        if meta.get("mimeType") != FOLDER:
            raise Trouble("That Drive item isn't a folder any more. Remove this source and pick the folder again.")
        if meta.get("trashed"):
            raise Trouble("The Drive folder is in the bin. Take it out, or remove this source.")
        self.name = _name(meta.get("name"))

    def close(self):
        pass

    def entries(self, wall):
        files, skipped, names, page = [], [], set(), None
        listed = 0
        now_seen = {}
        for _ in range(R.MAX_ENTRIES // PAGE):
            res = _call(self.acc, self.account, google.drive_files, f"'{_q(self.folder_id)}' in parents and trashed = false",
                        page, PAGE, LIST_FIELDS)
            for f in res.get("files") or []:
                if listed >= R.MAX_ENTRIES:
                    break
                listed += 1
                if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not _ID.fullmatch(f["id"]):
                    continue
                name, mime = str(f.get("name") or ""), str(f.get("mimeType") or "")
                low = name.lower()
                if mime == FOLDER or name.startswith(".") or low.endswith(R._IGNORED_SUFFIX):
                    continue                    # subfolders, dot files and files still being written
                report_like = bool(re.match(r"\d{4}-\d{2}-\d{2}-", name))
                if not low.endswith(".md"):
                    if report_like and mime.startswith("application/vnd.google-apps."):
                        skipped.append((R.shown_name(name), "It's a Google Doc. Callsheet reads .md files; have the "
                                                            "agent upload the report as a file."))
                    continue
                if R.shown_name(name) != name or len(name) > R.NAME_MAX:
                    skipped.append((R.shown_name(name), "The file name has odd characters or is too long."))
                    continue
                if name in names:
                    continue                    # the same name twice: the newest (listed first) is the one read
                names.add(name)
                if mime == SHORTCUT:
                    skipped.append((name, "It's a shortcut. Callsheet reads only files in the folder itself."))
                    continue
                if mime.startswith("application/vnd.google-apps."):
                    skipped.append((name, "It's a Google Doc. Callsheet reads .md files; have the agent upload the "
                                          "report as a file."))
                    continue
                if mime not in TEXT_TYPES:
                    skipped.append((name, "Google says it isn't a text file."))
                    continue
                try:
                    size = int(f.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                mtime_ns = _ns(f.get("modifiedTime"))
                md5 = f.get("md5Checksum") if isinstance(f.get("md5Checksum"), str) else ""
                key = (size, mtime_ns, md5 or str(f.get("headRevisionId") or ""))
                now_seen[name] = key
                age = wall - mtime_ns / 1e9
                young = -R.FUTURE_S < age < STABLE_S and self.seen.get(name) != key
                files.append({"name": name, "size": size, "mtime_ns": mtime_ns, "id": f["id"], "md5": md5.lower(),
                              "young": young})
            page = res.get("nextPageToken")
            if not page or listed >= R.MAX_ENTRIES:
                break
        self.seen.clear()
        self.seen.update(now_seen)
        return files, skipped

    def read(self, f):
        """The file's bytes, or Bad; None when it changed while read (the bytes don't match the listed checksum) or
        went away (tried later)."""
        import sources
        if f["size"] > R.MAX_BYTES:
            raise R.Bad(f"It's bigger than {R.MAX_BYTES // 1024} KB.")
        try:
            raw = self.acc.call(self.account, google.drive_media_bytes, f["id"], R.MAX_BYTES)
        except NotFound as e:
            raise Trouble(_plain(e)) from e
        except sources.Retry as e:
            raise Trouble(str(e)) from e
        except sources.SourceError as e:
            if e.status == 404:
                return None
            if e.status in (401, 0):
                raise Trouble(str(e)) from e
            raise R.Bad("Google wouldn't give Callsheet this file.") from None
        if len(raw) > R.MAX_BYTES:
            raise R.Bad(f"It's bigger than {R.MAX_BYTES // 1024} KB.")
        if f["md5"] and hashlib.md5(raw).hexdigest() != f["md5"]:
            return None
        if not f["md5"] and len(raw) != f["size"]:
            return None
        return raw

    @staticmethod
    def digest(raw):
        return "md5:" + hashlib.md5(raw).hexdigest()

    @staticmethod
    def same(old, f):
        """Already seen: the same size and time, or the same checksum (a re-save that changed nothing)."""
        if old is None:
            return False
        if old["size"] == f["size"] and old["mtime_ns"] == f["mtime_ns"]:
            return True
        return bool(f["md5"]) and old["hash"] == "md5:" + f["md5"] and old["size"] == f["size"]


def check_draft(conns, acc, account, folder_id, now, wall):
    """The owner's Check for a Drive folder: the folder's report files as they are now (nothing is recorded). At most
    SHOW_MAX files are opened (the newest)."""
    a = _account(conns, account)
    if not isinstance(folder_id, str) or not _ID.fullmatch(folder_id) or folder_id in ROOTS:
        raise InputError("Open the Drive folder for this source, then press Check.")
    try:
        fo = DriveFolder(acc, a["id"], folder_id)
        files, bad = fo.entries(wall)
    except Trouble as e:
        raise InputError(str(e)) from None
    files.sort(key=lambda f: -f["mtime_ns"])
    found = []
    for f in files[:R.SHOW_MAX]:
        try:
            raw = fo.read(f)
            if raw is None:
                continue
            p = R.parse(raw, f["name"], now, f["mtime_ns"] / 1e9)
        except R.Bad as e:
            bad.append((f["name"], str(e)))
            continue
        except Trouble as e:
            raise InputError(str(e)) from None
        found.append({"name": f["name"], "title": p["title"], "date": p["date"], "when": C._iso(p["ts"]),
                      "flagged": bool(p["instr"]), "expired": p["expires"] < now})
    label = C.clip(C.clean(fo.name.replace("-", " ").replace("_", " "), True), C.LABEL_MAX) or "Reports"
    label = label[:1].upper() + label[1:]
    n = len(found) + max(0, len(files) - R.SHOW_MAX)
    where = shown(fo.name, a["email"])
    return {"kind": "reports", "title": fo.name, "label": label, "host": "", "folder": where, "where": "drive",
            "email": a["email"],
            "config": {"where": "drive", "account": a["id"], "folder_id": folder_id, "shown": fo.name,
                       "email": a["email"]},
            "items": None, "latest": found[:R.SHOW_MAX], "files": n,
            "unreadable": [{"name": x, "why": y} for x, y in bad[:R.SHOW_MAX]], "unreadable_n": len(bad),
            "instruction": instruction(fo.name, a["email"], label),
            "hostline": (f"Callsheet will read the report files in the Drive folder {fo.name} {C.every_label(900)}, "
                         f"with {a['email']}’s read-only access. It never changes or deletes them."),
            "note": None if n or bad else "No reports in this folder yet. Callsheet will read them when they appear."}
