"""Signal (0.5.1): signal-cli as a linked device, read-only.

signal-cli (installed in the image from Alpine's community repository) runs as one child process speaking
JSON-RPC on its stdin/stdout (`jsonRpc`, no socket, no port). Its data lives in `<data>/signal-cli` (0700; left
out of Home Assistant backups, see config.yaml). Only the methods in ALLOWED_METHODS can be called: `Rpc.call`
refuses anything else before a byte is written, so nothing can send a message, a receipt, a typing note or
change the profile from here. (signal-cli itself still answers Signal's protocol: delivery receipts and
session/retry messages, like any linked device; see PRIVACY.md.)

Linking: startLink gives a `sgnl://linkdevice?...` address, shown as a QR code (qr.py); finishLink waits for the
phone to scan it. Receiving: subscribeReceive; each message becomes one row in the store's Signal tables (text,
sender name, an HMAC of the sender and chat ids, times, incoming or sent from the phone). The process is watched:
when it stops it is started again with a growing pause. Logs carry counts and plain states only.
"""
import collections
import datetime as dt
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

import qr

# The only signal-cli JSON-RPC methods Callsheet ever calls. Everything else (send, sendReceipt, sendTyping,
# updateProfile, ...) raises NotAllowed before anything is written to the process.
ALLOWED_METHODS = ("startLink", "finishLink", "subscribeReceive", "unsubscribeReceive", "listAccounts",
                   "listContacts", "listGroups")
DEVICE_NAME = "Callsheet"
DIR_NAME = "signal-cli"
JAVA_OPTS = "-Xmx256m -XX:+UseSerialGC"
CALL_TIMEOUT_S = 60
LINK_WAIT_S = 300          # how long a link code waits for the phone
BACKOFF_S = (5, 10, 30, 60, 120, 300, 600)
STABLE_S = 600             # a run this long resets the pause
MAX_LINE = 8 * 1024 * 1024
MAX_TEXT = 8000
MAX_LOG_LINES = 20         # signal-cli stderr lines logged per start (scrubbed, clipped)
LINK_PREFIX = "sgnl://linkdevice?"
OBJ = "￼"             # Signal's placeholder character for a mention in a message body


class NotAllowed(Exception):
    """A signal-cli method outside ALLOWED_METHODS."""


class RpcError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class RpcClosed(RpcError):
    pass


class RpcTimeout(RpcError):
    pass


class LinkError(Exception):
    """Linking can't start (plain sentence, HTTP 409/400)."""


def _stderr(line):
    sys.stderr.write(line + "\n")


def check_method(method):
    if method not in ALLOWED_METHODS:
        raise NotAllowed(f"Callsheet never calls signal-cli's {str(method)[:40]} method.")


def default_binary():
    """The signal-cli command: env CALLSHEET_SIGNAL_CLI (tests and development) or signal-cli on PATH."""
    return os.environ.get("CALLSHEET_SIGNAL_CLI") or shutil.which("signal-cli")


def command(binary, data_dir):
    """The full command line: global options, then jsonRpc with manual receiving and no downloads."""
    prefix = list(binary) if isinstance(binary, (list, tuple)) else [binary]
    return prefix + ["--config", str(data_dir), "--trust-new-identities=always", "--disable-send-log", "--scrub-log",
                     "jsonRpc", "--receive-mode=manual", "--ignore-attachments", "--ignore-stories",
                     "--ignore-avatars", "--ignore-stickers"]


_SCRUB = re.compile(r"\+?\d[\d ()-]{4,}\d|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def scrub(line):
    """A signal-cli log line without phone numbers or ids (signal-cli's --scrub-log does this too)."""
    return _SCRUB.sub("…", line)[:200]


def mask_number(number):
    """'+15551234567' -> '+•••••••••67' (only the last two digits)."""
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) < 4:
        return None
    return "+" + "•" * (len(digits) - 2) + digits[-2:]


