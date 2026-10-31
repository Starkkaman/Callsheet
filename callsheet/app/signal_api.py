"""The api/signal routes (0.5.1): link, status, settings, unlink, and the history import.

Kept out of api.py so the Signal feature stays in one place; api.App adds `SignalApi.routes()` to its table.

History upload: POST api/signal/import/start {name, size, chat?} -> {id}; then the file's bytes in order,
POST api/signal/import/<id>/chunk (raw body, at most 8 MB each; server.py lets only this route take raw
bytes); then POST api/signal/import/<id>/finish. The parsed text waits in the store's staging table until the
owner picks chats (POST api/signal/import/commit {chats}) or discards it (DELETE api/signal/import).
"""
import secrets
import threading
import time

import rules
import signal_import
import signalcli
from store import Conflict, InputError, NotFound

UPLOAD_ID = r"up_[0-9a-f]{8}"
MAX_CHUNK = 8 * 1024 * 1024
UPLOAD_IDLE_S = 600  # an upload with no chunk for this long is forgotten


def _obj(body):
    if not isinstance(body, dict):
        raise InputError("Send a JSON object.")
    return body


class SignalApi:
    def __init__(self, store, data_dir, clock=time.time, service=None):
        self.store, self.clock = store, clock
        self.service = service or signalcli.SignalService(store, data_dir, clock=clock)
        self._up = None
        self._last = None  # counts of the last finished upload
        self._lock = threading.Lock()

    def routes(self):
        return [
            ("GET", r"signal", self.status),
            ("PATCH", r"signal", self.patch),
            ("POST", r"signal/link", self.link),
            ("POST", r"signal/link/cancel", self.cancel_link),
            ("POST", r"signal/unlink", self.unlink),
            ("GET", r"signal/import", self.import_status),
            ("DELETE", r"signal/import", self.import_discard),
            ("POST", r"signal/import/start", self.import_start),
            ("POST", rf"signal/import/({UPLOAD_ID})/chunk", self.import_chunk),
            ("POST", rf"signal/import/({UPLOAD_ID})/finish", self.import_finish),
            ("POST", r"signal/import/commit", self.import_commit),
            ("POST", r"signal/history/clear", self.clear_history),
        ]

    # --- link and status ------------------------------------------------------
    def status(self, _body=None):
        st = self.service.status()
        st["chats"] = self.store.signal_chat_list()
        st["counts"] = self.store.signal_counts()
        st["keep_days"] = 30
        return st

    def link(self, _body):
        try:
            self.service.start_link()
        except signalcli.LinkError as e:
            raise Conflict(str(e)) from None
        return self.status()

    def cancel_link(self, _body):
        self.service.cancel_link()
        return self.status()

    def unlink(self, body):
        if _obj(body).get("confirm") is not True:
            raise InputError("Confirm unlinking first.")
        self.service.unlink()
        return self.status()

    def patch(self, body):
        b = _obj(body)
        for k in b:
            if k not in ("area_id", "ignored"):
                raise InputError(f"Signal settings can't change {str(k)[:40]}.")
        self.store.signal_settings(b)
        return self.status()

    # --- history import ---------------------------------------------------------
    def _current(self, uid):
        up = self._up
        if up is None or up["id"] != uid:
            raise NotFound("That upload isn't running any more. Choose the file again.")
        return up

    def import_status(self, _body=None):
        up = self._up
        if up is not None and self.clock() - up["touched"] > UPLOAD_IDLE_S:
            self._up = up = None
        staged = self.store.signal_stage_summary()
        return {"uploading": None if up is None else {k: up[k] for k in ("id", "name", "size", "received")},
                "staged": staged, "max_bytes": signal_import.MAX_UPLOAD,
                "last": self._last}

    def import_start(self, body):
        b = _obj(body)
        name, size = str(b.get("name") or "")[:200], b.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise InputError("Choose a file to upload.")
        if size > signal_import.MAX_UPLOAD:
            raise InputError("That file is larger than 200 MB. Export fewer chats, or upload each chat's "
                             "data.json on its own.")
        if not name.lower().endswith((".jsonl", ".json", ".txt")):
            raise InputError("Choose Signal's main.jsonl, or a chat's data.json.")
        chat = b.get("chat")
        if chat is not None and not isinstance(chat, str):
            raise InputError("The chat name must be text.")
        with self._lock:  # a new upload replaces one that is still running (one owner, one page)
            tz = rules.tzinfo(self.store.settings())
            parser = signal_import.Parser(self.store.signal_hash, tz, chat or name.rsplit(".", 1)[0])
            self._up = {"id": "up_" + secrets.token_hex(4), "name": name, "size": size, "received": 0,
                        "parser": parser, "touched": self.clock()}
            return {"id": self._up["id"], "max_chunk": MAX_CHUNK}

    def _stage(self, up, recs):
        if not recs:
            return
        if not up.get("cleared"):
            # a new main.jsonl replaces what is waiting; a data.json replaces only its own chat
            if up["parser"].format == "backup":
                self.store.signal_stage_clear()
            else:
                self.store.signal_stage_clear(recs[0]["chat"])
            up["cleared"] = True
        self.store.signal_stage_add(recs)

    def import_chunk(self, body, uid):
        with self._lock:
            up = self._current(uid)
            if not isinstance(body, (bytes, bytearray)):
                raise InputError("Send the file's bytes.")
            if len(body) > MAX_CHUNK or up["received"] + len(body) > min(up["size"], signal_import.MAX_UPLOAD):
                self._up = None
                raise InputError("The upload is larger than the file said. Choose the file again.")
            up["received"] += len(body)
            up["touched"] = self.clock()
            self._stage(up, up["parser"].feed(bytes(body)))
            if up["parser"].format is None and up["parser"].lines >= 20:
                self._up = None
                self.store.signal_stage_clear()
                raise InputError("That file isn't a Signal export. Choose main.jsonl or a chat's data.json.")
            return {"received": up["received"], "lines": up["parser"].lines}

    def import_finish(self, _body, uid):
        with self._lock:
            up = self._current(uid)
            p = up["parser"]
            self._stage(up, p.finish())
            self._up = None
            if p.format is None:
                raise InputError("That file isn't a Signal export. Choose main.jsonl or a chat's data.json.")
            if p.format == "export" and p.kept:
                self.store.signal_stage_kind("x" + self.store.signal_hash("x:" + p.chat_title), p.export_kind())
            self._last = {"name": up["name"], "format": p.format, "lines": p.lines, "kept": p.kept, "bad": p.bad,
                          "skipped": p.skipped}
        return self.import_status()

    def import_commit(self, body):
        added = self.store.signal_stage_commit(_obj(body).get("chats"), self.clock())
        return dict(self.import_status(), added=added, counts=self.store.signal_counts())

    def import_discard(self, _body=None):
        with self._lock:
            self._up = None
            self.store.signal_stage_clear()
            self._last = None
        return self.import_status()

    def clear_history(self, body):
        if _obj(body).get("confirm") is not True:
            raise InputError("Confirm deleting the imported history first.")
        self.store.signal_clear_imported()
        return self.status()
