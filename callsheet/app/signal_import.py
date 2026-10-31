"""Signal history import (0.5.1): a streamed parser for two export formats.

1. Signal Desktop's plain-text export, `main.jsonl` (Signal's backup format as JSON lines: a header line, then
   one frame per line: account, recipient, chat, chatItem, ...; numbers as strings, bytes as base64).
2. signal-export's per-chat `data.json` (JSON lines {date, sender, body, quote, ...}; "Me" is the owner).

Bytes are fed in chunks (no whole file in memory); each complete line is parsed and dropped. Only text, names
and times are kept. Every key and secret in the file (mediaRootBackupKey, profileKey, masterKey, identityKey,
attachment keys, ...) is skipped, and phone numbers are never kept (a contact without a name shows as a masked
number). Chat and sender ids are the store's salted hashes, as for messages received live, so a direct chat
imported here and the same chat received live share an id.
"""
import base64
import datetime as dt
import json
import uuid

import signalcli

MAX_LINE = 4 * 1024 * 1024
MAX_UPLOAD = 200 * 1024 * 1024
BACKUP_KEYS = ("account", "recipient", "chat", "chatItem", "stickerPack", "adHocCall", "notificationProfile",
               "chatFolder")


def _aci(b64):
    """A base64 ACI (16 bytes) from the backup file as the uuid string signal-cli uses, or None."""
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return None
    return str(uuid.UUID(bytes=raw)) if len(raw) == 16 else None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _join(*parts):
    return " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


def _contact_name(c):
    """The name the owner sees in Signal: their nickname, the phone's contact name, the profile name, the
    username; else a masked number."""
    nick = c.get("nickname") if isinstance(c.get("nickname"), dict) else {}
    for name in (_join(nick.get("given"), nick.get("family")), _join(c.get("systemNickname")),
                 _join(c.get("systemGivenName"), c.get("systemFamilyName")),
                 _join(c.get("profileGivenName"), c.get("profileFamilyName")), _join(c.get("username"))):
        if name:
            return name[:120]
    e164 = _int(c.get("e164"))
    return signalcli.mask_number(str(e164)) if e164 else "Someone"