class Rpc:
    """JSON-RPC 2.0 over a running signal-cli's stdin/stdout. Responses are matched by id; `receive`
    notifications go to on_receive(params) on the reader thread."""

    def __init__(self, proc, on_receive, log=_stderr):
        self.proc, self.on_receive, self.log = proc, on_receive, log
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self.closed = threading.Event()
        self.auth_failed = False
        self.stderr_tail = collections.deque(maxlen=10)
        self._logged = 0
        threading.Thread(target=self._read_out, name="callsheet-signal-out", daemon=True).start()
        threading.Thread(target=self._read_err, name="callsheet-signal-err", daemon=True).start()

    def call(self, method, params=None, timeout=CALL_TIMEOUT_S):
        check_method(method)  # before any I/O
        if self.closed.is_set():
            raise RpcClosed("signal-cli isn't running.")
        rid = next(self._ids)
        waiter = {"event": threading.Event(), "msg": None}
        with self._lock:
            self._pending[rid] = waiter
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            req["params"] = params
        try:
            with self._wlock:
                self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            with self._lock:
                self._pending.pop(rid, None)
            raise RpcClosed("signal-cli isn't running.") from None
        if not waiter["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise RpcTimeout(f"signal-cli didn't answer {method} in time.")
        msg = waiter["msg"]
        if msg is None:
            raise RpcClosed("signal-cli stopped.")
        if "error" in msg:
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            raise RpcError(str(err.get("message") or "signal-cli refused the request.")[:300], err.get("code"))
        return msg.get("result")

    def _resolve(self, rid, msg):
        with self._lock:
            waiter = self._pending.pop(rid, None)
        if waiter:
            waiter["msg"] = msg
            waiter["event"].set()

    def _read_out(self):
        skipping = False
        try:
            while True:
                raw = self.proc.stdout.readline(MAX_LINE)
                if not raw:
                    break
                if skipping or (len(raw) >= MAX_LINE and not raw.endswith(b"\n")):
                    skipping = not raw.endswith(b"\n")  # an oversized line: dropped
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" not in msg and "id" in msg:
                    self._resolve(msg.get("id"), msg)
                elif msg.get("method") == "receive" and isinstance(msg.get("params"), dict):
                    try:
                        self.on_receive(msg["params"])
                    except Exception as e:  # noqa: BLE001 (one bad message never stops the reader)
                        self.log(f"Callsheet Signal: a message was skipped ({type(e).__name__}).")
        except (OSError, ValueError):
            pass
        finally:
            self.closed.set()
            with self._lock:
                rids = list(self._pending)
            for rid in rids:
                self._resolve(rid, None)

    def _read_err(self):
        try:
            for raw in iter(lambda: self.proc.stderr.readline(65536), b""):
                line = scrub(raw.decode("utf-8", "replace").strip())
                if not line:
                    continue
                self.stderr_tail.append(line)
                if re.search(r"authoriz|unauthori[sz]ed|\b40[13]\b|deviceremoved|not registered", line, re.I):
                    self.auth_failed = True
                if self._logged < MAX_LOG_LINES:
                    self._logged += 1
                    self.log("Callsheet Signal: signal-cli: " + line)
        except (OSError, ValueError):
            pass


# --- received messages ------------------------------------------------------------
def _utf16_replace(text, spans):
    """Replaces [start, start+length) ranges given in UTF-16 code units (as Java counts them) with new strings.
    spans: [(start, length, new)]."""
    units = text.encode("utf-16-le")
    for start, length, new in sorted(spans, key=lambda s: s[0], reverse=True):
        if not (isinstance(start, int) and isinstance(length, int)) or start < 0 or length < 0 \
                or (start + length) * 2 > len(units):
            continue
        units = units[: start * 2] + new.encode("utf-16-le") + units[(start + length) * 2:]
    return units.decode("utf-16-le", "replace")


def _is_me(me, uuid=None, number=None):
    return bool((uuid and me.get("aci") and uuid == me["aci"]) or (number and me.get("number") and number == me["number"]))


def _body(dm, me):
    """The message text with mention placeholders as @names; (text, mentions_me)."""
    text = dm.get("message")
    text = text if isinstance(text, str) else ""
    spans, mine = [], False
    for m in dm.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        if _is_me(me, m.get("uuid"), m.get("number")):
            mine = True
            name = me.get("name") or "you"
        else:
            name = m.get("name") if isinstance(m.get("name"), str) and not str(m.get("name")).startswith("+") else ""
            name = name or "someone"
        spans.append((m.get("start"), m.get("length"), "@" + name))
    if spans:
        text = _utf16_replace(text, spans)
    return text.replace(OBJ, "")[:MAX_TEXT], mine


def from_envelope(env, me, hasher, now):
    """One signal-cli envelope -> None, or an action for the store:
    ("add", record) | ("edit", record) | ("delete", {chat, sender, ts}).
    record = {chat, kind ("dm"|"group"), title (None = keep the stored one), sender, sender_name, ts (seconds),
    text, outgoing, mentions_me, quotes_me, expires_at}. Ids are hashes (hasher), never numbers or uuids.
    me: {"number", "aci", "name"} of the linked account (memory only)."""
    if not isinstance(env, dict):
        return None
    src = env.get("sourceUuid") or env.get("sourceNumber") or env.get("source")
    src_name = env.get("sourceName") if isinstance(env.get("sourceName"), str) else ""
    if isinstance(env.get("dataMessage"), dict):
        if _is_me(me, env.get("sourceUuid"), env.get("sourceNumber")):
            return None  # our own message seen from another device arrives as a sync message instead
        return _from_data(env["dataMessage"], src, src_name, False, src, src_name, me, hasher, now)
    if isinstance(env.get("editMessage"), dict):
        e = env["editMessage"]
        if isinstance(e.get("dataMessage"), dict):
            act = _from_data(e["dataMessage"], src, src_name, False, src, src_name, me, hasher, now)
            return _as_edit(act, e.get("targetSentTimestamp"))
        return None
    sync = env.get("syncMessage")
    sent = sync.get("sentMessage") if isinstance(sync, dict) else None
    if isinstance(sent, dict):
        peer = sent.get("destinationUuid") or sent.get("destinationNumber") or sent.get("destination")
        if isinstance(sent.get("editMessage"), dict) and isinstance(sent["editMessage"].get("dataMessage"), dict):
            e = sent["editMessage"]
            act = _from_data(e["dataMessage"], "me", "", True, peer, None, me, hasher, now)
            return _as_edit(act, e.get("targetSentTimestamp"))
        return _from_data(sent, "me", "", True, peer, None, me, hasher, now)
    return None  # receipts, typing, calls, stories and other sync messages are not stored


def _as_edit(act, target):
    if not act or act[0] != "add" or not isinstance(target, (int, float)) or target <= 0:
        return None
    return ("edit", dict(act[1], ts=target / 1000.0))


def _from_data(dm, sender, sender_name, outgoing, peer, peer_name, me, hasher, now):
    gi = dm.get("groupInfo") if isinstance(dm.get("groupInfo"), dict) else None
    if gi and gi.get("groupId"):
        chat, kind, title = "g" + hasher("g:" + str(gi["groupId"])), "group", gi.get("groupName") or None
    elif peer:
        chat, kind, title = "d" + hasher("p:" + str(peer)), "dm", (peer_name or None)
    else:
        return None
    sid = "me" if outgoing else "s" + hasher("p:" + str(sender))
    rd = dm.get("remoteDelete")
    if isinstance(rd, dict) and isinstance(rd.get("timestamp"), (int, float)):
        return ("delete", {"chat": chat, "sender": sid, "ts": rd["timestamp"] / 1000.0})
    if dm.get("viewOnce") or dm.get("isExpirationUpdate") or dm.get("isEndSession") \
            or (gi and gi.get("type") == "UPDATE" and not dm.get("message")):
        return None
    if not isinstance(dm.get("message"), str) or not dm["message"].strip():
        if not (dm.get("attachments") or dm.get("sticker")):
            return None  # reactions, polls, profile key updates, ...: nothing to read
    ts = dm.get("timestamp")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    text, mentions_me = _body(dm, me)
    q = dm.get("quote") if isinstance(dm.get("quote"), dict) else {}
    quotes_me = not outgoing and _is_me(me, q.get("authorUuid"), q.get("authorNumber"))
    exp = dm.get("expiresInSeconds")
    return ("add", {"chat": chat, "kind": kind, "title": title, "sender": sid,
                    "sender_name": "" if outgoing else (sender_name or ""), "ts": ts / 1000.0, "text": text,
                    "outgoing": bool(outgoing), "mentions_me": bool(mentions_me and not outgoing),
                    "quotes_me": bool(quotes_me),
                    "expires_at": now + exp if isinstance(exp, (int, float)) and exp > 0 else None})


# --- the service --------------------------------------------------------------------
class SignalService:
    """Owns the signal-cli process: linking, receiving into the store, the watchdog, unlinking."""

    def __init__(self, store, data_dir, clock=time.time, log=_stderr, binary=None):
        self.store, self.clock, self.log = store, clock, log
        self.dir = pathlib.Path(data_dir) / DIR_NAME
        self.binary = binary if binary is not None else default_binary()
        self.backoff = BACKOFF_S
        self.link_wait = LINK_WAIT_S
        self._lock = threading.RLock()
        self._proc = None
        self._rpc = None
        self._link = {"state": "idle"}
        self._link_seq = 0
        self._me = {"number": None, "aci": None}
        self._stop = threading.Event()
        self._watch = None
        self._error = None
        self._received = 0
        self._last_prune = 0.0

    # --- process ---------------------------------------------------------
    def available(self):
        b = self.binary
        if not b:
            return False
        first = b[0] if isinstance(b, (list, tuple)) else b
        return bool(first) and (os.path.exists(first) or shutil.which(first) is not None)

    def _spawn(self):
        """The running Rpc, starting signal-cli when needed. Caller holds self._lock."""
        if self._rpc is not None and not self._rpc.closed.is_set():
            return self._rpc
        self._kill()
        if not self.available():
            raise LinkError("signal-cli isn't installed in this Callsheet image.")
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        env = dict(os.environ)
        env.setdefault("JAVA_OPTS", JAVA_OPTS)
        self._proc = subprocess.Popen(command(self.binary, self.dir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env, cwd=str(self.dir), start_new_session=True)
        self._rpc = Rpc(self._proc, self._on_receive, self.log)
        return self._rpc

    def _kill(self):
        """Stops signal-cli: end of input first (it exits cleanly), then terminate, then kill."""
        with self._lock:
            proc, self._proc, self._rpc = self._proc, None, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        for step in (None, "terminate", "kill"):
            if step:
                try:
                    getattr(proc, step)()
                except OSError:
                    pass
            try:
                proc.wait(timeout=10 if step is None else 5)
                break
            except subprocess.TimeoutExpired:
                continue
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def linked(self):
        return bool(self.store.signal_meta().get("linked"))

    def start(self):
        """At app start: watch signal-cli when Signal is linked."""
        if self.linked():
            self._start_watch()

    def _start_watch(self):
        with self._lock:
            if self._watch is not None and self._watch.is_alive():
                return
            self._stop.clear()
            self._watch = threading.Thread(target=self._watchdog, name="callsheet-signal", daemon=True)
            self._watch.start()

    def _connect(self):
        """Starts signal-cli if needed, reads the linked account, subscribes to new messages."""
        with self._lock:
            rpc = self._spawn()
        accounts = rpc.call("listAccounts")
        acc = accounts[0] if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict) else None
        if acc is None:
            self.store.signal_update_meta(linked=False)
            raise LinkError("Signal isn't linked any more. Link it again.")
        self._me = {"number": acc.get("number"), "aci": acc.get("aci")}
        rpc.call("subscribeReceive")
        return rpc

    def _watchdog(self):
        tries = 0
        while not self._stop.is_set() and self.linked():
            started = self.clock()
            rpc = None
            try:
                rpc = self._connect()
                self._error = None
            except LinkError as e:
                self._error = str(e)
                self.log(f"Callsheet Signal: {e}")
                if not self.linked():
                    break
            except (RpcError, OSError) as e:
                self._error = "signal-cli didn't start. Callsheet tries again soon."
                self.log(f"Callsheet Signal: signal-cli didn't start ({type(e).__name__}).")
            while rpc is not None and not self._stop.is_set() and not rpc.closed.wait(1):
                if rpc.auth_failed:
                    self._error = "Signal doesn't accept this link any more. Unlink, then link again."
            if self._stop.is_set() or not self.linked():
                break
            if rpc is not None and rpc.auth_failed:
                self._error = "Signal doesn't accept this link any more. Unlink, then link again."
            if self.clock() - started >= STABLE_S:
                tries = 0
            delay = self.backoff[min(tries, len(self.backoff) - 1)]
            tries += 1
            self.log(f"Callsheet Signal: signal-cli stopped; starting it again in {delay} s.")
            self._kill()
            self._stop.wait(delay)

    def close(self):
        self._stop.set()
        w = self._watch
        if w is not None and w is not threading.current_thread():
            w.join(timeout=15)
        self._kill()

    # --- linking -----------------------------------------------------------
    def start_link(self):
        """A new link code (QR) while not linked; finishLink waits for the phone on its own thread."""
        with self._lock:
            if self.linked():
                raise LinkError("Signal is already linked. Unlink it first to link another phone.")
            if self._link.get("state") == "waiting" and self.clock() - self._link.get("started", 0) < self.link_wait - 30:
                return self.status()
            rpc = self._spawn()
        try:
            res = rpc.call("startLink")
        except RpcError as e:
            self._kill()
            raise LinkError("signal-cli couldn't make a link code. Try again in a minute.") from e
        uri = res.get("deviceLinkUri") if isinstance(res, dict) else None
        if not isinstance(uri, str) or not uri.startswith(LINK_PREFIX) or len(uri) > 2000:
            self._kill()
            raise LinkError("signal-cli gave an unexpected link code. Try again.")
        with self._lock:
            self._link_seq += 1
            seq = self._link_seq
            self._link = {"state": "waiting", "qr_svg": qr.svg(uri), "started": self.clock(), "error": None}
        threading.Thread(target=self._finish, args=(rpc, uri, seq), name="callsheet-signal-link", daemon=True).start()
        return self.status()

    def _finish(self, rpc, uri, seq):
        try:
            res = rpc.call("finishLink", {"deviceLinkUri": uri, "deviceName": DEVICE_NAME}, timeout=self.link_wait)
        except RpcTimeout:
            self._link_failed(seq, "expired", "The code expired. Press Link Signal for a new one.")
            return
        except RpcError as e:
            msg = "Linking didn't finish. Press Link Signal to try again."
            if re.search(r"already|exists", str(e), re.I):
                msg = "This phone's Signal account is already set up in Callsheet. Unlink first, then link again."
            self._link_failed(seq, "failed", msg)
            return
        res = res if isinstance(res, dict) else {}
        with self._lock:
            if seq != self._link_seq:
                return
            now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="seconds")
            self.store.signal_update_meta(linked=True, number=mask_number(res.get("number")), linked_at=now)
            self._me = {"number": res.get("number"), "aci": res.get("aci")}
            self._link = {"state": "linked"}
        self.log("Callsheet Signal: linked.")
        try:
            rpc.call("subscribeReceive")
        except RpcError:
            pass  # the watchdog starts it again
        self._start_watch()

    def _link_failed(self, seq, state, message):
        with self._lock:
            if seq != self._link_seq:
                return
            self._link = {"state": state, "error": message}
        if not self.linked():
            self._kill()

    def cancel_link(self):
        with self._lock:
            self._link_seq += 1
            self._link = {"state": "idle"}
        if not self.linked():
            self._kill()
        return self.status()

    def unlink(self):
        """Stops signal-cli, deletes <data>/signal-cli and the messages received live (imported history stays)."""
        self._stop.set()
        w = self._watch
        if w is not None and w is not threading.current_thread():
            w.join(timeout=15)
        self._kill()
        with self._lock:
            self._link_seq += 1
            self._link = {"state": "idle"}
            self._me = {"number": None, "aci": None}
            self._error = None
            if self.dir.name == DIR_NAME and self.dir.exists():
                shutil.rmtree(self.dir, ignore_errors=True)
            self.store.signal_unlink()
            self._stop.clear()
        self.log("Callsheet Signal: unlinked; its data was deleted.")
        return self.status()

    # --- receiving ---------------------------------------------------------
    def _on_receive(self, params):
        res = params.get("result") if isinstance(params.get("result"), dict) else params
        me = dict(self._me, name=(self.store.settings().get("name") or "").strip() or None)
        now = self.clock()
        act = from_envelope(res.get("envelope"), me, self.store.signal_hash, now)
        if act is None:
            return
        kind, rec = act
        if kind == "add":
            if self.store.signal_add(rec, now):
                self._received += 1
        elif kind == "edit":
            self.store.signal_edit(rec)
        elif kind == "delete":
            self.store.signal_delete(rec["chat"], rec["sender"], rec["ts"])
        if now - self._last_prune > 3600:
            self._last_prune = now
            self.store.signal_prune(now)

    # --- status --------------------------------------------------------------
    def status(self):
        meta = self.store.signal_meta()
        with self._lock:
            link = dict(self._link)
            running = self._rpc is not None and not self._rpc.closed.is_set()
        if link.get("state") == "waiting":
            link["expires_in"] = max(0, int(self.link_wait - (self.clock() - link.pop("started", self.clock()))))
        else:
            link.pop("started", None)
            link.pop("qr_svg", None)
        return {"available": self.available(), "linked": bool(meta.get("linked")), "number": meta.get("number"),
                "linked_at": meta.get("linked_at"), "last_message_at": meta.get("last_message_at"),
                "running": running, "error": self._error if meta.get("linked") else None, "link": link,
                "area_id": meta.get("area_id"), "ignored": list(meta.get("ignored") or []),
                "device_name": DEVICE_NAME}
