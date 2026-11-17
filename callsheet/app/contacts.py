"""0.21.0 Google Contacts, read-only (approved 2026-11-12; docs/people.md).

Callsheet reads the owner's Google Contacts to link people across sources: a Signal number, a text's number, a Gmail
address and a Slack user's email become one person. Scope `contacts.readonly`, granted per Google account in Setup ›
Connections ("Allow contacts", an incremental grant like Drive's). The only call is
`GET people.googleapis.com/v1/people/me/connections` (google.people_connections; google.py's allowlist), paged, with a
sync token so later reads fetch only the changes. Read every 6 hours and when the owner presses Sync now. Contact data
stays in callsheet.db (table `contacts`); it is never sent anywhere.

- Normalising: norm_phone() (E.164; a national number takes the owner's country code, from the owner's own Signal number,
  else the owner's time zone, else +1) and canon_email() (case off; for gmail.com also dots and "+tag" off).
- Contacts: the grant, its status per account, the sync, the Sources row, birthdays for Coming up, and the resolver:
  resolve(source, handle, name) gives one person key ("person:c…" for a contact, "person:p…" for a People row, else
  the normalised handle) and the contact's name. Contacts come first, then the existing People rows (a People row that
  shares an address with a contact is that contact), then the normalised number or address.
- relink(ms): the ledger's items and follow-ups keyed by an older handle move to the person key (history "linked"),
  so "Dana on Signal" and "Dana by email" are one person for clashes and follow-ups.
"""
import datetime as dt
import hashlib
import json
import re
import threading
import time

import google
import people as people_mod
from store import Conflict, InputError

SYNC_EVERY_S = 6 * 3600          # a granted account's contacts are read every 6 hours (and on Sync now)
RETRY_S = 30 * 60                # after an error, the next try waits this long
TICK_S = 60
PAGE_SIZE = 1000
MAX_PAGES = 60                   # 60,000 contacts per account at most
MAX_LIST = 20                    # emails / phones / nicknames kept per contact
INDEX_TTL_S = 60                 # the resolver's view of People and Slack is at most a minute old
DEFAULT_CC = "1"                 # +1 (US) when nothing says otherwise
PHONE_SOURCES = ("signal", "sms", "sms_live")
NAME_SOURCES = ("signal", "sms", "sms_live")   # Signal's profile name, the phone's own name for a number

# country calling codes the owner's region can give (the owner's own number's prefix, or the owner's time zone)
CODES = ("1", "7", "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48",
         "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82",
         "84", "86", "90", "91", "92", "93", "94", "95", "98", "212", "234", "254", "351", "352", "353", "354", "356",
         "357", "358", "359", "370", "371", "372", "380", "385", "386", "420", "421", "852", "886", "966", "971",
         "972", "974")
KEEP_ZERO = frozenset({"39"})    # Italy keeps the leading 0 of a national number
TZ_CC = {"Europe/London": "44", "Europe/Dublin": "353", "Europe/Berlin": "49", "Europe/Paris": "33",
         "Europe/Amsterdam": "31", "Europe/Brussels": "32", "Europe/Madrid": "34", "Europe/Rome": "39",
         "Europe/Lisbon": "351", "Europe/Zurich": "41", "Europe/Vienna": "43", "Europe/Stockholm": "46",
         "Europe/Oslo": "47", "Europe/Copenhagen": "45", "Europe/Helsinki": "358", "Europe/Warsaw": "48",
         "Europe/Prague": "420", "Europe/Athens": "30", "Europe/Kiev": "380", "Europe/Kyiv": "380",
         "Australia/Sydney": "61", "Australia/Melbourne": "61", "Australia/Brisbane": "61", "Australia/Perth": "61",
         "Australia/Adelaide": "61", "Pacific/Auckland": "64", "Asia/Tokyo": "81", "Asia/Singapore": "65",
         "Asia/Hong_Kong": "852", "Asia/Kolkata": "91", "Asia/Jerusalem": "972", "Asia/Dubai": "971",
         "Africa/Johannesburg": "27", "America/Mexico_City": "52", "America/Sao_Paulo": "55"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  account TEXT NOT NULL, resource TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', given TEXT NOT NULL DEFAULT '',
  nicknames TEXT NOT NULL DEFAULT '[]', emails TEXT NOT NULL DEFAULT '[]', phones TEXT NOT NULL DEFAULT '[]',
  birthday TEXT NOT NULL DEFAULT '', org TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
  PRIMARY KEY (account, resource));
