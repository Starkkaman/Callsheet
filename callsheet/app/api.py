"""The JSON API behind the page. The HTTP layer (server.py) parses requests and calls `App.handle`;
everything here works on plain dicts. Secrets never leave this module: responses use the public
views from store.py (client ID hint, account objects without tokens).
"""
import datetime as dt
import json
import pathlib
import re
import sys
import threading
import time

import dates
import evaluate
import gather
import horizon
import memory
import presets
import writer

import google
import ha
import push
import rules
import signal_api
import slack
from oauth import OAuthFlows
import scan as index_scan            # 0.6.0 index
import sources as index_sources      # 0.6.0 index
from index import Index, IndexError_  # 0.6.0 index
import know_profile                  # 0.6.0 profile
import projects as projects_mod     # 0.7.0 projects
import chat                          # 0.6.0 chat
import inject
import people as people_mod          # 0.8.2 people
from scheduler import Scheduler
from store import Conflict, InputError, NotFound, Store
from sync import Syncer

HERE = pathlib.Path(__file__).resolve().parent
SAMPLE = HERE / "sample_brief.json"
# The app's version, the same as `version` in callsheet/config.yaml (the image only holds app/, so it is
# kept here; tests/test_server.py fails if the two differ). Sent in api/state and put into index.html.
APP_VERSION = "0.9.0"


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


PASTE_WARN = {"instruction": "This looks like an instruction copied from a message.",
              "claim": "This looks like a claim copied from a message.",
              "both": "This looks like an instruction and a claim copied from a message."}


