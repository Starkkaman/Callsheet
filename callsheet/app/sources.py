"""Read-only helpers and the sources the library still reads (Drive, notes, GitHub).

The 0.6.0 scanner and its Gmail, Calendar, Slack and Signal sources are retired: the live readers (live.py) keep
every message in the message store. What stays here is shared: Google access and its errors (`Retry`, `SourceError`),
Drive and notes reading, and GitHub. A source is one small class (see `Source`): `targets(ctx)`, `estimate(ctx,
target, since)` and `step(ctx, target, since, until, cursor)` returning `(items, cursor)`; cursor None means done.

Network access goes only through google.py and github.py and their allowlists. Errors are plain sentences: `Retry`
(asked to slow down, or a passing network problem: try the same step again) and `SourceError` (this target can't be
read now; the others go on).
"""
import datetime as dt
import os
import pathlib
import re
import time

import github
import google
from store import InputError

DRIVE_PAGE = 100
DRIVE_CHUNK = 5
DRIVE_PACE_S = 0.5
DRIVE_MAX_MEDIA = 1_000_000   # plain-text files larger than this are indexed by name only
NOTE_CHUNK = 200
NOTE_MAX_BYTES = 1_000_000
NOTE_MAX_FILES = 20000
ALL_TIME = dt.datetime(1990, 1, 1, tzinfo=dt.timezone.utc).timestamp()
SCOPE_CHECK_S = 3600
VAULT_ROOTS_ENV = "CALLSHEET_VAULT_ROOTS"   # tests only; Home Assistant maps /share and /media read-only

DRIVE_TYPES = {
    "application/vnd.google-apps.document": ("doc", "text/plain"),
    "application/vnd.google-apps.spreadsheet": ("sheet", "text/csv"),       # Drive exports the first sheet
    "application/vnd.google-apps.presentation": ("slides", "text/plain"),
    "text/plain": ("file", None),
    "text/markdown": ("file", None),
    "text/csv": ("file", None),
}


class Retry(Exception):
    """Wait, then try the same step again. .wait: the shortest wait in seconds."""

    def __init__(self, message, wait=60):
        super().__init__(message)
        self.wait = wait


class SourceError(Exception):
    """This target can't be read now (a plain sentence); the scan goes on with the others. .status: Google's HTTP
    status when there was one."""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def target(source, account, label, area="", unit="items", blocked=None):
    return {"source": source, "account": account, "label": label, "area": area or "", "unit": unit,
            "blocked": blocked}


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Google access: tokens, refresh on 401, granted scopes ----------------------------------------------------------
class GoogleAccess:
    """Access tokens for the index (saved back to the store like a sync does) and the granted scopes, read from the
    token endpoint's answer (`scope`), so no extra Google endpoint is needed."""

    def __init__(self, store, index, clock=time.time):
        self.store, self.index, self.clock = store, index, clock

    def _refresh(self, acc_id):
        client = self.store.google_client()
        if not client:
            raise SourceError("The Google client ID and secret are missing. Add them in Setup.")
        acc = self.store.account_secret(acc_id)
        if not acc.get("refresh_token"):
            raise SourceError("Google asked to sign in again. Reconnect the account in Setup.")
        try:
            res = google.refresh(client, acc["refresh_token"])
        except google.GoogleError as e:
            if e.code == "invalid_grant":
                raise SourceError("Google asked to sign in again. Reconnect the account in Setup.") from None
            raise _mapped(e) from None
        tok = res.get("access_token")
        if not tok:
            raise SourceError("Google didn't send an access token.")
        exp = self.clock() + float(res.get("expires_in", 3600))
        self.store.set_tokens(acc_id, tok, exp, res.get("refresh_token"))
        if isinstance(res.get("scope"), str):
            self._save_scopes(acc_id, res["scope"])
        return tok

    def _save_scopes(self, acc_id, scope):
        known = self.index.meta("scopes", {})
        known[acc_id] = {"scope": scope, "at": self.clock()}
        self.index.set_meta("scopes", known)

    def token(self, acc_id, force=False):
        acc = self.store.account_secret(acc_id)
        if not force and acc.get("access_token") and (acc.get("expires_at") or 0) - 60 > self.clock():
            return acc["access_token"]
        return self._refresh(acc_id)

    def call(self, acc_id, fn, *args, **kw):
        """fn(token, *args): refresh once on 401; Google's errors become Retry or SourceError."""
        try:
            try:
                return fn(self.token(acc_id), *args, **kw)
            except google.GoogleError as e:
                if e.status != 401:
                    raise
            return fn(self.token(acc_id, force=True), *args, **kw)
        except google.GoogleError as e:
            raise _mapped(e) from None

    def scopes(self, acc_id, force=False):
        """The scopes this account granted (a set), or None when Google didn't say. Cached for an hour."""
        known = self.index.meta("scopes", {}).get(acc_id)
        if force or not known or self.clock() - float(known.get("at") or 0) > SCOPE_CHECK_S:
            try:
                self._refresh(acc_id)
            except (SourceError, Retry):
                pass
            known = self.index.meta("scopes", {}).get(acc_id)
        return set(known["scope"].split()) if known else None

    def has_drive(self, acc_id, force=False):
        s = self.scopes(acc_id, force)
        return None if s is None else google.DRIVE_SCOPE in s

    def cached_drive(self, acc_id):
        known = self.index.meta("scopes", {}).get(acc_id)
        return None if not known else google.DRIVE_SCOPE in known["scope"].split()


