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

import memory
import writer

import google
import ha
import push
import rules
import signal_api
import slack
from oauth import OAuthFlows
import library as library_mod        # 0.21.0: messages.db as one library (replaced index.py and scan.py)
import actions as actions_mod         # 0.22.0 Chat does what the owner can: the registry and its journal
import oldata                        # 0.21.0 Setup › Advanced › Old data from earlier versions (owner-pressed)
import sources as index_sources      # Drive, notes and GitHub readers (live.DocsReader), the Drive grant
import know_profile                  # 0.6.0 profile
import projects as projects_mod     # 0.7.0 projects
import chat                          # 0.6.0 chat
import inject
import people as people_mod          # 0.8.2 people
import assistant                     # the assistant's name (settings assistant_name)
import background                    # the background model (the ledger's read, the brief)
import aisettings                    # Setup › AI: one server, a model per job, the upgrade from older settings
import visuals                       # 0.19.0 visual blocks
import visuals_sample                # 0.19.0 the Visuals gallery's sample store
import brief2 as brief2_mod          # 0.21.0 Talk it through
import tidy as tidy_mod              # 0.21.4 Tidy up
import connectors as connectors_mod  # 0.10.0 connectors
import net                           # 0.10.0 connectors
import websearch                     # 0.14.0 web search (the owner's SearXNG)
import msgstore                      # 0.15.0 message store
import live as live_mod              # 0.15.0 live reading
import texts as texts_mod            # 0.16.1 texts
import conflicts as conflicts_mod  # noqa: E402 (0.16.0)
import extract as extract_mod  # noqa: E402 (0.16.0)
import ledger as ledger_mod  # noqa: E402 (0.16.0)
import need as need_mod  # noqa: E402 (0.23.0)
import filters as filters_mod  # noqa: E402 (0.24.0 the audit's filtering fixes)
import prefs as prefs_mod  # noqa: E402 (0.17.0)
import today as today_mod  # noqa: E402 (0.18.0)
import heads as heads_mod  # noqa: E402 (0.21.0)
import calwrite as calwrite_mod  # noqa: E402 (0.21.0 Add to calendar)
import rsvp as rsvp_mod  # noqa: E402 (0.21.0 Answer invites, approved 2026-11-13)
import agenda as agenda_mod  # noqa: E402 (0.21.0 Agenda)
import suggest as suggest_mod  # noqa: E402 (0.21.0 suggested projects, Drive files on a project)
import gdrafts  # noqa: E402 (0.21.0 Put in Gmail Drafts)
import contacts as contacts_mod  # noqa: E402 (0.21.0 Google Contacts, read-only)
import replydraft  # noqa: E402 (0.21.0 Draft a reply)
import voiceapi  # noqa: E402 (0.21.0 the voice assistant's read-only API, its own listener)
import areas as areas_mod  # noqa: E402 (0.22.0 Areas: rename, colour, move connections)
import palette  # noqa: E402 (0.22.0 area colours)
from scheduler import Scheduler
from store import Conflict, InputError, NotFound, Store
from sync import Syncer