class Parser:
    """feed(bytes) -> records, finish() -> records. A record is the store's staging row: {chat, kind, title,
    sender, sender_name, ts, text, outgoing, mentions_me, quotes_me}. Counters: lines, bad (malformed or too
    long lines), skipped (not text, unknown chat, disappearing). `chat_title` names the chat of a
    signal-export data.json (its folder name)."""

    def __init__(self, hasher, tz, chat_title=None):
        self.hash, self.tz = hasher, tz or dt.timezone.utc
        self.chat_title = (chat_title or "").strip()[:120] or "Signal chat"
        self.format = None
        self.lines = self.bad = self.skipped = self.kept = 0
        self._buf = bytearray()
        self._skipping = False
        self._first = True
        self._recipients = {}   # recipient id -> {"kind", "name", "key", "aci"}
        self._chats = {}        # chat id -> recipient id
        self._acis = {}         # aci -> name (to show @mentions)
        self._self = set()
        self._my_name = None
        self._senders = set()   # signal-export: the other people's names seen

    # --- bytes -> lines ----------------------------------------------------
    def feed(self, data):
        out = []
        start = 0
        while True:
            nl = data.find(b"\n", start)
            if nl < 0:
                rest = data[start:]
                if not self._skipping:
                    self._buf += rest
                    if len(self._buf) > MAX_LINE:
                        self._buf.clear()
                        self._skipping = True
                        self.bad += 1
                break
            if self._skipping:
                self._skipping = False
            else:
                self._buf += data[start:nl]
                if len(self._buf) > MAX_LINE:
                    self.bad += 1
                else:
                    self._line(bytes(self._buf), out)
                self._buf.clear()
            start = nl + 1
        return out

    def finish(self):
        out = []
        if self._buf and not self._skipping:
            self._line(bytes(self._buf), out)
        self._buf.clear()
        self._skipping = False
        return out

    def export_kind(self):
        """signal-export: a chat with more than one other person is a group."""
        return "group" if len(self._senders) > 1 else "dm"

    def _line(self, raw, out):
        if self._first:
            self._first = False
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
        if not raw.strip():
            return
        self.lines += 1
        try:
            obj = json.loads(raw)
        except ValueError:
            self.bad += 1
            return
        if not isinstance(obj, dict):
            self.bad += 1
            return
        if self.format is None:
            if "backupTimeMs" in obj or any(k in obj for k in BACKUP_KEYS):
                self.format = "backup"
            elif "date" in obj and "sender" in obj:
                self.format = "export"
            else:
                self.bad += 1
                return
        try:
            rec = self._backup(obj) if self.format == "backup" else self._export(obj)
        except (TypeError, ValueError, KeyError, AttributeError):
            self.bad += 1
            return
        if rec:
            self.kept += 1
            out.append(rec)

    # --- Signal Desktop main.jsonl ------------------------------------------------
    def _backup(self, obj):
        if "backupTimeMs" in obj:
            return None  # the header: version, time and the media key (dropped)
        if isinstance(obj.get("account"), dict):
            a = obj["account"]
            self._my_name = _join(a.get("givenName"), a.get("familyName")) or None
            return None
        if isinstance(obj.get("recipient"), dict):
            self._recipient(obj["recipient"])
            return None
        if isinstance(obj.get("chat"), dict):
            c = obj["chat"]
            if c.get("id") is not None and c.get("recipientId") is not None:
                self._chats[str(c["id"])] = str(c["recipientId"])
            return None
        if isinstance(obj.get("chatItem"), dict):
            return self._item(obj["chatItem"])
        return None  # sticker packs, calls, folders, ...: not kept

    def _recipient(self, r):
        rid = r.get("id")
        if rid is None:
            return
        rid = str(rid)
        if isinstance(r.get("self"), dict):
            self._self.add(rid)
            self._recipients[rid] = {"kind": "self"}
        elif isinstance(r.get("contact"), dict):
            c = r["contact"]
            aci = _aci(c.get("aci")) if c.get("aci") else None
            e164 = _int(c.get("e164"))
            key = aci or (f"+{e164}" if e164 else None) or ("r:" + rid)
            name = _contact_name(c)
            self._recipients[rid] = {"kind": "contact", "name": name, "key": key}
            if aci:
                self._acis[aci] = name
        elif isinstance(r.get("group"), dict):
            g = r["group"]
            snap = g.get("snapshot") if isinstance(g.get("snapshot"), dict) else {}
            t = snap.get("title")
            title = t.get("title") if isinstance(t, dict) else t if isinstance(t, str) else ""
            # the id is a salted hash; the group's master key itself is never kept
            key = "gk:" + str(g.get("masterKey") or ("r:" + rid))
            self._recipients[rid] = {"kind": "group", "name": (title or "").strip()[:120] or "Signal group",
                                     "key": key}
        else:
            self._recipients[rid] = {"kind": "other"}  # release notes, stories, call links

    def _item(self, it):
        chat_rid = self._chats.get(str(it.get("chatId")))
        rcp = self._recipients.get(chat_rid) if chat_rid else None
        sm = it.get("standardMessage")
        if rcp is None or rcp["kind"] not in ("contact", "group") or not isinstance(sm, dict):
            self.skipped += 1
            return None
        if _int(it.get("expiresInMs")):
            self.skipped += 1  # disappearing messages are never imported
            return None
        text_obj = sm.get("text") if isinstance(sm.get("text"), dict) else {}
        body = text_obj.get("body") if isinstance(text_obj.get("body"), str) else ""
        ts = _int(it.get("dateSent"))
        if not body.strip() or not ts or ts <= 0:
            self.skipped += 1
            return None
        author = self._recipients.get(str(it.get("authorId"))) or {}
        outgoing = isinstance(it.get("outgoing"), dict) or author.get("kind") == "self"
        spans, mentions_me = [], False
        for br in text_obj.get("bodyRanges") or []:
            if not isinstance(br, dict) or not br.get("mentionAci"):
                continue
            aci = _aci(br["mentionAci"])
            name = self._acis.get(aci) if aci else None
            if name is None:  # every contact is in the file, so an unknown mention is the owner
                mentions_me, name = True, self._my_name or "you"
            spans.append((_int(br.get("start")) or 0, _int(br.get("length")) or 0, "@" + name))
        if spans:
            body = signalcli._utf16_replace(body, spans)
        body = body.replace(signalcli.OBJ, "")[:signalcli.MAX_TEXT]
        quote = sm.get("quote") if isinstance(sm.get("quote"), dict) else {}
        quotes_me = not outgoing and str(quote.get("authorId")) in self._self
        if rcp["kind"] == "group":
            chat, kind = "gi" + self.hash(rcp["key"]), "group"
        else:
            chat, kind = "d" + self.hash("p:" + rcp["key"]), "dm"
        if outgoing:
            sender, sender_name = "me", ""
        elif author.get("kind") == "contact":
            sender, sender_name = "s" + self.hash("p:" + author["key"]), author["name"]
        else:
            self.skipped += 1
            return None
        return {"chat": chat, "kind": kind, "title": rcp["name"], "sender": sender, "sender_name": sender_name,
                "ts": ts / 1000.0, "text": body, "outgoing": outgoing, "mentions_me": mentions_me and not outgoing,
                "quotes_me": quotes_me}

    # --- signal-export data.json ---------------------------------------------------
    def _when(self, value):
        d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=self.tz)  # signal-export writes the computer's local time
        return d.timestamp()

    def _export(self, obj):
        body = obj.get("body")
        sender = obj.get("sender")
        if not isinstance(body, str) or not body.strip() or not isinstance(sender, str) or not sender.strip():
            self.skipped += 1
            return None
        ts = self._when(obj.get("date"))
        outgoing = sender.strip() == "Me"
        if not outgoing:
            self._senders.add(sender.strip())
        name = sender.strip()[:120]
        return {"chat": "x" + self.hash("x:" + self.chat_title), "kind": "dm", "title": self.chat_title,
                "sender": "me" if outgoing else "s" + self.hash("n:" + name), "sender_name": "" if outgoing else name,
                "ts": ts, "text": body[:signalcli.MAX_TEXT], "outgoing": outgoing, "mentions_me": False,
                "quotes_me": False}