def _mapped(e):
    if e.status == 429 or e.code in google.RATE_REASONS or e.code == "RESOURCE_EXHAUSTED":
        wait = 3600 if e.code in ("dailyLimitExceeded", "quotaExceeded") else 60
        return Retry("Google asked Callsheet to slow down. The scan waits and tries again.", wait)
    if e.status >= 500 or e.status == 0:
        return Retry(str(e), 60)
    return SourceError(str(e), e.status)


class Ctx:
    """What a source step needs: the store, the library (library.py; `index` for the readers' meta and documents),
    Google access, the clock, a pacing sleep and the settings."""

    def __init__(self, store, index, clock=time.time, sleep=time.sleep, settings=None, stop=None):
        self.store, self.index, self.clock, self.sleep = store, index, clock, sleep
        self.google = GoogleAccess(store, index, clock)
        self.settings = settings or {}
        self.stop = stop   # callable: True when the scanner should stop early (pause, cancel, call time)

    def pace(self, seconds):
        if seconds > 0:
            self.sleep(seconds)

    def stopping(self):
        return bool(self.stop and self.stop())


class Source:
    key = ""
    label = ""
    unit = "items"

    def targets(self, ctx):
        return []

    def estimate(self, ctx, tgt, since):
        return None

    def step(self, ctx, tgt, since, until, cursor):
        return [], None


# --- Gmail ---------------------------------------------------------------------------------------------------------
def _headers(payload):
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers") or []
            if isinstance(h, dict)}


# --- Calendar ------------------------------------------------------------------------------------------------------
def _event_ts(ev, tz):
    st = ev.get("start") or {}
    if st.get("dateTime"):
        try:
            return dt.datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    if st.get("date"):
        try:
            return dt.datetime.combine(dt.date.fromisoformat(st["date"]), dt.time(0, 0), tzinfo=tz).timestamp()
        except ValueError:
            return 0.0
    return 0.0


# --- Drive ---------------------------------------------------------------------------------------------------------
def drive_query(since):
    types = " or ".join(f"mimeType = '{m}'" for m in DRIVE_TYPES)
    q = f"trashed = false and ({types})"
    if since:
        q = f"modifiedTime > '{iso(since)}' and " + q
    return q


