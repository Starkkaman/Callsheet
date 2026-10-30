"""SQLite storage for Callsheet: settings, areas, Google client, accounts, VIPs, writer, last sync,
cached brief.

One connection shared by all threads behind a lock. The database file is created with mode 0600
because it holds the Google client secret, the account tokens and the writer API keys. Nothing in
here logs values.
"""
import datetime as dt
import json
import os
import pathlib
import re
import secrets
import sqlite3
import threading
import urllib.parse
import zoneinfo

import evaluate
import writer

SCHEMA_VERSION = 1
DB_NAME = "callsheet.db"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
READ_KEYS = ("calendar", "mail", "chat", "tasks", "writer")
LOOKS = ("bold", "clean", "easy")
MODES = ("auto", "light", "dark")
TIME_FORMATS = ("12h", "24h")
CALL_LABELS = {"morning": "Morning brief", "midday": "Midday check-in", "afternoon": "Afternoon check-in"}
GOOGLE_ID_SUFFIX = ".apps.googleusercontent.com"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, colour INTEGER NOT NULL, reads TEXT NOT NULL,
    fixed INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, area_id TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
    refresh_token TEXT, access_token TEXT, expires_at REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'connected', error TEXT,
    services TEXT NOT NULL, calendars TEXT NOT NULL, created_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS vips (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, area_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slack (
    id TEXT PRIMARY KEY, area_id TEXT NOT NULL, team_id TEXT NOT NULL UNIQUE, team_name TEXT NOT NULL,
    domain TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL, token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'connected', error TEXT, ignored TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0);
"""
SUPPRESS_KINDS = ("thread", "sender", "domain")
MAX_SUPPRESS = 500
MAX_IGNORED = 500
MAX_PROFILES = 10
DONE_KEEP_S = 30 * 86400
MAX_DONE = 2000


class InputError(ValueError):
    """A request the user can fix. The message is a plain sentence shown in the UI (HTTP 400)."""


class NotFound(LookupError):
    """Unknown id (HTTP 404). The message is a plain sentence."""


class Conflict(RuntimeError):
    """The request can't run right now (HTTP 409), e.g. a sync is running. The message is a plain sentence."""


class StoreError(RuntimeError):
    """The existing database can't be opened or brought up to date. The server stops instead of
    starting with a fresh, empty database; the file is left as it is."""


def default_timezone():
    tz = os.environ.get("TZ", "").strip()
    if tz:
        try:
            zoneinfo.ZoneInfo(tz)
            return tz
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC"


def default_settings():
    return {
        "name": "",
        "timezone": default_timezone(),
        "workdays": ["mon", "tue", "wed", "thu", "fri"],
        "reply_age_days": 2,
        "call_times": [
            {"id": "morning", "label": CALL_LABELS["morning"], "time": "06:30", "enabled": True},
            {"id": "midday", "label": CALL_LABELS["midday"], "time": "12:30", "enabled": True},
            {"id": "afternoon", "label": CALL_LABELS["afternoon"], "time": "17:00", "enabled": True},
        ],
        "skip_checkins_days_off": True,
        "look": "bold",
        "mode": "auto",
        "time_format": "12h",
    }


def new_id(prefix):
    return prefix + "_" + secrets.token_hex(4)


def client_id_hint(client_id):
    """A short, recognisable hint: the last 6 characters before the common Google suffix."""
    core = client_id[: -len(GOOGLE_ID_SUFFIX)] if client_id.endswith(GOOGLE_ID_SUFFIX) else client_id
    return "…" + core[-6:]


def key_hint(key):
    """The last 4 characters of an API key, or None."""
    return "…" + key[-4:] if key else None


LOCAL_NUMBER_NAMES = {"reasoning_budget": "The thinking budget", "max_tokens": "Max tokens",
                      "context": "The context size", "timeout": "The timeout (seconds)"}


def default_writer():
    return {"provider": "off",
            "claude": {"api_key": None, "model": writer.DEFAULT_MODEL},
            "local": dict({"base_url": "", "model": "", "api_key": None}, **writer.LOCAL_DEFAULTS),
            "fallback_to_claude": False, "suggest_replies": True,
            # the owner's writing-instruction profiles (the built-in "default" is never stored) and the one in use
            "prompt_profiles": [], "active_profile": "default",
            # set on any change through the API: from then on the app options never touch the writer
            "configured_by_owner": False}


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:24].strip("-") or "area"


