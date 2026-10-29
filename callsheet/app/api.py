"""The JSON API behind the page. The HTTP layer (server.py) parses requests and calls `App.handle`;
everything here works on plain dicts. Secrets never leave this module: responses use the public
views from store.py (client ID hint, account objects without tokens).
"""
import datetime as dt
import json
import pathlib
import re
import sys
import time

import writer

import google
import rules
from oauth import OAuthFlows
from scheduler import Scheduler
from store import InputError, NotFound, Store
from sync import Syncer

HERE = pathlib.Path(__file__).resolve().parent
SAMPLE = HERE / "sample_brief.json"


class MethodNotAllowed(Exception):
    pass


def load_sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _obj(body):
    if not isinstance(body, dict):
        raise InputError("Send a JSON object.")
    return body


class App:
    def __init__(self, data_dir, clock=time.time):
        self.clock = clock
        self.store = Store(data_dir)
        self._apply_options(data_dir)
        self.flows = OAuthFlows(self.store, clock=clock)
        self.syncer = Syncer(self.store, clock=clock)
        self.scheduler = Scheduler(self.store, self.syncer, clock=clock)
        self.routes = [
            ("GET", r"brief", self.brief),
            ("GET", r"state", self.state),
            ("POST", r"google-client", self.set_client),
            ("DELETE", r"google-client", self.delete_client),
            ("POST", r"areas", self.add_area),
            ("PATCH", r"areas/([\w-]+)", self.patch_area),
            ("DELETE", r"areas/([\w-]+)", self.delete_area),
            ("POST", r"accounts/start", self.start_account),
            ("POST", r"accounts/finish", self.finish_account),
            ("PATCH", r"accounts/([\w-]+)", self.patch_account),
            ("DELETE", r"accounts/([\w-]+)", self.delete_account),
            ("POST", r"vips", self.add_vip),
            ("DELETE", r"vips/([\w-]+)", self.delete_vip),
            ("PATCH", r"settings", self.patch_settings),
            ("POST", r"sync", self.sync),
            ("PATCH", r"writer", self.patch_writer),
            ("DELETE", r"writer/claude-key", self.delete_claude_key),
            ("POST", r"writer/test", self.test_writer),
        ]

    def _apply_options(self, data_dir):
        """Home Assistant writes the app options to <data>/options.json. writer_local_url and
        writer_local_model seed the local writer until the owner changes the writer in Callsheet."""
        try:
            opts = json.loads((pathlib.Path(data_dir) / "options.json").read_text())
        except (OSError, ValueError):
            return
        if not isinstance(opts, dict):
            return
        try:
            if self.store.seed_local_writer(opts.get("writer_local_url"), opts.get("writer_local_model")):
                print("Callsheet writer: local model set from the app options.", file=sys.stderr, flush=True)
        except InputError as e:
            print(f"Callsheet writer: app options not used: {e}", file=sys.stderr, flush=True)

    def close(self):
        self.scheduler.stop()
        self.store.close()

    def handle(self, method, path, body):
        """path: the part after "api/" without a trailing slash. Raises NotFound, MethodNotAllowed,
        InputError. Returns a JSON-able value."""
        known = False
        for m, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            known = True
            if m == method:
                return fn(body, *match.groups())
        if known:
            raise MethodNotAllowed()
        raise NotFound("There's nothing here.")

    # --- reads ------------------------------------------------------------
    def brief(self, _body=None):
        s = self.store.settings()
        now = dt.datetime.fromtimestamp(self.clock(), rules.tzinfo(s))
        return rules.compose_brief(load_sample(), s, self.store.areas(), self.store.cached_brief(),
                                   self.store.has_accounts(), now,
                                   writer_on=self.store.writer_config()["provider"] != "off")

    def state(self, _body=None):
        client = self.store.google_client_public()
        accounts = self.store.accounts()
        areas = []
        for a in self.store.areas():
            a = dict(a)
            a["accounts"] = [acc for acc in accounts if acc["area_id"] == a["id"]]
            areas.append(a)
        return {"google_client": client, "areas": areas, "vips": self.store.vips(),
                "settings": self.store.settings(), "last_sync": self.store.last_sync(),
                "writer": self.store.writer_public(), "last_writer": self.store.last_writer(),
                "setup_done": self.store.setup_done() or (client["configured"] and bool(accounts))}

    # --- Google client ----------------------------------------------------
    def set_client(self, body):
        b = _obj(body)
        self.store.set_google_client(b.get("client_id"), b.get("client_secret"))
        return self.store.google_client_public()

    def delete_client(self, _body):
        self.store.clear_google_client()
        return self.store.google_client_public()

    # --- areas ------------------------------------------------------------
    def _area_out(self, area):
        return dict(area, accounts=self.store.accounts(area["id"]))

    def add_area(self, body):
        b = _obj(body)
        return self._area_out(self.store.add_area(b.get("name"), b.get("colour")))

    def patch_area(self, body, area_id):
        return self._area_out(self.store.update_area(area_id, _obj(body)))

    def delete_area(self, _body, area_id):
        for acc in self.store.delete_area(area_id):
            google.revoke(acc.get("refresh_token") or acc.get("access_token"))
        return {"ok": True}

    # --- accounts ---------------------------------------------------------
    def start_account(self, body):
        return self.flows.start(_obj(body).get("area_id"))

    def finish_account(self, body):
        b = _obj(body)
        return self.flows.finish(b.get("flow_id"), b.get("pasted"))

    def patch_account(self, body, account_id):
        return self.store.update_account(account_id, _obj(body))

    def delete_account(self, _body, account_id):
        acc = self.store.delete_account(account_id)
        google.revoke(acc.get("refresh_token") or acc.get("access_token"))
        return {"ok": True}

    # --- VIPs, settings, sync ---------------------------------------------
    def add_vip(self, body):
        b = _obj(body)
        return self.store.add_vip(b.get("name"), b.get("email"), b.get("area_id"))

    def delete_vip(self, _body, vip_id):
        self.store.delete_vip(vip_id)
        return {"ok": True}

    def patch_settings(self, body):
        return self.store.update_settings(_obj(body))

    def sync(self, _body):
        if not self.store.has_accounts():
            raise InputError("Connect a Google account first.")
        return self.syncer.run_now()

    # --- AI writer --------------------------------------------------------
    def patch_writer(self, body):
        return self.store.update_writer(_obj(body))

    def delete_claude_key(self, _body):
        return self.store.clear_claude_key()

    def test_writer(self, body):
        provider = _obj(body).get("provider")
        w = self.store.writer_config()
        try:
            if provider == "claude":
                message = writer.claude_test(w["claude"]["api_key"], w["claude"]["model"])
            elif provider == "local":
                message = writer.local_test(w["local"]["base_url"], w["local"]["api_key"])
            else:
                raise InputError("Test claude or local.")
        except writer.WriterError as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": message}