def _drive_ts(f):
    try:
        return dt.datetime.fromisoformat(str(f.get("modifiedTime")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class DriveSource(Source):
    key, label, unit = "drive", "Google Drive", "files"

    def targets(self, ctx):
        out = []
        for a in ctx.store.accounts():
            has = ctx.google.cached_drive(a["id"])
            out.append(target("drive", a["id"], "Drive · " + a["email"], a["area_id"], self.unit,
                              None if has else "Grant Drive access first"))
        return out

    def estimate(self, ctx, tgt, since):
        n, page = 0, None
        for _ in range(3):
            res = ctx.google.call(tgt["account"], google.drive_files, drive_query(since), page, 1000,
                                  "nextPageToken,files(id)")
            n += len(res.get("files") or [])
            page = res.get("nextPageToken")
            if not page:
                break
        return n

    def step(self, ctx, tgt, since, until, cursor):
        cur = dict(cursor or {"page": None, "pending": [], "last": False})
        if cur["pending"]:
            acc = ctx.store.account(tgt["account"])
            items = []
            for f in cur["pending"][:DRIVE_CHUNK]:
                items.append(self._item(ctx, acc, tgt, f))
                ctx.pace(DRIVE_PACE_S)
            cur["pending"] = cur["pending"][DRIVE_CHUNK:]
            return items, cur if (cur["pending"] or not cur["last"]) else None
        if cur["last"]:
            return [], None
        res = ctx.google.call(tgt["account"], google.drive_files, drive_query(since), cur["page"], DRIVE_PAGE)
        files = [{k: f.get(k) for k in ("id", "name", "mimeType", "modifiedTime", "owners", "webViewLink", "size")}
                 for f in res.get("files") or [] if isinstance(f, dict) and f.get("id")]
        cur["pending"] = files
        cur["page"] = res.get("nextPageToken")
        cur["last"] = not cur["page"]
        return [], cur if (files or not cur["last"]) else None

    @staticmethod
    def _item(ctx, acc, tgt, f):
        kind, export = DRIVE_TYPES.get(f.get("mimeType"), ("file", None))
        text = ""
        try:
            if export:
                text = ctx.google.call(tgt["account"], google.drive_export, f["id"], export)
            elif int(f.get("size") or 0) <= DRIVE_MAX_MEDIA:
                text = ctx.google.call(tgt["account"], google.drive_media, f["id"])
        except SourceError:
            text = ""   # too large to export, or not readable: indexed by name, owners and date only
        owners = [f"{o.get('displayName')} <{o.get('emailAddress')}>" if o.get("displayName") and o.get("emailAddress")
                  else o.get("emailAddress") or o.get("displayName") for o in f.get("owners") or [] if isinstance(o, dict)]
        return {"source": "drive", "account": acc["id"], "source_id": f["id"], "kind": kind, "thread": "",
                "title": f.get("name") or "(untitled)", "people": [o for o in owners if o], "area": tgt["area"],
                "ts": _drive_ts(f), "url": f.get("webViewLink") or "", "body": text}


# --- Obsidian ------------------------------------------------------------------------------------------------------
def vault_roots():
    raw = os.environ.get(VAULT_ROOTS_ENV) or "/share:/media"
    return [r for r in raw.split(":") if r]


def check_vault(path):
    """The vault folder as a resolved path, or InputError with a plain sentence. It must be under /share or /media
    (mapped read-only into the app), exist, be a folder and be readable."""
    if not isinstance(path, str) or not path.strip():
        raise InputError("Type the vault folder, for example /share/Obsidian/My vault.")
    path = path.strip()
    if len(path) > 500 or "\x00" in path:
        raise InputError("That folder name is too long.")
    if not path.startswith("/"):
        raise InputError("Type the full folder path, starting with /share or /media.")
    real = os.path.realpath(path)
    roots = [os.path.realpath(r) for r in vault_roots()]
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
        raise InputError("The vault folder must be inside /share or /media (Home Assistant's shared folders).")
    if not os.path.exists(real):
        raise InputError("Callsheet can't find that folder. Check the path and that the vault is synced there.")
    if not os.path.isdir(real):
        raise InputError("That's a file, not a folder. Type the vault folder.")
    if not os.access(real, os.R_OK | os.X_OK):
        raise InputError("Callsheet can't read that folder. Check its permissions.")
    return real


def list_notes(root):
    """Relative paths of the vault's .md files, sorted; dot folders (.obsidian, .trash, .git) and other files
    (attachments) are skipped; symlinks are not followed."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            out.append(os.path.relpath(full, root))
            if len(out) >= NOTE_MAX_FILES:
                return out
    return out


_FM_LIST = re.compile(r"^\s*-\s+(.*)$")
_LINK = re.compile(r"(!?)\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_TAG = re.compile(r"(?<![\w/#&])#([A-Za-z][\w/-]{0,60})")
_ATTACH = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".mp3", ".mp4", ".mov", ".wav", ".zip", ".excalidraw")


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def front_matter(text):
    """(fields, rest) for a note starting with a '---' block; a small YAML subset: `key: value`, `key: [a, b]`
    and `key:` followed by `- item` lines. Anything else is ignored."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, min(len(lines), 300)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text
    fields, key = {}, None
    for ln in lines[1:end]:
        m = _FM_LIST.match(ln)
        if m and key:
            if not isinstance(fields.get(key), list):
                fields[key] = []
            fields[key].append(_unquote(m.group(1)))
            continue
        if ":" in ln and not ln.startswith((" ", "\t")):
            k, v = ln.split(":", 1)
            key = k.strip().lower()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                fields[key] = [_unquote(x) for x in v[1:-1].split(",") if x.strip()]
            elif v:
                fields[key] = _unquote(v)
            else:
                fields[key] = []
    return fields, "\n".join(lines[end + 1:])


def _as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
    return []


def parse_note(text, rel, mtime, areas=None):
    """An index item for one note: title (front matter title, else the first # heading, else the file name), tags
    (front matter and #tags), [[links]] as people/topic hints, the date (front matter date/created, else the file's
    modified time), the area (front matter `area` matching an area's name), the body without front matter."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    fm, body = front_matter(text)
    title = fm.get("title") if isinstance(fm.get("title"), str) else ""
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.M)
        title = m.group(1).strip() if m else pathlib.PurePath(rel).stem
    tags = [t.lstrip("#") for t in _as_list(fm.get("tags") or fm.get("tag"))]
    tags += _TAG.findall(re.sub(r"```.*?```", " ", body, flags=re.S))
    links = []
    for bang, name in _LINK.findall(body):
        name = name.strip()
        if bang and name.lower().endswith(_ATTACH):
            continue
        if name and not name.lower().endswith(_ATTACH):
            links.append(name)
    aliases = fm.get("aliases") if isinstance(fm.get("aliases"), list) else _as_list(fm.get("aliases"))
    hints = links + _as_list(fm.get("people")) + aliases
    # 0.7.0 projects: a front matter `project:` (any value) or `type`/`kind`/`category: project` marks a project note
    if "project" in fm or any(str(fm.get(k) or "").strip().lower() == "project" for k in ("type", "kind", "category")):
        tags.append("project")
    hints += ["#" + t for t in dict.fromkeys(tags)]
    ts = mtime
    for k in ("date", "created", "updated"):
        v = fm.get(k)
        if isinstance(v, str) and v:
            try:
                d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                ts = (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
                break
            except ValueError:
                try:
                    ts = dt.datetime.combine(dt.date.fromisoformat(v[:10]), dt.time(12, 0),
                                             tzinfo=dt.timezone.utc).timestamp()
                    break
                except ValueError:
                    pass
    area = ""
    if isinstance(fm.get("area"), str) and areas:
        want = fm["area"].strip().lower()
        area = next((a["id"] for a in areas if a["name"].lower() == want or a["id"] == want), "")
    return {"source": "notes", "account": "vault", "source_id": rel, "kind": "note", "thread": "",
            "title": title, "people": hints, "area": area, "ts": ts, "url": rel, "body": body}


class ObsidianSource(Source):
    key, label, unit = "notes", "Notes", "notes"

    def targets(self, ctx):
        path = ctx.settings.get("vault") or ""
        if not path:
            return []
        try:
            check_vault(path)
            blocked = None
        except InputError as e:
            blocked = str(e)
        return [target("notes", "vault", "Notes · " + os.path.basename(path.rstrip("/")), "", self.unit, blocked)]

    def estimate(self, ctx, tgt, since):
        try:
            return len(list_notes(check_vault(ctx.settings.get("vault") or "")))
        except InputError:
            return None

    def step(self, ctx, tgt, since, until, cursor):
        try:
            root = check_vault(ctx.settings.get("vault") or "")
        except InputError as e:
            raise SourceError(str(e)) from None
        if cursor is None:
            files = list_notes(root)
            return [], {"files": files, "all": files}
        cur = dict(cursor)
        areas = ctx.store.areas()
        items = []
        for rel in cur["files"][:NOTE_CHUNK]:
            full = os.path.join(root, rel)
            try:
                st = os.stat(full, follow_symlinks=False)
                if st.st_size > NOTE_MAX_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(NOTE_MAX_BYTES)
            except OSError:
                continue
            items.append(parse_note(text, rel, st.st_mtime, areas))
        cur["files"] = cur["files"][NOTE_CHUNK:]
        if not cur["files"]:
            # notes deleted from the folder since the last read leave the library too
            ctx.index.delete_missing("notes", "vault", set(cur["all"]))
            return items, None
        return items, cur


# --- GitHub (0.7.0, read-only, approved 2026-11-03) ------------------------------------------------------------
GH_PHASES = ("commits", "issues", "pulls")


class GitHubSource(Source):
    """Commits, issues and pull requests of the repos the owner follows (Setup, Projects, GitHub), in the scan window.
    One page per step; github.py holds the allowlist (GET only, api.github.com, five paths)."""
    key, label, unit = "github", "GitHub", "items"

    def targets(self, ctx):
        c = github.config(ctx.store)
        if not c["token"]:
            return []
        return [target("github", r, "GitHub · " + r, "", self.unit) for r in c["repos"]]

    def estimate(self, ctx, tgt, since):
        return None

    def step(self, ctx, tgt, since, until, cursor):
        c = github.config(ctx.store)
        repo = tgt["account"]
        if not c["token"]:
            raise SourceError(github.NO_TOKEN)
        if repo not in c["repos"]:
            return [], None
        etags = ctx.index.meta("github_etags:" + repo) or {}
        try:
            cl = github.Client(c["token"], etags=etags, clock=ctx.clock)
        except ValueError as e:
            raise SourceError(str(e)) from None
        cur = dict(cursor or {"phase": 0, "url": None})
        kind = GH_PHASES[cur["phase"]]
        url = cur["url"] or cl.list_url(repo, kind, iso(since) if since else None)
        try:
            data, nxt = cl.request(url, repo)
        except github.RateLimited as e:
            raise Retry(str(e), int(min(3600, max(60, e.until - ctx.clock())))) from None
        except github.GitHubError as e:
            if e.status >= 500 or e.status == 0:
                raise Retry(str(e), 60) from None
            raise SourceError(str(e), e.status) from None
        finally:
            ctx.index.set_meta("github_etags:" + repo, etags)
        items, stop = [], False
        for x in data if isinstance(data, list) else []:
            if not isinstance(x, dict):
                continue
            it = github.commit_item(repo, x) if kind == "commits" else github.issue_item(repo, x, kind == "pulls")
            if it is None:
                continue
            if since and it["ts"] < since:
                stop = kind != "commits"   # issues and pulls come newest first
                continue
            if until and it["ts"] >= until:
                continue
            items.append(it)
        if nxt and not stop and data is not None:
            return items, {"phase": cur["phase"], "url": nxt}
        if cur["phase"] + 1 < len(GH_PHASES):
            return items, {"phase": cur["phase"] + 1, "url": None}
        return items, None


