"""0.21.0 voice assistant API: its settings, per-device tokens, rate limit and counts-only audit log (docs/voice-api.md).

Approved 2026-11-13 (docs/voice-api.md: a read-only API for the owner's phone voice assistant). Tables in callsheet.db
(additive, CREATE TABLE IF NOT EXISTS):
- voice_tokens: one row per device. Only sha256(per-install salt + token) is kept; the token itself is shown once,
  when it is made, and never again. Revoked tokens stay listed (revoked time) so the audit log keeps its names.
- voice_audit: time, token id and name, endpoint template ("/v1/threads/{id}"), status and result count. Never the
  query string, a search word, an id from the path or any message text. Rows older than 90 days are removed.
The settings (on/off, extra host names) and the salt are in the meta table. Off by default.
"""
import collections
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time

SCOPES = ("messages:read", "ledger:read", "calendar:read", "people:read", "brief:read", "status:read")
# 0.24.3 (owner approved 2026-11-18, "Yes, those three"): dismiss, snooze and mark done (voiceactions.py). Never a
# default: only the per-device checkbox in Setup (or scopes at creation) grants it.
WRITE_SCOPES = ("actions:write",)
ALL_SCOPES = SCOPES + WRITE_SCOPES
TOKEN_PREFIX = "csva_"
MAX_TOKENS = 20                 # active (not revoked) tokens
NAME_MAX = 40
RATE_N, RATE_S = 60, 60         # 60 requests a minute per token
AUDIT_DAYS = 90
AUDIT_SHOW = 50
LAST_USED_EVERY_S = 60          # last-used time written at most once a minute per token
MAX_HOSTS = 10
DEFAULT_PORT = 8100
META = "voice_api"
SALT = "voice_api_salt"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_tokens (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '[]', hash TEXT NOT NULL,
  created REAL NOT NULL, last_used REAL, revoked REAL);