def paste_warning(texts):
    """0.8.2: the 0.8.1 chat checks on notes the owner types or edits by hand, as a warning only (the owner may save
    anyway with confirm: true): {"saved": False, "warning", "kinds"} or None."""
    kinds = []
    for t in texts or ():
        if not isinstance(t, str) or not t.strip():
            continue
        if "instruction" not in kinds and inject.instructions(t):
            kinds.append("instruction")
        if "claim" not in kinds and writer.states_decision(t):
            kinds.append("claim")
    if not kinds:
        return None
    key = "both" if len(kinds) == 2 else kinds[0]
    return {"saved": False, "warning": PASTE_WARN[key] + " Save anyway?", "kinds": kinds}


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
        self.notifier = push.Notifier(self.store, self.syncer, clock=clock)  # 0.5.1 phone notifications
        self.scheduler = Scheduler(self.store, self.syncer, clock=clock, notifier=self.notifier)
        self.evaluator = evaluate.Evaluator(self.store, self.syncer, clock=clock)
        self.memory = self.syncer.memory  # memory.md (0.5.0): one object, one lock
        # Signal (0.5.1): signal-cli as a read-only linked device, its routes in signal_api.py
        self.signal = signal_api.SignalApi(self.store, data_dir, clock=clock)
        self._index_init(data_dir)  # 0.6.0 index: Know me (index.db, scanner)
        self._profile_init()  # 0.6.0 profile: the drafted profile and questions
        self._chat_init(data_dir)  # 0.6.0 chat: Ask Callsheet over the index (chat.py)
        # 0.7.0 projects: the planning assistant (projects.py, docs/projects.md)
        self.projects = projects_mod.Projects(self.store, self.index, notifier=self.notifier,
                                              near_call=self.evaluator.near_call,
                                              sync_busy=self.syncer.lock.locked, clock=clock)
        self.scheduler.projects = self.projects
        self.chat.projects = self.projects  # 0.7.1: the project summary for a project chat's first lookup
        # 0.8.2 people: one person across email, Slack, Signal and Drive (people.py, docs/people.md)
        self.people = people_mod.People(self.store, self.index, clock=clock)
        self.syncer.people = self.notifier.people = self.chat.people = self.projects.people = self.people
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
            ("GET", r"writer/models", self.writer_models),            # 0.6.1 presets
            ("POST", r"writer/presets", self.add_preset),
            ("PATCH", rf"writer/presets/({presets.PRESET_ID})", self.patch_preset),
            ("DELETE", rf"writer/presets/({presets.PRESET_ID})", self.delete_preset),
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
        ] + self._memory_routes() + self._push_routes() + self.signal.routes() + self._index_routes() \
            + self._profile_routes()
        self.routes += self._chat_routes()  # 0.6.0 chat
        self.routes += projects_mod.ProjectsApi(self.projects).routes()  # 0.7.0 projects
        self.routes += [("POST", r"horizon", self.horizon_mark)]  # 0.8.0 Coming up
        self.routes += self._people_routes()  # 0.8.2 people

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
        self.signal.service.close()  # Signal (0.5.1): stops signal-cli cleanly
        self.chat.close()  # 0.6.0 chat
        if self.scanner:  # 0.6.0 index
            self.scanner.stop()
            self.index.close()
        self.store.close()

    def _signal_area(self):
        """Signal (0.5.1): the area id Signal rows go to while Signal is linked, else None."""
        m = self.store.signal_meta()
        return m.get("area_id") if m.get("linked") else None

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
        self._brief_080(brief, cache, s, now)
        self._brief_writing(brief, cache, s, now)
        self._brief_people(brief)
        if not brief.get("sample"):
            try:  # 0.7.0: the Slipped line and the weekly review card
                brief["projects"] = self.projects.brief_line()
            except Exception as e:  # noqa: BLE001 (the brief must always load)
                sys.stderr.write(f"Callsheet projects line failed: {type(e).__name__}\n")
        return brief

    def _brief_writing(self, brief, cache, settings, now):
        """0.6.1: while the writer works on the cached brief, "writing" tells the page to show "Writing the
        summary…" and to ask again (the rules brief is already there)."""
        w = self.syncer.writing
        if not (w and cache and cache.get("written_at") == w.get("written_at")) \
                or self.store.writer_config()["provider"] == "off":
            return
        try:
            since = rules.clock(dt.datetime.fromisoformat(w["since"]).astimezone(now.tzinfo), rules.time_format(settings))
        except (KeyError, TypeError, ValueError):
            since = None
        brief["writing"] = {"text": presets.WRITING, "since": since, "preset": w.get("preset"), "poll_s": 10}

    # --- 0.8.2: people (people.py, docs/people.md) ---------------------------------------------------------------
    def _people_routes(self):
        return [("GET", r"people", self.get_people),
                ("POST", r"people/refresh", self.people_refresh),
                ("PATCH", r"people/me", self.people_me),
                ("POST", r"people/me/send-as", self.people_send_as),
                ("POST", r"people/proposals/(mp[0-9a-f]{10})", self.people_decide),
                ("POST", r"people/merges/(\d{1,9})/split", self.people_split),
                ("PATCH", r"people/(p[0-9a-f]{11})", self.people_patch)]

    def _ppl(self, fn, *args):
        try:
            return fn(*args)
        except people_mod.PeopleError as e:
            raise InputError(str(e)) from None
        except people_mod.Gone as e:
            raise NotFound(str(e)) from None

    def get_people(self, body=None):
        q = (body or {}).get("q") if isinstance(body, dict) else ""
        return self._ppl(self.people.view, q if isinstance(q, str) else "")

    def people_refresh(self, _body):
        out = self._ppl(self.people.seed)
        return dict(self.people.view(), refreshed=out)

    def people_me(self, body):
        self._ppl(self.people.my_names, _obj(body).get("names"))
        return self.people.view()

    def people_send_as(self, _body):
        """The owner's button: each connected mail account's Gmail send-as addresses (GET users/me/settings/sendAs,
        read-only, gmail.readonly); only the account's own primary address and accepted aliases count."""
        client = self.store.google_client()
        found, errors = {}, []
        for a in self.store.accounts():
            if not a["services"].get("mail"):
                continue
            acc = self.store.account_secret(a["id"])
            try:
                res = google.send_as(self.syncer._token(acc, client)) or {}
            except google.GoogleError as e:
                errors.append(f"{a['email']}: {e}")
                continue
            found[a["email"]] = [x.get("sendAsEmail") for x in res.get("sendAs") or [] if isinstance(x, dict) and
                                 (x.get("isPrimary") or x.get("verificationStatus") == "accepted")]
        self.people.set_send_as(found)
        return dict(self.people.view(), errors=errors)

    def people_decide(self, body, prop_id):
        """{answer, confirm?}. A Same person into a VIP answers needs_confirm with the evidence and a one-time token
        first (nothing changes); the same answer with {confirm: token} merges."""
        b = _obj(body)
        out = self._ppl(self.people.decide, prop_id, b.get("answer"), b.get("confirm"))
        return dict(out, open=len(self.people.proposals()))

    def people_split(self, _body, audit_id):
        return self._ppl(self.people.split, audit_id)

    def people_patch(self, body, pid):
        return self._ppl(self.people.patch, pid, _obj(body))

    def _brief_people(self, brief):
        """0.8.2: reply rows show the person's name when the owner confirmed the person (by the sender's address, Slack
        user or Signal id, never a display name); a row whose sender is in an open "Same person?" pair carries the
        card; mail, Slack or Signal that shows one of the owner's names from an address or account that isn't the owner's
        is marked."""
        if brief.get("sample") or not brief.get("replies"):
            return
        try:
            d = self.people.directory()
            refs = self.store.brief_refs()
            mine = self.people.me_keys()
            own = self.people.owner_names()
            cards = 0
            for r in brief["replies"]:
                ref = refs.get(r.get("id")) or {}
                kind = {"mail": "email", "slack": "slack", "signal": "signal"}.get(ref.get("source"))
                sender = str(ref.get("sender") or "").lower()
                if not kind or not sender:
                    continue
                p = d.person_of(kind, sender)
                if (kind, sender) not in mine:  # mail, Slack or Signal that shows the owner's name from elsewhere
                    shown = {people_mod.norm_name(r.get("who"))}
                    if p is not None and kind != "email":  # the Slack real name or Signal contact name too
                        shown |= {people_mod.norm_name(n) for n in p["names"]}
                    if shown & own:
                        r["uses_your_name"] = True
                if p is None or p["state"] == people_mod.ME:
                    continue
                if p["state"] == "confirmed" and p["name"] and not r.get("sender_fail") and not r.get("uses_name") \
                        and not r.get("uses_your_name"):
                    r["who"] = p["name"][:60]
                if cards < 3:
                    props = self.people.proposals(person=p["id"], limit=1)
                    if props:
                        r["same"] = props[0]
                        cards += 1
        except Exception as e:  # noqa: BLE001 (the brief must always load)
            sys.stderr.write(f"Callsheet people on the brief failed: {type(e).__name__}\n")

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

    # --- 0.8.0: See everything (gather.py, horizon.py; rules only, nothing reaches the writer) -----------------
    def _see_parts(self, cache, settings, now):
        tz = rules.tzinfo(settings)
        today = now.date()
        since = (cache or {}).get("changed_since")
        try:
            since_ts = dt.datetime.fromisoformat(since).timestamp() if since else None
        except (TypeError, ValueError):
            since_ts = None
        try:
            milestones, threads = self.projects.see_080(since_ts)
        except Exception as e:  # noqa: BLE001 (the brief must always load)
            sys.stderr.write(f"Callsheet coming up: projects skipped ({type(e).__name__})\n")
            milestones, threads = [], []
        notes = []
        for e in self.memory.entries():
            if e["section"] != "Notes":
                continue
            for d in dates.find(e["text"], today)[:1]:
                day = d.get("date") if isinstance(d, dict) else None
                if day:
                    notes.append({"key": "note:" + str(e["meta"].get("id") or e["text"][:40]), "date": day,
                                  "title": rules._clip(e["text"], 120)})
        cands = horizon.items((cache or {}).get("upcoming"), (cache or {}).get("invites"), milestones, notes, today,
                              tz)
        return cands, threads

    def _brief_080(self, brief, cache, settings, now):
        """Adds the 0.8.0 fields to a live brief: invites (+ marks as text), cal_gaps, security, sorted, changed and
        coming_up (horizon.build over the cached 60-day events, invites, project milestones, dated notes and the
        owner's marks). The sample brief gets none."""
        if brief.get("sample") or not cache:
            return
        tz, fmt, today = rules.tzinfo(settings), rules.time_format(settings), now.date()
        try:
            brief["invites"] = gather.invites_for_page(cache.get("invites") or [], tz, fmt, today)
            brief["cal_gaps"] = [dict(g, text=gather.gap_text(g)) for g in cache.get("cal_gaps") or []]
            brief["security"] = gather.security_for_page(cache.get("security") or [], tz, fmt, today)
            brief["security_n"] = int(cache.get("security_n") or 0)
            brief["sorted"] = gather.sorted_for_page(cache.get("sorted") or {}, tz, fmt, today)
            cands, threads = self._see_parts(cache, settings, now)
            brief["changed"] = gather.changes_for_page(cache.get("changed") or [], tz, fmt, today) + \
                [{"kind": "thread", "text": f"New in {t['project']}: {t['title']}"} for t in threads]
            brief["coming_up"] = horizon.build(cands, self.store.see_meta("horizon_marks"), today)
        except Exception as e:  # noqa: BLE001 (the brief must always load)
            sys.stderr.write(f"Callsheet see-everything: page fields skipped ({type(e).__name__})\n")

    def horizon_mark(self, body):
        """POST api/horizon {key, action}: Got it, Remind me a week before, Not big, Big or undo on a Coming up item."""
        b = _obj(body)
        s = self.store.settings()
        now = dt.datetime.fromtimestamp(self.clock(), rules.tzinfo(s))
        cands, _ = self._see_parts(self.store.cached_brief(), s, now)
        try:
            marks = horizon.apply_mark(self.store.see_meta("horizon_marks"), b.get("key"), b.get("action"), cands,
                                       now.date())
        except ValueError:
            raise InputError("That isn't something Coming up can do.")
        except KeyError:
            raise NotFound("That item isn't coming up any more.")
        self.store.set_see_meta("horizon_marks", marks)
        return {"ok": True, "coming_up": horizon.build(cands, marks, now.date())}

    def _memory_routes(self):
        return [("GET", r"memory", self.get_memory),
                ("PATCH", r"memory", self.save_memory),
                ("POST", r"memory/reset", self.reset_memory),
                ("POST", r"memory/notes", self.add_note),
                ("PATCH", r"memory/notes/(n[0-9a-f]{6,12})", self.patch_note),
                ("DELETE", r"memory/notes/(n[0-9a-f]{6,12})", self.delete_note)]

    # --- 0.5.1: phone notifications through Home Assistant (push.py, ha.py) ------------------------------------
    def _push_routes(self):
        return [("GET", r"push", self.get_push),
                ("PATCH", r"push", self.patch_push),
                ("GET", r"push/targets", self.push_targets),
                ("POST", r"push/test", self.push_test)]

    def _push_out(self):
        st = self.store.push_state()
        cfg = self.store.push_config()
        return {"settings": cfg, "names": {t: ha.friendly(t) for t in cfg["targets"]},
                "available": ha.available(), "link": push.ingress_path(),
                "last_sent": st.get("last_sent"), "last_error": st.get("last_error"),
                "every_min": push.CHECK_EVERY_S // 60, "max_targets": push.MAX_TARGETS}

    def get_push(self, _body=None):
        return self._push_out()

    def patch_push(self, body):
        self.store.update_push(_obj(body))
        return self._push_out()

    def push_targets(self, _body=None):
        """The phones Home Assistant lists (notify.mobile_app_*), with friendly names."""
        if not ha.available():
            return {"available": False, "targets": [], "error": ha.NOT_IN_HA}
        try:
            return {"available": True, "targets": ha.notify_targets(), "error": None}
        except ha.HAError as e:
            raise UpstreamError(str(e)) from None

    def push_test(self, _body=None):
        try:
            sent, errors = self.notifier.test()
        except ha.HAError as e:
            if e.code in ("none", "unavailable"):
                raise InputError(str(e)) from None
            raise UpstreamError(str(e)) from None
        n = len(self.store.push_config()["targets"])
        if not errors:
            msg = "Sent. Check your phone." if n == 1 else f"Sent to {n} phones. Check them."
        elif sent:
            msg = f"Sent to {sent} of {n} phones. " + " ".join(dict.fromkeys(errors))
        else:
            msg = " ".join(dict.fromkeys(errors))
        return dict(self._push_out(), ok=not errors, message=msg)

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
        if "text" not in b or set(b) - {"text", "confirm"}:
            raise InputError("Send the memory as {\"text\": ...}.")
        if b.get("confirm") is not True and isinstance(b["text"], str):
            # 0.8.2: notes the owner wrote or changed by hand get the chat checks as a warning (people paste from mail)
            try:
                old = {e["text"] for e in self.memory.entries()}
                new = [e["text"] for e in memory.parse(b["text"])["entries"] if e["text"] not in old]
            except (memory.MemoryError_, KeyError, TypeError):
                new = []
            warn = paste_warning(new)
            if warn:
                return dict(self._mem(self.memory.view), rule=writer.NOTES_RULE, cap=memory.NOTES_CAP, **warn)
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
        if b.get("kind") == "chat":
            # 0.8.1 Remember: the owner pressed Save on a chat Save card (one-time token, the checks; chat.Chat.save_note)
            return self._mem(self.chat.save_note, b, self.memory)
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
        warn = paste_warning([b.get("text")]) if b.get("confirm") is not True else None
        if warn:
            return warn
        return self._mem(self.memory.add, title, meta, b.get("text"))

    def patch_note(self, body, nid):
        b = _obj(body)
        warn = paste_warning([b.get("text")]) if b.get("confirm") is not True else None
        if warn:
            return warn
        return self._mem(self.memory.update, nid, b.get("text"))

    def delete_note(self, _body, nid):
        out = self._mem(self.memory.delete, nid)
        self.chat.unsaved(nid)  # 0.8.1: Undo of a note saved from a chat Save card
        return out

    def state(self, _body=None):
        client = self.store.google_client_public()
        accounts = self.store.accounts()
        workspaces = self.store.slack_workspaces()
        signal_area = self._signal_area()
        areas = []
        for a in self.store.areas():
            a = dict(a)
            a["accounts"] = [acc for acc in accounts if acc["area_id"] == a["id"]]
            a["slack"] = [w for w in workspaces if w["area_id"] == a["id"]]
            a["chat_available"] = any(w["status"] != "needs_reconnect" for w in a["slack"])
            a["signal"] = a["id"] == signal_area  # Signal (0.5.1)
            a["chat_available"] = a["chat_available"] or a["signal"]
            areas.append(a)
        return {"google_client": client, "areas": areas, "vips": self.store.vips(),
                "people": {"open": self.people.open_count()},  # 0.8.2: Setup home's People card
                "suppress": self.store.suppress_rules(),
                "settings": self.store.settings(), "last_sync": self.store.last_sync(),
                "writer": self.store.writer_public(), "last_writer": self.store.last_writer(),
                "eval": self.store.eval_settings(), "push": self.store.push_config(), "version": APP_VERSION,
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
        sig = area["id"] == self._signal_area()  # Signal (0.5.1)
        return dict(area, accounts=self.store.accounts(area["id"]), slack=ws, signal=sig,
                    chat_available=sig or any(w["status"] != "needs_reconnect" for w in ws))

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
            raise InputError("Connect a Google account or a Slack workspace, or link Signal, first.")
        if self.evaluator.job is not None:  # the live writer would swap the local model mid-evaluation
            raise Conflict("An evaluation is running. Try again when it's finished.")
        # 0.6.1: with another preset than Fast for Refresh, the page gets the rules brief at once and the writer
        # finishes in the background ("Writing the summary…")
        return self.syncer.run_now(kind="refresh", background=presets.is_slow(self.store.writer_config(), "refresh"))

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

    # --- 0.6.1 writer presets -----------------------------------------------
    def writer_models(self, _body=None):
        """The local server's model ids for the preset form; a server that can't be reached is a sentence."""
        loc = self.store.writer_config()["local"]
        try:
            return {"models": writer.local_models(loc["base_url"], loc["api_key"]), "error": None}
        except writer.WriterError as e:
            return {"models": [], "error": str(e)}

    def _check_preset_model(self, model):
        """A preset's model id must be one the local server lists (GET /models). When the server can't be
        reached the preset is still saved, with a sentence saying it wasn't checked."""
        if not isinstance(model, str) or not model.strip():
            return None  # store.py says what's wrong
        loc = self.store.writer_config()["local"]
        try:
            ids = writer.local_models(loc["base_url"], loc["api_key"])
        except writer.WriterError as e:
            return f"Couldn't check the model on the server: {e}"
        if ids and model.strip() not in ids:
            shown = ", ".join(ids[:5]) + (f" and {len(ids) - 5} more" if len(ids) > 5 else "")
            raise InputError(f"The server doesn't list {model.strip()[:120]}. Its models: {shown}.")
        if not ids:
            return "Couldn't check the model: the server lists no models."
        return None

    def add_preset(self, body):
        b = dict(_obj(body))
        if "suggestion" in b:  # the built-in suggestion, one click (the model id can be sent edited)
            if b.pop("suggestion") != "careful":
                raise InputError("The only suggestion is careful.")
            b = dict({k: v for k, v in presets.SUGGESTION.items() if k != "label"}, **b)
        check = self._check_preset_model(b.get("model"))
        p = self.store.add_preset(b)
        return dict(self.store.writer_public(), preset_id=p["id"], model_check=check)

    def patch_preset(self, body, pid):
        b = _obj(body)
        check = self._check_preset_model(b["model"]) if "model" in b else None
        self.store.update_preset(pid, b)
        return dict(self.store.writer_public(), preset_id=pid, model_check=check)

    def delete_preset(self, _body, pid):
        return self.store.delete_preset(pid)

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

    # --- 0.6.0 index: Know me, the local index (index.py, sources.py, scan.py) -----------------------------------
    # Separate section (Signal is added on another branch). The page reads api/index/status and drives the scan;
    # 3b/3c read through Index.search/get/stats (docs/know-me-index.md).
    def _index_init(self, data_dir):
        self.index = self.scanner = self.index_error = None
        try:
            self.index = Index(data_dir, clock=self.clock)
        except IndexError_ as e:
            self.index_error = str(e)
            print(f"Callsheet index: {e}", file=sys.stderr, flush=True)
            return
        self.scanner = index_scan.Scanner(self.store, self.index, self.syncer, clock=self.clock)
        # 0.6.0 chat: one-off planted-instruction flags for rows indexed before the detector (reads compute them
        # on the spot until then)
        threading.Thread(target=self._index_backfill, name="callsheet-index-flags", daemon=True).start()

    def _index_backfill(self):
        try:
            n = self.index.backfill_instructions(batch=500, pause=0.05)
        except Exception as e:  # noqa: BLE001 (closed at shutdown, or a locked database: reads still compute flags)
            print(f"Callsheet index: instruction flags not stored yet ({type(e).__name__})", file=sys.stderr, flush=True)
            return
        if n:
            print(f"Callsheet index: checked {n} items for planted instructions", file=sys.stderr, flush=True)

    def _index_routes(self):
        return [("GET", r"index/status", self.index_status),
                ("GET", r"index/settings", self.get_index_settings),
                ("PATCH", r"index/settings", self.patch_index_settings),
                ("POST", r"index/estimate", self.index_estimate),
                ("POST", r"index/scan", self.index_scan),
                ("POST", r"index/pause", self.index_pause),
                ("POST", r"index/resume", self.index_resume),
                ("POST", r"index/cancel", self.index_cancel),
                ("GET", r"index/search", self.index_search),
                ("GET", r"index/item/(\d{1,12})", self.index_item),
                ("DELETE", r"index", self.index_wipe),
                ("POST", r"index/drive/check", self.index_drive_check),
                ("POST", r"index/drive/start", self.index_drive_start),
                ("POST", r"index/drive/finish", self.index_drive_finish)]

    def _scanner(self):
        if not self.scanner:
            raise Conflict(self.index_error or "The index isn't available.")
        return self.scanner

    def _index_sources(self, sc):
        """What the page shows per source: on/off, a short detail, Drive's grant state per account, the vault."""
        accounts = self.store.accounts()
        st = sc.settings()
        g = index_sources.GoogleAccess(self.store, self.index, self.clock)
        wss = self.store.slack_workspaces()
        mail = [a for a in accounts if a["services"].get("mail", True)]
        cals = sum(1 for a in accounts for c in a["calendars"] if c.get("selected"))
        drive = [{"account_id": a["id"], "email": a["email"], "granted": g.cached_drive(a["id"])} for a in accounts]
        vault = {"path": st["vault"], "ok": False, "error": None}
        if st["vault"]:
            try:
                index_sources.check_vault(st["vault"])
                vault["ok"] = True
            except InputError as e:
                vault["error"] = str(e)
        def n(k, one, many):
            return f"{k} {one if k == 1 else many}"
        return [
            {"key": "gmail", "label": "Mail", "on": st["sources"]["gmail"], "available": bool(mail),
             "detail": ", ".join(a["email"] for a in mail) or "No Google account connected"},
            {"key": "calendar", "label": "Calendars", "on": st["sources"]["calendar"], "available": cals > 0,
             "detail": n(cals, "calendar", "calendars") if cals else "No calendar chosen"},
            {"key": "slack", "label": "Slack", "on": st["sources"]["slack"], "available": bool(wss),
             "detail": ", ".join(w.get("team") or w.get("domain") or "Slack" for w in wss) or "No workspace connected"},
            {"key": "drive", "label": "Google Drive", "on": st["sources"]["drive"],
             "available": any(d["granted"] for d in drive), "accounts": drive,
             "detail": "Read-only" if any(d["granted"] for d in drive) else "Needs a read-only permission"},
            {"key": "obsidian", "label": "Obsidian vault", "on": st["sources"]["obsidian"], "available": vault["ok"],
             "vault": vault, "detail": st["vault"] or "Add the vault folder (under /share or /media)"},
            {"key": "signal", "label": "Signal", "on": st["sources"]["signal"],
             "available": index_sources.signal_ready(self.store),
             "detail": "Messages Callsheet received or imported" if index_sources.signal_ready(self.store)
             else "No Signal messages stored"},
        ]

    def index_status(self, _body=None):
        if not self.scanner:
            return {"available": False, "error": self.index_error}
        sc = self.scanner
        out = sc.status()
        out.update(available=True, error=None, stats=self.index.stats(), sources=self._index_sources(sc),
                   free_bytes=sc.free_bytes())
        return out

    def get_index_settings(self, _body=None):
        return self._scanner().settings()

    def patch_index_settings(self, body):
        return self._scanner().update_settings(_obj(body))

    @staticmethod
    def _index_args(body):
        body = _obj(body)
        sources = body.get("sources")
        if sources is not None and (not isinstance(sources, list) or not all(isinstance(x, str) for x in sources)):
            raise InputError("sources must be a list of source names.")
        if not isinstance(body.get("window"), str):
            raise InputError("Pick how far back to read.")
        return body["window"], sources

    def index_estimate(self, body):
        win, sources = self._index_args(body)
        return self._scanner().estimate(win, sources)

    def index_scan(self, body):
        win, sources = self._index_args(body)
        return self._scanner().start(win, sources)

    def index_pause(self, _body):
        return self._scanner().pause()

    def index_resume(self, _body):
        return self._scanner().resume()

    def index_cancel(self, _body):
        return self._scanner().cancel()

    def _index_day(self, val, what):
        if val in (None, ""):
            return None
        try:
            d = dt.date.fromisoformat(str(val)[:10])
        except ValueError:
            raise InputError(f"{what} must be a date like 2026-10-30.") from None
        return rules.day_bounds(d, rules.tzinfo(self.store.settings()))[0].timestamp()

    def index_search(self, body=None):
        """GET api/index/search?q=&source=&account=&area=&people=&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=&offset="""
        sc = self._scanner()
        q = body or {}
        try:
            limit = int(q.get("limit") or 20)
            offset = int(q.get("offset") or 0)
        except ValueError:
            raise InputError("limit and offset must be numbers.") from None
        since = self._index_day(q.get("from"), "from")
        until = self._index_day(q.get("to"), "to")
        if until is not None:
            until += 86400  # "to" includes that whole day
        source = q.get("source") or None
        if source and any(x not in sc.sources for x in source.split(",")):
            raise InputError("Unknown source.")
        rows = self.index.search(q.get("q") or "", source=source, account=q.get("account") or None,
                                 area=q.get("area") or None, people=q.get("people") or None, since=since,
                                 until=until, limit=limit, offset=offset)
        return {"q": q.get("q") or "", "results": rows}

    def index_item(self, _body, item_id):
        self._scanner()
        item = self.index.get(item_id)
        if item is None:
            raise NotFound("That item isn't in the index.")
        return item

    def index_wipe(self, body):
        body = body if isinstance(body, dict) else {}
        return self._scanner().wipe(body.get("confirm"))

    def index_drive_check(self, _body):
        """Asks Google (through a token refresh) which scopes each account granted; returns the Drive state."""
        sc = self._scanner()
        g = index_sources.GoogleAccess(self.store, self.index, self.clock)
        out = []
        for a in self.store.accounts():
            out.append({"account_id": a["id"], "email": a["email"], "granted": g.has_drive(a["id"], force=True)})
        sc._estimates = {}
        return {"accounts": out}

    def index_drive_start(self, body):
        """The incremental grant: Google's page asks only for Drive (read-only) on top of what the account gave."""
        self._scanner()
        acc_id = _obj(body).get("account_id")
        if not isinstance(acc_id, str):
            raise InputError("Pick the account to give Drive access.")
        acc = self.store.account(acc_id)
        return self.flows.start(acc["area_id"], extra_scopes=(google.DRIVE_SCOPE,), login_hint=acc["email"])

    def index_drive_finish(self, body):
        self._scanner()
        body = _obj(body)
        acc = self.flows.finish(body.get("flow_id"), body.get("pasted"))
        granted = index_sources.GoogleAccess(self.store, self.index, self.clock).has_drive(acc["id"], force=True)
        self.scanner._estimates = {}
        return {"account": acc, "granted": granted}

    # --- 0.6.0 profile: the drafted profile and Callsheet's questions (profile.py) --------------------------------
    # The draft, the AI's suggestions and the questions live in index.db meta; only the owner's saves and answers
    # (these routes, the owner's clicks) write memory.md. docs/know-me-profile.md.
    def _profile_init(self):
        self.profile = None
        if self.index is not None:
            self.profile = know_profile.Profile(self.store, self.index, self.memory, self.evaluator.near_call,
                                                self.syncer.lock.locked, clock=self.clock)

    def _profile_routes(self):
        kinds = r"(people|projects|items)/([prjh][0-9a-f]{10})"
        return [("GET", r"profile", self.get_profile),
                ("POST", r"profile/build", self.profile_build),
                ("POST", r"profile/suggest", self.profile_suggest),
                ("POST", r"profile/questions", self.profile_questions),
                ("POST", r"profile/questions/(g[0-9a-f]{10})", self.profile_answer),
                ("POST", r"profile/" + kinds, self.profile_save),
                ("DELETE", r"profile/" + kinds, self.profile_remove),
                ("POST", r"profile/" + kinds + r"/restore", self.profile_restore),
                ("DELETE", r"profile/(people|projects)/([pj][0-9a-f]{10})/suggestion", self.profile_unsuggest),
                ("POST", r"profile/people/(p[0-9a-f]{10})/vip", self.profile_vip)]

    def _pf(self, name, *args):
        """Calls Profile.<name> and maps its errors (Refused: 409, unknown key: 404, bad input: 400)."""
        if not self.profile:
            raise Conflict(self.index_error or "The index isn't available.")
        try:
            return getattr(self.profile, name)(*args)
        except know_profile.Refused as e:
            raise Conflict(str(e)) from None
        except memory.MemoryError_ as e:
            raise InputError(str(e)) from None
        except LookupError as e:
            raise NotFound(str(e)) from None
        except ValueError as e:
            raise InputError(str(e)) from None

    def get_profile(self, _body=None):
        out = self._pf("view")
        try:  # 0.8.2: the first "Same person?" card shows in Know me too
            if isinstance(out, dict):
                out["same"] = self.people.proposals(limit=1)
                out["same_open"] = self.people.open_count()
        except Exception as e:  # noqa: BLE001 (the profile must always load)
            sys.stderr.write(f"Callsheet people in Know me failed: {type(e).__name__}\n")
        return out

    def profile_build(self, _body=None):
        if self.syncer.lock.locked():
            raise Conflict("A sync is running. Try again in a minute.")
        self._pf("build")
        return self._pf("view")

    def profile_suggest(self, _body=None):
        return self._pf("start", "suggest")

    def profile_questions(self, _body=None):
        return self._pf("start", "ask")

    def profile_answer(self, body, gap):
        return self._pf("answer", gap, _obj(body))

    def profile_save(self, body, kind, key):
        return self._pf({"people": "save_person", "projects": "save_project", "items": "save_item"}[kind], key,
                        _obj(body))

    def profile_remove(self, _body, kind, key):
        return self._pf("remove", kind, key)

    def profile_restore(self, _body, kind, key):
        return self._pf("restore", kind, key)

    def profile_unsuggest(self, _body, kind, key):
        return self._pf("clear_suggestion", kind, key)

    def profile_vip(self, body, key):
        body = _obj(body)
        if not isinstance(body.get("vip"), bool):
            raise InputError("vip takes true or false.")
        return self._pf("set_vip", key, body["vip"])

    # --- 0.6.0 chat: Ask Callsheet, read-only tool use over the local index (chat.py, docs/chat.md) ---------------
    def _chat_init(self, data_dir):
        self.chat = chat.Chat(self.store, self.index, syncer=self.syncer, evaluator=self.evaluator,
                              data_dir=data_dir, clock=self.clock)

    def _chat_routes(self):
        cid = f"({chat.CONV_ID})"
        return [("GET", r"chat", self.chat_overview),
                ("DELETE", r"chat", self.chat_clear),
                ("POST", r"chat/ask", self.chat_ask),
                ("GET", r"chat/settings", self.chat_settings),
                ("PATCH", r"chat/settings", self.patch_chat_settings),
                ("POST", r"chat/models", self.chat_models),
                ("POST", r"chat/test", self.chat_test),
                ("GET", r"chat/item/(\d{1,12})", self.chat_item),
                ("GET", rf"chat/{cid}", self.chat_view),
                ("DELETE", rf"chat/{cid}", self.chat_delete),
                ("POST", rf"chat/{cid}/stop", self.chat_stop)]

    def chat_overview(self, _body=None):
        return self.chat.overview()

    def chat_clear(self, _body=None):
        return self.chat.clear()

    def chat_ask(self, body):
        body = _obj(body)
        conv = body.get("conversation_id")
        if conv is not None and not isinstance(conv, str):
            raise InputError("conversation_id must be text.")
        pid = body.get("project_id")
        scope = None
        if pid is not None:  # 0.7.0: a new chat about one project (a filter on the existing read-only tools)
            if conv:
                raise InputError("A project scope starts a new chat.")
            scope = self.projects.scope(pid if isinstance(pid, str) else "")
        return self.chat.ask(conv or None, body.get("text"), scope=scope)

    def chat_settings(self, _body=None):
        return chat.public_settings(self.store)

    def patch_chat_settings(self, body):
        before = chat.effective_local(self.store)
        out = chat.update_settings(self.store, _obj(body))
        after = chat.effective_local(self.store)
        if (before["base_url"], before["model"]) != (after["base_url"], after["model"]):
            self.chat.forget_fallback()  # a model change: native tool calling is tried again
        return out

    def chat_models(self, body):
        body = body if isinstance(body, dict) else {}
        return chat.list_models(self.store, body.get("base_url") or None, body.get("api_key") or None)

    def chat_test(self, _body=None):
        return chat.test_connection(self.store)

    def chat_item(self, _body, item_id):
        return self.chat.item(int(item_id))

    def chat_view(self, _body, conv_id):
        return self.chat.view(conv_id)

    def chat_delete(self, _body, conv_id):
        return self.chat.delete(conv_id)

    def chat_stop(self, _body, conv_id):
        return self.chat.stop(conv_id)
