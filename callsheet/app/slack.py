"""The only code in Callsheet that talks to Slack. Read-only by construction.

Every request goes through `request()`, which checks it against an allowlist of Slack Web API read methods
(GET or POST to exactly these method URLs). Anything else raises NotAllowed before any network I/O. Redirects are
not followed. The token is a user token (xoxp-...) from an internal Slack app created from MANIFEST, which asks
for read scopes only.

`Client` adds the rate-limit handling (HTTP 429: wait Retry-After, then back off; at most MAX_TRIES per request,
never past the sync deadline) and a cap on requests per sync. `read_workspace` does one sync's reads and keeps a
small state per workspace (ids and timestamps only, no message text). The rules that turn the read into "Needs a
reply" rows are in rules.py (pure).

CALLSHEET_SLACK_API_BASE (tests only) points the client at a fake Slack. Errors carry plain sentences and Slack's
short error code, never the token.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

READ_METHODS = ("auth.test", "team.info", "users.info", "users.list", "conversations.list",
                "conversations.history", "conversations.replies", "conversations.info")
SCOPES = ("channels:history", "channels:read", "groups:history", "groups:read", "im:history", "im:read",
          "mpim:history", "mpim:read", "users:read", "team:read")
MANIFEST = {
    "display_information": {
        "name": "Callsheet",
        "description": "Reads your channels and DMs for your Daily Brief. Read-only: it never posts.",
        "background_color": "#1d1d1b",
    },
    "oauth_config": {"scopes": {"user": list(SCOPES)}},
    "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
}
TIMEOUT = 15
MAX_CALLS = 120          # Slack requests per workspace per sync
MAX_TRIES = 3            # per request, on HTTP 429
MAX_WAIT = 30            # longest single wait for Retry-After (s)
WINDOW_MAX_S = 24 * 3600
OVERLAP_S = 300
MAX_DMS = 40
MAX_THREADS = 30         # conversations.replies per sync
WATCH_KEEP_S = 7 * 86400
MENTION_KEEP_S = 14 * 86400
MAX_WATCH = 20
MAX_USER_LOOKUPS = 30
MAX_PAGES = 5
RECONNECT_CODES = ("invalid_auth", "not_authed", "token_revoked", "account_inactive", "token_expired")


def api_base():
    return os.environ.get("CALLSHEET_SLACK_API_BASE", "https://slack.com/api").rstrip("/")


def manifest_json():
    return json.dumps(MANIFEST, separators=(",", ":"))


def manifest_yaml():
    """The manifest as YAML (Slack's other manifest format), written out by hand: no YAML library."""
    d, o, s = MANIFEST["display_information"], MANIFEST["oauth_config"], MANIFEST["settings"]
    lines = ["display_information:"] + [f"  {k}: {json.dumps(v)}" for k, v in d.items()]
    lines += ["oauth_config:", "  scopes:", "    user:"] + [f"      - {sc}" for sc in o["scopes"]["user"]]
    lines += ["settings:"] + [f"  {k}: {json.dumps(v)}" for k, v in s.items()]
    return "\n".join(lines) + "\n"


def create_url():
    return "https://api.slack.com/apps?new_app=1&manifest_json=" + urllib.parse.quote(manifest_json(), safe="")


class NotAllowed(Exception):
    """A request outside the allowlist. A programming error, never a user error."""


class SlackError(Exception):
    """Slack said no or couldn't be reached. .code: Slack's error code ("" for network errors)."""

    def __init__(self, message, code="", status=0):
        super().__init__(message)
        self.code = code
        self.status = status


class RateLimited(SlackError):
    def __init__(self, retry_after):
        super().__init__("Slack asked Callsheet to slow down.", "ratelimited", 429)
        self.retry_after = retry_after


class Budget(Exception):
    """The request cap or the sync deadline was reached; the rest of this workspace is skipped."""


def check_allowed(method, url):
    method = method.upper()
    parts = urllib.parse.urlsplit(url)
    plain = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if method not in ("GET", "POST") or parts.fragment:
        raise NotAllowed(f"{method} to this URL is not allowed")
    base = api_base()
    if plain.startswith(base + "/") and plain[len(base) + 1:] in READ_METHODS:
        return
    raise NotAllowed(f"{method} {parts.path} is not allowed")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _plain_error(code):
    if code in RECONNECT_CODES:
        return "Slack no longer accepts the token. Connect the workspace again."
    if code == "missing_scope":
        return "The Slack app is missing a read permission. Create it again from the manifest."
    if code in ("channel_not_found", "thread_not_found"):
        return "Slack couldn't find that conversation."
    return f"Slack said no ({str(code)[:40] or 'unknown error'})."


def request(method_name, token, params=None, http_method="GET"):
    """One allowlisted Web API request; returns the parsed JSON (ok true). Raises RateLimited on 429 and
    SlackError for ok false or network problems."""
    url = api_base() + "/" + method_name
    check_allowed(http_method, url)
    params = {k: v for k, v in (params or {}).items() if v is not None}
    data = None
    if http_method.upper() == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=http_method.upper())
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read(8_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0
            raise RateLimited(wait) from None
        raise SlackError(f"Slack answered HTTP {e.code}.", "", e.code) from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        raise SlackError("Couldn't reach Slack.") from None
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        raise SlackError("Slack sent an answer Callsheet can't read.") from None
    if not isinstance(out, dict) or not out.get("ok"):
        code = str((out or {}).get("error") or "") if isinstance(out, dict) else ""
        raise SlackError(_plain_error(code), code)
    return out


class Client:
    """Requests for one workspace in one sync: rate limits, a request cap and the sync deadline."""

    def __init__(self, token, clock=time.time, sleep=time.sleep, deadline=None, max_calls=None):
        self.token, self.clock, self.sleep = token, clock, sleep
        self.deadline = deadline
        self.max_calls = MAX_CALLS if max_calls is None else max_calls
        self.calls = 0
        self.waited = 0.0
        self.limited = 0

    def get(self, name, **params):
        backoff = 1.0
        for attempt in range(MAX_TRIES):
            if self.calls >= self.max_calls:
                raise Budget("cap")
            if self.deadline is not None and self.clock() > self.deadline:
                raise Budget("deadline")
            self.calls += 1
            try:
                return request(name, self.token, params)
            except RateLimited as e:
                self.limited += 1
                wait = min(e.retry_after if e.retry_after > 0 else backoff, MAX_WAIT)
                backoff *= 2
                if attempt == MAX_TRIES - 1 or (self.deadline is not None and self.clock() + wait > self.deadline):
                    raise Budget("ratelimited") from None
                self.waited += wait
                self.sleep(wait)
        raise Budget("ratelimited")  # not reached


# --- connect ---------------------------------------------------------------------
def domain_of(url):
    host = urllib.parse.urlsplit(url or "").hostname or ""
    return host[: -len(".slack.com")] if host.endswith(".slack.com") else ""


def validate(token):
    """auth.test, then team.info (best effort). Returns {team_id, team_name, domain, user_id, user_name}."""
    auth = request("auth.test", token)
    info = {"team_id": str(auth.get("team_id") or ""), "team_name": str(auth.get("team") or "")[:80],
            "domain": domain_of(auth.get("url")), "user_id": str(auth.get("user_id") or ""),
            "user_name": str(auth.get("user") or "")[:80]}
    if not info["team_id"] or not info["user_id"]:
        raise SlackError("Slack didn't say which workspace this token belongs to.")
    try:
        team = request("team.info", token).get("team") or {}
        info["team_name"] = str(team.get("name") or info["team_name"])[:80]
        info["domain"] = str(team.get("domain") or info["domain"])[:80]
    except SlackError:
        pass
    return info


# --- reading ---------------------------------------------------------------------
def list_conversations(cl):
    """Every conversation the owner is in: [{id, kind: channel|private|dm|group, name, user, rank}]."""
    out, cursor = [], None
    for _ in range(MAX_PAGES):
        res = cl.get("conversations.list", types="public_channel,private_channel,mpim,im", exclude_archived="true",
                     limit=200, cursor=cursor)
        for c in res.get("channels") or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            if c.get("is_im"):
                if c.get("is_user_deleted") or c.get("user") in (None, "USLACKBOT"):
                    continue
                kind, name = "dm", ""
            elif c.get("is_mpim"):
                kind, name = "group", str(c.get("name") or "")
            else:
                if c.get("is_member") is False:
                    continue
                kind = "private" if c.get("is_private") or c.get("is_group") else "channel"
                name = str(c.get("name") or c["id"])
            rank = (float(c.get("priority") or 0), float(c.get("updated") or 0), float(c.get("created") or 0))
            out.append({"id": str(c["id"]), "kind": kind, "name": name[:80], "user": c.get("user"), "rank": rank})
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def channel_list(convs):
    return sorted(({"id": c["id"], "name": c["name"], "kind": c["kind"]} for c in convs
                   if c["kind"] in ("channel", "private")), key=lambda c: c["name"].lower())


def is_real(m):
    """A message a person wrote (not a bot, join/leave or other system message)."""
    if not isinstance(m, dict) or m.get("bot_id") or not m.get("user") or not m.get("ts"):
        return False
    return m.get("subtype") in (None, "thread_broadcast", "file_share", "me_message")


def _ts(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def mentions(text, uid):
    return isinstance(text, str) and f"<@{uid}" in text


def _history(cl, channel, **kw):
    msgs = cl.get("conversations.history", channel=channel, **kw).get("messages") or []
    return sorted((m for m in msgs if isinstance(m, dict)), key=lambda m: _ts(m.get("ts")))


def read_workspace(cl, ws, now_ts, log=None):
    """One sync's reads for a workspace. ws: the stored workspace with token, user_id, ignored and state.
    Returns (data, state, problems): data for rules.slack_rows, the new saved state, and plain sentences for
    anything skipped. Raises SlackError when the very first request fails."""
    me = ws["user_id"]
    state = dict(ws.get("state") or {})
    ignored = set(ws.get("ignored") or [])
    problems = []
    data = {"me": me, "domain": ws.get("domain") or "", "convs": {}, "dm": {}, "chan": {}, "threads": {},
            "users": dict(state.get("users") or {}), "owner_last": dict(state.get("owner_last") or {}),
            "window_start": 0.0}
    last = _ts(state.get("last_read_at"))
    window = max(now_ts - WINDOW_MAX_S, last - OVERLAP_S) if last else now_ts - WINDOW_MAX_S
    data["window_start"] = window
    watch = {tuple(w[:2]): _ts(w[2]) for w in state.get("watch") or [] if isinstance(w, list) and len(w) == 3}
    pending = [tuple(p) for p in state.get("mentions") or [] if isinstance(p, list) and len(p) == 3]
    convs = list_conversations(cl)  # a failure here is a failure of the whole workspace
    state["channels"] = channel_list(convs)
    readable = [c for c in convs if c["id"] not in ignored]
    data["convs"] = {c["id"]: {"kind": c["kind"], "name": c["name"], "user": c.get("user")} for c in readable}
    skipped = 0
    try:
        dms = sorted((c for c in readable if c["kind"] in ("dm", "group")), key=lambda c: c["rank"], reverse=True)
        skipped += max(0, len(dms) - MAX_DMS)
        for c in dms[:MAX_DMS]:
            msgs = _history(cl, c["id"], limit=10)
            data["dm"][c["id"]] = msgs
            for m in msgs:
                _note_threads(m, c["id"], me, watch, now_ts)
        to_fetch = {}
        for c in (c for c in readable if c["kind"] in ("channel", "private")):
            # the last 24 h, so replies to older parents show up in their reply counts; only messages since the
            # last read count as new (heads up, mentions)
            msgs = _history(cl, c["id"], oldest=f"{now_ts - WINDOW_MAX_S:.6f}", limit=200)
            data["chan"][c["id"]] = msgs
            for m in msgs:
                if not is_real(m):
                    continue
                if m["user"] == me:
                    data["owner_last"][c["id"]] = max(_ts(data["owner_last"].get(c["id"])), _ts(m["ts"]))
                elif mentions(m.get("text"), me) and _ts(m["ts"]) >= window:
                    root = m.get("thread_ts") or m["ts"]
                    p = (c["id"], m["ts"], m.get("thread_ts") or "")
                    if p not in pending:
                        pending.append(p)
                    if m.get("reply_count"):
                        to_fetch[(c["id"], root)] = _ts(m.get("latest_reply") or m["ts"])
                _note_threads(m, c["id"], me, watch, now_ts)
                if m.get("reply_count") and _ts(m.get("latest_reply")) >= window:
                    to_fetch.setdefault((c["id"], m["ts"]), _ts(m.get("latest_reply")))
        # threads the owner posted in, then threads of pending mentions, then other recently active threads
        live = {k: v for k, v in watch.items() if k[0] in data["convs"] and now_ts - v <= WATCH_KEEP_S}
        order = sorted(live, key=lambda k: live[k], reverse=True)[:MAX_WATCH]
        in_window = {(ch, m.get("ts")) for ch, msgs in data["chan"].items() for m in msgs}
        order += [(ch, th or ts) for ch, ts, th in pending
                  if ch in data["convs"] and (th or (ch, ts) not in in_window)]
        order += sorted(to_fetch, key=lambda k: to_fetch[k], reverse=True)
        done = 0
        for key in dict.fromkeys(order):
            if done >= MAX_THREADS:
                skipped += 1
                continue
            ch, root = key
            if (ch, root) in data["threads"]:
                continue
            try:
                msgs = cl.get("conversations.replies", channel=ch, ts=root, limit=200).get("messages") or []
            except SlackError as e:
                if e.code in ("thread_not_found", "channel_not_found", "message_not_found"):
                    watch.pop(key, None)
                    pending = [p for p in pending if (p[0], p[2] or p[1]) != key]
                    continue
                raise
            done += 1
            msgs = sorted((m for m in msgs if isinstance(m, dict)), key=lambda m: _ts(m.get("ts")))
            data["threads"][key] = msgs
            for m in msgs:
                _note_threads(m, ch, me, watch, now_ts)
                if is_real(m) and m["user"] != me and mentions(m.get("text"), me) and _ts(m["ts"]) >= window \
                        and m.get("thread_ts") and (ch, m["ts"], m["thread_ts"]) not in pending:
                    pending.append((ch, m["ts"], m["thread_ts"]))
                if is_real(m) and m["user"] == me and m.get("thread_ts") is None:
                    data["owner_last"][ch] = max(_ts(data["owner_last"].get(ch)), _ts(m["ts"]))
        # names (and emails, when Slack gives them) of the people in rows
        want = set()
        for c in readable:
            if c["kind"] == "dm" and c.get("user"):
                want.add(c["user"])
        for msgs in list(data["dm"].values()) + list(data["chan"].values()) + list(data["threads"].values()):
            for m in msgs[-10:]:
                if is_real(m):
                    want.add(m["user"])
                for uid in re.findall(r"<@([A-Z0-9]+)", m.get("text") or ""):
                    want.add(uid)
        lookups = 0
        for uid in sorted(want - set(data["users"])):
            if lookups >= MAX_USER_LOOKUPS:
                break
            lookups += 1
            try:
                u = cl.get("users.info", user=uid).get("user") or {}
            except SlackError:
                continue
            prof = u.get("profile") or {}
            data["users"][uid] = {"name": str(prof.get("display_name") or prof.get("real_name") or u.get("real_name")
                                              or u.get("name") or uid)[:80],
                                  "real_name": str(prof.get("real_name") or u.get("real_name") or "")[:80],
                                  "email": str(prof.get("email") or "").lower()[:254]}
    except Budget as b:
        reason = {"cap": f"Slack reading stopped after {cl.max_calls} requests",
                  "deadline": "Slack reading stopped at the time limit",
                  "ratelimited": "Slack asked Callsheet to slow down"}.get(str(b), "Slack reading stopped")
        problems.append(reason + ", so some conversations were skipped. They are read at the next check-in.")
    else:
        state["last_read_at"] = now_ts
    if skipped and not problems:
        problems.append(f"{skipped} older Slack conversation{'s were' if skipped != 1 else ' was'} not read this time.")
    # keep: pending mentions not answered yet (14 days), watched threads (7 days), names of up to 500 people
    keep = []
    for ch, ts, th in pending:
        if now_ts - _ts(ts) > MENTION_KEEP_S:
            continue
        msgs = data["threads"].get((ch, th or ts))
        if msgs is not None and any(is_real(m) and m["user"] == me and _ts(m["ts"]) > _ts(ts) for m in msgs):
            continue  # answered in the thread
        if not th and _ts(data["owner_last"].get(ch)) > _ts(ts):
            continue  # answered later in the channel
        keep.append([ch, ts, th])
    state["mentions"] = keep[-100:]
    state["watch"] = [[k[0], k[1], v] for k, v in sorted(watch.items(), key=lambda kv: kv[1], reverse=True)
                      if now_ts - v <= WATCH_KEEP_S][:MAX_WATCH * 2]
    state["owner_last"] = data["owner_last"]
    state["users"] = dict(list(data["users"].items())[-500:])
    data["mentions"] = [tuple(p) for p in keep]
    if log:
        log(f"Callsheet Slack: {len(data['dm'])} DMs, {len(data['chan'])} channels, {len(data['threads'])} threads, "
            f"{cl.calls} requests, {cl.limited} rate-limited, waited {cl.waited:.0f}s")
    return data, state, problems


def _note_threads(m, channel, me, watch, now_ts):
    """Remember a thread the owner started or replied in (by its root ts, with its latest activity)."""
    if not is_real(m) and not m.get("reply_count"):
        return
    root = m.get("thread_ts")
    mine = m.get("user") == me or me in (m.get("reply_users") or [])
    if root and mine:
        latest = max(_ts(m.get("latest_reply")), _ts(m.get("ts")))
        watch[(channel, root)] = max(watch.get((channel, root), 0.0), latest)
    elif m.get("reply_count") and mine:
        watch[(channel, m["ts"])] = max(watch.get((channel, m["ts"]), 0.0), _ts(m.get("latest_reply") or m["ts"]))