CREATE TABLE IF NOT EXISTS voice_audit (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, token TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
  endpoint TEXT NOT NULL, status INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS voice_audit_at ON voice_audit (at);
"""

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
# Names a home network or Tailscale gives (no public domain): MagicDNS, mDNS and the private-use suffixes
LOCAL_SUFFIXES = (".ts.net", ".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")


class Invalid(ValueError):
    """A Setup input the owner can fix (shown as it is)."""


def clean_name(v):
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        raise Invalid("Give the device a name, like “Kitchen speaker”.")
    if len(s) > NAME_MAX:
        raise Invalid(f"Keep the name under {NAME_MAX} characters.")
    return s


def clean_host(v):
    h = str(v or "").strip().lower().rstrip(".")
    if not h or len(h) > 253 or not _HOST.match(h):
        raise Invalid(f"“{str(v)[:60]}” isn't a host name.")
    return h


def split_host(header):
    """The Host header without its port, lower-case; IPv6 literals without brackets. None when malformed."""
    h = str(header or "").strip().lower()
    if not h or len(h) > 300:
        return None
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return None
        return h[1:end]
    if h.count(":") == 1:
        h, port = h.split(":")
        if not port.isdigit():
            return None
    return h.rstrip(".")


def host_allowed(header, extra=()):
    """The Host header check (no DNS rebinding through a public name): an IP address, localhost, a one-word name
    ("nas", "homeassistant"), a home-network or Tailscale name (LOCAL_SUFFIXES), or one the owner added in Setup."""
    h = split_host(header)
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if not _HOST.match(h):
        return False
    if h == "localhost" or "." not in h or h.endswith(LOCAL_SUFFIXES):
        return True
    return h in set(extra or ())


def client_allowed(addr):
    """Only the home network, Tailscale and this machine: loopback, private (RFC 1918, IPv6 ULA, which holds
    Tailscale's fd7a:115c:a1e0::/48), link-local and Tailscale's 100.64.0.0/10. Never a public address."""
    try:
        ip = ipaddress.ip_address(str(addr or ""))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in TAILSCALE_V4:
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local


class RateLimit:
    """A sliding window per token: at most n requests in s seconds. In memory (a restart clears it)."""

    def __init__(self, n=RATE_N, s=RATE_S, clock=time.time):
        self.n, self.s, self.clock = n, s, clock
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def take(self, key):
        """True when this request may go ahead; else the seconds until the next one may (an int >= 1)."""
        now = self.clock()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.s:
                q.popleft()
            if len(q) >= self.n:
                return max(1, int(q[0] + self.s - now + 0.999))
            q.append(now)
            return True


class Tokens:
    """The settings, tokens and audit log, over Store's connection and lock."""

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.rate = RateLimit(clock=clock)
        self._pruned = 0.0
        with store.lock:
            store.db.executescript(_SCHEMA)

    # settings --------------------------------------------------------------------------------------------------------
    def settings(self):
        s = self.store._get_meta(META, None) or {}  # noqa: SLF001
        return {"enabled": s.get("enabled") is True, "hosts": [h for h in s.get("hosts") or [] if isinstance(h, str)]}

    def update(self, body):
        if not isinstance(body, dict) or set(body) - {"enabled", "hosts"}:
            raise Invalid("Only on/off and host names can be changed here.")
        s = self.settings()
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise Invalid("On or off.")
            s["enabled"] = body["enabled"]
        if "hosts" in body:
            v = body["hosts"]
            if isinstance(v, str):
                v = [x for x in re.split(r"[\s,]+", v) if x]
            if not isinstance(v, list) or len(v) > MAX_HOSTS:
                raise Invalid(f"Up to {MAX_HOSTS} host names, separated by commas.")
            s["hosts"] = list(dict.fromkeys(clean_host(x) for x in v))
        self.store._set_meta(META, s)  # noqa: SLF001
        return s

    def _salt(self):
        with self.store.lock:
            salt = self.store._get_meta(SALT, None)  # noqa: SLF001
            if not salt:
                salt = secrets.token_hex(16)
                self.store._set_meta(SALT, salt)  # noqa: SLF001
        return salt

    def _hash(self, token):
        return hashlib.sha256((self._salt() + ":" + str(token)).encode("utf-8")).hexdigest()

    # tokens ----------------------------------------------------------------------------------------------------------
    def _rows(self, active=False):
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM voice_tokens" + (" WHERE revoked IS NULL" if active else "") +
                                         " ORDER BY created").fetchall()
        return [dict(r, scopes=json.loads(r["scopes"] or "[]")) for r in rows]

    @staticmethod
    def public(r):
        return {"id": r["id"], "name": r["name"], "scopes": r["scopes"], "created": r["created"],
                "last_used": r["last_used"], "revoked": r["revoked"]}

    def list(self):
        return [self.public(r) for r in self._rows()]

    def create(self, name, scopes=None):
        """A new token: (its public row, the token text). The text is returned only here, once."""
        name = clean_name(name)
        if scopes is None:
            scopes = list(SCOPES)
        if not isinstance(scopes, list) or any(s not in ALL_SCOPES for s in scopes) or \
                not any(s in SCOPES for s in scopes):
            raise Invalid("Pick at least one of the read permissions.")
        scopes = [s for s in ALL_SCOPES if s in scopes]
        if len(self._rows(active=True)) >= MAX_TOKENS:
            raise Invalid(f"There are already {MAX_TOKENS} devices. Revoke one first.")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        row = {"id": "vt_" + secrets.token_hex(6), "name": name, "scopes": scopes, "created": self.clock(),
               "last_used": None, "revoked": None}
        with self.store.lock:
            self.store.db.execute("INSERT INTO voice_tokens (id, name, scopes, hash, created) VALUES (?,?,?,?,?)",
                                  (row["id"], name, json.dumps(scopes), self._hash(token), row["created"]))
        return row, token

    def revoke(self, tid):
        with self.store.lock:
            n = self.store.db.execute("UPDATE voice_tokens SET revoked=? WHERE id=? AND revoked IS NULL",
                                      (self.clock(), str(tid))).rowcount
        if not n:
            raise LookupError("There's no such device, or it was revoked already.")
        with self.rate.lock:
            self.rate.hits.pop(str(tid), None)

    def set_actions(self, tid, on):
        """0.24.3: grant or take back actions:write for one device (Setup's checkbox)."""
        row = next((r for r in self._rows(active=True) if r["id"] == str(tid)), None)
        if row is None:
            raise LookupError("There's no such device, or it was revoked.")
        scopes = [s for s in ALL_SCOPES if s in row["scopes"] and s not in WRITE_SCOPES] + (list(WRITE_SCOPES) if on
                                                                                             else [])
        with self.store.lock:
            self.store.db.execute("UPDATE voice_tokens SET scopes=? WHERE id=? AND revoked IS NULL",
                                  (json.dumps(scopes), row["id"]))
        return scopes

    def check(self, header):
        """The Authorization header -> the token's row, or None. Every active token's hash is compared in constant time
        (hmac.compare_digest), with no early exit, so the time taken doesn't depend on which one matched."""
        m = re.fullmatch(r"Bearer ([A-Za-z0-9_\-]{10,200})", str(header or "").strip())
        if not m:
            return None
        want = self._hash(m.group(1)).encode()
        hit = None
        for r in self._rows(active=True):
            if hmac.compare_digest(r["hash"].encode(), want):
                hit = r
        if hit is not None:
            now = self.clock()
            if not hit["last_used"] or now - hit["last_used"] >= LAST_USED_EVERY_S:
                with self.store.lock:
                    self.store.db.execute("UPDATE voice_tokens SET last_used=? WHERE id=?", (now, hit["id"]))
        return hit

    # the audit log: counts only --------------------------------------------------------------------------------------
    def audit(self, token, endpoint, status, count=0):
        """token: the row or None. endpoint: the route template, never the path or the query."""
        now = self.clock()
        with self.store.lock:
            self.store.db.execute("INSERT INTO voice_audit (at, token, name, endpoint, status, count) VALUES "
                                  "(?,?,?,?,?,?)", (now, (token or {}).get("id", ""), (token or {}).get("name", ""),
                                                    str(endpoint)[:60], int(status), max(0, int(count or 0))))
            if now - self._pruned > 3600:
                self._pruned = now
                self.store.db.execute("DELETE FROM voice_audit WHERE at < ?", (now - AUDIT_DAYS * 86400,))

    def audit_rows(self, limit=AUDIT_SHOW):
        with self.store.lock:
            rows = self.store.db.execute("SELECT at, name, endpoint, status, count FROM voice_audit ORDER BY id DESC "
                                         "LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