"""


# --- normalising -------------------------------------------------------------------------------------------------------
def canon_email(e):
    """Lower case; for gmail.com (and googlemail.com) also without dots and "+tag" (Gmail delivers those as one)."""
    e = str(e or "").strip().lower()
    if e.startswith("mailto:"):
        e = e[7:]
    if e.count("@") != 1 or " " in e:
        return ""
    local, dom = e.split("@")
    if not local or "." not in dom:
        return ""
    if dom == "googlemail.com":
        dom = "gmail.com"
    if dom == "gmail.com":
        local = local.split("+", 1)[0].replace(".", "")
    return local + "@" + dom


def code_of(number):
    """The country calling code of an E.164 number ("+447700900123" -> "44"), or ""."""
    d = re.sub(r"\D", "", str(number or ""))
    if not str(number or "").strip().startswith("+") or len(d) < 8:
        return ""
    for n in (3, 2, 1):
        if d[:n] in CODES:
            return d[:n]
    return ""


def norm_phone(raw, cc=DEFAULT_CC):
    """A phone number as E.164 ("+15551234567"), or "" when it isn't one (a short code, an alphanumeric sender, a
    Signal uuid, an email). A national number takes the owner's country code cc (its trunk 0 dropped)."""
    s = str(raw or "").strip()
    s = re.sub(r"(?i)\s*(?:;ext=|ext\.?|x|#)\s*\d{1,6}$", "", s)        # an extension is not part of the number
    if not s or re.search(r"[^\d\s+().\-/]", s) or s.count("+") > 1 or ("+" in s and not s.startswith("+")):
        return ""
    s = re.sub(r"^(\+\d{1,3})\s*\(0\)", r"\1", s)               # "+44 (0)7700 …": the (0) is the trunk prefix
    plus = s.startswith("+")
    d = re.sub(r"\D", "", s)
    if not plus and d.startswith("00"):
        d, plus = d[2:], True
    elif not plus and cc == "1" and d.startswith("011"):
        d, plus = d[3:], True
    if plus:
        return "+" + d if 8 <= len(d) <= 15 and d[0] != "0" else ""
    cc = cc if cc in CODES else DEFAULT_CC
    if cc == "1":
        if len(d) == 10 and d[0] not in "01":
            return "+1" + d
        if len(d) == 11 and d[0] == "1" and d[1] not in "01":
            return "+" + d
        return ""
    if d.startswith("0") and cc not in KEEP_ZERO:
        d = d[1:]
    if not 6 <= len(d) <= 15 - len(cc) or (d[:1] == "0" and cc not in KEEP_ZERO):
        return ""
    return "+" + cc + d


def region_code(own_number=None, tz_name=None):
    """The owner's country calling code: the owner's own number's, else the owner's time zone's, else +1."""
    c = code_of(own_number)
    if c:
        return c
    tz = str(tz_name or "")
    if tz in TZ_CC:
        return TZ_CC[tz]
    if tz.startswith(("America/", "US/", "Canada/")) and not tz.startswith(("America/Mexico", "America/Sao")):
        return "1"
    return DEFAULT_CC


def _clip(v, n=200):
    return people_mod.clean(v, n)


def _gid(account, resource):
    return "c" + hashlib.sha1(f"{account}\x00{resource}".encode("utf-8")).hexdigest()[:11]


def person_row(p):
    """One People API person -> the stored fields (or None for a person without a resourceName)."""
    if not isinstance(p, dict) or not isinstance(p.get("resourceName"), str):
        return None

    def first(lst):
        lst = [x for x in lst or [] if isinstance(x, dict)]
        prim = [x for x in lst if (x.get("metadata") or {}).get("primary")]
        return (prim or lst or [{}])[0]

    nm = first(p.get("names"))
    name = _clip(nm.get("displayName") or " ".join(x for x in (nm.get("givenName"), nm.get("familyName")) if x), 120)
    emails, phones = [], []
    for e in p.get("emailAddresses") or []:
        v = canon_email(e.get("value")) if isinstance(e, dict) else ""
        if v and v not in emails:
            emails.append(v)
    for ph in p.get("phoneNumbers") or []:
        if isinstance(ph, dict) and (ph.get("value") or ph.get("canonicalForm")):
            phones.append({"value": _clip(ph.get("value"), 40), "canonical": _clip(ph.get("canonicalForm"), 20)})
    nicks = [_clip(n.get("value"), 60) for n in p.get("nicknames") or [] if isinstance(n, dict) and n.get("value")]
    org = first(p.get("organizations"))
    bday = ""
    for b in p.get("birthdays") or []:
        d = b.get("date") if isinstance(b, dict) else None
        if isinstance(d, dict) and isinstance(d.get("month"), int) and isinstance(d.get("day"), int):
            try:
                dt.date(2000, d["month"], d["day"])          # a leap year: 02-29 is a date
            except ValueError:
                continue
            y = d.get("year") if isinstance(d.get("year"), int) and 1900 <= d["year"] <= 2100 else None
            bday = (f"{y:04d}-" if y else "") + f"{d['month']:02d}-{d['day']:02d}"
            break
    return {"resource": p["resourceName"][:200], "name": name, "given": _clip(nm.get("givenName"), 60),
            "nicknames": nicks[:MAX_LIST], "emails": emails[:MAX_LIST], "phones": phones[:MAX_LIST], "birthday": bday,
            "org": _clip(org.get("name"), 120)}