HERE = pathlib.Path(__file__).resolve().parent
SAMPLE = HERE / "sample_brief.json"
# The app's version, the same as `version` in callsheet/config.yaml (the image only holds app/, so it is
# kept here; tests/test_server.py fails if the two differ). Sent in api/state and put into index.html.
APP_VERSION = "0.24.0"
DRAFT_SUG = "Draft a reply to "   # 0.21.0: today.py's Chat suggestion that means Draft a reply


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
        # 0.21.0 Put in Gmail Drafts (approved 2026-11-12): the only write to Google is drafts.create
        self.gdrafts = gdrafts.GmailDrafts(self.store, self.flows, clock=clock,
                                           log=lambda line: print(line, file=sys.stderr, flush=True))
        self.syncer = Syncer(self.store, clock=clock)
        self.notifier = push.Notifier(self.store, self.syncer, clock=clock)  # 0.5.1 phone notifications
        self.scheduler = Scheduler(self.store, self.syncer, clock=clock, notifier=self.notifier)
        self.memory = self.syncer.memory  # memory.md (0.5.0): one object, one lock
        # Signal (0.5.1): signal-cli as a read-only linked device, its routes in signal_api.py
        self.signal = signal_api.SignalApi(self.store, data_dir, clock=clock)
        self._messages_init(data_dir)  # 0.15.0 message store and live reading (msgstore.py, live.py)
        self._profile_init()  # 0.6.0 profile: the drafted profile and questions
        self._chat_init(data_dir)  # 0.6.0 chat: Ask Callsheet over the index (chat.py)
        # 0.7.0 projects: the planning assistant (projects.py, docs/projects.md)
        self.projects = projects_mod.Projects(self.store, self.index, notifier=self.notifier,
                                              near_call=self._near_call,
                                              sync_busy=self.syncer.lock.locked, clock=clock)
        self.scheduler.projects = self.projects
        self.chat.projects = self.projects  # 0.7.1: the project summary for a project chat's first lookup
        # 0.8.2 people: one person across email, Slack, Signal and Drive (people.py, docs/people.md)
        self.people = people_mod.People(self.store, self.index, clock=clock)
        self.syncer.people = self.notifier.people = self.chat.people = self.projects.people = self.people
        # the background model (background.py): its setting, slot, budget and Chat's preemption
        self.background = background.Background(self.store, self.syncer, clock=clock)
        # Setup › AI in one place (approved 2026-11-12): older settings (presets, Chat's own server, Claude as
        # the writer) mapped once onto one server and a model per job; what moved is said on the page
        for note in aisettings.migrate(self.store, clock):
            print("Callsheet AI settings: " + note, file=sys.stderr, flush=True)
        # 0.21.0 Google Contacts, read-only (approved 2026-11-12): one person across Signal, texts, mail, Slack
        self.contacts = contacts_mod.Contacts(
            self.store, self.flows, people=self.people,
            own_number=lambda: (getattr(self.signal.service, "_me", None) or {}).get("number"),
            tz_name=lambda: getattr(rules.tzinfo(self.store.settings()), "key", "UTC"), clock=clock,
            log=lambda line: print(line, file=sys.stderr, flush=True))
        if self.messages:
            self.messages.name_of = self.contacts.name_of
        self._connectors_init()  # 0.10.0 connectors
        self._ledger_init()  # 0.16.0 the ledger (ledger.py), read once by extract.py; clashes (conflicts.py)
        # 0.17.0 Chat on the store: the store tools, and the owner-pressed setting and action cards (prefs.py)
        self.chat.msgstore, self.chat.ledger, self.chat.prefs = self.messages or None, self.ledger, self.prefs
        self.chat.calwrite = self.calwrite   # 0.21.0: Chat's "add to calendar" card (owner's Confirm only)
        self.chat.tidy = getattr(self, "tidy", None)   # 0.21.4: Chat's Tidy up card (owner's Confirm only)
        self.chat.actions = getattr(self, "actions", None)   # 0.22.0: the action registry (actions.py)
        self.chat.oldata = getattr(self, "oldata", None)     # 0.22.0: Clear old data by Chat (always Confirm)
        self.gdrafts.messages, self.chat.gdrafts = self.messages or None, self.gdrafts   # 0.21.0 Gmail drafts
        self.routes = [
            ("GET", r"brief", self.brief),
            ("GET", r"talk", self.talk),                     # 0.21.0: Talk it through (brief2.py)
            ("POST", r"talk/start", self.talk_start),
            ("POST", r"talk/reply", self.talk_reply),
            ("POST", r"talk/skip", self.talk_skip),          # 0.24.0 (B1): Skip a step while it is written
            ("GET", r"state", self.state),
            ("POST", r"google-client", self.set_client),
            ("DELETE", r"google-client", self.delete_client),
            ("GET", r"areas", self.get_areas),               # 0.22.0 Areas: the board
            ("POST", r"areas/move", self.move_connection),   # 0.22.0 one connection to another area
            ("POST", r"areas/undo", self.undo_area),         # 0.22.0 Undo of an area change
            ("POST", r"areas/colour-check", self.colour_check),  # 0.22.0 More colours: AA shades and warnings
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
            ("DELETE", r"writer/claude-key", self.delete_claude_key),
            ("POST", r"writer/test", self.test_writer),
        ] + self._memory_routes() + self._push_routes() + self.signal.routes() + self._index_routes() \
            + self._profile_routes()
        self.routes += self._chat_routes()  # 0.6.0 chat
        self.routes += self.gdrafts.routes()  # 0.21.0 Put in Gmail Drafts (Setup › Connections, the brief's replies)
        self.routes += self.contacts.routes()  # 0.21.0 Google Contacts (Setup › Connections: Allow contacts, Sync now)
        self.routes += projects_mod.ProjectsApi(self.projects).routes()  # 0.7.0 projects
        self.routes += self._people_routes()  # 0.8.2 people
        self.routes += self._background_routes()  # Setup › AI › Background model
        self.routes += self._connector_routes()  # 0.10.0 connectors (0.11.1: Chat's setup cards use the same endpoints)
        self.routes += [("GET", r"sources", self.sources_status),           # 0.15.0 Setup → Connections → Sources
                        ("PATCH", r"sources/settings", self.patch_sources_settings)]
        self.routes += [("GET", r"texts", self.texts_view),                     # 0.16.1 Setup → Connections → Texts
                        ("PATCH", r"texts", self.patch_texts),
                        ("GET", r"texts/sensors", self.texts_sensors)]
        self.routes += [("GET", r"ledger", self.ledger_view),                  # 0.16.0 the ledger (stage-4 Today reads it)
                        ("GET", r"ledger/items/(\d+)", self.ledger_item),
                        ("GET", r"ledger/rejected", self.ledger_rejected)]
        self.routes += [("GET", r"today", self.today_view),                    # 0.18.0 the new Today
                        ("GET", r"today/items/(\d+)", self.today_item),
                        ("POST", r"today/items/(\d+)/draft", self.today_draft),     # 0.21.0 Draft a reply
                        ("POST", r"today/items/(\d+)/gmail", self.today_gmail),
                        ("GET", r"today/clashes/(\d+)", self.today_clash),
                        ("GET", r"today/replies", self.today_replies),
                        ("GET", r"today/cleanup", self.today_cleanup),
                        ("POST", r"today/done", self.today_done),
                        ("POST", r"today/undone", self.today_undone),
                        ("POST", r"today/dismiss", self.today_dismiss),
                        ("POST", r"today/related", self.today_related),       # 0.22.0 related clashes
                        ("POST", r"today/related/dismiss", self.today_related_dismiss),
                        ("GET", r"actions/recent", self.actions_recent),      # 0.22.0 Recent changes
                        ("POST", r"actions/undo", self.actions_undo),
                        ("POST", r"today/notes", self.today_note),
                        ("POST", r"today/remind", self.today_remind),
                        ("POST", r"today/refresh", self.today_refresh),        # 0.21.4 Refresh at the top of Today
                        ("POST", r"today/tidy", self.today_tidy),              # 0.21.4 the owner's Tidy up
                        ("POST", r"today/tidy/undo", self.today_tidy_undo),
                        ("POST", r"today/tidy/later", self.today_tidy_later),
                        ("POST", r"today/gotit", self.today_gotit),            # 0.23.0 heads-ups: Got it per item
                        ("POST", r"today/gotit/undo", self.today_gotit_undo),
                        ("POST", r"today/need", self.today_need),              # 0.23.0 "Does this need you?"
                        ("POST", r"today/back", self.today_back),              # 0.24.0 Move back to Needs you
                        ("POST", r"today/sender", self.today_sender),          # 0.24.0 Never from this sender / promo
                        ("POST", r"today/sender/undo", self.today_sender_undo),
                        ("POST", r"today/calendar", self.today_calendar)]      # 0.24.0 a calendar row's choice
        self.routes += [("GET", r"calendar/access", self.cal_access),            # 0.21.0 Add to calendar (calwrite.py)
                        ("POST", r"calendar/access/check", self.cal_access_check),
                        ("POST", r"calendar/access/start", self.cal_access_start),
                        ("POST", r"calendar/access/finish", self.cal_access_finish),
                        ("POST", r"calendar/access/default", self.cal_access_default),
                        ("POST", r"calendar/add", self.cal_add)]
        self.routes += [("GET", r"agenda", self.agenda_view),                  # 0.21.0 Agenda (agenda.py)
                        ("POST", r"agenda/rsvp", self.agenda_rsvp),             # 0.21.0 Answer invites (rsvp.py)
                        ("GET", r"projects/more", self.projects_more),          # 0.21.0 suggested projects, Drive files
                        ("POST", r"projects/suggested/(s[0-9a-f]{12})/add", self.projects_sug_add),
                        ("POST", r"projects/suggested/(s[0-9a-f]{12})/not", self.projects_sug_not)]
        self.routes += [("GET", r"visuals/schema", self.visuals_schema),      # 0.19.0 visual blocks
                        ("POST", r"visuals/preview", self.visuals_preview),
                        ("GET", r"visuals/gallery", self.visuals_gallery)]
        # 0.21.0 Setup → Connections → Voice assistant: the read-only API's switch and tokens (voiceapi.py)
        self.voice = voiceapi.Voice(self, clock=clock)
        self.routes += self.voice.routes()
        self.routes += [("GET", r"ai", self.ai_view),                        # Setup → AI: server, models per job
                        ("PATCH", r"ai", self.patch_ai),
                        ("GET", r"ai/models", self.ai_models)]               # the owner's server's GET /models only
        # 0.21.0 Setup › Advanced › Old data from earlier versions: listed by GET, removed only by the owner's POST
        self.oldata = oldata.OldData(self.store, lambda: self.messages, clock=clock)
        self.routes += self.oldata.routes()
        if getattr(self, "actions", None) is not None:   # 0.22.0: Clear old data by Chat (always the owner's Confirm)
            self.actions.oldata = self.oldata
        if getattr(self, "chat", None) is not None:
            self.chat.oldata = self.oldata
        self.routes += [("GET", r"web-search", self.web_search),            # 0.14.0 Setup → AI → Web search
                        ("PATCH", r"web-search", self.patch_web_search),
                        ("POST", r"web-search/check", self.web_search_check)]

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
        self.voice.stop()  # 0.21.0 the voice assistant API's listener
        self.scheduler.stop()
        self.connectors.stop()  # 0.10.0 connectors
        self.contacts.stop()  # 0.21.0 contacts reader
        self.signal.service.close()  # Signal (0.5.1): stops signal-cli cleanly
        if self.live:  # 0.15.0 live readers
            self.live.stop()
        if getattr(self, "ledger_runner", None):  # 0.16.0 the ledger's reader
            self.ledger_runner.stop()
        self.chat.close()  # 0.6.0 chat
        if self.messages:  # 0.15.0 message store
            self.messages.close()
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
    def _near_call(self):
        s = self.store.settings()
        return rules.near_call(s, dt.datetime.fromtimestamp(self.clock(), rules.tzinfo(s)))

    def brief(self, _body=None):
        """The page shell (the rework): the day, the time format, the areas and the Agenda's day from messages.db
        calendar events and ledger items (today.agenda). The rules brief (sync.py, rules.compose_brief) is retired; with
        no source connected yet the bundled sample shows."""
        s = self.store.settings()
        tz = rules.tzinfo(s)
        now = dt.datetime.fromtimestamp(self.clock(), tz)
        if not self.store.has_sources():
            return dict(load_sample(), sample=True)
        fmt = rules.time_format(s)
        out = {"sample": False, "time_format": fmt, "kind": "Today",
               "areas": [{"id": a["id"], "name": a["name"], "colour": a["colour"], "swatch": a["swatch"]}
                         for a in self.store.areas()]}
        out.update(rules.date_fields(now.date()))
        try:
            out["schedule"] = today_mod.agenda(getattr(self, "messages", None), getattr(self, "ledger", None), tz,
                                               now.date(), fmt)
        except Exception as e:  # noqa: BLE001 (the page must always load)
            sys.stderr.write(f"Callsheet agenda failed: {type(e).__name__}\n")
            out["schedule"] = []
        return out

    # --- 0.10.0: connectors (connectors.py, net.py, docs/connectors.md) -------------------------------------------
    def _connectors_init(self):
        """Read-only public feeds the owner adds (approved 2026-11-04). The poller starts in server.py."""
        net.set_version(APP_VERSION)
        self.connectors = connectors_mod.Connectors(self.store, self.index, clock=self.clock)
        self.chat.connectors = self.connectors  # 0.11.1: setup cards (allowed hosts for the "new site" note)
        # 0.14.0 web search (approved 2026-11-06): Chat only; result pages go through the connectors' Net
        self.web = websearch.Web(self.store, net_=self.connectors.net, clock=self.clock)
        self.chat.web = self.web

    # --- 0.14.0 web search: Setup → AI → Web search (the SearXNG address, On, the owner's Check) -----------------
    # --- Setup › AI (aisettings.py) ------------------------------------------------------------------------------
    def ai_view(self, _body=None):
        return aisettings.view(self.store, self.background)

    def patch_ai(self, body):
        before = chat.effective_local(self.store)
        out = aisettings.update(self.store, self.background, _obj(body))
        if before["model"] != chat.effective_local(self.store)["model"]:
            self.chat.forget_fallback()  # a model change: native tool calling is tried again
        return out

    def ai_models(self, _body=None):
        return aisettings.list_models(self.store, self.clock)

    def web_search(self, _body=None):
        return websearch.public(self.store, self.web.today())

    def patch_web_search(self, body):
        return websearch.update(self.store, _obj(body), self.web.today())

    def web_search_check(self, _body=None):
        """The owner's Check: one GET <address>/search?q=test&format=json, then the settings with the outcome."""
        out = websearch.run_check(self.store, self.web.searx, self.web.today())
        return dict(websearch.public(self.store, self.web.today()), result=out)

    def _connector_routes(self):
        """Setup's connector endpoints. 0.11.1: Check, Add and Remove also serve Chat's setup cards: a body with
        `chat` ({chat, turn, token}: the card's one-time token, bound to the chat and the turn) must carry a valid
        token, and the card's address rules run first (chat.card_address); Add then uses the token once and folds the
        card; Remove (Undo) marks the card. The checks, reads and the approval record are Setup's own."""
        out = []
        for method, pat, fn in connectors_mod.ConnectorsApi(self.connectors).routes():
            if (method, pat) == ("GET", r"connectors"):
                fn = self.connectors_view
            elif (method, pat) == ("POST", r"connectors/check"):
                fn = self.connectors_check
            elif (method, pat) == ("POST", r"connectors"):
                fn = self.connectors_add
            elif method == "DELETE" and pat == rf"connectors/{connectors_mod.CID}":
                fn = self.connectors_remove
            out.append((method, pat, fn))
        return out

    def connectors_view(self, _body=None):
        return self.connectors.view()

    def connectors_check(self, body):
        if not (isinstance(body, dict) and "chat" in body):
            return self.connectors.check(body)
        bad = [k for k in body if k not in ("kind", "address", "via", "chat")
               and not (k in ("where", "account") and body.get("kind") == "reports")]   # 0.12.1: Drive folders
        if bad:
            raise InputError(f"A card's Check doesn't take {str(bad[0])[:30]}.")
        kind, via = body.get("kind"), body.get("via") or None
        pid = self.chat.card_claim(body.get("chat"), kind, via)
        where = body.get("where") if kind == "reports" else None
        a = chat.card_address(kind, body.get("address"), bool(getattr(self.connectors.net, "allow_test_names", False)),
                              where=where, account=body.get("account"))
        if not a["ok"]:   # refused before anything is read
            return {"stage": "refused", "why": a["why"], "word": a["word"], "text": a["text"]}
        if not a["address"]:
            raise InputError("Pick a folder first." if kind == "reports" else "Paste an address first.")
        try:
            extra = {"where": where, "account": body.get("account")} if where == "drive" else {}
            r = self.connectors.check({"kind": kind, "address": a["address"], **({"via": via} if via else {}), **extra})
        except InputError as e:
            if "private network" in str(e):   # net.py, after DNS: the name points into the home network
                word, text = chat.REFUSALS["home"]
                return {"stage": "refused", "why": "home", "word": word, "text": text}
            raise
        self.chat.card_draft(pid, r.get("token"))
        host = r.get("host") or ""
        if host:
            known = {h["host"] for h in self.connectors.hosts()}
            _h, uni = chat.host_names(host)
            r.update(host_unicode=uni, idn=bool(uni), new_site=host not in known)
        return r

    def connectors_add(self, body):
        if not (isinstance(body, dict) and "chat" in body):
            return self.connectors.add(body)
        bad = [k for k in body if k not in ("token", "label", "tell", "chat")]
        if bad:
            raise InputError(f"A card's Add doesn't take {str(bad[0])[:30]}.")
        pid = self.chat.card_use(body.get("chat"), body.get("token"))
        try:
            r = self.connectors.add({k: body[k] for k in ("token", "label", "tell") if k in body})
        except Exception:
            self.chat.card_release(pid)   # nothing was written: the card can still be used
            raise
        self.chat.card_added(pid, r)
        return r

    def connectors_remove(self, _body, cid):
        r = self.connectors.remove(cid)
        self.chat.connector_removed(cid)  # 0.11.1: Undo on a card's "Added" line
        return r

    # --- end 0.10.0 connectors ---------------------------------------------------------------------------------------

    # --- the background model (background.py) -----------------------------------------------------------------------
    def _background_routes(self):
        """Setup › AI › Background model: the model and its context size, and the local model's health check."""
        return [("GET", r"background", self.background_view),
                ("PATCH", r"background", self.background_settings),
                ("POST", r"background/check", self.background_check)]

    def background_view(self, _body=None):
        return self.background.view()

    def background_settings(self, body):
        try:
            return self.background.update_settings(body)
        except background.InputError as e:
            raise InputError(str(e)) from None

    def background_check(self, _body=None):
        try:   # 0.8.5 server faults: "Check again" runs the local model's health probe now
            return self.background.check_model()
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(f"The check didn't run ({type(e).__name__}).") from None

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

    # --- 0.5.0: possible events and notes on the brief, memory (memory.py) -------------------------------------

    # --- 0.8.0: See everything (gather.py, horizon.py; rules only, nothing reaches the writer) -----------------

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
        # the rework: notes on the rules brief's rows are retired with it (Chat's Remember card is the way in)
        raise InputError("Ask Chat to remember it: notes on brief rows are gone with the old brief.")

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
            a["accounts"] = [dict(acc, cal_add=self.calwrite.public(acc) if self.calwrite else None)  # 0.21.0
                             for acc in accounts if acc["area_id"] == a["id"]]
            a["slack"] = [w for w in workspaces if w["area_id"] == a["id"]]
            a["chat_available"] = any(w["status"] != "needs_reconnect" for w in a["slack"])
            a["signal"] = a["id"] == signal_area  # Signal (0.5.1)
            a["chat_available"] = a["chat_available"] or a["signal"]
            areas.append(a)
        return {"google_client": client, "areas": areas, "vips": [dict(v, how=prefs_mod.vip_words(v)) for v in self.store.vips()],
                "people": {"open": self.people.open_count()},  # 0.8.2: Setup home's People card
                "background": {"model": self.background.settings()["model"]},  # Setup home's AI card
                "suppress": self.store.suppress_rules(),
                "settings": self.store.settings(), "last_sync": self.store.last_sync(),
                "writer": self.store.writer_public(), "last_writer": self.store.last_writer(),
                "push": self.store.push_config(), "version": APP_VERSION,
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

    def _areas(self):
        if getattr(self, "areas", None) is None:
            self.areas = areas_mod.Areas(self.store, getattr(self, "messages", None) or None, self.clock)
        return self.areas

    @staticmethod
    def _area_do(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except (areas_mod.AreaError, palette.Invalid) as e:
            raise InputError(str(e)) from None

    def get_areas(self, _body=None):
        """0.22.0 GET api/areas: the areas with their connections, those not in an area, the palette, the last Undo."""
        return self._areas().board()

    def add_area(self, body):
        b = _obj(body)
        area, token = self._area_do(self._areas().add, b.get("name"), b.get("colour"))
        return dict(self._area_out(area), undo=token)

    def patch_area(self, body, area_id):
        b = _obj(body)
        if set(b) & {"name", "colour"}:   # 0.22.0: rename and colour with Undo (what it may read: no Undo, as before)
            rest = {k: v for k, v in b.items() if k not in ("name", "colour")}
            if rest:
                self.store.update_area(area_id, rest)
            area, token = self._area_do(self._areas().update, area_id, b.get("name"), b.get("colour"))
            return dict(self._area_out(area), undo=token)
        return self._area_out(self.store.update_area(area_id, b))

    def delete_area(self, body, area_id):
        """0.22.0: the area's connections move to `to` (Personal by default) with Undo; {disconnect: true} is the old
        way (Callsheet signs out of its accounts and forgets them; no Undo)."""
        b = body if isinstance(body, dict) else {}
        if b.get("disconnect") is True:
            for acc in self.store.delete_area(area_id):
                google.revoke(acc.get("refresh_token") or acc.get("access_token"))
            return {"ok": True}
        res, token = self._area_do(self._areas().delete, area_id, b.get("to") or "personal")
        return dict(res, ok=True, undo=token)

    def move_connection(self, body):
        """0.22.0 POST api/areas/move {conn, to, past}: past (default true) moves the items it already read too."""
        b = _obj(body)
        res, token = self._area_do(self._areas().move, b.get("conn"), b.get("to"), b.get("past", True))
        return dict(res, ok=True, undo=token)

    def undo_area(self, body):
        return self._area_do(self._areas().undo, _obj(body).get("token"))

    def colour_check(self, body):
        return self._area_do(palette.check_hex, _obj(body).get("hex"))

    # --- accounts ---------------------------------------------------------
    def start_account(self, body):
        return self.flows.start(_obj(body).get("area_id"))

    def finish_account(self, body):
        b = _obj(body)
        return self.flows.finish(b.get("flow_id"), b.get("pasted"))

    def patch_account(self, body, account_id):
        out = self.store.update_account(account_id, _obj(body))
        self._sync_cals(recheck=True)          # 0.23.0: Mine / Ask me / Heads-up / Hide, where the ledger reads it
        return out

    def _one_per_thread(self, *_a, **_k):
        """0.24.0 after reads: the thread-level merge (filters.thread_merges) and items whose mail is in Trash close."""
        now = self.clock()
        try:
            self.messages.write(lambda db: (filters_mod.thread_merges(db, now), filters_mod.close_trashed(db, now)))
        except Exception as e:  # noqa: BLE001 (reading goes on)
            print(f"one per thread: {type(e).__name__}", file=sys.stderr, flush=True)

    def _labels_refreshed(self, aid, gids):
        """0.24.0: Gmail labels changed on these stored messages (the one-time refresh, Trash, deleted): their items
        whose every message is in Trash close, then the tidy rules re-run on the rest (one run, with Undo)."""
        gids = [str(g) for g in gids or []][:500]
        if not gids or not getattr(self, "messages", None):
            return
        marks = ",".join("?" * len(gids))
        items = sorted({r["item"] for r in self.messages._read(  # noqa: SLF001
            f"SELECT DISTINCT s.item FROM ledger_sources s JOIN messages m ON m.id=s.message WHERE m.source='gmail' "
            f"AND m.account=? AND m.msg_id IN ({marks})", (aid, *gids))})
        filters_mod.forget(self.messages)
        if not items:
            return
        now = self.clock()
        self.messages.write(lambda db: filters_mod.close_trashed(db, now, items))
        if getattr(self, "tidy", None) is not None:
            self.tidy.run(how="labels", only=items)

    def _sync_cals(self, recheck=False):
        """0.23.0: Setup's calendar choices copied into messages.db (need.calendar_use); a change re-checks clashes."""
        if not getattr(self, "messages", None):
            return False
        accounts = self.store.accounts()
        mine = set()
        try:                                   # 0.24.0: his identity in People (send-as addresses) as well
            with self.store.lock:
                mine = {str(r[0]).lower() for r in self.store.db.execute(
                    "SELECT value FROM person_handles WHERE person='me' AND kind='email'")}
        except Exception:  # noqa: BLE001 (the accounts' addresses still count)
            mine = set()
        changed = self.messages.write(lambda db: need_mod.sync_calendars(db, accounts, mine))
        if changed and recheck and getattr(self, "today", None):
            self.today.recheck()
        return changed

    def delete_account(self, _body, account_id):
        acc = self.store.delete_account(account_id)
        google.revoke(acc.get("refresh_token") or acc.get("access_token"))
        self.contacts.forget_gone_accounts()   # 0.21.0: its contacts go with it
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

    # --- the library: Find, the documents' settings and the Drive grant (library.py, live.DocsReader) --------------
    # 0.21.0: messages.db is the one store; index.db (0.6.0's index and its scanner) is retired and left on disk.
    def _index_routes(self):
        return [("GET", r"index/status", self.index_status),
                ("GET", r"index/settings", self.get_index_settings),
                ("PATCH", r"index/settings", self.patch_index_settings),
                ("GET", r"index/search", self.index_search),
                ("GET", r"find", self.find),   # 0.24.0 (B2): Find's people, items and messages
                ("GET", r"index/item/(\d{1,12})", self.index_item),
                ("POST", r"index/drive/check", self.index_drive_check),
                ("POST", r"index/drive/start", self.index_drive_start),
                ("POST", r"index/drive/finish", self.index_drive_finish)]

    def _library(self):
        if self.index is None:
            raise Conflict(self.index_error or "The message store isn't available.")
        return self.index

    def _index_sources(self):
        """What Know me shows per document source: on/off, a short detail, Drive's grant per account, the folder."""
        accounts = self.store.accounts()
        st = self.index.settings()
        g = index_sources.GoogleAccess(self.store, self.index, self.clock)
        drive = [{"account_id": a["id"], "email": a["email"], "granted": g.cached_drive(a["id"])} for a in accounts]
        vault = {"path": st["vault"], "ok": False, "error": None}
        if st["vault"]:
            try:
                index_sources.check_vault(st["vault"])
                vault["ok"] = True
            except InputError as e:
                vault["error"] = str(e)
        return [
            {"key": "drive", "label": "Google Drive", "on": st["sources"]["drive"],
             "available": any(d["granted"] for d in drive), "accounts": drive,
             "detail": "Read-only" if any(d["granted"] for d in drive) else "Needs a read-only permission"},
            {"key": "notes", "label": "Notes", "on": st["sources"]["notes"], "available": vault["ok"],
             "vault": vault, "detail": st["vault"] or "Add the notes folder (under /share or /media)"},
        ]

    def index_status(self, _body=None):
        if self.index is None:
            return {"available": False, "error": self.index_error}
        return {"available": True, "error": None, "stats": self.index.stats(), "sources": self._index_sources(),
                "settings": self.index.settings()}

    def get_index_settings(self, _body=None):
        return self._library().settings()

    def patch_index_settings(self, body):
        return self._library().update_settings(_obj(body))

    def _index_day(self, val, what):
        if val in (None, ""):
            return None
        try:
            d = dt.date.fromisoformat(str(val)[:10])
        except ValueError:
            raise InputError(f"{what} must be a date like 2026-10-30.") from None
        return rules.day_bounds(d, rules.tzinfo(self.store.settings()))[0].timestamp()

    def index_search(self, body=None):
        """GET api/index/search?q=&source=&account=&area=&people=&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=&offset=
        (Find: every source in messages.db)."""
        lib = self._library()
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
        if source and any(x not in library_mod.SOURCES for x in source.split(",")):
            raise InputError("Unknown source.")
        rows = lib.search(q.get("q") or "", source=source, account=q.get("account") or None,
                          area=q.get("area") or None, people=q.get("people") or None, since=since,
                          until=until, limit=limit, offset=offset)
        return {"q": q.get("q") or "", "results": rows}

    def find(self, body=None):
        """GET api/find?q= (0.24.0, B2; secret shopper: Find gave only "Ask: …" while the message store had hits):
        people, open items and messages for Find, each with what its row opens (the People page, the item's view,
        the message, or the item that message made). Read-only; a part that can't be read is left empty."""
        q = (body or {}).get("q") if isinstance(body, dict) else ""
        q = " ".join(str(q or "").split())[:120]
        out = {"q": q, "people": [], "items": [], "messages": []}
        if len(q) < 2:
            return out
        try:
            out["people"] = self.people.find(q)
        except Exception:  # noqa: BLE001 - Find shows what it can
            pass
        if self.today is not None:
            try:
                out["items"] = self.today.find(q)
            except Exception:  # noqa: BLE001
                pass
        if self.index is not None:
            try:
                rows = self.index.search(q, source=",".join(library_mod.MESSAGE_SOURCES), limit=8)
            except Exception:  # noqa: BLE001
                rows = []
            made = {}
            mids = [int(r["id"]) - msgstore.STORE_ID_BASE for r in rows
                    if msgstore.STORE_ID_BASE <= int(r["id"]) < msgstore.EVENT_ID_BASE]
            if mids and self.messages is not None:
                for m in self.messages._read(  # noqa: SLF001
                        "SELECT s.message, s.item FROM ledger_sources s JOIN ledger_items i ON i.id=s.item WHERE "
                        f"s.message IN ({','.join('?' * len(mids))}) AND i.merged_into IS NULL ORDER BY s.at",
                        mids):
                    made.setdefault(m["message"] + msgstore.STORE_ID_BASE, m["item"])
            seen = set()
            for r in rows:
                if r.get("relaxed"):
                    continue   # Find shows rows with every word only
                key = (str(r.get("title") or "").casefold(), str(r.get("people") or "").casefold())
                if key in seen:
                    continue
                seen.add(key)
                who = people_mod._split(r.get("people"))   # noqa: SLF001
                out["messages"].append({"id": int(r["id"]), "title": str(r.get("title") or "(no subject)")[:120],
                                        "who": (who[0][0] or "") if who else "", "date": str(r.get("date") or "")[:10],
                                        "source": r.get("source") or "", "item": made.get(int(r["id"]))})
                if len(out["messages"]) >= 5:
                    break
        return out

    def index_item(self, _body, item_id):
        item = self._library().get(item_id)
        if item is None:
            raise NotFound("That item isn't in the message store.")
        return item

    def index_drive_check(self, _body):
        """Asks Google (through a token refresh) which scopes each account granted; returns the Drive state."""
        g = index_sources.GoogleAccess(self.store, self._library(), self.clock)
        return {"accounts": [{"account_id": a["id"], "email": a["email"], "granted": g.has_drive(a["id"], force=True)}
                             for a in self.store.accounts()]}

    def index_drive_start(self, body):
        """The incremental grant: Google's page asks only for Drive (read-only) on top of what the account gave."""
        self._library()
        acc_id = _obj(body).get("account_id")
        if not isinstance(acc_id, str):
            raise InputError("Pick the account to give Drive access.")
        acc = self.store.account(acc_id)
        return self.flows.start(acc["area_id"], extra_scopes=(google.DRIVE_SCOPE,), login_hint=acc["email"])

    def index_drive_finish(self, body):
        lib = self._library()
        body = _obj(body)
        acc = self.flows.finish(body.get("flow_id"), body.get("pasted"))
        granted = index_sources.GoogleAccess(self.store, lib, self.clock).has_drive(acc["id"], force=True)
        return {"account": acc, "granted": granted}

    # --- 0.6.0 profile: the drafted profile and Callsheet's questions (profile.py) --------------------------------
    # The draft, the AI's suggestions and the questions live in messages.db's meta (0.21.0; index.db's before, imported
    # once by library.import_index); only the owner's saves and answers
    # (these routes, the owner's clicks) write memory.md. docs/know-me-profile.md.
    def _profile_init(self):
        self.profile = None
        if self.index is not None:
            self.profile = know_profile.Profile(self.store, self.index, self.memory, self._near_call,
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
            raise Conflict(self.index_error or "The message store isn't available.")
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
    def _messages_init(self, data_dir):
        """0.15.0: messages.db (kept, in backups) and the live readers. The readers' threads start in server.main
        (not in tests); Signal's envelopes reach the store through signalcli's on_envelope hook."""
        self.messages = self.live = self.index = self.index_error = None
        try:
            self.messages = msgstore.MessageStore(data_dir, clock=self.clock)
        except msgstore.StoreError as e:
            print(f"Callsheet messages: {e}", file=sys.stderr, flush=True)
            self.index_error = str(e)
            return
        self.messages.owner_names = lambda: background.owner_names(self.store, getattr(self, "people", None))
        # 0.21.0: the library over messages.db is what Chat, Find, Know me, Projects, People and the connectors read
        # (`self.index` keeps its old name); index.db's documents and Know me state come over once
        self.index = library_mod.Library(self.messages, self.store, clock=self.clock)
        try:
            self.index.import_index(data_dir)
        except Exception as e:  # noqa: BLE001 (it runs again at the next start; index.db is only read)
            print(f"Callsheet library: index.db import failed ({type(e).__name__})", file=sys.stderr, flush=True)
        threading.Thread(target=self._index_backfill, args=(data_dir,), name="callsheet-doc-flags", daemon=True).start()
        self.live = live_mod.Live(self.store, self.messages, signal=self.signal.service,
                                  clock=self.clock, lib=self.index)
        self.signal.service.on_envelope = self.live.signal_ingest.on_envelope

    def _index_backfill(self, data_dir):
        """index.db's older messages, once (library.import_index_messages: every message is kept), then the
        planted-instruction flags for documents checked by older rules (reads compute them until then)."""
        try:
            self.index.import_index_messages(data_dir)
        except Exception as e:  # noqa: BLE001 (it runs again at the next start; index.db is only read)
            print(f"Callsheet library: index.db's messages not imported yet ({type(e).__name__})", file=sys.stderr,
                  flush=True)
        try:
            n = self.index.backfill_instructions(batch=500, pause=0.05)
        except Exception as e:  # noqa: BLE001 (closed at shutdown: reads still compute flags)
            print(f"Callsheet library: instruction flags not stored yet ({type(e).__name__})", file=sys.stderr,
                  flush=True)
            return
        if n:
            print(f"Callsheet library: checked {n} documents for planted instructions", file=sys.stderr, flush=True)

    def _ledger_init(self):
        """0.16.0: the ledger tables in messages.db, the extractor (one message per request on the background model and
        slot) and the clash alerts. The reader's thread starts in server.main (not in tests)."""
        self.ledger = self.extractor = self.ledger_runner = self.prefs = self.today = self.brief2 = self.heads = None
        self.replies = None
        self.calwrite = None
        self.rsvp = self.agenda = self.suggest = None
        if not self.messages:
            return
        cache = {"at": -1e9, "dir": None, "vip": None}

        def tz_name():
            return getattr(rules.tzinfo(self.store.settings()), "key", "UTC")

        def owner():
            try:
                names = background.owner_names(self.store, self.people)
            except Exception:  # noqa: BLE001 (no About you yet)
                names = []
            return (names[0] if names else "") or "the owner"

        def fresh():
            if self.clock() - cache["at"] > 60:
                cache["at"] = self.clock()
                try:
                    cache["dir"] = self.people.directory()
                    cache["vip"] = people_mod.vip_map(self.store, self.people)
                except Exception:  # noqa: BLE001 (people not ready: handles stand for people)
                    cache["dir"] = cache["vip"] = None
            return cache

        def person(source, handle, name=""):
            # 0.21.0 (contacts.py): contacts first, then People rows, then the normalised number or address
            got = None
            if getattr(self, "contacts", None) is not None:
                try:
                    got = self.contacts.resolve(source, handle, name)
                except Exception:  # noqa: BLE001 (contacts not ready: People and handles as before)
                    got = None
            kind = {"gmail": "email", "slack": "slack", "signal": "signal"}.get(source)
            d = fresh()["dir"]
            h = str(handle or "")
            p = d.person_of(kind, h) if d is not None and kind else None
            if p is None and d is not None and kind == "email" and h.lower() != h:
                p = d.person_of(kind, h.lower())
            if p and p.get("id") is not None:
                # 0.21.4: read_tier and the promo rules (a suggested person isn't known). 0.22.1: also when contacts
                # resolved the sender to the same People row, as they do first on every install ("person:p…")
                person.states[f"person:{p['id']}"] = p.get("state") or ""
            if got:
                return got
            if p and p.get("id") is not None:
                return f"person:{p['id']}", p.get("name") or ""
            return h.lower(), ""
        person.takes_name = True
        person.states = {}

        def vip(source, handle):
            v = fresh()["vip"]
            h = str(handle or "").lower()
            if v is None:
                return False
            return h in v if source == "gmail" else h in getattr(v, source, frozenset())

        self.ledger = ledger_mod.Ledger(self.messages, tz_name, self.clock)
        # 0.17.0: the owner's rules and ledger actions from chat cards (prefs.py; quiet hours, VIPs through quiet hours
        # and alerts on/off are the push settings themselves)
        self.prefs = prefs_mod.Prefs(self.messages, push_get=lambda: push.config_from(self.store.push_config()),
                                     push_set=self.store.update_push, clock=self.clock,
                                     settings_get=self.store.settings, settings_set=self.store.update_settings)
        alerter = conflicts_mod.Alerter(self.messages, self.ledger, lambda: push.config_from(self.store.push_config()),
                                        self.notifier._send, push._payload, vip=vip, clock=self.clock,  # noqa: SLF001
                                        prefs=self.prefs, names=lambda s, h: [person(s, h)[1]])
        self.ledger.snoozed = self.prefs.snoozed
        self.ledger.vip_of = alerter.needs_you_vip
        self.prefs.areas = self._areas()   # 0.22.0: Chat's area cards (prefs.AREA_KINDS)
        self.prefs.vip_store = self.store   # 0.22.0 VIPs in one store: Chat's VIPs go on Setup's list
        try:
            if self.prefs.migrate_vips():   # VIPs earlier versions kept as Chat rules move onto the list, once
                print("Callsheet: VIPs set in Chat moved onto Setup's VIP list", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001 (the rules stay; it tries again at the next start)
            print(f"Callsheet: moving Chat's VIPs onto the list failed ({type(e).__name__})", file=sys.stderr, flush=True)
        self.ledger.cleanup_rule = lambda handle, names, text: self.prefs.rules().cleanup_rule(handle, names, text)
        self.extractor = extract_mod.Extractor(self.messages, tz_name, owner, None,
                                               model=lambda: self.background.settings()["model"], clock=self.clock,
                                               person=person, alerter=alerter,
                                               log=lambda line: print(line, file=sys.stderr, flush=True),
                                               date_order=lambda: background.date_order(self.store))
        self.ledger_runner = extract_mod.Runner(self.extractor, self.background, alerter, clock=self.clock,
                                                log=lambda line: print(line, file=sys.stderr, flush=True))
        self.extractor.call = self.ledger_runner.call
        conns = getattr(self, "connectors", None)
        self.today = today_mod.Today(self.messages, self.ledger, self.clock, alerter=alerter,
                                     reader=lambda: {"backlog": self.extractor.backlog()},
                                     log=lambda line: print(line, file=sys.stderr, flush=True),
                                     feeds=(lambda: {"feeds": conns.brief_list(), "reports": conns.reports_list()})
                                     if conns is not None else None)
        self.ledger_runner.hooks.append(self.today.tick)          # 0.18.0: Today's reminders
        self.ledger_runner.hooks.append(self._one_per_thread)     # 0.24.0: one conversation, one item; Trash closes
        # 0.21.4 Tidy up (tidy.py): confirmed People only count as known (never suggested ones)
        # 0.22.1: the read-time rules use the same Known as Tidy up, so new mail is judged as the owner's Tidy up judged
        self.tidy = tidy_mod.Tidy(self.messages, self.clock, known=self._tidy_known,
                                  owner_names=lambda: background.owner_names(self.store, getattr(self, "people", None)))
        self.extractor.known_load = self._tidy_known
        self.today.tidy = self.tidy
        gm = (getattr(getattr(self, "live", None), "readers", None) or {}).get("gmail")
        if gm is not None:
            gm.on_refreshed = self._labels_refreshed   # 0.24.0: the label refresh and Trash re-run the rules
        self.prefs.on_need = self.today.recheck     # 0.23.0: an answer (a press, Chat, Undo) re-checks the clashes
        try:
            self._sync_cals()                  # 0.23.0: the calendar choices the ledger reads (need.py)
        except Exception as e:  # noqa: BLE001
            print(f"calendar choices: {type(e).__name__}", file=sys.stderr, flush=True)
        # 0.22.0 (owner approved 2026-11-16): the action registry and its journal (actions.py), shared with Chat
        self.actions = actions_mod.Actions(self.messages, self.clock, today=self.today, prefs=self.prefs,
                                           tidy=self.tidy, store=self.store, oldata=getattr(self, "oldata", None),
                                           tz=self.ledger.tz)
        if getattr(self, "chat", None) is not None:
            self.chat.actions = self.actions
        # 0.21.0 Add to calendar (approved 2026-11-11): the one write, only on the owner's press (calwrite.py)
        self.calwrite = calwrite_mod.CalWrite(self.store, self.messages, tz_name, clock=self.clock)
        self.today.cal = self.calwrite
        self.today.contacts = getattr(self, "contacts", None)   # 0.21.0: birthdays in Coming up
        self.today.areas = self._areas()   # 0.22.0: each row's area (its colour on Today, Replies and the Agenda)
        # 0.21.0: security alerts, invites to answer and Reports into the store, by rule, model or not (heads.py)
        self.heads = heads_mod.Heads(self.messages, self.ledger.tz, self.clock, alerter=alerter,
                                     reports=conns.report_items if conns is not None else None,
                                     log=lambda line: print(line, file=sys.stderr, flush=True))
        self.ledger_runner.hooks.insert(0, self.heads.tick)
        # 0.21.0: Answer invites (only the owner's press on one invite) and the Agenda; suggested projects, Drive files
        self.rsvp = rsvp_mod.Rsvp(self.messages, self.calwrite, clock=self.clock, tz=self.ledger.tz)
        self.agenda = agenda_mod.Agenda(self.messages, self.ledger, self.today, self.clock, cal=self.calwrite,
                                        rsvp=self.rsvp)
        self.suggest = suggest_mod.Suggest(self.messages, self.projects, clock=self.clock) \
            if getattr(self, "projects", None) is not None else None

        def brief_call(body, stream=None):  # 0.21.0: one step of the talk-through on the background slot (Chat preempts it)
            body["id_slot"] = background.slot_for(body.get("model"))
            left = getattr(stream, "left", None)   # 0.24.0 (B1): the first word, too, comes within the step's time
            timeout = max(1.0, min(brief2_mod.STEP_TIMEOUT_S, left())) if left else brief2_mod.STEP_TIMEOUT_S
            return self.background.post_bg(body, timeout, kind="brief", units=1, stream=stream)
        self.brief2 = brief2_mod.Brief(self.messages, self.ledger, self.today, self.prefs, clock=self.clock,
                                       call=brief_call, model=lambda: self.background.settings()["model"],
                                       owner=lambda: background.owner_names(self.store, getattr(self, "people", None)),
                                       log=lambda line: print(line, file=sys.stderr, flush=True),
                                       name=lambda: assistant.of(self.store.settings()))
        if getattr(self, "scheduler", None) is not None:
            self.scheduler.brief2 = self.brief2
        # 0.21.0 Draft a reply: on-demand, on Chat's model path (preempts background work); kept per ledger item
        self.replies = replydraft.ReplyDrafts(self.messages, self.today, self.store, chat=self.chat,
                                              gdrafts=self.gdrafts, notes=lambda: self.memory.entries(),
                                              owner=lambda: background.owner_names(self.store, getattr(self, "people", None)),
                                              clock=self.clock, log=lambda line: print(line, file=sys.stderr, flush=True))
        self.chat.replies = self.replies   # 0.24.0 (B1): Chat's "draft a reply to …" reads the thread as Draft a reply does

    def ledger_view(self, _body):
        """0.16.0 GET api/ledger: Now and next, Needs you, Coming up (30 days), Clean up, clashes, follow-ups."""
        if not self.ledger:
            raise Conflict("The message store isn't available.")
        out = self.ledger.view()
        out["reader"] = dict(self.ledger_runner.state, backlog=self.extractor.backlog())
        return out

    def ledger_item(self, _body, item_id):
        if not self.ledger:
            raise Conflict("The message store isn't available.")
        it = self.ledger.item(int(item_id))
        if it is None:
            raise NotFound("There's no such item.")
        return it

    # --- 0.18.0 Today (docs/today.md) ---------------------------------------------------------------------------------
    def _today(self):
        if not self.today:
            raise Conflict("The message store isn't available.")
        return self.today

    def _today_do(self, fn, *args):
        try:
            return fn(*args)
        except today_mod.Invalid as e:
            raise InputError(str(e)) from None

    def today_view(self, _body):
        v = self._today().view()
        if self.rsvp is not None:   # 0.21.0: Yes / No / Maybe on a calendar invite in Needs you
            for r in (v.get("needs_you") or {}).get("rows") or []:
                if r.get("invite"):
                    r["invite"]["rsvp"] = self.rsvp.state_of_item(self.today._item(r["id"]))  # noqa: SLF001
        return v

    def today_item(self, _body, item_id):
        v = self._today().item_view(int(item_id))
        if v is None:
            raise NotFound("There's no such item.")
        it = self.today._item(int(item_id)) if self.rsvp is not None else None  # noqa: SLF001
        v["rsvp"] = self.rsvp.state_of_item(it) if it and it.get("subtype") == "invite" else None  # 0.21.0
        if self.replies is not None:   # 0.21.0 Draft a reply: the ask's draft, and which Chat suggestion means it
            v["draft"] = self.replies.view(int(item_id))
            v["draft_sug"] = next((q for q in v.get("chat") or [] if q.startswith(DRAFT_SUG)), None) \
                if v["draft"] and v["draft"]["can"] else None
        return v

    def _replies(self):
        if not self.replies:
            raise Conflict("The message store isn't available.")
        return self.replies

    def today_draft(self, _body, item_id):
        """0.21.0 POST api/today/items/<id>/draft: write (again) a suggested reply to this ask; the page polls
        GET api/today/items/<id> while its draft says "writing"."""
        return {"draft": self._replies().start(int(item_id))}

    def today_gmail(self, body, item_id):
        """0.21.0 POST api/today/items/<id>/gmail {text, reply_all?}: the owner's Put in Gmail Drafts (gdrafts.py)."""
        return self._replies().to_gmail(int(item_id), body)

    # --- 0.21.0 Add to calendar (calwrite.py; approved 2026-11-11) ---------------------------------------------
    def _cal(self):
        if not self.calwrite:
            raise Conflict("The message store isn't available.")
        return self.calwrite

    def _cal_account(self, body):
        acc_id = _obj(body).get("account_id")
        if not isinstance(acc_id, str):
            raise InputError("Pick the account.")
        return self.store.account(acc_id)

    def cal_access(self, _body=None):
        """GET api/calendar/access: per Google account, whether it allows adding events and its default calendar."""
        c = self._cal()
        return {"accounts": c.accounts(), "can_add": c.can_add()}

    def cal_access_check(self, body):
        """Asks Google (a token refresh) whether each account (or the one named) gave calendar.events."""
        c, b = self._cal(), _obj(body)
        accs = [self._cal_account(b)] if b.get("account_id") is not None else self.store.accounts()
        out = []
        for a in accs:
            try:
                out.append(c.check(a["id"]))
            except Conflict:
                out.append(c.public(a))
        return {"accounts": out, "can_add": c.can_add()}

    def cal_access_start(self, body):
        """"Allow adding events": the incremental grant; Google's page asks only for calendar.events on top of what
        the account gave (the access it gave before stays as it is)."""
        self._cal()
        acc = self._cal_account(body)
        return self.flows.start(acc["area_id"], extra_scopes=(google.CAL_WRITE_SCOPE,), login_hint=acc["email"])

    def cal_access_finish(self, body):
        c, b = self._cal(), _obj(body)
        acc = self.flows.finish(b.get("flow_id"), b.get("pasted"))
        try:
            state = c.check(acc["id"])
        except Conflict:
            state = c.public(acc)
        return {"account": state, "granted": bool(state["granted"]), "can_add": c.can_add()}

    def cal_access_default(self, body):
        b = _obj(body)
        return {"account": self._cal().set_default(self._cal_account(b)["id"], b.get("calendar"))}

    def cal_add(self, body):
        """POST api/calendar/add {item, title, date, time, minutes, account_id, calendar}: the owner pressed Add on the
        confirm card. One event per item, ever (calwrite.CalWrite.add)."""
        b = _obj(body)
        bad = [k for k in b if k not in ("item", "title", "date", "time", "minutes", "account_id", "calendar")]
        if bad:
            raise InputError("Unknown field: " + bad[0])
        acc, cal = b.get("account_id"), b.get("calendar")
        if (acc is None) != (cal is None):
            raise InputError("Pick a calendar.")
        return self._cal().add(b.get("item"), b.get("title"), b.get("date"), b.get("time"), b.get("minutes", 60),
                               acc, cal, via="today")

    # --- 0.21.0 Agenda, Answer invites, suggested projects -------------------------------------------------------------
    def agenda_view(self, body):
        """GET api/agenda?view=day|week|month&date=YYYY-MM-DD[&sel=YYYY-MM-DD] (agenda.py)."""
        if not self.agenda:
            raise Conflict("The message store isn't available.")
        try:
            return self.agenda.view(body if isinstance(body, dict) else {})
        except agenda_mod.Invalid as e:
            raise InputError(str(e)) from None

    def agenda_rsvp(self, body):
        """POST api/agenda/rsvp {item | event, answer: yes|no|maybe, via?}: the owner pressed Yes, No or Maybe on one
        invite (approved 2026-11-13). The only caller of Rsvp.answer; the server requires X-Callsheet."""
        if not self.rsvp:
            raise Conflict("The message store isn't available.")
        return self.rsvp.answer(_obj(body))

    def _suggest(self):
        if not self.suggest:
            raise Conflict("The message store isn't available.")
        return self.suggest

    def projects_more(self, body):
        return self._suggest().view(body)

    def projects_sug_add(self, body, key):
        return self._suggest().add(key, body if isinstance(body, dict) else None)

    def projects_sug_not(self, body, key):
        return self._suggest().not_project(key, body)

    def today_clash(self, _body, cid):
        v = self._today().clash_view(int(cid))
        if v is None:
            raise NotFound("There's no such clash.")
        v["draft_sug"] = None   # 0.21.0: "Draft a reply to <name>" opens that ask and drafts it there
        q = next((q for q in v.get("chat") or [] if q.startswith(DRAFT_SUG)), None)
        if q and self.replies is not None:
            for i in v.get("items") or []:
                it = self.replies._item(i)  # noqa: SLF001
                side = self._today().item_view(i) if self.replies.eligible(it) else None
                if side and q == DRAFT_SUG + (side.get("threads") or [{}])[0].get("first", "\0"):
                    v["draft_sug"] = {"q": q, "item": i}
                    break
        return v

    def today_replies(self, _body):
        return self._today().replies()

    def today_cleanup(self, _body):
        return self._today().cleanup()

    # --- 0.21.4: Refresh and Tidy up (owner-pressed only) ---------------------------------------------------------
    def _confirmed_emails(self):
        """Known addresses for the rules: confirmed People's and (0.22.1) the owner's contacts' (the read-time rules
        already took a contact as known; Tidy up now does too, so it never moves what new mail would keep)."""
        try:
            rows = self.store.db.execute("SELECT h.value FROM person_handles h JOIN people p ON p.id=h.person WHERE "
                                         "p.state IN ('confirmed','me') AND h.kind='email'").fetchall()
        except Exception:  # noqa: BLE001 (no People yet: nobody counts as known)
            rows = []
        out = [r[0] for r in rows]
        try:
            out += list(self.contacts.index()["by_email"]) if getattr(self, "contacts", None) is not None else []
        except Exception:  # noqa: BLE001 (contacts not ready: People only)
            pass
        return out

    def _tidy_known(self):
        return tidy_mod.Known.load(self.messages, self._confirmed_emails())

    def today_refresh(self, _body):
        """POST api/today/refresh: rebuild Today and start a fresh day's brief from what is already read (no mail is
        read again; the brief is written in the background, as Talk it through does)."""
        brief = None
        if self.brief2 is not None:
            try:
                brief = self.brief2.start(call="talk", force=True)
            except Exception as e:  # noqa: BLE001 (Today still refreshes)
                print(f"Callsheet today: brief refresh failed ({type(e).__name__})", file=sys.stderr, flush=True)
        self._today().refreshed_at = self.clock()
        v = self.today_view(None)
        v["brief_run"] = brief
        return v

    def _tidy(self):
        if getattr(self, "tidy", None) is None:
            raise Conflict("The message store isn't available.")
        return self.tidy

    def today_tidy(self, body):
        """POST api/today/tidy: the owner's Tidy up: re-applies the 0.21.4 rules to items already read, one Undo."""
        how = "chat" if _obj(body).get("from") == "chat" else "today"
        return self._tidy().run(how=how)

    def today_tidy_undo(self, body):
        try:
            rid = int(_obj(body).get("run"))
        except (TypeError, ValueError):
            raise InputError("Which tidy-up?") from None
        return self._tidy().undo(rid)

    def today_gotit(self, body):
        """0.23.0 POST api/today/gotit {key}: Got it on one heads-up (hidden from Today and the brief; Undo)."""
        try:
            return self._today().gotit(_obj(body).get("key"))
        except today_mod.Invalid as e:
            raise InputError(str(e)) from None

    def today_gotit_undo(self, body):
        try:
            return self._today().gotit_undo(_obj(body).get("id"))
        except today_mod.Invalid as e:
            raise InputError(str(e)) from None

    def today_need(self, body):
        """0.23.0 POST api/today/need {event, answer: yes|no}: the owner's press on "Does this need you?". It teaches
        that organizer or calendar (a `need` rule, Undo in Setup › Your rules) and re-checks the clashes."""
        b = _obj(body)
        if not self.prefs:
            raise Conflict("The message store isn't available.")
        try:
            return self._today().need_answer(b.get("event"), b.get("answer"), self.prefs)
        except today_mod.Invalid as e:
            raise InputError(str(e)) from None

    def today_back(self, body):
        """0.24.0 POST api/today/back {item}: Move back to Needs you (a Clean up line that looks like a real ask)."""
        return self._today().move_back(_obj(body).get("item"))

    def today_sender(self, body):
        """0.24.0 POST api/today/sender {item, kind: never|promo}: Not for me, and teach the sender (one Undo)."""
        b = _obj(body)
        return self._today().sender_rule(b.get("item"), b.get("kind"))

    def today_sender_undo(self, body):
        return self._today().sender_rule_undo(_obj(body).get("rule"))

    def today_calendar(self, body):
        """0.24.0 POST api/today/calendar {account, calendar, use}: "From calendar X: change how it counts" on a
        calendar row, the same choice as Setup's (Mine / Ask me / Heads-up / Hide). Returns {use, was} for Undo."""
        b = _obj(body)
        acc, cal, use = b.get("account"), b.get("calendar"), b.get("use")
        if use not in need_mod.USES:
            raise InputError("Pick Mine, Ask me, Heads-up or Hide.")
        a = next((x for x in self.store.accounts() if x["id"] == acc), None)
        c = next((x for x in (a or {}).get("calendars") or [] if x.get("id") == cal), None)
        if c is None:
            raise InputError("There's no such calendar.")
        was = c.get("use") or need_mod.default_use(c)
        self.patch_account({"calendar_use": {cal: use}}, acc)
        return {"account": acc, "calendar": cal, "use": use, "was": was, "name": c.get("summary") or cal}

    def today_tidy_later(self, _body):
        """The banner's Not now: the offer doesn't come back (Tidy up stays in Chat)."""
        self._tidy().dismiss()
        return {"ok": True}

    def today_done(self, body):
        return self._today_do(self._today().done, _obj(body).get("items"))

    def today_undone(self, body):
        return self._today_do(self._today().undo, _obj(body).get("items"))

    def _acts(self):
        if getattr(self, "actions", None) is None:
            raise Conflict("The message store isn't available.")
        return self.actions

    def _act_do(self, fn, *args):
        try:
            return fn(*args)
        except (actions_mod.Invalid, today_mod.Invalid) as e:
            raise InputError(str(e)) from None

    def today_dismiss(self, body):
        """0.22.0: Dismiss goes through the registry (journaled, with Undo) and says how many related clashes are
        open (the page offers "Also dismiss N related clashes")."""
        cid = _obj(body).get("clash")
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise InputError("Send the clash id as a number.")
        e = self._act_do(self._acts().run, "clash.dismiss", [cid], {}, "today")
        return {"dismissed": cid, "log": e["id"], "label": e["label"], "related": self._acts().related(clash=cid)}

    def today_related(self, body):
        """0.22.0: the open clashes related to items the owner just finished or filed, or to a clash."""
        b = _obj(body)
        items, clash = b.get("items") or [], b.get("clash")
        if not isinstance(items, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in items) or \
                (clash is not None and (isinstance(clash, bool) or not isinstance(clash, int))) or len(items) > 500:
            raise InputError("Send item ids or a clash id.")
        return {"related": self._acts().related(items=items, clash=clash)}

    def today_related_dismiss(self, body):
        """0.22.0 "Also dismiss N related clashes": one press, one journal row, one Undo for all."""
        ids = _obj(body).get("clashes")
        if not isinstance(ids, list) or not ids or any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
            raise InputError("Send the clash ids as a list of numbers.")
        e = self._act_do(self._acts().run, "clash.dismiss", ids, {}, "today")
        return {"dismissed": e["result"]["dismissed"], "log": e["id"], "label": e["label"]}

    def actions_recent(self, _body):
        """0.22.0 Setup › Your rules › Recent changes: labels only, newest first."""
        return {"changes": self._acts().recent()}

    def actions_undo(self, body):
        lid = _obj(body).get("id")
        e = self._act_do(self._acts().undo, lid, "setup")
        if e and e.get("ref") and getattr(self, "chat", None) is not None:
            self.chat.card_undone("log", e["id"], e["ref"])   # the Chat card says Undone too
        return {"ok": True, "change": e}

    def today_note(self, body):
        return self._today_do(self._today().note, _obj(body))

    def today_remind(self, body):
        return self._today_do(self._today().remind, _obj(body))

    def ledger_rejected(self, _body):
        """0.16.0: what the checks left out of the last readings (Setup › Advanced debug list), with the counts."""
        if not self.ledger:
            raise Conflict("The message store isn't available.")
        return {"reads": self.ledger.reads(), "rejected": self.ledger.rejected()}

    def visuals_schema(self, _body):
        """0.19.0 GET api/visuals/schema: every block type's JSON schema and the limits."""
        return visuals.catalogue()

    def visuals_preview(self, body):
        """0.19.0 POST api/visuals/preview {"block": {...}} or {"show": [...]}, "store": "sample"|"live" ->
        what visuals.js would draw: the filled blocks, what was dropped and why. Read-only; for tests and the gallery."""
        body = body if isinstance(body, dict) else {}
        show = body.get("show", body.get("block"))
        if show is None:
            raise InputError("Send a block (or a show list) to preview.")
        where = body.get("store", "sample")
        if where == "sample":
            facts = visuals_sample.facts()
        elif where == "live":
            if not self.ledger:
                raise Conflict("The message store isn't available.")
            facts = visuals.StoreFacts(self.messages, self.ledger)
        else:
            raise InputError('store is "sample" or "live".')
        return visuals.compose(show, facts)

    def visuals_gallery(self, _body):
        """0.19.0 GET api/visuals/gallery: every block type drawn from the sample store (Setup › Advanced › Visuals)."""
        return visuals_sample.gallery()

    def sources_status(self, _body):
        """0.15.0 Setup → Connections → Sources: each source's freshness."""
        if not self.live:
            raise Conflict("The message store isn't available.")
        out = self.live.status()
        row = self.contacts.sources_row()   # 0.21.0: "Contacts · 412 people · Synced 2 h ago"
        if row:
            out["sources"].append(row)
        return out

    def patch_sources_settings(self, body):
        if not self.live:
            raise Conflict("The message store isn't available.")
        body = body if isinstance(body, dict) else {}
        if set(body) - {"backfill_days"}:
            raise InputError("Only backfill_days can be changed here.")
        if "backfill_days" in body:
            v = body["backfill_days"]
            if isinstance(v, bool) or v not in msgstore.BACKFILL_CHOICES:
                raise InputError("Read back 7, 30, 90, 180 or 365 days.")
            self.messages.set_backfill_days(v)
        return self.live.status()

    # --- 0.16.1 texts (texts.py): the backup folder, the phone's Last notification sensor, the apps ---------------
    def _texts_out(self):
        # 0.21.2: no default folder; an empty folder means texts aren't set up, folder_hint is only a placeholder
        return {"settings": texts_mod.settings(self.messages), "folder_hint": texts_mod.FOLDER_HINT,
                "apps": [{"id": a, "name": n} for a, n in texts_mod.APPS], "available": ha.available()}

    def texts_view(self, _body=None):
        if not self.messages:
            raise Conflict("The message store isn't available.")
        return self._texts_out()

    def patch_texts(self, body):
        if not self.messages:
            raise Conflict("The message store isn't available.")
        texts_mod.update_settings(self.messages, _obj(body))
        return self._texts_out()

    def texts_sensors(self, _body=None):
        """The phones' Last notification sensors (asked from Home Assistant only when Setup opens the list)."""
        if not ha.available():
            return {"available": False, "sensors": [], "error": ha.NOT_IN_HA}
        try:
            return {"available": True, "sensors": texts_mod.sensors(), "error": None}
        except ha.HAError as e:
            raise UpstreamError(str(e)) from None

    def _chat_init(self, data_dir):
        # 0.21.0: Chat's index tools read the library (messages, events and documents in messages.db)
        self.chat = chat.Chat(self.store, self.index, syncer=self.syncer,
                              data_dir=data_dir, clock=self.clock)

    def _chat_routes(self):
        cid = f"({chat.CONV_ID})"
        return [("GET", r"chat", self.chat_overview),
                ("DELETE", r"chat", self.chat_clear),
                ("POST", r"chat/ask", self.chat_ask),
                ("POST", r"chat/apply", self.chat_apply),        # 0.17.0: the owner pressed a setting/action card
                ("POST", r"chat/undo", self.chat_undo),          # 0.17.0: Undo on a card (its one-time token)
                ("POST", r"chat/brief", self.chat_brief),        # 0.17.0: a brief reply (the day's Morning brief chat)
                ("GET", r"preferences", self.prefs_list),        # 0.17.0: See all rules (Setup › Your day)
                ("POST", r"preferences/(\d{1,9})/undo", self.prefs_undo),
                ("DELETE", r"preferences/(\d{1,9})", self.prefs_delete),
                ("POST", r"ledger/actions/(\d{1,9})/undo", self.action_undo),
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
        for key, kind in (("item_id", "item"), ("clash_id", "clash")):   # 0.18.0: Chat about a Today item or clash
            ref = body.get(key)
            if ref is None:
                continue
            if conv or scope is not None:
                raise InputError("A Today scope starts a new chat.")
            if isinstance(ref, bool) or not isinstance(ref, int):
                raise InputError(f"{key} must be a number.")
            scope = self._today().chat_scope(kind, ref)
            if scope is None:
                raise NotFound("There's no such item.")
        return self.chat.ask(conv or None, body.get("text"), scope=scope)

    # 0.17.0 configuring by chat ----------------------------------------------------------------------------------
    def _prefs(self):
        if not self.prefs:
            raise Conflict("The message store isn't available.")
        return self.prefs

    def chat_apply(self, body):
        self._prefs()
        return self.chat.card_apply(_obj(body))

    def chat_undo(self, body):
        self._prefs()
        return self.chat.card_undo(_obj(body))

    def chat_brief(self, body):
        return self.chat.brief_reply(_obj(body).get("text"))

    def _brief2(self):
        if not self.brief2:
            raise Conflict("The message store isn't available.")
        return self.brief2

    def talk(self, _body=None):
        """0.21.0 GET api/talk: today's talk-through (the steps written so far, pictures filled from the store now)
        and the day's Morning brief conversation (the replies)."""
        out = self._brief2().view()
        day = (out.get("run") or {}).get("day")
        try:
            cid = self.chat.brief_conversation(dt.date.fromisoformat(day) if day else None, create=False)
            out["conversation"] = self.chat.view(cid) if cid else None
        except Exception:  # noqa: BLE001 (no replies yet)
            out["conversation"] = None
        return out

    def talk_start(self, body):
        """0.21.0 POST api/talk/start: "Talk it through" (today's cached run when nothing changed)."""
        b = _obj(body)
        rid = self._brief2().start(call="talk", force=bool(b.get("fresh")))
        return self.talk()

    def talk_skip(self, body):
        """0.24.0 (B1) POST api/talk/skip {run, step}: stop writing that step (Callsheet's own sentence is shown)."""
        b = _obj(body)
        self._brief2().skip(b.get("run"), str(b.get("step") or ""))
        return self.talk()

    def talk_reply(self, body):
        """0.21.0 POST api/talk/reply {text, step, run}: a Chat turn on that step's items (the scope comes from the
        cached plan, not the page), saved in the day's Morning brief conversation."""
        b = _obj(body)
        scope = self._brief2().step_scope(b.get("run"), str(b.get("step") or ""))
        if scope is None:
            raise InputError("That step isn't in today's brief.")
        return self.chat.brief_reply(b.get("text"), step=scope)

    def prefs_list(self, _body=None):
        out = self._prefs().listing()
        titles = {}
        for r in out["rules"] + out["undone"] + out["actions"]:   # each rule's source chat, when it still exists
            cid = r.get("chat")
            if cid and cid not in titles:
                titles[cid] = self.chat.title_of(cid)
            r["chat_title"] = titles.get(cid) if cid else None
            if cid and not titles.get(cid):
                r["chat"] = None
        return out

    def _pref_end(self, fn, what, rid):
        try:
            r = fn(int(rid))
        except prefs_mod.Invalid as e:
            raise InputError(str(e)) from None
        self.chat.card_undone(what, int(rid), r.get("ref") if isinstance(r, dict) else None)
        return r

    def prefs_undo(self, _body, rid):
        r = self._pref_end(self._prefs().undo, "setting", rid)
        self._need_recheck(r)
        return {"ok": True, "rule": prefs_mod.Prefs.public(r)}

    def prefs_delete(self, _body, rid):
        r = self._pref_end(self._prefs().delete, "setting", rid)
        self._need_recheck(r)
        return {"ok": True, "rule": prefs_mod.Prefs.public(r)}

    def _need_recheck(self, r):
        """0.23.0: a "Does this need you?" answer undone or deleted: Prefs.on_need re-checks the clashes."""

    def action_undo(self, _body, rid):
        return {"ok": True, "action": prefs_mod.Prefs.public_action(self._pref_end(self._prefs().undo_action, "action", rid))}

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
