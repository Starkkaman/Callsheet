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

import dates
import evaluate
import memory
import writer

import google
import rules
import slack
from oauth import OAuthFlows
from scheduler import Scheduler
from store import Conflict, InputError, NotFound, Store
from sync import Syncer

HERE = pathlib.Path(__file__).resolve().parent
SAMPLE = HERE / "sample_brief.json"
# The app's version, the same as `version` in callsheet/config.yaml (the image only holds app/, so it is
# kept here; tests/test_server.py fails if the two differ). Sent in api/state and put into index.html.
APP_VERSION = "0.4.1"


class MethodNotAllowed(Exception):
    pass


class UpstreamError(RuntimeError):
    """A service Callsheet reads (Slack) couldn't be reached (HTTP 502). The message is a plain sentence."""


class Download:
    """A file response (not JSON): server.py sends it as an attachment."""

    def __init__(self, body, content_type, filename):
        self.body, self.content_type, self.filename = body, content_type, filename


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
        self.evaluator = evaluate.Evaluator(self.store, self.syncer, clock=clock)
        self.memory = self.syncer.memory  # memory.md (0.5.0): one object, one lock
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
            ("GET", r"writer", self.get_writer),
            ("PATCH", r"writer", self.patch_writer),
            ("POST", r"writer/profiles", self.add_profile),
            ("PATCH", r"writer/profiles/([\w-]+)", self.patch_profile),
            ("DELETE", r"writer/profiles/([\w-]+)", self.delete_profile),
            ("GET", r"slack/manifest", self.slack_manifest),
            ("POST", r"slack/connect", self.slack_connect),
            ("GET", r"slack/([\w-]+)/channels", self.slack_channels),
            ("PATCH", r"slack/([\w-]+)", self.patch_slack),
            ("DELETE", r"slack/([\w-]+)", self.delete_slack),
            ("POST", r"suppress", self.add_suppress),
            ("DELETE", r"suppress/([\w-]+)", self.delete_suppress),
            ("POST", r"done", self.set_done),
            ("DELETE", r"writer/claude-key", self.delete_claude_key),
            ("POST", r"writer/test", self.test_writer),
            ("PATCH", r"eval/settings", self.patch_eval),
            ("GET", r"eval/status", self.eval_status),
            ("GET", r"eval/models", self.eval_models),
            ("POST", r"eval/contenders", self.add_contender),
            ("PATCH", rf"eval/contenders/({evaluate.CONTENDER_ID})", self.patch_contender),
            ("DELETE", rf"eval/contenders/({evaluate.CONTENDER_ID})", self.delete_contender),
            ("POST", r"eval/run", self.eval_run),
            ("GET", rf"eval/({evaluate.RUN_ID})", self.eval_view),
            ("POST", rf"eval/({evaluate.RUN_ID})/choices", self.eval_choices),
            ("GET", rf"eval/({evaluate.RUN_ID})/export", self.eval_export),
        ] + self._memory_routes()

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
        cache = self.store.cached_brief()
        brief = rules.compose_brief(load_sample(), s, self.store.areas(), cache,
                                    self.store.has_sources(), now,
                                    writer_on=self.store.writer_config()["provider"] != "off")
        self._brief_050(brief, cache, s)
        return brief

    # --- 0.5.0: possible events and notes on the brief, memory (memory.py) -------------------------------------
    def _brief_050(self, brief, cache, settings):
        """Adds the possible events (dates.for_page: display time in the owner's format, the Add to calendar
        URL) and the note marks ({ref: {id, text}} for the notes made from a brief item) to a live brief."""
        if brief.get("sample"):  # the bundled sample stays as it is (no notes, no possible events)
            return
        day = dt.date.fromisoformat(brief["date"]) if brief.get("date") else None
        brief["possible"] = dates.for_page((cache or {}).get("possible"), str(rules.tzinfo(settings)),
                                           rules.time_format(settings), day) if cache and day else []
        marks = {}
        for e in self.memory.entries():
            ref, nid = e["meta"].get("ref"), e["meta"].get("id")
            if ref and nid and e["section"] == "Notes" and ref not in marks:
                marks[ref] = {"id": nid, "text": e["text"]}
        brief["notes"] = marks

    def _memory_routes(self):
        return [("GET", r"memory", self.get_memory),
                ("PATCH", r"memory", self.save_memory),
                ("POST", r"memory/reset", self.reset_memory),
                ("POST", r"memory/notes", self.add_note),
                ("PATCH", r"memory/notes/(n[0-9a-f]{6,12})", self.patch_note),
                ("DELETE", r"memory/notes/(n[0-9a-f]{6,12})", self.delete_note)]

    def _mem(self, fn, *args):
        try:
            return fn(*args)
        except memory.MemoryError_ as e:
            raise InputError(str(e)) from None
        except LookupError as e:
            raise NotFound(str(e)) from None

    def get_memory(self, _body=None):
        return dict(self._mem(self.memory.view), rule=writer.NOTES_RULE, cap=memory.NOTES_CAP)

    def save_memory(self, body):
        b = _obj(body)
        if set(b) != {"text"}:
            raise InputError("Send the memory as {\"text\": ...}.")
        return dict(self._mem(self.memory.save, b["text"]), rule=writer.NOTES_RULE, cap=memory.NOTES_CAP)

    def reset_memory(self, body):
        if _obj(body).get("confirm") is not True:
            raise InputError("Confirm clearing the memory.")
        return dict(self._mem(self.memory.reset), rule=writer.NOTES_RULE, cap=memory.NOTES_CAP)

    def add_note(self, body):
        """A note on a brief item: kind reply | event | para | heads | pe, id (the row, event, possible event or
        area id; for heads the text's hash on the page, with text in "item"), ref (the page's key for the item, so
        the item shows its note), text. Metadata comes from the cached brief and row refs, never from the page."""
        b = _obj(body)
        kind, item_id, ref = b.get("kind"), b.get("id"), b.get("ref")
        if kind not in ("reply", "event", "para", "heads", "pe"):
            raise InputError("Notes go on a reply, an event, a briefing paragraph, a heads-up or a possible event.")
        if not isinstance(ref, str) or not re.fullmatch(r"[\w:.-]{1,80}", ref):
            raise InputError("That item can't take a note. Reload the page.")
        s = self.store.settings()
        now = dt.datetime.fromtimestamp(self.clock(), rules.tzinfo(s))
        cache = self.store.cached_brief() or {}
        if not self.store.has_sources() or not cache:
            raise InputError("Notes work once Callsheet has read your accounts.")
        area_name = {a["id"]: a["name"] for a in self.store.areas()}
        sender = domain = None
        if kind in ("reply", "pe"):
            pe = None
            if kind == "pe":
                pe = next((p for p in cache.get("possible") or [] if p.get("id") == item_id), None)
                if pe is None:
                    raise NotFound("That possible event isn't in the brief any more. Reload the page.")
            rid = pe["row"] if pe else item_id
            row = next((r for r in (cache.get("replies") or []) + (cache.get("done") or []) if r.get("id") == rid),
                       None)
            ref_row = self.store.brief_refs().get(rid) if isinstance(rid, str) else None
            if pe is None and (row is None or ref_row is None):
                raise NotFound("That row isn't in the brief any more. Reload the page.")
            item = dict(row or {}, **({"who": pe["who"], "text": (row or {}).get("text") or pe["title"]} if pe else {}))
            if ref_row and ref_row.get("source") == "mail":
                sender, domain = ref_row.get("sender"), ref_row.get("domain")
            area = (row or pe).get("area")
        elif kind == "event":
            item = next((e for e in cache.get("schedule") or [] if e.get("id") == item_id), None)
            if item is None:
                raise NotFound("That event isn't in the brief any more. Reload the page.")
            area = item.get("area")
        elif kind == "para":
            if item_id not in area_name:
                raise NotFound("That area doesn't exist any more. Reload the page.")
            item, area = None, item_id
        else:
            item = next((h for h in cache.get("heads_up") or [] if h == b.get("item")), None)
            if item is None:
                raise NotFound("That heads-up isn't in the brief any more. Reload the page.")
            area = None
        title, meta = memory.meta_for(kind, item, ref, now, area_name.get(area, ""), sender, domain)
        return self._mem(self.memory.add, title, meta, b.get("text"))

    def patch_note(self, body, nid):
        return self._mem(self.memory.update, nid, _obj(body).get("text"))

    def delete_note(self, _body, nid):
        return self._mem(self.memory.delete, nid)

    def state(self, _body=None):
        client = self.store.google_client_public()
        accounts = self.store.accounts()
        workspaces = self.store.slack_workspaces()
        areas = []
        for a in self.store.areas():
            a = dict(a)
            a["accounts"] = [acc for acc in accounts if acc["area_id"] == a["id"]]
            a["slack"] = [w for w in workspaces if w["area_id"] == a["id"]]
            a["chat_available"] = any(w["status"] != "needs_reconnect" for w in a["slack"])
            areas.append(a)
        return {"google_client": client, "areas": areas, "vips": self.store.vips(),
                "suppress": self.store.suppress_rules(),
                "settings": self.store.settings(), "last_sync": self.store.last_sync(),
                "writer": self.store.writer_public(), "last_writer": self.store.last_writer(),
                "eval": self.store.eval_settings(), "version": APP_VERSION,
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
        ws = self.store.slack_workspaces(area["id"])
        return dict(area, accounts=self.store.accounts(area["id"]), slack=ws,
                    chat_available=any(w["status"] != "needs_reconnect" for w in ws))

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
        if not self.store.has_sources():
            raise InputError("Connect a Google account or a Slack workspace first.")
        if self.evaluator.job is not None:  # the live writer would swap the local model mid-evaluation
            raise Conflict("An evaluation is running. Try again when it's finished.")
        return self.syncer.run_now()

    # --- AI writer --------------------------------------------------------
    def get_writer(self, _body=None):
        return self.store.writer_public()

    def patch_writer(self, body):
        return self.store.update_writer(_obj(body))

    def add_profile(self, body):
        b = _obj(body)
        return self.store.add_profile(b.get("copy_from"), b.get("name"))

    def patch_profile(self, body, prof_id):
        return self.store.update_profile(prof_id, _obj(body))

    def delete_profile(self, _body, prof_id):
        return self.store.delete_profile(prof_id)

    # --- Slack (read-only) ------------------------------------------------
    def slack_manifest(self, _body=None):
        return {"manifest": slack.MANIFEST, "manifest_json": slack.manifest_json(),
                "manifest_yaml": slack.manifest_yaml(), "create_url": slack.create_url(), "scopes": list(slack.SCOPES)}

    def slack_connect(self, body):
        b = _obj(body)
        area_id, token = b.get("area_id"), b.get("token")
        if not isinstance(area_id, str):
            raise InputError("Pick an area for this workspace.")
        self.store.area(area_id)
        token = token.strip() if isinstance(token, str) else ""
        if not re.fullmatch(r"xoxp-[A-Za-z0-9-]{10,250}", token):
            raise InputError("Paste the User OAuth Token. It starts with xoxp-.")
        try:
            info = slack.validate(token)
        except slack.SlackError as e:
            if e.code in slack.RECONNECT_CODES:
                raise InputError("Slack didn't accept that token. Copy the User OAuth Token again.") from None
            if e.code == "missing_scope":
                raise InputError("The Slack app is missing a read permission. Create it again from the "
                                 "manifest.") from None
            if not e.code:
                raise UpstreamError("Couldn't reach Slack. Try again in a minute.") from None
            raise InputError(str(e)) from None
        channels = None
        try:
            channels = slack.channel_list(slack.list_conversations(slack.Client(token, clock=self.clock)))
        except (slack.SlackError, slack.Budget):
            pass  # read again at the next sync
        ws_id = self.store.save_slack(area_id, info, token, self.clock(), channels)
        return self.store.slack_workspace(ws_id)

    def slack_channels(self, _body, ws_id):
        ws = self.store.slack_secret(ws_id)
        ignored = set(ws["ignored"])
        stale, chans = False, None
        try:
            chans = slack.channel_list(slack.list_conversations(slack.Client(ws["token"], clock=self.clock)))
            state = dict(ws["state"], channels=chans)
            self.store.set_slack_state(ws_id, state=state)
        except (slack.SlackError, slack.Budget):
            stale, chans = True, ws["state"].get("channels") or []
        at = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="seconds")
        return {"channels": [dict(c, ignored=c["id"] in ignored) for c in chans],
                "fetched_at": None if stale else at, "stale": stale}

    def patch_slack(self, body, ws_id):
        b = _obj(body)
        self.store.slack_workspace(ws_id)
        for k in b:
            if k != "ignored":
                raise InputError(f"Slack workspaces can't change {str(k)[:40]}.")
        if "ignored" not in b:
            return self.store.slack_workspace(ws_id)
        return self.store.set_slack_ignored(ws_id, b["ignored"])

    def delete_slack(self, _body, ws_id):
        self.store.delete_slack(ws_id)
        return {"ok": True}

    # --- doesn't need a reply ----------------------------------------------
    def add_suppress(self, body):
        b = _obj(body)
        row_id, scope, key = b.get("row_id"), b.get("scope"), b.get("key")
        if scope not in ("thread", "sender", "domain"):
            raise InputError("Hide this thread, this sender or this domain.")
        brief = self.store.cached_brief() or {}
        refs = self.store.brief_refs()
        row = next((r for r in brief.get("replies") or [] if r.get("id") == row_id), None)
        ref = refs.get(row_id) if isinstance(row_id, str) else None
        if row is None or ref is None:
            raise NotFound("That row isn't in the brief any more. Reload the page.")
        if key is not None and key != row.get("key"):
            raise Conflict("The brief has changed since the page loaded. Reload and try again.")
        if scope == "domain" and not ref.get("domain"):
            raise InputError("Slack rows can be hidden for this thread or this sender only.")
        now = dt.datetime.fromtimestamp(self.clock(), rules.tzinfo(self.store.settings()))
        rule = self.store.add_suppress(scope, ref.get(scope), ref.get("source"),
                                       (ref.get("labels") or {}).get(scope), now.isoformat(timespec="seconds"))
        # hide the rows this rule covers in the brief now, not only at the next refresh
        with self.store.lock:
            brief = self.store.cached_brief() or {}
            keep, gone = [], set()
            for r in brief.get("replies") or []:
                sup = refs.get(r.get("id"))
                if sup is not None and r.get("key") == sup.get("key") and rules.suppressed(sup, sup.get("vip"), [rule]):
                    gone.add(r.get("id"))
                else:
                    keep.append(r)
            if gone:
                brief["replies"] = keep
                brief["hidden"] = int(brief.get("hidden") or 0) + len(gone)
                self.store.set_cached_brief(brief)
        return {"rule": rule, "hidden": int(brief.get("hidden") or 0)}

    def set_done(self, body):
        """Marks a reply row done (hidden from Needs a reply, listed under Done today) or undoes it. The cached
        brief changes at once; the stored key also hides the row in later briefs until a newer message."""
        b = _obj(body)
        row_id, done, key = b.get("row_id"), b.get("done"), b.get("key")
        if not isinstance(done, bool):
            raise InputError("done must be true or false.")
        s = self.store.settings()
        tz = rules.tzinfo(s)
        now_ts = self.clock()
        now_iso = dt.datetime.fromtimestamp(now_ts, tz).isoformat(timespec="seconds")
        refs = self.store.brief_refs()
        ref = refs.get(row_id) if isinstance(row_id, str) else None
        with self.store.lock:
            brief = self.store.cached_brief() or {}
            replies, done_rows = brief.get("replies") or [], brief.get("done") or []
            row = next((r for r in replies if r.get("id") == row_id), None)
            drow = next((r for r in done_rows if r.get("id") == row_id), None)
            if (row is None and drow is None) or ref is None or not ref.get("done_key"):
                raise NotFound("That row isn't in the brief any more. Reload the page.")
            if key is not None and key != (row or drow).get("key"):
                raise Conflict("The brief has changed since the page loaded. Reload and try again.")
            if done and row is not None:
                self.store.set_done(ref["done_key"], ref.get("last"), now_iso, now_ts)
                brief["replies"] = [r for r in replies if r is not row]
                brief["done"] = done_rows + [dict(row, done_at=now_iso)]
                self.store.set_cached_brief(brief)
            elif not done:
                self.store.clear_done([ref["done_key"]], now_ts)
                if drow is not None:
                    back = {k: v for k, v in drow.items() if k != "done_at"}
                    brief["done"] = [r for r in done_rows if r is not drow]
                    brief["replies"] = sorted(replies + [back], key=lambda r: int(str(r.get("id") or "r0")[1:] or 0))
                    self.store.set_cached_brief(brief)
        return {"ok": True, "done": rules.done_list(brief.get("done"), tz, rules.time_format(s),
                                                    self.store.writer_config()["provider"] != "off")}

    def delete_suppress(self, _body, rule_id):
        self.store.delete_suppress(rule_id)
        return {"ok": True}

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

    # --- writer evaluation (owner-run; evaluate.py) -------------------------
    def patch_eval(self, body):
        return self.store.update_eval(_obj(body))

    def _eval_on(self):
        if not self.store.eval_settings()["enabled"]:
            raise NotFound(evaluate.OFF)
        return self.evaluator

    def eval_status(self, _body=None):
        return self._eval_on().status()

    def eval_models(self, _body=None):
        return self._eval_on().models()

    def add_contender(self, body):
        self._eval_on()
        return self.store.save_contender(None, _obj(body))

    def patch_contender(self, body, cid):
        self._eval_on()
        return self.store.save_contender(cid, _obj(body))

    def delete_contender(self, _body, cid):
        self._eval_on()
        return self.store.delete_contender(cid)

    def eval_run(self, _body):
        try:
            return self._eval_on().start()
        except evaluate.Refused as e:
            raise Conflict(str(e)) from None

    def _found(self, value):
        if value is None:
            raise NotFound("That evaluation run doesn't exist.")
        return value

    def eval_view(self, _body, run_id):
        return self._found(self._eval_on().view(run_id))

    def eval_choices(self, body, run_id):
        try:
            return self._found(self._eval_on().choose(run_id, _obj(body)))
        except evaluate.Refused as e:
            raise Conflict(str(e)) from None
        except InputError:
            raise
        except ValueError as e:
            raise InputError(str(e)) from None

    def eval_export(self, _body, run_id):
        text = self._found(self._eval_on().export(run_id))
        return Download(text.encode("utf-8"), "application/x-ndjson; charset=utf-8",
                        f"callsheet-writer-eval-{run_id}.jsonl")