def next_birthday(bday, today):
    """The next date (today or later) of a birthday "MM-DD" or "YYYY-MM-DD"; Feb 29 is Feb 28 in other years."""
    m, d = (int(x) for x in bday[-5:].split("-"))
    for y in (today.year, today.year + 1):
        try:
            day = dt.date(y, m, d)
        except ValueError:
            day = dt.date(y, 2, 28)
        if day >= today:
            return day
    return None


def ago(sec):
    sec = max(0, int(sec))
    if sec < 90:
        return "just now"
    if sec < 3600:
        return f"{round(sec / 60)} min ago"
    if sec < 86400:
        return f"{round(sec / 3600)} h ago"
    n = round(sec / 86400)
    return f"{n} day{'s' if n != 1 else ''} ago"


# --- the grant, the sync, the resolver --------------------------------------------------------------------------------
class Contacts:
    """store: store.Store; flows: oauth.OAuthFlows; people: people.People or None; own_number() -> the owner's own
    number (the linked Signal account) or None; tz_name() -> the owner's time zone; fetch(token, page, sync) -> one
    page (google.people_connections; a fake in tests)."""

    def __init__(self, store, flows, people=None, own_number=None, tz_name=None, clock=time.time, log=print,
                 fetch=None):
        self.store, self.flows, self.people, self.clock, self.log = store, flows, people, clock, log
        self.own_number = own_number or (lambda: None)
        self.tz_name = tz_name or (lambda: getattr(store, "settings", lambda: {})().get("timezone") or "")
        self.fetch = fetch or (lambda token, page, sync: google.people_connections(token, page, sync, PAGE_SIZE))
        self.lock = threading.RLock()          # one sync at a time
        self.ix_lock = threading.Lock()
        self._ix = None
        self._ver = 0
        self._linked_ver = -1
        self._stop = threading.Event()
        self._thread = None
        with self.store.lock:
            self.store.db.executescript(_SCHEMA)

    # --- state (meta "contacts": {"accounts": {id: {granted, checked, sync_token, synced_at, count, error, ...}}}) ---
    def _state(self):
        v = self.store._get_meta("contacts", {}) or {}   # noqa: SLF001
        v.setdefault("accounts", {})
        return v

    def _acc_state(self, acc_id, **patch):
        with self.lock:
            v = self._state()
            a = v["accounts"].setdefault(acc_id, {})
            a.update(patch)
            self.store._set_meta("contacts", v)   # noqa: SLF001
            return dict(a)

    def _token(self, acc_id, force=False):
        acc = self.store.account_secret(acc_id)
        if not force and acc.get("access_token") and (acc.get("expires_at") or 0) - 60 > self.clock():
            return acc["access_token"]
        client = self.store.google_client()
        if not client:
            raise Conflict("Add the Google sign-in key in Setup first.")
        if not acc.get("refresh_token"):
            raise Conflict("Google asked to sign in again. Reconnect the account in Setup.")
        try:
            res = google.refresh(client, acc["refresh_token"])
        except google.GoogleError as e:
            if e.code == "invalid_grant":
                raise Conflict("Google asked to sign in again. Reconnect the account in Setup.") from None
            raise Conflict(str(e)) from None
        tok = res.get("access_token")
        if not tok:
            raise Conflict("Google didn't send an access token.")
        self.store.set_tokens(acc_id, tok, self.clock() + float(res.get("expires_in", 3600)), res.get("refresh_token"))
        if isinstance(res.get("scope"), str):
            self._acc_state(acc_id, granted=google.CONTACTS_SCOPE in res["scope"].split(), checked=self.clock())
        return tok

    def _call(self, acc_id, page, sync):
        try:
            return self.fetch(self._token(acc_id), page, sync)
        except google.GoogleError as e:
            if e.status != 401:
                raise
        return self.fetch(self._token(acc_id, force=True), page, sync)

    # --- the sync ---------------------------------------------------------------------------------------------------
    def sync(self, acc_id, full=False):
        """Reads one account's contacts: all of them the first time (or when the sync token expired), else only the
        changes since the last read. Returns {"changed", "deleted", "count", "full"}; raises Conflict with a plain
        sentence (also kept as the account's error)."""
        with self.lock:
            st = self._state()["accounts"].get(acc_id, {})
            if st.get("granted") is not True:
                raise Conflict("Allow contacts for this account first (Setup › Connections).")
            sync_token = None if full else st.get("sync_token")
            now = self.clock()
            self._acc_state(acc_id, attempt_at=now)
            page, seen, changed, deleted, pages, next_sync = None, set(), 0, 0, 0, None
            try:
                while True:
                    try:
                        res = self._call(acc_id, page, sync_token)
                    except google.GoogleError as e:
                        if sync_token and (e.status == 410 or e.code in ("FAILED_PRECONDITION", "EXPIRED_SYNC_TOKEN")):
                            self.log("Callsheet contacts: the sync token expired; reading all contacts again")
                            self._acc_state(acc_id, sync_token=None)
                            return self.sync(acc_id, full=True)
                        if e.status == 403 or e.code in ("PERMISSION_DENIED", "insufficientPermissions"):
                            self._acc_state(acc_id, granted=False)
                            raise Conflict("Google didn't allow reading contacts. Press Allow contacts again.") from None
                        raise Conflict(str(e)) from None
                    res = res if isinstance(res, dict) else {}
                    c, d = self._save(acc_id, res.get("connections") or [], seen)
                    changed, deleted = changed + c, deleted + d
                    page = res.get("nextPageToken") if isinstance(res.get("nextPageToken"), str) else None
                    if not page:
                        next_sync = res.get("nextSyncToken") if isinstance(res.get("nextSyncToken"), str) else None
                        break
                    pages += 1
                    if pages >= MAX_PAGES:
                        raise Conflict("Too many contacts to read at once; Callsheet kept the first ones.")
                if sync_token is None:               # a full read replaces what was there
                    deleted += self._drop_unseen(acc_id, seen)
            except Conflict as e:
                self._acc_state(acc_id, error=str(e), error_at=self.clock())
                self._bump()
                raise
            n = self.count(acc_id)
            self._acc_state(acc_id, sync_token=next_sync, synced_at=self.clock(), count=n, error="", error_at=None,
                            **({"full_at": self.clock()} if sync_token is None else {}))
            self._bump()
            return {"changed": changed, "deleted": deleted, "count": n, "full": sync_token is None}

    def _save(self, acc_id, conns, seen):
        changed = deleted = 0
        now = self.clock()
        with self.store.lock:
            db = self.store.db
            db.execute("BEGIN")
            try:
                for p in conns:
                    row = person_row(p)
                    if row is None:
                        continue
                    if (p.get("metadata") or {}).get("deleted"):
                        deleted += db.execute("DELETE FROM contacts WHERE account=? AND resource=?",
                                              (acc_id, row["resource"])).rowcount
                        continue
                    seen.add(row["resource"])
                    db.execute("INSERT INTO contacts (account, resource, name, given, nicknames, emails, phones, "
                               "birthday, org, updated) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account, resource) DO "
                               "UPDATE SET name=excluded.name, given=excluded.given, nicknames=excluded.nicknames, "
                               "emails=excluded.emails, phones=excluded.phones, birthday=excluded.birthday, "
                               "org=excluded.org, updated=excluded.updated",
                               (acc_id, row["resource"], row["name"], row["given"], json.dumps(row["nicknames"]),
                                json.dumps(row["emails"]), json.dumps(row["phones"]), row["birthday"], row["org"], now))
                    changed += 1
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return changed, deleted

    def _drop_unseen(self, acc_id, seen):
        with self.store.lock:
            have = [r["resource"] for r in self.store.db.execute("SELECT resource FROM contacts WHERE account=?",
                                                                  (acc_id,))]
            gone = [r for r in have if r not in seen]
            for r in gone:
                self.store.db.execute("DELETE FROM contacts WHERE account=? AND resource=?", (acc_id, r))
        return len(gone)

    def count(self, acc_id=None):
        with self.store.lock:
            if acc_id is None:
                return self.store.db.execute("SELECT count(*) FROM contacts").fetchone()[0]
            return self.store.db.execute("SELECT count(*) FROM contacts WHERE account=?", (acc_id,)).fetchone()[0]

    def forget_gone_accounts(self):
        """Contacts of an account that was disconnected are forgotten with it."""
        ids = {a["id"] for a in self.store.accounts()}
        with self.store.lock:
            for (acc,) in self.store.db.execute("SELECT DISTINCT account FROM contacts").fetchall():
                if acc not in ids:
                    self.store.db.execute("DELETE FROM contacts WHERE account=?", (acc,))
                    self._bump()

    def due(self, now=None):
        """Granted accounts whose contacts are older than 6 hours (an error waits RETRY_S before the next try)."""
        now = self.clock() if now is None else now
        st = self._state()["accounts"]
        out = []
        for a in self.store.accounts():
            s = st.get(a["id"]) or {}
            if s.get("granted") is not True or a.get("status") not in (None, "connected"):
                continue
            if s.get("error") and now - (s.get("attempt_at") or 0) < RETRY_S:
                continue
            if now - (s.get("synced_at") or 0) >= SYNC_EVERY_S:
                out.append(a["id"])
        return out

    def tick(self, ms=None):
        """One pass of the reader: sync what is due, then relink the ledger when the people changed."""
        self.forget_gone_accounts()
        for acc_id in self.due():
            try:
                r = self.sync(acc_id)
                self.log(f"Callsheet contacts: read {r['changed']} changed, {r['deleted']} removed, {r['count']} kept")
            except Conflict as e:
                self.log(f"Callsheet contacts: {e}")
            except Exception as e:  # noqa: BLE001 (the reader keeps running)
                self.log(f"Callsheet contacts: sync failed ({type(e).__name__})")
        if ms is not None and self._linked_ver != self._ver:
            try:
                n = self.relink(ms)
                if n:
                    self.log(f"Callsheet contacts: {n} ledger entries now point at one person")
            except Exception as e:  # noqa: BLE001
                self.log(f"Callsheet contacts: relink failed ({type(e).__name__})")

    def start_thread(self, ms=None):
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                self.tick(ms)
                self._stop.wait(TICK_S)
        self._thread = threading.Thread(target=loop, name="contacts", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # --- the resolver -------------------------------------------------------------------------------------------------
    def _bump(self):
        with self.ix_lock:
            self._ver += 1
            self._ix = None

    def region(self):
        try:
            own = self.own_number()
        except Exception:  # noqa: BLE001
            own = None
        try:
            tz = self.tz_name()
        except Exception:  # noqa: BLE001
            tz = ""
        return region_code(own, tz)

    def _rows(self):
        with self.store.lock:
            return [dict(r) for r in self.store.db.execute("SELECT * FROM contacts ORDER BY account, resource")]

    def _owner_emails(self):
        out = set()
        for a in self.store.accounts():
            out.add(canon_email(a.get("email")))
        return out - {""}

    def _slack_emails(self):
        """Slack user id (lower case) -> the user's email, when Slack gave one (the workspace's users or People)."""
        out = {}
        try:
            for ws in self.store.slack_workspaces():
                for uid, u in ((ws.get("state") or {}).get("users") or {}).items():
                    if isinstance(u, dict) and canon_email(u.get("email")):
                        out[str(uid).lower()] = canon_email(u["email"])
        except Exception:  # noqa: BLE001 (no Slack)
            pass
        try:
            with self.store.lock:
                rows = self.store.db.execute("SELECT value, extra FROM person_handles WHERE kind='slack'").fetchall()
            for r in rows:
                try:
                    e = canon_email((json.loads(r["extra"] or "{}") or {}).get("email"))
                except ValueError:
                    e = ""
                if e:
                    out.setdefault(str(r["value"]).lower(), e)
        except Exception:  # noqa: BLE001 (People not set up)
            pass
        return out

    def _directory(self):
        if self.people is None:
            return None
        try:
            return self.people.directory()
        except Exception:  # noqa: BLE001
            return None

    def index(self):
        """The resolver's lookups, rebuilt after a sync (and at most a minute old for People and Slack)."""
        now = self.clock()
        with self.ix_lock:
            ix = self._ix
            if ix is not None and ix["ver"] == self._ver and now - ix["at"] < INDEX_TTL_S:
                return ix
            ver = self._ver
        cc = self.region()
        rows = self._rows()
        mine = self._owner_emails()
        # contacts in two accounts with a shared address or number are one person (union-find)
        parent = {}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        owner_of = {}
        keys = {}
        for r in rows:
            g = _gid(r["account"], r["resource"])
            parent[g] = g
            r["_g"] = g
            r["_emails"] = [e for e in json.loads(r["emails"] or "[]") if e]
            r["_phones"] = []
            for ph in json.loads(r["phones"] or "[]"):
                n = norm_phone(ph.get("canonical"), cc) or norm_phone(ph.get("value"), cc)
                if n and n not in r["_phones"]:
                    r["_phones"].append(n)
            for k in [("e", e) for e in r["_emails"]] + [("p", p) for p in r["_phones"]]:
                if k in owner_of:
                    a, b = find(owner_of[k]), find(g)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
                else:
                    owner_of[k] = g
        groups = {}
        for r in rows:
            g = find(r["_g"])
            v = groups.setdefault(g, {"id": g, "name": "", "given": "", "emails": [], "phones": [], "birthday": "",
                                      "nicknames": [], "org": ""})
            # the fullest name wins ("Dana Ruiz" over "Dana R."), so the shown name never hangs on account ids
            nm = r["name"] or ""
            if nm and (len(nm), v["name"]) > (len(v["name"]), nm):
                v["name"], v["given"] = nm, r["given"] or v["given"]
            elif not v["given"] and r["given"]:
                v["given"] = r["given"]
            for f in ("birthday", "org"):
                if not v[f] and r[f]:
                    v[f] = r[f]
            for f, src in (("emails", r["_emails"]), ("phones", r["_phones"]),
                           ("nicknames", json.loads(r["nicknames"] or "[]"))):
                v[f] += [x for x in src if x not in v[f]]
        by_email, by_phone, by_name = {}, {}, {}
        for g, v in list(groups.items()):
            if mine & set(v["emails"]):
                del groups[g]                   # the owner's own card: the owner is "owner", never a contact
                continue
            if not v["name"]:
                v["name"] = v["nicknames"][0] if v["nicknames"] else (v["emails"] + v["phones"] + [""])[0]
            for e in v["emails"]:
                by_email[e] = g
            for p in v["phones"]:
                by_phone[p] = g
            n = people_mod.full_name(v["name"])
            if n:
                by_name.setdefault(n, set()).add(g)
        slack_email = self._slack_emails()
        d = self._directory()
        pid_gid = {}
        if d is not None:
            for pid, p in d.persons.items():
                if p.get("state") in ("me", "merged"):
                    continue
                for e in list(p.get("email") or ()):
                    g = by_email.get(canon_email(e))
                    if g:
                        pid_gid.setdefault(pid, g)
                for s in p.get("slack") or ():
                    g = by_email.get(slack_email.get(str(s).lower(), ""))
                    if g:
                        pid_gid.setdefault(pid, g)
        ix = {"ver": ver, "at": now, "cc": cc, "groups": groups, "by_email": by_email, "by_phone": by_phone,
              "by_name": {k: next(iter(s)) for k, s in by_name.items() if len(s) == 1}, "slack_email": slack_email,
              "dir": d, "pid_gid": pid_gid, "mine": mine}
        with self.ix_lock:
            if self._ver == ver:
                self._ix = ix
        return ix

    def contact_of(self, source, handle, name="", ix=None):
        """The contact group id a handle belongs to, or None. Emails and numbers first; Signal's profile name or the
        phone's own name for a number only when it is a full name exactly one contact has."""
        ix = ix or self.index()
        h = str(handle or "").strip()
        if not h or h.lower() in ("me", "owner") or h.startswith("group:"):
            return None
        g = None
        if "@" in h:
            g = ix["by_email"].get(canon_email(h))
        elif source == "slack":
            g = ix["by_email"].get(ix["slack_email"].get(h.lower(), ""))
        else:
            p = norm_phone(h, ix["cc"])
            g = ix["by_phone"].get(p) if p else None
        if g is None and name and source in NAME_SOURCES:
            n = people_mod.full_name(name)
            g = ix["by_name"].get(n) if n else None
        return g

    def resolve(self, source, handle, name=""):
        """(person key, name) for a message's sender or participant: a contact ("person:c…", the contact's name),
        else a People row ("person:p…"; its contact when they share an address), else the normalised address or
        number; None when nothing is known (the caller keeps the handle)."""
        ix = self.index()
        h = str(handle or "").strip()
        if not h or h.lower() in ("me", "owner"):
            return None
        g = self.contact_of(source, h, name, ix)
        if g is not None:
            return "person:" + g, ix["groups"][g]["name"]
        d = ix["dir"]
        if d is not None:
            kind = "email" if "@" in h else {"gmail": "email", "calendar": "email", "slack": "slack",
                                             "signal": "signal"}.get(source)
            p = d.person_of(kind, h.lower()) if kind else None
            if p is None and kind == "email":
                p = d.person_of("email", canon_email(h))
            if p and p.get("id") is not None:
                g = ix["pid_gid"].get(p["id"])
                if g and g in ix["groups"]:
                    return "person:" + g, ix["groups"][g]["name"]
                return f"person:{p['id']}", p.get("name") or ""
        if "@" in h:
            e = canon_email(h)
            return (e, "") if e else None
        if source in PHONE_SOURCES:
            p = norm_phone(h, ix["cc"])
            return (p, "") if p else None
        return None

    def name_of(self, source, handle, name=""):
        """The contact's name for a handle (shown instead of a raw number or address), or ""."""
        try:
            ix = self.index()
            g = self.contact_of(source, handle, name, ix)
        except Exception:  # noqa: BLE001 (names are a nicety: the handle shows)
            return ""
        return ix["groups"][g]["name"] if g is not None and g in ix["groups"] else ""

    def rekey(self, key):
        """An older ledger key (a raw handle, "person:p…" or a full "name:…") -> its person key and name, or None."""
        k = str(key or "").strip()
        if not k or k in ("owner", "me") or k.startswith("person:c"):
            return None
        ix = self.index()
        if k.startswith("person:"):
            g = ix["pid_gid"].get(k[7:])
            return ("person:" + g, ix["groups"][g]["name"]) if g and g in ix["groups"] else None
        if k.startswith("name:"):
            n = people_mod.full_name(k[5:])
            g = ix["by_name"].get(n) if n else None
            return ("person:" + g, ix["groups"][g]["name"]) if g else None
        if "@" in k:
            got = self.resolve("gmail", k)
        elif re.fullmatch(r"u[a-z0-9]{6,}", k):
            got = self.resolve("slack", k.upper())
        elif norm_phone(k, ix["cc"]):
            got = self.resolve("sms", k)
        else:
            got = None
        return got if got and got[0] != k else None

    def relink(self, ms):
        """Ledger items (who) and follow-ups (tell) keyed by an older handle move to the person key, with a "linked"
        history line; the shown name becomes the contact's. Returns how many rows changed."""
        ver = self._ver
        rows = ms._read("SELECT id, who, who_name FROM ledger_items WHERE who NOT IN ('', 'owner') "  # noqa: SLF001
                        "AND who NOT LIKE 'person:c%'")
        fus = ms._read("SELECT id, item, tell, tell_name FROM ledger_followups WHERE tell NOT IN ('', 'owner') "  # noqa: SLF001
                       "AND tell NOT LIKE 'person:c%'")
        memo = {}

        def new(k):
            if k not in memo:
                memo[k] = self.rekey(k)
            return memo[k]
        items = [(r["id"], r["who"], r["who_name"], new(r["who"])) for r in rows]
        items = [x for x in items if x[3]]
        tells = [(r["id"], r["item"], r["tell"], r["tell_name"], new(r["tell"])) for r in fus]
        tells = [x for x in tells if x[4]]
        if not items and not tells:
            self._linked_ver = ver
            return 0
        now = self.clock()

        def run(db):
            for iid, old, old_name, (key, nm) in items:
                db.execute("UPDATE ledger_items SET who=?, who_name=? WHERE id=?", (key, nm or old_name, iid))
                db.execute("INSERT INTO ledger_history (item, at, kind, status_from, status_to, message, fields, note) "
                           "VALUES (?,?,?,?,?,?,?,?)", (iid, now, "linked", "", "", None,
                                                        json.dumps({"who": {"from": old, "to": key}}),
                                                        "same person (contacts and people)"))
            for fid, _item, _old, old_name, (key, nm) in tells:
                db.execute("UPDATE ledger_followups SET tell=?, tell_name=? WHERE id=?", (key, nm or old_name, fid))
            return len(items) + len(tells)
        n = ms.write(run)
        self._linked_ver = ver
        return n

    # --- birthdays (Coming up) ------------------------------------------------------------------------------------
    def birthdays(self, first, last):
        """[{"date", "name", "id"}] of contacts' birthdays from date first to last (yearly)."""
        out = []
        try:
            groups = self.index()["groups"]
        except Exception:  # noqa: BLE001
            return out
        for g, v in groups.items():
            if not v["birthday"] or not v["name"]:
                continue
            day = next_birthday(v["birthday"], first)
            if day is not None and first <= day <= last:
                out.append({"date": day, "name": v["name"], "id": g})
        out.sort(key=lambda x: (x["date"], x["name"]))
        return out

    # --- Setup › Connections and the Sources row --------------------------------------------------------------------
    def people_count(self):
        try:
            return len(self.index()["groups"])
        except Exception:  # noqa: BLE001
            return self.count()

    def status(self, _body=None):
        st = self._state()["accounts"]
        now = self.clock()
        out = []
        for a in self.store.accounts():
            s = st.get(a["id"]) or {}
            out.append({"account_id": a["id"], "email": a["email"], "granted": s.get("granted"),
                        "count": self.count(a["id"]), "synced_at": s.get("synced_at"),
                        "synced": ago(now - s["synced_at"]) if s.get("synced_at") else "",
                        "error": s.get("error") or ""})
        return {"scope": google.CONTACTS_SCOPE, "accounts": out, "people": self.people_count()}

    def sources_row(self, now=None):
        """The Sources panel's row: "Contacts · 412 people", "Synced 2 h ago" (None until an account allows it)."""
        now = self.clock() if now is None else now
        st = self._state()["accounts"]
        ids = {a["id"] for a in self.store.accounts()}
        granted = [s for k, s in st.items() if k in ids and s.get("granted") is True]
        if not granted:
            return None
        synced = [s.get("synced_at") for s in granted]
        errors = [s.get("error") for s in granted if s.get("error")]
        n = self.people_count()
        if all(synced):
            oldest = min(synced)
            line = "Synced " + ago(now - oldest)
            state = "behind" if now - oldest > 2 * SYNC_EVERY_S else "current"
        else:
            line, state = "Not synced yet", "starting"
        if errors:
            state = "error"
        return {"key": "contacts", "name": f"Contacts · {n:,} {'person' if n == 1 else 'people'}", "state": state,
                "word": {"current": "Current", "behind": "Behind", "starting": "Starting", "error": "Needs attention"}[state],
                "line": line, "warn": errors[0] if errors else None,
                "checked_at": min(synced) if all(synced) else None, "people": n}

    def check(self, _body=None):
        """Asks Google (one token refresh per account) which accounts allow contacts."""
        for a in self.store.accounts():
            try:
                self._token(a["id"], force=True)
            except Conflict:
                pass
        return self.status()

    def _account(self, body):
        acc_id = body.get("account_id") if isinstance(body, dict) else None
        if not isinstance(acc_id, str):
            raise InputError("Pick the account.")
        return self.store.account(acc_id)

    def grant_start(self, body):
        """The incremental grant: Google's page asks only for "See and download your contacts" on top of what was
        given before."""
        acc = self._account(body)
        return self.flows.start(acc["area_id"], extra_scopes=(google.CONTACTS_SCOPE,), login_hint=acc["email"])

    def grant_finish(self, body):
        b = body if isinstance(body, dict) else {}
        acc = self.flows.finish(b.get("flow_id"), b.get("pasted"))
        try:
            self._token(acc["id"], force=True)
        except Conflict:
            pass
        granted = self._state()["accounts"].get(acc["id"], {}).get("granted")
        self.log(f"Callsheet contacts: Allow contacts finished ({'allowed' if granted else 'not allowed'})")
        if granted:
            try:
                self.sync(acc["id"], full=True)
            except Conflict:
                pass                                   # the error shows in Setup; the reader tries again
        return {"account": acc, "granted": granted, "status": self.status()}

    def sync_now(self, body):
        """Sync now: every granted account (or the one given)."""
        b = body if isinstance(body, dict) else {}
        ids = [self._account(b)["id"]] if b.get("account_id") is not None else \
            [a["id"] for a in self.store.accounts() if self._state()["accounts"].get(a["id"], {}).get("granted")]
        if not ids:
            raise Conflict("Allow contacts for an account first (Setup › Connections).")
        errors = []
        for i in ids:
            try:
                self.sync(i)
            except Conflict as e:
                errors.append(str(e))
        out = self.status()
        out["said"] = errors[0] if errors else ""
        return out

    def routes(self):
        return [("GET", r"contacts", self.status),
                ("POST", r"contacts/check", self.check),
                ("POST", r"contacts/grant/start", self.grant_start),
                ("POST", r"contacts/grant/finish", self.grant_finish),
                ("POST", r"contacts/sync", self.sync_now)]