class Store:
    def __init__(self, data_dir):
        self.dir = pathlib.Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / DB_NAME
        # Create the file 0600 before SQLite opens it; SQLite gives its -wal/-shm files the same mode.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # Upgrades are additive only: CREATE ... IF NOT EXISTS, new meta keys read with defaults
        # (0.3.0's writer settings live in meta). Nothing is dropped or recreated.
        try:
            with self.lock:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.executescript(_SCHEMA)
                row = self.db.execute("SELECT version FROM schema_version").fetchone()
                if row is None:
                    self.db.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                elif row["version"] > SCHEMA_VERSION:
                    raise StoreError(f"{self.path} was written by a newer Callsheet (database version "
                                     f"{row['version']}, this one knows {SCHEMA_VERSION}).")
                if self.db.execute("SELECT 1 FROM areas WHERE id='personal'").fetchone() is None:
                    self.db.execute(
                        "INSERT INTO areas (id, name, colour, reads, fixed, position) VALUES (?,?,?,?,1,0)",
                        ("personal", "Personal", 1, json.dumps({k: True for k in READ_KEYS})))
        except sqlite3.Error as e:
            self.db.close()
            raise StoreError(f"Couldn't open or update {self.path} ({type(e).__name__}: {e}).") from None
        except StoreError:
            self.db.close()
            raise
        for extra in ("-wal", "-shm"):
            p = self.path.with_name(self.path.name + extra)
            if p.exists():
                os.chmod(p, 0o600)

    def close(self):
        with self.lock:
            self.db.close()

    # --- meta -----------------------------------------------------------
    def _get_meta(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def _set_meta(self, key, value):
        with self.lock:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                                "SET value=excluded.value", (key, json.dumps(value)))

    # --- settings -------------------------------------------------------
    def settings(self):
        s = default_settings()
        saved = self._get_meta("settings", {})
        s.update({k: v for k, v in saved.items() if k in s})
        return s

    def update_settings(self, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the settings as a JSON object.")
        s = self.settings()
        setup_done = None
        for key, val in patch.items():
            if key == "setup_done":
                if not isinstance(val, bool):
                    raise InputError("setup_done must be true or false.")
                setup_done = val
            elif key == "name":
                if not isinstance(val, str) or len(val) > 80:
                    raise InputError("Your name must be text of at most 80 characters.")
                s["name"] = val.strip()
            elif key == "timezone":
                try:
                    if not isinstance(val, str) or not val:
                        raise ValueError
                    zoneinfo.ZoneInfo(val)
                except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                    raise InputError("That time zone is not known. Use a name like Europe/Amsterdam.") from None
                s["timezone"] = val
            elif key == "workdays":
                if not isinstance(val, list) or any(d not in WEEKDAYS for d in val):
                    raise InputError("Workdays must be a list of mon, tue, wed, thu, fri, sat, sun.")
                s["workdays"] = [d for d in WEEKDAYS if d in val]
            elif key == "reply_age_days":
                if not isinstance(val, int) or isinstance(val, bool) or not 0 <= val <= 30:
                    raise InputError("Reply age must be a whole number of days from 0 to 30.")
                s["reply_age_days"] = val
            elif key == "call_times":
                s["call_times"] = self._merge_call_times(s["call_times"], val)
            elif key == "skip_checkins_days_off":
                if not isinstance(val, bool):
                    raise InputError("skip_checkins_days_off must be true or false.")
                s["skip_checkins_days_off"] = val
            elif key == "look":
                if val not in LOOKS:
                    raise InputError("Look must be bold, clean or easy.")
                s["look"] = val
            elif key == "time_format":
                if val not in TIME_FORMATS:
                    raise InputError("Time format must be 12h or 24h.")
                s["time_format"] = val
            elif key == "mode":
                if val not in MODES:
                    raise InputError("Mode must be auto, light or dark.")
                s["mode"] = val
            else:
                raise InputError(f"Unknown setting: {str(key)[:40]}.")
        self._set_meta("settings", s)
        if setup_done is not None:
            self._set_meta("setup_done", setup_done)
        return s

    @staticmethod
    def _merge_call_times(current, val):
        if not isinstance(val, list):
            raise InputError("Call times must be a list.")
        by_id = {c["id"]: dict(c) for c in current}
        for item in val:
            if not isinstance(item, dict) or item.get("id") not in by_id:
                raise InputError("Each call time needs an id: morning, midday or afternoon.")
            c = by_id[item["id"]]
            if "time" in item:
                t = item["time"]
                if not isinstance(t, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                    raise InputError("Times must look like 06:30 (24-hour clock).")
                c["time"] = t
            if "enabled" in item:
                if not isinstance(item["enabled"], bool):
                    raise InputError("enabled must be true or false.")
                c["enabled"] = item["enabled"]
            if "label" in item:
                if not isinstance(item["label"], str) or not item["label"].strip() or len(item["label"]) > 40:
                    raise InputError("A call time label must be 1 to 40 characters.")
                c["label"] = item["label"].strip()
        return [by_id[k] for k in ("morning", "midday", "afternoon")]

    def setup_done(self):
        return bool(self._get_meta("setup_done", False))

    # --- Google client --------------------------------------------------
    def google_client(self):
        """{client_id, client_secret} or None. Internal only: never send this to the browser."""
        return self._get_meta("google_client")

    def set_google_client(self, client_id, client_secret):
        if not isinstance(client_id, str) or not isinstance(client_secret, str):
            raise InputError("Paste both the client ID and the client secret.")
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not client_id or not client_secret:
            raise InputError("Paste both the client ID and the client secret.")
        if not client_id.endswith(GOOGLE_ID_SUFFIX) or re.search(r"\s", client_id) or len(client_id) > 200:
            raise InputError("That doesn't look like a Google client ID. It ends in .apps.googleusercontent.com.")
        if re.search(r"\s", client_secret) or len(client_secret) > 200:
            raise InputError("That doesn't look like a Google client secret.")
        self._set_meta("google_client", {"client_id": client_id, "client_secret": client_secret})

    def clear_google_client(self):
        self._set_meta("google_client", None)

    def google_client_public(self):
        c = self.google_client()
        return {"configured": bool(c), "client_id_hint": client_id_hint(c["client_id"]) if c else None}

    # --- areas ----------------------------------------------------------
    @staticmethod
    def _area(row):
        reads = {k: True for k in READ_KEYS}  # keys added later (writer) default to on
        reads.update(json.loads(row["reads"]))
        return {"id": row["id"], "name": row["name"], "colour": row["colour"],
                "reads": reads, "fixed": bool(row["fixed"])}

    def areas(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM areas ORDER BY position, rowid").fetchall()
        return [self._area(r) for r in rows]

    def area(self, area_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
        if row is None:
            raise NotFound("That area doesn't exist.")
        return self._area(row)

    @staticmethod
    def _check_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise InputError("An area name must be 1 to 40 characters.")
        return name.strip()

    @staticmethod
    def _check_colour(colour):
        if not isinstance(colour, int) or isinstance(colour, bool) or not 1 <= colour <= 4:
            raise InputError("Colour must be 1, 2, 3 or 4.")
        return colour

    def add_area(self, name, colour):
        name, colour = self._check_name(name), self._check_colour(colour)
        with self.lock:
            base = _slug(name)
            area_id, n = base, 2
            while self.db.execute("SELECT 1 FROM areas WHERE id=?", (area_id,)).fetchone():
                area_id, n = f"{base}-{n}", n + 1
            pos = self.db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM areas").fetchone()[0]
            self.db.execute("INSERT INTO areas (id, name, colour, reads, fixed, position) VALUES (?,?,?,?,0,?)",
                            (area_id, name, colour, json.dumps({k: True for k in READ_KEYS}), pos))
        return self.area(area_id)

    def update_area(self, area_id, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the change as a JSON object.")
        with self.lock:
            a = self.area(area_id)
            for key, val in patch.items():
                if key == "name":
                    a["name"] = self._check_name(val)
                elif key == "colour":
                    a["colour"] = self._check_colour(val)
                elif key == "reads":
                    if not isinstance(val, dict) or any(k not in READ_KEYS or not isinstance(v, bool)
                                                        for k, v in val.items()):
                        raise InputError("reads takes true/false for calendar, mail, chat, tasks and writer.")
                    a["reads"].update(val)
                else:
                    raise InputError(f"Areas can't change {str(key)[:40]}.")
            self.db.execute("UPDATE areas SET name=?, colour=?, reads=? WHERE id=?",
                            (a["name"], a["colour"], json.dumps(a["reads"]), area_id))
        return a

    def delete_area(self, area_id):
        """Deletes the area and its accounts. Returns the deleted accounts (with tokens) so the
        caller can revoke them. VIPs of the area move to Personal."""
        with self.lock:
            a = self.area(area_id)
            if a["fixed"]:
                raise InputError("Personal can't be deleted.")
            accounts = [self.account_secret(r["id"]) for r in
                        self.db.execute("SELECT id FROM accounts WHERE area_id=?", (area_id,)).fetchall()]
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM accounts WHERE area_id=?", (area_id,))
                self.db.execute("DELETE FROM slack WHERE area_id=?", (area_id,))
                self.db.execute("UPDATE vips SET area_id='personal' WHERE area_id=?", (area_id,))
                self.db.execute("DELETE FROM areas WHERE id=?", (area_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return accounts

    # --- accounts -------------------------------------------------------
    @staticmethod
    def _account_public(row):
        return {"id": row["id"], "area_id": row["area_id"], "email": row["email"], "status": row["status"],
                "error": row["error"], "services": json.loads(row["services"]),
                "calendars": json.loads(row["calendars"])}

    def accounts(self, area_id=None):
        """Public account objects (no tokens)."""
        with self.lock:
            if area_id is None:
                rows = self.db.execute("SELECT * FROM accounts ORDER BY created_at, rowid").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM accounts WHERE area_id=? ORDER BY created_at, rowid",
                                       (area_id,)).fetchall()
        return [self._account_public(r) for r in rows]

    def has_accounts(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None

    def account(self, account_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise NotFound("That account doesn't exist.")
        return self._account_public(row)

    def account_secret(self, account_id):
        """Account with tokens. Internal only (sync, revoke)."""
        with self.lock:
            row = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise NotFound("That account doesn't exist.")
        a = self._account_public(row)
        a.update(refresh_token=row["refresh_token"], access_token=row["access_token"],
                 expires_at=row["expires_at"])
        return a

    def save_account(self, area_id, email, refresh_token, access_token, expires_at, now):
        """Insert, or update the tokens (and area) of an existing account with this email.
        Returns the account id."""
        self.area(area_id)
        with self.lock:
            row = self.db.execute("SELECT id, refresh_token FROM accounts WHERE lower(email)=lower(?)",
                                  (email,)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE accounts SET area_id=?, refresh_token=?, access_token=?, expires_at=?, "
                    "status='connected', error=NULL WHERE id=?",
                    (area_id, refresh_token or row["refresh_token"], access_token, expires_at, row["id"]))
                return row["id"]
            account_id = new_id("acc")
            self.db.execute(
                "INSERT INTO accounts (id, area_id, email, refresh_token, access_token, expires_at, status, "
                "services, calendars, created_at) VALUES (?,?,?,?,?,?, 'connected', ?, '[]', ?)",
                (account_id, area_id, email, refresh_token, access_token, expires_at,
                 json.dumps({"calendar": True, "mail": True}), now))
            return account_id

    def set_tokens(self, account_id, access_token, expires_at, refresh_token=None):
        with self.lock:
            if refresh_token:
                self.db.execute("UPDATE accounts SET access_token=?, expires_at=?, refresh_token=? WHERE id=?",
                                (access_token, expires_at, refresh_token, account_id))
            else:
                self.db.execute("UPDATE accounts SET access_token=?, expires_at=? WHERE id=?",
                                (access_token, expires_at, account_id))

    def set_status(self, account_id, status, error=None):
        with self.lock:
            self.db.execute("UPDATE accounts SET status=?, error=? WHERE id=?", (status, error, account_id))

    def set_calendar_list(self, account_id, items):
        """items: [{id, summary, primary}] from Google. Keeps earlier selections; primary is selected
        by default for calendars seen for the first time."""
        with self.lock:
            old = {c["id"]: c for c in self.account(account_id)["calendars"]}
            cals = []
            for it in items:
                prev = old.get(it["id"])
                cals.append({"id": it["id"], "summary": it.get("summary") or it["id"],
                             "primary": bool(it.get("primary")),
                             "selected": prev["selected"] if prev else bool(it.get("primary"))})
            self.db.execute("UPDATE accounts SET calendars=? WHERE id=?", (json.dumps(cals), account_id))

    def update_account(self, account_id, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the change as a JSON object.")
        with self.lock:
            a = self.account(account_id)
            for key, val in patch.items():
                if key == "services":
                    if not isinstance(val, dict) or any(k not in ("calendar", "mail") or not isinstance(v, bool)
                                                        for k, v in val.items()):
                        raise InputError("services takes true/false for calendar and mail.")
                    a["services"].update(val)
                elif key == "calendars":
                    known = {c["id"] for c in a["calendars"]}
                    if not isinstance(val, list) or any(not isinstance(c, str) or c not in known for c in val):
                        raise InputError("calendars must be a list of this account's calendar ids.")
                    for c in a["calendars"]:
                        c["selected"] = c["id"] in val
                else:
                    raise InputError(f"Accounts can't change {str(key)[:40]}.")
            self.db.execute("UPDATE accounts SET services=?, calendars=? WHERE id=?",
                            (json.dumps(a["services"]), json.dumps(a["calendars"]), account_id))
        return a

    def delete_account(self, account_id):
        """Deletes the account and returns it with tokens so the caller can revoke them."""
        with self.lock:
            a = self.account_secret(account_id)
            self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return a

    # --- VIPs -----------------------------------------------------------
    def vips(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM vips ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def add_vip(self, name, email, area_id):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise InputError("A VIP needs a name of at most 80 characters.")
        if not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()) \
                or len(email) > 254:
            raise InputError("That email address doesn't look right.")
        if not isinstance(area_id, str):
            raise InputError("Pick an area for this VIP.")
        self.area(area_id)
        vip = {"id": new_id("vip"), "name": name.strip(), "email": email.strip().lower(), "area_id": area_id}
        with self.lock:
            self.db.execute("INSERT INTO vips (id, name, email, area_id) VALUES (?,?,?,?)",
                            (vip["id"], vip["name"], vip["email"], vip["area_id"]))
        return vip

    def delete_vip(self, vip_id):
        with self.lock:
            if self.db.execute("DELETE FROM vips WHERE id=?", (vip_id,)).rowcount == 0:
                raise NotFound("That VIP doesn't exist.")

    # --- writer ---------------------------------------------------------
    def writer_config(self):
        """Writer settings with the API keys. Internal only: never send this to the browser."""
        w = default_writer()
        saved = self._get_meta("writer", {})
        for k, v in saved.items():
            if k == "prompt_profiles":
                if isinstance(v, list):
                    w[k] = [x for x in v if isinstance(x, dict) and isinstance(x.get("id"), str)]
            elif isinstance(w.get(k), dict) and isinstance(v, dict):
                w[k].update({kk: vv for kk, vv in v.items() if kk in w[k]})
            elif k in w:
                w[k] = v
        if not any(x["id"] == w["active_profile"] for x in w["prompt_profiles"]):
            w["active_profile"] = "default"
        return w

    @staticmethod
    def _profiles(w):
        """Every profile: the built-in read-only Default first, then the owner's."""
        out = [dict(writer.DEFAULT_PROMPTS, id="default", name="Default", read_only=True)]
        for x in w["prompt_profiles"]:
            texts = writer.effective_prompts(x)
            out.append(dict(texts, id=x["id"], name=str(x.get("name") or "Profile"), read_only=False))
        return out

    def active_prompts(self, w=None):
        """{summary, areas, drafts} of the active profile (for the writer)."""
        w = w or self.writer_config()
        p = next(x for x in self._profiles(w) if x["id"] == w["active_profile"])
        return {k: p[k] for k in writer.PROMPT_KEYS}

    def writer_public(self):
        w = self.writer_config()
        return {"provider": w["provider"],
                "claude": {"key_hint": key_hint(w["claude"]["api_key"]), "model": w["claude"]["model"]},
                "local": dict({"base_url": w["local"]["base_url"], "model": w["local"]["model"],
                               "key_hint": key_hint(w["local"]["api_key"])}, **writer.local_options(w["local"])),
                "fallback_to_claude": w["fallback_to_claude"], "suggest_replies": w["suggest_replies"],
                "prompt_profiles": self._profiles(w), "active_profile": w["active_profile"],
                "prompts": self.active_prompts(w), "prompt_defaults": dict(writer.DEFAULT_PROMPTS),
                "prompt_max": writer.PROMPT_MAX, "profile_max": MAX_PROFILES}

    @staticmethod
    def _check_key(val, what):
        if not isinstance(val, str) or re.search(r"\s", val.strip()) or len(val.strip()) > 300:
            raise InputError(f"That doesn't look like {what}.")
        return val.strip()

    @staticmethod
    def _check_base_url(val):
        if not isinstance(val, str) or len(val.strip()) > 300:
            raise InputError("The server address must be text of at most 300 characters.")
        val = val.strip().rstrip("/")
        if not val:
            return ""
        try:
            parts = urllib.parse.urlsplit(val)
            parts.port  # noqa: B018 (raises ValueError for a bad port)
        except ValueError:
            parts = None
        if (parts is None or parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or re.search(r"\s", val)):
            raise InputError("The server address must look like http://192.168.1.20:8080/v1.")
        return val

    def update_writer(self, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the writer settings as a JSON object.")
        with self.lock:
            w = self.writer_config()
            for key, val in patch.items():
                if key == "provider":
                    if val not in ("off", "claude", "local"):
                        raise InputError("The writer must be off, claude or local.")
                    w["provider"] = val
                elif key == "claude":
                    if not isinstance(val, dict):
                        raise InputError("claude takes api_key and model.")
                    for k, v in val.items():
                        if k == "api_key":
                            v = self._check_key(v, "an Anthropic API key")
                            if not v.startswith("sk-ant-"):
                                raise InputError("That doesn't look like an Anthropic API key. It starts with sk-ant-.")
                            w["claude"]["api_key"] = v
                        elif k == "model":
                            if v not in writer.MODELS:
                                raise InputError("Pick Claude Opus 5.5, Sonnet 5.5 or Haiku 5.5.")
                            w["claude"]["model"] = v
                        else:
                            raise InputError(f"claude can't change {str(k)[:40]}.")
                elif key == "local":
                    if not isinstance(val, dict):
                        raise InputError("local takes base_url, model, api_key, thinking, reasoning_budget, "
                                         "max_tokens, context and timeout.")
                    for k, v in val.items():
                        if k == "base_url":
                            w["local"]["base_url"] = self._check_base_url(v)
                        elif k == "model":
                            if not isinstance(v, str) or len(v.strip()) > 120 or re.search(r"[\x00-\x1f]", v):
                                raise InputError("The model name must be text of at most 120 characters.")
                            w["local"]["model"] = v.strip()
                        elif k == "api_key":
                            v = "" if v is None else v
                            w["local"]["api_key"] = self._check_key(v, "an API key") or None
                        elif k == "thinking":
                            if not isinstance(v, bool):
                                raise InputError("thinking must be true or false.")
                            w["local"]["thinking"] = v
                        elif k in writer.LOCAL_LIMITS:
                            lo, hi = writer.LOCAL_LIMITS[k]
                            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                                raise InputError(f"{LOCAL_NUMBER_NAMES[k]} must be a whole number from "
                                                 f"{lo:,} to {hi:,}.")
                            w["local"][k] = v
                        else:
                            raise InputError(f"local can't change {str(k)[:40]}.")
                elif key == "active_profile":
                    if not isinstance(val, str) or not any(x["id"] == val for x in self._profiles(w)):
                        raise InputError("That writing profile doesn't exist.")
                    w["active_profile"] = val
                elif key in ("fallback_to_claude", "suggest_replies"):
                    if not isinstance(val, bool):
                        raise InputError(f"{key} must be true or false.")
                    w[key] = val
                else:
                    raise InputError(f"The writer can't change {str(key)[:40]}.")
            w["configured_by_owner"] = True
            self._set_meta("writer", w)
        return self.writer_public()

    # --- writing-instruction profiles -------------------------------------
    @staticmethod
    def _check_profile_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40 or re.search(r"[\x00-\x1f]", name):
            raise InputError("A profile name must be 1 to 40 characters.")
        return name.strip()

    @staticmethod
    def _check_prompt(val):
        """Text of at most 1500 characters; empty = the Default text."""
        if not isinstance(val, str) or len(val) > writer.PROMPT_MAX:
            raise InputError(f"Writing instructions must be text of at most {writer.PROMPT_MAX:,} characters.")
        if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", val):
            raise InputError("Writing instructions can't contain control characters.")
        return val.strip()

    def add_profile(self, copy_from, name):
        with self.lock:
            w = self.writer_config()
            src = next((x for x in self._profiles(w) if x["id"] == (copy_from or "default")), None)
            if src is None:
                raise NotFound("That writing profile doesn't exist.")
            name = self._check_profile_name(name)
            if len(w["prompt_profiles"]) >= MAX_PROFILES:
                raise InputError(f"There can be at most {MAX_PROFILES} profiles of your own. Delete one first.")
            prof = dict({k: src[k] for k in writer.PROMPT_KEYS}, id=new_id("prof"), name=name)
            w["prompt_profiles"].append(prof)
            w["configured_by_owner"] = True
            self._set_meta("writer", w)
        return next(x for x in self._profiles(w) if x["id"] == prof["id"])

    def update_profile(self, prof_id, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the change as a JSON object.")
        with self.lock:
            w = self.writer_config()
            if prof_id == "default":
                raise InputError("The Default profile can't be changed. Copy it to make your own.")
            prof = next((x for x in w["prompt_profiles"] if x["id"] == prof_id), None)
            if prof is None:
                raise NotFound("That writing profile doesn't exist.")
            for k, v in patch.items():
                if k == "name":
                    prof["name"] = self._check_profile_name(v)
                elif k in writer.PROMPT_KEYS:
                    prof[k] = self._check_prompt(v) or writer.DEFAULT_PROMPTS[k]
                else:
                    raise InputError(f"Profiles can't change {str(k)[:40]}.")
            w["configured_by_owner"] = True
            self._set_meta("writer", w)
        return next(x for x in self._profiles(w) if x["id"] == prof_id)

    def delete_profile(self, prof_id):
        with self.lock:
            w = self.writer_config()
            if prof_id == "default":
                raise InputError("The Default profile can't be deleted.")
            if not any(x["id"] == prof_id for x in w["prompt_profiles"]):
                raise NotFound("That writing profile doesn't exist.")
            if w["active_profile"] == prof_id:
                raise InputError("This profile is in use. Pick another one first.")
            w["prompt_profiles"] = [x for x in w["prompt_profiles"] if x["id"] != prof_id]
            self._set_meta("writer", w)
        return self.writer_public()

    def clear_claude_key(self):
        with self.lock:
            w = self.writer_config()
            w["claude"]["api_key"] = None
            w["configured_by_owner"] = True
            self._set_meta("writer", w)
        return self.writer_public()

    def seed_local_writer(self, base_url, model):
        """From the app options (Home Assistant's Options tab): turn on the local writer with this
        server and model, unless the owner has set up the writer in Callsheet. Empty values change
        nothing. Returns True when applied; raises InputError for a bad address or model name."""
        if not (isinstance(base_url, str) and base_url.strip() and isinstance(model, str) and model.strip()):
            return False
        with self.lock:
            w = self.writer_config()
            if w["configured_by_owner"]:
                return False
            base_url = self._check_base_url(base_url)
            if len(model.strip()) > 120 or re.search(r"[\x00-\x1f]", model):
                raise InputError("The model name must be text of at most 120 characters.")
            w["provider"] = "local"
            w["local"]["base_url"], w["local"]["model"] = base_url, model.strip()
            self._set_meta("writer", w)
        return True

    # --- Slack ------------------------------------------------------------
    @staticmethod
    def _slack_public(row):
        state = json.loads(row["state"] or "{}")
        return {"id": row["id"], "area_id": row["area_id"], "team_id": row["team_id"], "team_name": row["team_name"],
                "domain": row["domain"], "user_id": row["user_id"], "user_name": row["user_name"],
                "token_hint": key_hint(row["token"]), "status": row["status"], "error": row["error"],
                "channels_total": len(state.get("channels") or []), "ignored": json.loads(row["ignored"] or "[]"),
                "last_read_at": state.get("last_read_at")}

    def slack_workspaces(self, area_id=None):
        """Public workspace objects (no token)."""
        with self.lock:
            if area_id is None:
                rows = self.db.execute("SELECT * FROM slack ORDER BY created_at, rowid").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM slack WHERE area_id=? ORDER BY created_at, rowid",
                                       (area_id,)).fetchall()
        return [self._slack_public(r) for r in rows]

    def slack_workspace(self, ws_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM slack WHERE id=?", (ws_id,)).fetchone()
        if row is None:
            raise NotFound("That Slack workspace isn't connected.")
        return self._slack_public(row)

    def slack_secret(self, ws_id):
        """Workspace with its token and saved state. Internal only (sync, channel list)."""
        with self.lock:
            row = self.db.execute("SELECT * FROM slack WHERE id=?", (ws_id,)).fetchone()
        if row is None:
            raise NotFound("That Slack workspace isn't connected.")
        ws = self._slack_public(row)
        ws.update(token=row["token"], state=json.loads(row["state"] or "{}"))
        return ws

    def save_slack(self, area_id, info, token, now, channels=None):
        """Insert, or update the token (and area) of the workspace with this team id. Returns its id."""
        self.area(area_id)
        with self.lock:
            row = self.db.execute("SELECT id, state FROM slack WHERE team_id=?", (info["team_id"],)).fetchone()
            if row:
                state = json.loads(row["state"] or "{}")
                if info["user_id"] != self.db.execute("SELECT user_id FROM slack WHERE id=?",
                                                      (row["id"],)).fetchone()[0]:
                    state = {}  # another person's token: forget the old person's threads and mentions
                if channels is not None:
                    state["channels"] = channels
                self.db.execute(
                    "UPDATE slack SET area_id=?, team_name=?, domain=?, user_id=?, user_name=?, token=?, "
                    "status='connected', error=NULL, state=? WHERE id=?",
                    (area_id, info["team_name"], info["domain"], info["user_id"], info["user_name"], token,
                     json.dumps(state), row["id"]))
                return row["id"]
            ws_id = new_id("slk")
            self.db.execute(
                "INSERT INTO slack (id, area_id, team_id, team_name, domain, user_id, user_name, token, status, "
                "ignored, state, created_at) VALUES (?,?,?,?,?,?,?,?, 'connected', '[]', ?, ?)",
                (ws_id, area_id, info["team_id"], info["team_name"], info["domain"], info["user_id"],
                 info["user_name"], token, json.dumps({"channels": channels or []}), now))
            return ws_id

    def set_slack_ignored(self, ws_id, ignored):
        with self.lock:
            ws = self.slack_secret(ws_id)
            known = {c["id"] for c in ws["state"].get("channels") or []}
            if not isinstance(ignored, list) or len(ignored) > MAX_IGNORED or any(
                    not isinstance(c, str) or c not in known for c in ignored):
                raise InputError("ignored must be a list of this workspace's channel ids.")
            self.db.execute("UPDATE slack SET ignored=? WHERE id=?", (json.dumps(list(dict.fromkeys(ignored))), ws_id))
        return self.slack_workspace(ws_id)

    def set_slack_state(self, ws_id, state=None, status=None, error=None):
        with self.lock:
            if state is not None:
                self.db.execute("UPDATE slack SET state=? WHERE id=?", (json.dumps(state), ws_id))
            if status is not None:
                self.db.execute("UPDATE slack SET status=?, error=? WHERE id=?", (status, error, ws_id))

    def delete_slack(self, ws_id):
        with self.lock:
            if self.db.execute("DELETE FROM slack WHERE id=?", (ws_id,)).rowcount == 0:
                raise NotFound("That Slack workspace isn't connected.")

    def has_sources(self):
        """A Google account or a Slack workspace is connected (the brief is live)."""
        with self.lock:
            return self.has_accounts() or self.db.execute("SELECT 1 FROM slack LIMIT 1").fetchone() is not None

    # --- doesn't need a reply ------------------------------------------------
    def suppress_rules(self):
        rules_list = self._get_meta("suppress", [])
        return rules_list if isinstance(rules_list, list) else []

    def add_suppress(self, kind, value, source, label, now_iso):
        """Adds a rule, or returns the same existing one. Returns the rule."""
        if kind not in SUPPRESS_KINDS:
            raise InputError("Hide this thread, this sender or this domain.")
        value = str(value or "").strip().lower()
        if not value:
            raise InputError("This row has nothing to hide it by.")
        with self.lock:
            rules_list = self.suppress_rules()
            for r in rules_list:
                if (r.get("kind"), r.get("value"), r.get("source")) == (kind, value, source):
                    return r
            if len(rules_list) >= MAX_SUPPRESS:
                raise InputError(f"There are already {MAX_SUPPRESS} rules. Remove some in Setup first.")
            rule = {"id": new_id("sup"), "kind": kind, "value": value[:300], "source": source,
                    "created": now_iso, "label": str(label or value)[:120]}
            self._set_meta("suppress", rules_list + [rule])
        return rule

    def delete_suppress(self, rule_id):
        with self.lock:
            rules_list = self.suppress_rules()
            left = [r for r in rules_list if r.get("id") != rule_id]
            if len(left) == len(rules_list):
                raise NotFound("That rule doesn't exist.")
            self._set_meta("suppress", left)

    # --- done ------------------------------------------------------------------
    def done_keys(self):
        """{done key: {done_at: ISO time, last: last message time (epoch seconds)}}."""
        v = self._get_meta("done", {})
        return {k: e for k, e in v.items() if isinstance(e, dict)} if isinstance(v, dict) else {}

    @staticmethod
    def _prune_done(done, now_ts):
        def at(e):
            try:
                return dt.datetime.fromisoformat(e.get("done_at")).timestamp()
            except (TypeError, ValueError):
                return 0.0
        keep = {k: e for k, e in done.items() if now_ts - at(e) <= DONE_KEEP_S}
        if len(keep) > MAX_DONE:
            keep = dict(sorted(keep.items(), key=lambda kv: at(kv[1]))[-MAX_DONE:])
        return keep

    def set_done(self, key, last, now_iso, now_ts):
        """Marks a row's thread done (with the last message time seen); prunes keys older than 30 days."""
        with self.lock:
            done = self.done_keys()
            done[str(key)] = {"done_at": now_iso, "last": float(last or 0)}
            self._set_meta("done", self._prune_done(done, now_ts))

    def clear_done(self, keys, now_ts=None):
        """Removes these keys (Undo, or reopened by a newer message); with now_ts also prunes old keys."""
        with self.lock:
            done = self.done_keys()
            left = {k: e for k, e in done.items() if k not in set(keys)}
            if now_ts is not None:
                left = self._prune_done(left, now_ts)
            if left != done:
                self._set_meta("done", left)

    # --- new since, row refs ----------------------------------------------------
    def seen_day(self):
        """{date, keys: [row keys of every brief written that day], briefs: [ISO times]}."""
        v = self._get_meta("seen_day", {})
        return v if isinstance(v, dict) else {}

    def set_seen_day(self, value):
        self._set_meta("seen_day", value)

    def brief_refs(self):
        """Per row id of the cached brief: what "Doesn't need a reply" hides it by. Internal only."""
        v = self._get_meta("brief_refs", {})
        return v if isinstance(v, dict) else {}

    def set_brief_refs(self, value):
        self._set_meta("brief_refs", value)

    # --- writer evaluation (owner-run, see evaluate.py) -------------------
    def eval_settings(self):
        """{enabled (default off), gemma_model, qwen_model}: model ids the evaluation sends to the local server."""
        e = {"enabled": False, "gemma_model": evaluate.GEMMA_MODEL, "qwen_model": evaluate.QWEN_MODEL}
        saved = self._get_meta("eval", {})
        e.update({k: v for k, v in (saved if isinstance(saved, dict) else {}).items() if k in e})
        return e

    def update_eval(self, patch):
        if not isinstance(patch, dict):
            raise InputError("Send the evaluation settings as a JSON object.")
        with self.lock:
            e = self.eval_settings()
            for key, val in patch.items():
                if key == "enabled":
                    if not isinstance(val, bool):
                        raise InputError("enabled must be true or false.")
                    e["enabled"] = val
                elif key in ("gemma_model", "qwen_model"):
                    if not isinstance(val, str) or len(val.strip()) > 120 or re.search(r"[\x00-\x1f]", val):
                        raise InputError("The model name must be text of at most 120 characters.")
                    e[key] = val.strip() or (evaluate.GEMMA_MODEL if key == "gemma_model" else evaluate.QWEN_MODEL)
                else:
                    raise InputError(f"The evaluation settings can't change {str(key)[:40]}.")
            self._set_meta("eval", e)
        return e

    def last_writer(self):
        return self._get_meta("last_writer", {"at": None, "ok": None, "by": None, "input_tokens": 0,
                                              "output_tokens": 0, "empty_suggestions": 0, "error": None})

    def set_last_writer(self, value):
        self._set_meta("last_writer", value)

    # --- sync results ---------------------------------------------------
    def last_sync(self):
        return self._get_meta("last_sync", {"at": None, "ok": None, "errors": []})

    def set_last_sync(self, value):
        self._set_meta("last_sync", value)

    def cached_brief(self):
        return self._get_meta("brief")

    def set_cached_brief(self, brief):
        self._set_meta("brief", brief)
