#!/usr/bin/env python3
"""Replay an acceptance-set cases.jsonl through the stage-2 extractor and score it (the acceptance set's README).

Extractors: `oracle` (a deterministic fake that answers each message with the case's own expectations: should score
100 %), `bad` (a deterministic poor fake: every incoming message becomes an undated ask), `live` (the local model
server: --base and --model; the key, if any, comes from the LOCAL_MODEL_KEY environment variable and is never
printed).

Each `isolated` case runs on a fresh ledger with its own calendar; all `global` cases run together on one ledger with
the set's calendar.json. Messages are fed in ts order (ties on mid), one at a time, with a simulated clock equal to the
message's ts; the ledger is snapshotted after each message. `facts_only` and `calendar` cases are counted but not
scored (they need the live 30-day backfill and the stage-4 Coming-up view).

Live runs honour the model server's quiet times (--quiet-times "01:00-04:00,12:00-13:00" in --tz; none by default):
by default they wait them out (--on-block stop: stop and write the partial summary). A server fault stops the run
with a partial summary.

--keep-db DIR keeps the replayed ledger (the global cases' one, else the last case's) as DIR/messages.db (mode 600,
never inside the repo) with DIR/replay.json {tz, owner, now: the last message's ts}, for tools/replay_brief.py.

Output: a JSON summary (stdout, and --out) that names the scorer version (SCORER_VERSION) and the set version (the
cases' `set_version`): compare models only on the same pair. --detail writes the per-expectation failures (mode 600, never inside the
repo); for the real set it holds ids and reasons only, no message words.

Mail and calendar are fed as live feeds them (msg_row, sender_flags, load_contacts, cal_rows; scorer 2.2): the
acceptance README's "Replay inputs" lists the fields.

--second-labels B.jsonl (scorer 2.4): a second annotator's labels for some cases (one line per case, the cases'
schema). Adds the `two_annotator` block (TwoAnn): required items (both labellings) for recall, optional items (one
labelling) neither missed nor false, false items outside the union, the same for clashes, confidence/section/time on
required items only (a time or section only when both labellings check it), and the agreement between the two
labellings. Works with every extractor, `raw` included.

Report only (no bar reads them; scorer 2.1 unchanged): `false.split` splits the false items into no-item cases (noise)
and cases with expectations (real threads), by the read tier of the message that made them (extract.read_tier), and
the extra clashes into those with a false item on one side, those between real items, and other; `tiers` gives the
expected items and asks found per read tier (sure and likely apart) and, per case with a narrow read, its routing and
its expected items found. A case not in `tiers.narrow_cases` was read in full.
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))

import extract as X  # noqa: E402
import ledger as L  # noqa: E402
import msgstore  # noqa: E402
import readrules as reader  # noqa: E402
import writer  # noqa: E402

SCORER_VERSION = "2.4"   # bump on any change to how a replay is scored; every model is scored with the same version
# 2.4 (the acceptance rules, 2026-11-13): two-annotator scoring (--second-labels B.jsonl, the same schema as the
# cases, one line per case). Items of the two labellings are paired by type (or overlapping types), who and `after`.
# REQUIRED = both have it (a miss counts against recall); OPTIONAL = only one has it (producing it is neither a miss
# nor a false item; counted for information); FALSE = a model item outside the union. Clashes the same way.
# Confidence, section and time are scored on required items only. Reported apart (`two_annotator`); every 2.3 score
# and bar is unchanged.
# 2.3 (0.22.x, the grouped trip clash): the owner's trip or stay of more than a day against the busy timed calendar entries
# during it is ONE clash that lists them (ledger_conflicts.entries). It is one clash item, matched against an
# expectation that lists the overlapped entry ids (`entries: [...]`, `b` optional); a pair expectation {a, b: cal:…}
# met by a group is read as entries [b]. A missing or extra entry inside the group makes that one clash wrong
# (`clash_groups` counts them), never a separate missed or extra clash. Otherwise 2.2's rules.
# 2.2 (0.22.x): the scoring rules are 2.1's; the replay now feeds the extractor what live mail feeds it (the sender's
# display name, the account, cc, Gmail's labelIds and category, bulk from the headers, the sets' automated / role
# sender flags, an optional contacts.json, the owner's RSVP and free/busy on calendar entries). A live model's prompt
# changes with them, so 2.1 and 2.2 results don't compare; the oracle's scores are the same on both.
SRC = {"mail": "gmail", "gmail": "gmail", "signal": "signal", "sms": "sms", "slack": "slack"}
KEY_ENV = "LOCAL_MODEL_KEY"
EMPTY_WHEN = {"phrase": "", "date": "", "end_date": "", "start": "", "end": "", "all_day": False, "approx": False,
              "tz": "", "options": []}


class Stop(Exception):
    pass


def ts_of(s):
    return dt.datetime.fromisoformat(s).timestamp()


STATUS_SAME = {"confirmed": "accepted", "scheduled": "accepted"}   # words some real cases use for "accepted"


def load_cases(path, only=()):
    cases = [json.loads(line) for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for c in cases:
        for x in list(c.get("expect") or []) + list(c.get("updates") or []):
            for k in ("status", "final_status"):
                if x.get(k) in STATUS_SAME:
                    x[k] = STATUS_SAME[x[k]]
    if only:
        cases = [c for c in cases if any(c["case"].startswith(p) for p in only)]
    return cases


# --- fixtures ------------------------------------------------------------------------------------------------------------
RSVP = {"needsaction": "needsAction", "needs_action": "needsAction", "accepted": "accepted", "yes": "accepted",
        "tentative": "tentative", "maybe": "tentative", "declined": "declined", "no": "declined"}


def cal_rows(events, tz):
    """A set's calendar events -> events rows, as live.event_record stores them: the owner's own RSVP (`rsvp` or
    `responseStatus`) becomes the owner's `self: true` attendee entry, and `transparency` (free: "transparent") and
    `event_type` pass through, so a tentative or free entry keeps that information."""
    out = []
    for e in events or []:
        atts = [a if isinstance(a, dict) else {"name": X._names_of(a), "address": str(a), "response": "",  # noqa: SLF001
                                                "self": False} for a in e.get("attendees") or []]
        rsvp = e.get("rsvp") or e.get("responseStatus") or ""
        if rsvp:
            rsvp = RSVP.get(str(rsvp).lower(), str(rsvp))
            atts = [a for a in atts if not a.get("self")] + [{"name": "", "address": "me", "response": rsvp,
                                                              "self": True}]
        tr = str(e.get("transparency") or ("transparent" if str(e.get("show_as") or "").lower() == "free" else ""))
        row = {"account": "replay", "calendar": e.get("calendar") or "primary", "event_id": e["id"],
               "title": e.get("title") or "", "status": e.get("status") or "confirmed",
               "attendees": atts, "all_day": bool(e.get("all_day")), "transparency": tr,
               "event_type": e.get("event_type") or ""}
        if e.get("all_day"):
            if e.get("start_date"):               # the synthetic set: inclusive end date
                sd, ed = e["start_date"], e.get("end_date") or e["start_date"]
            else:                                 # Google's shape: the end date is exclusive
                sd = str(e.get("start"))[:10]
                end = dt.date.fromisoformat(str(e.get("end") or sd)[:10])
                ed = str(max(dt.date.fromisoformat(sd), end - dt.timedelta(days=1)))
            row.update(start=sd, end=ed, start_ts=L.day_bounds(sd, tz)[0], end_ts=L.day_bounds(ed, tz)[1])
        else:
            row.update(start=e.get("start") or "", end=e.get("end") or "", start_ts=ts_of(e["start"]),
                       end_ts=ts_of(e["end"]) if e.get("end") else None)
        out.append(row)
    return out


GMAIL_CATEGORY = {"primary": "CATEGORY_PERSONAL", "personal": "CATEGORY_PERSONAL", "updates": "CATEGORY_UPDATES",
                  "promotions": "CATEGORY_PROMOTIONS", "social": "CATEGORY_SOCIAL", "forums": "CATEGORY_FORUMS"}


def _bulk(m):
    """messages.bulk as the Gmail reader stores it: the set's `bulk`, else msgstore.is_bulk on the message's
    List-Unsubscribe / Precedence headers when the set gives them."""
    if m.get("bulk") is not None:
        return bool(m["bulk"])
    return msgstore.is_bulk({"list-unsubscribe": m.get("list_unsubscribe") or "", "precedence": m.get("precedence") or ""})


def msg_row(m, owner, chat_people):
    """One set message -> the row the live reader would store (msgstore.normalize_message's fields). Mail: the
    sender's display name (`sender_name`), the account, to and cc recipients (kind "to" / "cc"), Gmail's labelIds
    (plus the category label `gmail_category` names) and `bulk`; Signal, Slack and texts: the chat kind as the label,
    as live."""
    out = m["direction"] == "out"
    snd = m.get("sender") or ""
    to = m.get("to")
    if to is None:                                 # the real set: a chat's other people, as seen so far
        to = ["owner"] if not out else sorted(chat_people.get(m.get("chat"), ()))
    src = SRC.get(m["source"], m["source"])
    recips = [{"name": owner if t == "owner" else X._names_of(t), "address": "me" if t == "owner" else t,  # noqa: SLF001
               "kind": k} for k, ts in (("to", to), ("cc", m.get("cc") or ())) for t in ts]
    if src == "gmail":
        labels = list(m.get("labels") or [])
        cat = GMAIL_CATEGORY.get(str(m.get("gmail_category") or "").lower())
        if cat and not any(str(x).startswith("CATEGORY_") for x in labels):
            labels.append(cat)
    else:
        labels = [m.get("chat_kind") or ""] + (["mentions_me"] if m.get("mentions_owner") else []) + \
            (["quotes_me"] if m.get("quotes_owner") else []) + list(m.get("labels") or [])
    return {"source": src, "account": m.get("account") or "replay", "msg_id": m["mid"],
            "thread": m.get("chat") or m["mid"], "direction": m["direction"],
            "sender": "me" if out or snd == "owner" else snd,
            "sender_name": owner if out else (m.get("sender_name") or X._names_of(snd)),  # noqa: SLF001
            "recipients": recips, "ts": ts_of(m["ts"]), "subject": m.get("subject") or "", "text": m.get("text") or "",
            "channel": m.get("channel") or "", "labels": labels, "bulk": _bulk(m)}


def sender_flags(m):
    """The per-message sender facts live reads off the real address and the set keeps as flags (the addresses are
    hashed): `automated_sender` (people.AUTOMATED_SENDER), `role_sender` (people.is_role) and `sender_domain_kind`.
    A set without them: sender `p:auto` is automated (and so a role address too)."""
    auto = m.get("automated_sender")
    if auto is None:
        auto = (m.get("sender") or "") == "p:auto" or None
    role = m.get("role_sender")
    if role is None and auto:
        role = True
    return {"automated_sender": auto, "role_sender": role, "sender_domain_kind": m.get("sender_domain_kind") or ""}


def load_contacts(path):
    """A set's optional contacts.json: the hashed handles of people in the owner's contacts, as
    {"known": [handle, ...]} or {"known": {handle: name}} (or a bare list). Empty when there is no file."""
    p = pathlib.Path(path) if path else None
    if not p or not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    known = data.get("known", []) if isinstance(data, dict) else data
    if isinstance(known, dict):
        return {str(k): str(v or "") for k, v in known.items()}
    return {str(k): "" for k in known or []}


def replay_tier(row, ctx, contacts=None):
    """The read tier in a replay (extract.read_tier) on what live sees: the row's `automated_sender` and `role_sender`
    flags (sender_flags; the sets hash the addresses) stand for the checks live runs on the real address; a set
    without flags names an automated sender `p:auto`. A sender in the set's contacts (load_contacts) takes the
    person resolver's known-person path ("person:" key), as a live contact does."""
    auto, role = row.get("automated_sender"), row.get("role_sender")
    if auto is None and row.get("source") == "gmail" and str(row.get("sender") or "") == "p:auto":
        auto = True
    if contacts and row.get("direction") == "in" and str(row.get("sender") or "") in contacts:
        ctx = dict(ctx, sender=("person:" + str(row["sender"]), ctx["sender"][1]))
    return X.read_tier(row, ctx, automated=None if auto is None else bool(auto),
                       role=None if role is None else bool(role))


# --- fakes -----------------------------------------------------------------------------------------------------------------
def _when_json(w):
    out = dict(EMPTY_WHEN, options=[])
    if not w:
        return out
    out["phrase"] = w.get("phrase") or ""
    out["approx"] = bool(w.get("approx"))
    if w.get("candidates"):
        for c in w["candidates"]:
            a, _, b = str(c).partition("/")
            s = dt.datetime.fromisoformat(a)
            opt = f"{s.date().isoformat()} {s:%H:%M}"
            if b:
                opt += f"-{dt.datetime.fromisoformat(b):%H:%M}"
            out["options"].append(opt)
    elif w.get("start"):
        s = dt.datetime.fromisoformat(w["start"])
        out.update(date=s.date().isoformat(), start=f"{s:%H:%M}")
        if w.get("end"):
            e = dt.datetime.fromisoformat(w["end"])
            out["end"] = f"{e:%H:%M}"
            if e.date() != s.date():
                out["end_date"] = e.date().isoformat()
    elif w.get("start_date"):
        out.update(date=w["start_date"], end_date=w.get("end_date") or "", all_day=True)
    elif w.get("time"):
        out["start"] = str(w["time"])[:5]
    return out


def _move_when(u, e):
    w = {}
    old = (e or {}).get("when") or {}
    if u.get("new_start"):
        w["start"] = u["new_start"]
        if u.get("new_end"):
            w["end"] = u["new_end"]
    elif u.get("new_start_date") or u.get("new_end_date"):
        w = {"start_date": u.get("new_start_date") or old.get("start_date"), "end_date": u.get("new_end_date"),
             "all_day": True}
    return _when_json(w) if w else dict(EMPTY_WHEN, options=[])


class Oracle:
    """Answers each message with exactly what the case expects at it (items, updates, follow-ups, a repeat for a
    same_item link). Clashes and the clash follow-ups are left to the deterministic code."""

    def __init__(self, cases, followups=True):
        self.at = collections.defaultdict(lambda: {"items": [], "updates": [], "fups": [], "dups": []})
        self.ids = {}
        self.emitted = {}
        self.pending = None
        self.msgs = {}
        self.exp = {e["eid"]: e for c in cases for e in c["expect"]}
        for c in cases:
            for m in c["messages"]:
                self.msgs[m["mid"]] = m
        group = set(self.msgs)
        for c in cases:
            mids = [m["mid"] for m in c["messages"]] + sorted(group - {m["mid"] for m in c["messages"]})
            exp = {e["eid"]: e for e in c["expect"]}
            for e in c["expect"]:
                if e["after"] in mids:
                    self.at[e["after"]]["items"].append(e)
            covered = set()
            for u in c["updates"]:
                self.at[u["after"]]["updates"].append(u)
                covered.add((u["eid"], u["status"]))
            for e in c["expect"]:
                own = [m["mid"] for m in c["messages"]]
                if e.get("final_status") and e["final_status"] != e["status"] and own and \
                        not any(k == e["eid"] and s == e["final_status"] for k, s in covered):
                    self.at[own[-1]]["updates"].append({"eid": e["eid"], "status": e["final_status"]})
            if followups:
                for f in c["follow_ups"]:
                    after = f.get("after") or (exp.get(f["eid"]) or {}).get("after")
                    if after:
                        self.at[after]["fups"].append((f, exp.get(f["eid"])))
            for lk in c["links"]:
                if lk.get("rel") == "same_item" and lk.get("a") in mids and lk.get("b") in mids:
                    self.at[lk["b"]]["dups"].append(lk["a"])

    def _who(self, e, m):
        w = e["who"]
        if w == "owner":
            return "owner"
        if m and m["direction"] == "in" and w == m.get("sender"):
            return "sender"
        return X._names_of(w)  # noqa: SLF001

    def _item(self, e, m):
        return {"type": _types(e)[0], "subtype": e.get("subtype") or "", "who": self._who(e, m), "what": e["what"],
                "for": e.get("for") or "", "when": _when_json(e.get("when")), "status": e["status"]
                if e["status"] in X.ITEM_STATUSES else "open", "says": X._names_of(e.get("attributed_to") or ""),  # noqa: SLF001
                "confidence": e.get("confidence") if e.get("confidence") in L.CONFIDENCE else "likely",
                "never_hide": bool(e.get("never_hide"))}

    def __call__(self, body, info):
        mid = info["msg_id"]
        m = self.msgs.get(mid)
        at = self.at.get(mid) or {"items": [], "updates": [], "fups": [], "dups": []}
        items, eids = [], []
        for e in at["items"]:
            items.append(self._item(e, m))
            eids.append(e["eid"])
        for a in at["dups"]:
            for eid, it in self.emitted.get(a, []):
                items.append(dict(it, who=it["who"] if it["who"] != "sender" else X._names_of(  # noqa: SLF001
                    self.msgs[a]["sender"])))
                eids.append(eid)
        self.emitted[mid] = list(zip(eids, items))
        ups = []
        for u in at["updates"]:
            ref = f"L{self.ids[u['eid']]}" if u["eid"] in self.ids else \
                f"N{eids.index(u['eid']) + 1}" if u["eid"] in eids else None
            if ref is None:
                continue
            st = u["status"]
            ups.append({"id": ref, "status": st if st in X.UPDATE_STATUSES else "changed",
                        "when": _move_when(u, self.exp.get(u["eid"])), "note": u.get("note") or "", "tell": "", "due": ""})
        for f, e in at["fups"]:
            ref = f"N{eids.index(f['eid']) + 1}" if f["eid"] in eids else \
                f"L{self.ids[f['eid']]}" if f["eid"] in self.ids else None
            if ref is None:
                continue
            tell = ""
            if e and e["who"] not in ("owner", "p:auto"):
                tell = X._names_of(e["who"])  # noqa: SLF001
            else:
                names = re.findall(r"\b([A-Z][a-z]+)\b", f.get("clears_on") or "")
                tell = names[-1] if names else ""
            ups.append({"id": ref, "status": "", "when": dict(EMPTY_WHEN, options=[]),
                        "note": f.get("reason") or "", "tell": tell or "someone", "due": f.get("due") or ""})
        self.pending = (mid, eids)
        return {"items": items, "updates": ups}

    def bind(self, mid, item_ids):
        if self.pending and self.pending[0] == mid:
            for eid, iid in zip(self.pending[1], item_ids):
                self.ids.setdefault(eid, iid)
        self.pending = None


class Bad:
    """A poor extractor: every incoming message becomes an undated ask from its sender; nothing else."""

    def __init__(self, cases):
        self.msgs = {m["mid"]: m for c in cases for m in c["messages"]}

    def __call__(self, body, info):
        m = self.msgs.get(info["msg_id"]) or {}
        if m.get("direction") != "in":
            return {"items": [], "updates": []}
        what = " ".join(str(m.get("text") or "").split()[:6]) or "message"
        return {"items": [{"type": "ask", "subtype": "", "who": "sender", "what": what, "for": "",
                           "when": dict(EMPTY_WHEN, options=[]), "status": "open", "says": "",
                           "confidence": "sure", "never_hide": False}], "updates": []}

    def bind(self, mid, item_ids):
        pass


# The clock and the wait guard() uses: the real ones in a live run; tests set a fake clock (now_in(tz) -> an aware
# datetime) so they never depend on the time of day.
now_in = dt.datetime.now
wait = time.sleep


def quiet_windows(arg):
    """--quiet-times "HH:MM-HH:MM,..." -> ((start, end), ...) (the model server's quiet times, local time in --tz)."""
    out = []
    for part in (arg or "").split(","):
        if part.strip():
            a, _, b = part.strip().partition("-")
            dt.time.fromisoformat(a.strip()), dt.time.fromisoformat(b.strip())   # refuses a malformed time
            out.append((a.strip(), b.strip()))
    return tuple(out)


def guard(args, log):
    """Wait out (or stop at) the model server's quiet times (--quiet-times), by the clock (now_in, the real one) in
    --tz."""
    windows = quiet_windows(getattr(args, "quiet_times", ""))
    if not windows:
        return
    tz = L.zone(args.tz)
    while True:
        now = now_in(tz)
        if not reader.in_windows(now.time(), windows) and reader.seconds_to_window(now, windows) > 180:
            return
        if args.on_block == "stop":
            raise Stop("blocked window")
        log("blocked window: waiting")
        wait(60)


EXTRA_BODY_KEEP = ("model", "messages", "id_slot")   # --extra-body never overrides these


def parse_extra_body(text):
    """--extra-body: a JSON object of model-specific request fields (e.g. {"thinking_budget_tokens": 0}) merged at the
    top level into every live request body. ValueError if it isn't an object or names model, messages or id_slot."""
    if not text:
        return {}
    try:
        extra = json.loads(text)
    except ValueError as e:
        raise ValueError(f"--extra-body is not JSON: {e}") from None
    if not isinstance(extra, dict):
        raise ValueError("--extra-body must be a JSON object")
    bad = sorted(k for k in extra if k in EXTRA_BODY_KEEP)
    if bad:
        raise ValueError(f"--extra-body may not set {', '.join(bad)}")
    return extra


def with_extra(body, extra):
    """The request body with --extra-body's fields added at the top level (model, messages and id_slot kept)."""
    if not extra:
        return body
    out = dict(body)
    out.update({k: v for k, v in extra.items() if k not in EXTRA_BODY_KEEP})
    return out


def live_caller(args, log):
    key = os.environ.get(KEY_ENV, "")
    extra = getattr(args, "extra_body_obj", None) or {}

    def call(body, info):
        guard(args, log)
        body = with_extra(body, extra)
        for attempt in range(3):
            try:
                res = writer._post(args.base, key, body, {"timeout": 180}, None, time.monotonic,  # noqa: SLF001
                                   budget=writer.INFLIGHT_MAX)
                return X.content_of(res)
            except (writer.ContextFull, writer.ServerFault):
                raise
            except writer.Busy:
                log(f"server busy (try {attempt + 1})")
                time.sleep(10)
        raise writer.Busy("busy")
    return call


def raw_keeper(call, path):
    """0.22.0 --raw-out: keep each answer of the model (one JSON line per request: the case's message id, the read
    tier, 0.22.x the prompt it was read with, and the content) so a run can be re-scored offline after a change to the
    code that reads it (--extractor raw)."""
    def keep(body, info):
        content = call(body, info)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"mid": info.get("msg_id"), "tier": info.get("tier"),
                                "prompt": info.get("prompt") or X.PROMPT_FULL, "content": content},
                               ensure_ascii=False) + "\n")
        return content
    return keep


def kept_prompt(r):
    """The prompt a kept answer was read with: its own "prompt" (0.22.x); before that, a narrow-tier answer was read
    with the retired narrow prompt, and every other answer (no tier: before the tiers) with the full prompt."""
    return r.get("prompt") or ("narrow" if r.get("tier") == X.TIER_NARROW else X.PROMPT_FULL)


class raw_caller:
    """--extractor raw: answer each message with the model's kept answer (the last one for that message). A message
    with no kept answer fails like a failed read. Item ids in the answers are the run's own (L12), so a change that
    makes or merges other items may point an update elsewhere: the re-score is close, not exact. 0.22.x --raw-ids
    (a map written by --ids-out from a replay of the code the answers were read with) makes it exact: each "L12" is
    turned into the id this replay gave the same item (its message, type and words), or "L0" when this code left it
    out. An answer kept from another prompt (kept_prompt: the retired narrow prompt) fails like a missing one; 0.22.x
    both tiers read with the full prompt, so a full answer re-scores a message of either tier (the narrowing is code)."""
    def __init__(self, path, ids=None):
        self.kept = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    self.kept[r["mid"]] = (kept_prompt(r), r["content"])
        self.ids = json.load(open(ids, encoding="utf-8")) if ids else None
        self.now = {}                     # (group, mid, type, what) -> this replay's id

    def bind_snap(self, gkey, snap):
        for it in snap["items"].values():
            self.now.setdefault((gkey, it["created_mid"], it["type"], it["what"]), it["id"])

    def _id(self, gkey, n):
        was = (self.ids["groups"].get(gkey) or {}).get(n)
        if was is None:
            return n
        return str(self.now.get((gkey, *was), 0))

    def __call__(self, body, info):
        if info.get("msg_id") not in self.kept:
            raise writer.WriterError("no kept answer")
        prompt, content = self.kept[info["msg_id"]]
        if prompt != (info.get("prompt") or X.PROMPT_FULL):
            raise writer.WriterError("the kept answer was read with another prompt")
        if self.ids is not None and isinstance(content, str):
            gkey = self.ids["group_of"].get(info["msg_id"])
            content = re.sub(r'("id"\s*:\s*"\s*[Ll])(\d+)', lambda m: m.group(1) + self._id(gkey, m.group(2)), content)
        return content


def ids_map(groups_snaps):
    """--ids-out: {"groups": {first mid: {id: [created mid, type, what]}}, "group_of": {mid: first mid}}."""
    out = {"groups": {}, "group_of": {}}
    for gkey, mids, end in groups_snaps:
        out["groups"][gkey] = {str(it["id"]): [it["created_mid"], it["type"], it["what"]]
                               for it in (end or {"items": {}})["items"].values()}
        out["group_of"].update({m: gkey for m in mids})
    return out


# --- the replay ----------------------------------------------------------------------------------------------------------
def snapshot(ms, lv, now, mid_of, ev_of):
    tz = lv.tz()
    src = collections.defaultdict(list)
    for r in ms._read("SELECT item, message, role FROM ledger_sources ORDER BY at"):  # noqa: SLF001
        src[r["item"]].append((mid_of.get(r["message"]), r["role"]))
    hist = collections.defaultdict(list)
    for r in ms._read("SELECT item, kind, status_to, message FROM ledger_history ORDER BY id"):  # noqa: SLF001
        hist[r["item"]].append((r["kind"], r["status_to"], mid_of.get(r["message"])))
    items = {}
    for it in lv.items():
        items[it["id"]] = {
            "id": it["id"], "type": it["type"], "subtype": it["subtype"], "who": it["who"], "who_name": it["who_name"],
            "what": it["what"], "purpose": it["purpose"], "status": it["status"], "section": L.section_of(it, now, tz),
            "start": L.iso(it["start_ts"], tz), "end": L.iso(it["end_ts"], tz), "start_ts": it["start_ts"],
            "end_ts": it["end_ts"], "all_day": bool(it["all_day"]), "start_date": it["start_date"],
            "end_date": it["end_date"], "time": it["time"], "date_resolved": bool(it["date_resolved"]),
            "candidates": [(c.get("start_ts"), c.get("end_ts")) for c in it["candidates"]],
            "mids": [m for m, _ in src[it["id"]]], "created_mid": next((m for m, r in src[it["id"]] if r == "created"),
                                                                       None),
            "cal_ref": ev_of.get(it["cal_ref"]), "parent": it["parent"], "predecessor": it["predecessor"],
            "merged_into": it["merged_into"], "never_hide": bool(it["never_hide"]), "hist": hist[it["id"]],
            "confidence": it.get("confidence")}
    confs = []
    for c in ms._read("SELECT * FROM ledger_conflicts"):  # noqa: SLF001
        side = [(k, ev_of.get(r) if k == "event" else r) for k, r in ((c["a_kind"], c["a_ref"]),
                                                                     (c["b_kind"], c["b_ref"]))]
        confs.append({"id": c["id"], "sides": side, "state": c["state"], "kind": c["kind"],
                      "confidence": c["confidence"], "created_mid": mid_of.get(c["message"]),
                      "entries": [ev_of.get(e) for e in L.group_entries(c)]})   # 2.3: a grouped trip clash
    fups = [{"id": f["id"], "item": f["item"], "state": f["state"], "tell": f["tell"]}
            for f in ms._read("SELECT * FROM ledger_followups")]  # noqa: SLF001
    return {"items": items, "conflicts": confs, "followups": fups}


def keep_db(ms, dest, tz, owner, now):
    """Copies the replayed ledger to dest/messages.db (a consistent sqlite backup) with dest/replay.json."""
    import sqlite3
    d = pathlib.Path(dest)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = d / msgstore.DB_NAME
    if out.exists():
        out.unlink()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    src, dst = sqlite3.connect(str(ms.path)), sqlite3.connect(str(out))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    (d / "replay.json").write_text(json.dumps({"tz": tz, "owner": owner, "now": now}) + "\n", encoding="utf-8")


def run_group(cases, calendar, call, fake, args, log, run, keep=None, tiers=None, contacts=None):
    tz = L.zone(args.tz)
    msgs = sorted({m["mid"]: m for c in cases for m in c["messages"]}.values(), key=lambda m: (ts_of(m["ts"]), m["mid"]))
    owner = next((c.get("owner") for c in cases if c.get("owner")), None) or args.owner
    snaps, lat = {}, {}
    sim = [ts_of(msgs[0]["ts"]) if msgs else time.time()]
    with tempfile.TemporaryDirectory(prefix="callsheet-replay-") as tmp:
        ms = msgstore.MessageStore(tmp, clock=lambda: sim[0])
        try:
            L.ensure(ms)
            ms.upsert_events(cal_rows(calendar, tz))
            ev_of = {r["id"]: r["event_id"] for r in ms._read("SELECT id, event_id FROM events")}  # noqa: SLF001
            ex = X.Extractor(ms, args.tz, owner, call, model=lambda: args.model or "replay-fake",
                             clock=lambda: sim[0], tier=lambda row, ctx: replay_tier(row, ctx, contacts))
            mid_of, chat_people, fails = {}, collections.defaultdict(set), 0
            for m in msgs:
                sim[0] = ts_of(m["ts"])
                ms.upsert_message(msg_row(m, owner, chat_people))
                if m["direction"] == "in" and m.get("sender"):
                    chat_people[m.get("chat")].add(m["sender"])
                row = dict(ms.message(SRC.get(m["source"], m["source"]), m.get("account") or "replay", m["mid"]),
                           **sender_flags(m))      # what live reads off the real address (the store keeps no flags)
                mid_of[row["id"]] = m["mid"]
                t0 = time.monotonic()
                try:
                    res = ex.read(row)
                    fails = 0
                    if res.get("failed"):
                        run["failed_reads"] += 1
                except writer.ServerFault as e:
                    run["stopped"] = f"server fault: {e}"
                    break
                except Stop as e:
                    run["stopped"] = str(e)
                    break
                except (writer.Busy, writer.WriterError) as e:
                    run["failed_reads"] += 1
                    fails += 1
                    log(f"read failed ({type(e).__name__})")
                    if fails >= 3:
                        run["stopped"] = f"three failures in a row ({type(e).__name__})"
                        break
                    res = {"items": [], "drops": []}
                if tiers is not None:          # the read tier of each message (report only)
                    tiers[m["mid"]] = (res.get("tier") or "unread", res.get("tier_why") or "")
                lat[m["mid"]] = time.monotonic() - t0
                run["messages"] += 1
                run["drops"] += len(res.get("drops") or [])
                fake.bind(m["mid"], res.get("items") or []) if fake else None
                snaps[m["mid"]] = snapshot(ms, ex.lv, sim[0], mid_of, ev_of)
                if hasattr(call, "bind_snap"):
                    call.bind_snap(msgs[0]["mid"], snaps[m["mid"]])
                if run["messages"] % 25 == 0:
                    log(f"{run['messages']} messages")
            run["requests"] += ex.stats["requests"]
            if keep:
                keep_db(ms, keep, args.tz, owner, sim[0])
                log(f"kept the ledger in {keep}")
        finally:
            ms.close()
    return msgs, snaps, lat


# --- scoring ---------------------------------------------------------------------------------------------------------------
METRICS = ("asks", "items", "times", "clashes", "clash_resolved", "updates", "follow_ups", "dedupe", "sections",
           "never_hide", "status_initial")


class Tally:
    def __init__(self):
        self.t = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.Counter()))
        self.lat = collections.defaultdict(lambda: collections.defaultdict(list))
        self.false = collections.Counter()
        self.fails = []
        # report only (no bar or score reads these): the false items and extra clashes split by where they come from,
        # and the read tiers (extract.read_tier) per case kind, with the expected items found per tier
        self.split = collections.Counter()
        self.leftover = set()             # this group's asks left open after the owner's agreement (score_case)
        self.reads = collections.defaultdict(collections.Counter)
        self.false_tier = collections.Counter()
        self.recall = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
        self.narrow_cases = {}
        self.out = {}                     # 2.4: (metric, ref) -> ok, and ("clash", case, i) -> ok (two_annotator)

    def rec(self, case, conf, metric, ok, why=None, ref=None, words=None):
        conf = "sure" if conf == "sure" else "likely"
        self.out[(metric, ref)] = ok
        for scope in ["all"] + list(case.get("patterns") or []):
            c = self.t[scope][conf]
            c[metric + ".n"] += 1
            c[metric + ".ok"] += 1 if ok else 0
        if not ok:
            self.fails.append({"case": case["case"], "metric": metric, "ref": ref, "confidence": conf, "why": why,
                               **({"words": words} if words else {})})

    def latency(self, case, conf, metric, s):
        conf = "sure" if conf == "sure" else "likely"
        for scope in ["all"] + list(case.get("patterns") or []):
            self.lat[scope][conf + "." + metric].append(s)


def _p95(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, math.ceil(0.95 * len(xs)) - 1)], 3)


def _first(name):
    return L.norm_who(name).replace("_", " ").split(" ")[0]


def _who_eq(it, who, people=(), autos=()):
    """README: who must be equal (owner, person or automated sender). Scorer 2: an automated sender (`p:auto`) is a
    name that is the sender of one of the case's automated messages, or the case's declared `auto_sender` (`autos`),
    never the owner or a person of the case; a person may carry a last name ("Kofi Mensah" for p:kofi) only when that
    first name is unique among the case's people."""
    w = L.norm_who(who)
    got = {g for g in (L.norm_who(it["who"]), L.norm_who(it["who_name"])) if g}
    if w in got:
        return True
    pn = {L.norm_who(p) for p in people}
    if w == "auto":
        return bool(got & set(autos)) and L.OWNER not in got and not got & pn
    if [_first(p) for p in people].count(w) != 1:
        return False
    return any(g.split(" ")[0] == w for g in got)


def _people(c):
    """The case's people: senders and recipients of its messages and the people its expectations name."""
    out = set()
    for m in c["messages"]:
        out |= {m.get("sender") or ""} | set(m.get("to") or ())
    for e in c["expect"]:
        out |= {e.get("who") or "", e.get("attributed_to") or ""}
    return {p for p in out if p and p not in ("owner", "p:auto")}


def _autos(c):
    """Scorer 2: the names `p:auto` may match in a case: the senders of its automated messages (sender p:auto, with
    the message's sender_name if the set gives one) and the case's declared automated sender (`auto_sender`, a name
    or a list)."""
    out = set()
    for m in c["messages"]:
        if L.norm_who(m.get("sender")) == "auto":
            out |= {L.norm_who(m.get("sender")), L.norm_who(m.get("sender_name"))}
    decl = c.get("auto_sender")
    out |= {L.norm_who(x) for x in ([decl] if isinstance(decl, str) else decl or [])}
    return {x for x in out if x}


def _auto_names(c):
    decl = c.get("auto_sender")
    return [str(x) for x in ([decl] if isinstance(decl, str) else decl or [])]


def set_errors(cases):
    """Scorer 2.1: mistakes in the set itself, which stop the run before anything is scored.
    - `auto_sender` is filled from the case's own messages, never from a model's output: each name must be the
      sender of one of the case's automated messages (sender p:auto: its sender_name, or the name its subject or text
      gives), and never the owner or a person of the case;
    - a `section` list goes with a `types` list of the same length (paired by position);
    - an update's `into` names another expectation of the case, and the update is an ask's `done`."""
    errs = []
    for c in cases:
        cid = c.get("case")
        autos = [m for m in c.get("messages") or [] if L.norm_who(m.get("sender")) == "auto"]
        people = {L.norm_who(p) for p in _people(c)} | {L.OWNER}
        for name in _auto_names(c):
            n = L.norm_who(name)
            word = re.compile(r"(?<!\w)" + re.escape(name.strip().lower()) + r"(?!\w)")
            if not autos:
                errs.append(f"{cid}: auto_sender {name!r} but the case has no automated message")
            elif not any(L.norm_who(m.get("sender_name")) == n or
                         word.search(f"{m.get('subject') or ''} {m.get('text') or ''}".lower()) for m in autos):
                errs.append(f"{cid}: auto_sender {name!r} is not the sender of any of its automated messages")
            if n in people or not n:
                errs.append(f"{cid}: auto_sender {name!r} is the owner or a person of the case")
        eids = {e["eid"]: e for e in c.get("expect") or []}
        for e in eids.values():
            secs = e.get("section")
            if isinstance(secs, (list, tuple)) and len(secs) > 1 and len(secs) != len(_types(e)):
                errs.append(f"{e['eid']}: {len(secs)} sections for {len(_types(e))} types (paired by position)")
        for u in c.get("updates") or []:
            if not u.get("into"):
                continue
            if u["into"] not in eids or u["into"] == u["eid"]:
                errs.append(f"{u['eid']}@{u.get('after')}: into {u['into']!r} is not another expectation of the case")
            if u.get("status") != "done" or "ask" not in _types(eids.get(u["eid"]) or {"type": ""}):
                errs.append(f"{u['eid']}@{u.get('after')}: into is only for an ask's done update")
    return errs


def _types(e):
    """Set 1.1: an expectation gives one `type`, or for genuinely ambiguous wording an allowed `types` list."""
    return list(e.get("types") or [e["type"]])


def _sections(e):
    """`section` is one section, or (with a `types` list) the allowed sections, one for each reading."""
    s = e.get("section")
    return list(s) if isinstance(s, (list, tuple)) else [s]


def _section_for(e, typ):
    """Scorer 2.1: a `section` list is paired with `types` by position, so an item counts only in the section of the
    type it matched as (an ask in the ask's section, an event in the event's); a type the list doesn't give has no
    section. One section applies to every type."""
    secs = _sections(e)
    if len(secs) == 1:
        return secs
    types = _types(e)
    return [secs[types.index(typ)]] if typ in types and len(secs) == len(types) else []


def _type_eq(it, e):
    """0.22.0: a security alert is one item (heads.py: subtype security, never hidden) whatever type the set gives it."""
    return it["type"] in _types(e) or bool(e.get("never_hide") and it["subtype"] == "security")


def _exp_span(w, tz):
    if not w:
        return None
    if w.get("candidates"):
        a, _, b = str(w["candidates"][0]).partition("/")
        return ts_of(a), ts_of(b) if b else ts_of(a) + 3600
    if w.get("start"):
        return ts_of(w["start"]), ts_of(w["end"]) if w.get("end") else ts_of(w["start"]) + 3600
    if w.get("start_date"):
        return L.day_bounds(w["start_date"], tz)[0], L.day_bounds(w.get("end_date") or w["start_date"], tz)[1]
    return None


def _item_span(it, tz):
    if it["all_day"] and it["start_date"]:
        return L.day_bounds(it["start_date"], tz)[0], L.day_bounds(it["end_date"] or it["start_date"], tz)[1]
    if it["start_ts"] is not None:
        return it["start_ts"], it["end_ts"] or it["start_ts"] + 3600
    return None


def _same_instant(a_iso, b_iso):
    if not a_iso or not b_iso:
        return False
    a, b = dt.datetime.fromisoformat(a_iso), dt.datetime.fromisoformat(b_iso)
    return a == b and a.utcoffset() == b.utcoffset()


def time_ok(w, it, tz):
    """(checked?, ok, why) by the README's wrong-times rules."""
    if not w or not any(w.get(k) for k in ("start", "start_date", "candidates", "time", "date")):
        return False, True, None
    if w.get("candidates"):
        mine = [(it["start_ts"], it["end_ts"])] + list(it["candidates"])
        for c in w["candidates"]:
            a, _, b = str(c).partition("/")
            if any(s is not None and abs(s - ts_of(a)) < 60 and (not b or e is None or abs(e - ts_of(b)) < 60)
                   for s, e in mine):
                return True, True, None
        return True, False, "no offered slot kept"
    if w.get("date_resolved") is False:
        ok = (not it["date_resolved"]) and (it["time"] or "")[:5] == str(w.get("time") or "")[:5]
        return True, ok, None if ok else "time-only: the time differs or a date was invented"
    if w.get("all_day") and w.get("start_date"):
        ok = it["all_day"] and it["start_date"] == w["start_date"] and (not w.get("end_date") or
                                                                       it["end_date"] == w["end_date"])
        return True, ok, None if ok else "all-day dates differ"
    if w.get("start"):
        if it["start_ts"] is None:
            return True, False, "no start time"
        if w.get("approx"):
            s = dt.datetime.fromisoformat(w["start"])
            mine = dt.datetime.fromtimestamp(it["start_ts"], tz)
            ok = abs(it["start_ts"] - s.timestamp()) <= 7200 and mine.date() == s.date()
            return True, ok, None if ok else "approximate start more than 2 h off or another date"
        ok = _same_instant(it["start"], w["start"]) and (not w.get("end") or _same_instant(it["end"], w["end"]))
        return True, ok, None if ok else "start or end not exact (or the offset is wrong)"
    return False, True, None


def chain(S, iid):
    """An item and the items it became or came from (merge, a yes that carried it on)."""
    if iid is None:
        return set()
    out, todo = set(), [iid]
    while todo:
        i = todo.pop()
        if i in out or i not in S["items"]:
            continue
        out.add(i)
        it = S["items"][i]
        for nxt in (it["merged_into"], it["predecessor"]):
            if nxt:
                todo.append(nxt)
        todo += [j for j, o in S["items"].items() if o["predecessor"] == i or o["merged_into"] == i]
    return out


def items_from(S, mid):
    out = set()
    for it in S["items"].values():
        if mid in it["mids"]:
            i = it["id"]
            while S["items"][i]["merged_into"]:
                i = S["items"][i]["merged_into"]
            out.add(i)
    return out


def match_case(c, snaps, tz, shared):
    """eid -> item id (or None) at each expectation's `after` snapshot. Each item matches one expectation, except
    expectations a dedupe link says are one item (`shared`)."""
    match, used, people, autos = {}, set(), _people(c), _autos(c)
    for e in c["expect"]:
        S = snaps.get(e["after"])
        if S is None:
            match[e["eid"]] = None
            continue
        cands = [it for it in S["items"].values() if it["merged_into"] is None and e["after"] in it["mids"] and
                 _type_eq(it, e) and _who_eq(it, e["who"], people, autos) and (it["id"] not in used or e["eid"] in shared)]
        if not cands:
            match[e["eid"]] = None
            continue
        es = _exp_span(e.get("when"), tz)

        def key(it):
            sp = _item_span(it, tz)
            over = bool(es and sp and es[0] < sp[1] and sp[0] < es[1])
            return (over, L.similar(it["what"] + " " + it["purpose"], e["what"] + " " + (e.get("for") or "")),
                    -it["id"])
        best = max(cands, key=key)
        match[e["eid"]] = best["id"]
        used.add(best["id"])
    return match


def shared_of(c):
    eids = {e["eid"] for e in c["expect"]}
    return {x for lk in c["links"] if lk.get("rel") in ("dedupe", "same_item") and lk.get("a") in eids and
            lk.get("b") in eids for x in (lk["a"], lk["b"])}


def section_ok(e, it, iid, end):
    """At the expectation's message, or (an answered or closed item the set marks by where it ends) at the end."""
    secs = _section_for(e, it["type"])
    if it["section"] in secs:
        return True
    if e.get("never_hide") and it["section"] == "security" and "none" not in _sections(e):
        return True                       # 0.22.0: a security alert's own section, never Clean up (README)
    if e.get("final_status") and end is not None and iid in end["items"]:
        j = iid
        while end["items"][j]["merged_into"]:
            j = end["items"][j]["merged_into"]
        fin = end["items"][j]
        return fin["section"] in (_section_for(e, fin["type"]) if len(_sections(e)) > 1 and fin["type"] in _types(e)
                                  else secs)
    return False


def _top(S, i):
    while S["items"][i]["merged_into"] and S["items"][i]["merged_into"] in S["items"]:
        i = S["items"][i]["merged_into"]
    return i


def resolved_into(S, aid, tid, mid=None):
    """Scorer 2.1 (`into` on an update): the ask item `aid` is done (now, or made done at `mid`), or merged into the
    item `tid` became (its chain); never a second item left beside it."""
    if aid is None or aid not in S["items"]:
        return False
    a = S["items"][aid]
    if a["merged_into"]:
        return tid is not None and _top(S, aid) in chain(S, tid)
    return a["status"] == "done" or any(to == "done" and m == mid for _k, to, m in a["hist"] if mid)


def leftover_ask(S, aid):
    """An ask still open (not merged, not done) after the owner's agreement: a false item (scorer 2.1)."""
    return aid is not None and aid in S["items"] and not S["items"][aid]["merged_into"] and \
        S["items"][aid]["status"] == "open"


def score_case(c, snaps, lat, end, tz, T, synthetic, gmatch=None, gexp=None):
    match = match_case(c, snaps, tz, shared_of(c))
    exp = {e["eid"]: e for e in c["expect"]}
    look = dict(gmatch or {}, **match)        # updates, clashes and follow-ups may name another case's expectation
    gexp = dict(gexp or {}, **exp)
    unj = bool(c.get("unjudged"))
    for e in c["expect"]:
        iid = match[e["eid"]]
        S = snaps.get(e["after"])
        conf = e.get("confidence")
        words = {"what": e["what"]} if synthetic else None
        if S is None:
            T.rec(c, conf, "items", False, "not run", e["eid"])
            if "ask" in _types(e):
                T.rec(c, conf, "asks", False, "not run", e["eid"])
            continue
        T.rec(c, conf, "items", iid is not None, "no matching item", e["eid"], words)
        if "ask" in _types(e):              # an ask, or a reading of ambiguous wording that may be one
            T.rec(c, conf, "asks", iid is not None, "missed ask", e["eid"], words)
            if iid is not None:
                T.latency(c, conf, "asks", lat.get(e["after"], 0))
        if iid is None:
            continue
        it = S["items"][iid]
        checked, ok, why = time_ok(e.get("when"), it, tz)
        if checked:
            T.rec(c, conf, "times", ok, why, e["eid"], dict(words or {}, got=it["start"] or it["start_date"] or it["time"])
                  if synthetic else None)
        T.rec(c, conf, "sections", section_ok(e, it, iid, end), f"section {it['section']}", e["eid"])
        first = it["status"] == e["status"] or any(to == e["status"] and m == e["after"] for _k, to, m in it["hist"])
        T.rec(c, conf, "status_initial", first, f"status {it['status']}", e["eid"])   # made and changed by one message
        if e.get("never_hide"):
            T.rec(c, conf, "never_hide", it["section"] != "none", "hidden", e["eid"])
        if _sections(e) == ["none"] and not section_ok(e, it, iid, end):
            T.false["section_none_shown"] += 1
        if e.get("not_in") and it["section"] == e["not_in"]:
            T.false["not_in"] += 1
    # updates and final statuses
    for u in c["updates"]:
        e = gexp.get(u["eid"], {})
        S = snaps.get(u["after"])
        iid = look.get(u["eid"])
        ok, why = False, "item missing"
        if S is not None and iid is not None and u.get("into"):
            ok = resolved_into(S, iid, look.get(u["into"]), u["after"])
            why = None if ok else ("ask left open after the owner's agreement" if leftover_ask(S, iid) else
                                   f"not done or merged into {u['into']}")
        elif S is not None and iid is not None:
            ids = chain(S, iid)
            st = u["status"]
            hits = [S["items"][i] for i in ids if S["items"][i]["status"] == st or
                    any(k == st or (st in L.HISTORY_ONLY and k in L.HISTORY_ONLY and m == u["after"]) or
                        (to == st and m == u["after"]) for k, to, m in S["items"][i]["hist"])]
            ok = bool(hits)
            why = None if ok else f"status {S['items'][iid]['status']}"
            if ok and u.get("new_start"):
                ok = any(_same_instant(h["start"], u["new_start"]) for h in hits)
                why = None if ok else "moved to another time"
            if ok and u.get("new_end_date"):
                ok = any(h["end_date"] == u["new_end_date"] for h in hits)
                why = None if ok else "moved to another date"
        T.rec(c, e.get("confidence"), "updates", ok, why, f"{u['eid']}@{u['after']}")
    into = {u["eid"]: u["into"] for u in c["updates"] if u.get("into")}
    for e in c["expect"]:
        if e.get("final_status") and end is not None:
            iid = match.get(e["eid"])
            ok = iid is not None and end["items"][iid]["status"] == e["final_status"]
            if iid is not None and e["eid"] in into and e["final_status"] == "done":
                ok = resolved_into(end, iid, look.get(into[e["eid"]]))
            T.rec(c, e.get("confidence"), "updates", ok, "final status" if iid is not None else "item missing",
                  f"{e['eid']}@end")
    for eid in into:                       # scorer 2.1: an ask left open after the owner's agreement is a false item
        iid = match.get(eid)
        if end is not None and leftover_ask(end, iid):
            T.false["false_items"] += 1
            T.false["open_after_agreement"] += 1
            T.leftover.add(iid)
            T.fails.append({"case": c["case"], "metric": "false_item", "ref": f"item {iid}",
                            "why": f"{eid} still open after the owner's agreement"})
    # clashes
    def side(ref):
        ref = str(ref)
        return ("event", ref) if ref.startswith(("cal:", "inv:")) else ("item", look.get(ref))

    def same_side(S, got, want):
        """A clash with an item that repeats a calendar entry (its cal_ref) is the clash with that entry."""
        (k, r), (wk, wr) = got, want
        if wk == "event":
            return r == wr if k == "event" else (S["items"].get(r) or {}).get("cal_ref") == wr
        return k == "item" and r in chain(S, wr)

    def found(S, a, b, open_only=True):
        for x in S["conflicts"]:
            if open_only and x["state"] != "open":
                continue
            if x.get("entries"):         # 2.3: a grouped trip clash is matched by its trip (group_found)
                continue
            (ka, ra), (kb, rb) = x["sides"]
            for (k1, r1), (k2, r2) in (((ka, ra), (kb, rb)), ((kb, rb), (ka, ra))):
                if same_side(S, (k1, r1), a) and same_side(S, (k2, r2), b):
                    return x
        return None
    order = {m["mid"]: (ts_of(m["ts"]), m["mid"]) for m in c["messages"]}

    def after_of(x):
        if x.get("after"):
            return x["after"]
        mids = [exp[r]["after"] for r in (x.get("a"), x.get("b"), x.get("eid")) if r in exp and exp[r]["after"] in order]
        return max(mids, key=lambda m: order[m]) if mids else None
    def group_found(S, a, open_only=True):
        """2.3: the open grouped clash of the trip `a` (its item side)."""
        for x in S["conflicts"]:
            if x.get("entries") and (x["state"] == "open" or not open_only) and same_side(S, x["sides"][0], a):
                return x
        return None

    def still_open(R, a, b, grouped):
        """The clash is still open: the trip's group (a group expectation), else the pair or a group listing b."""
        if grouped:
            return group_found(R, a) is not None
        if b[1] is None or found(R, a, b):
            return True
        g = group_found(R, a) if b[0] == "event" else None
        return g is not None and b[1] in g["entries"]

    expected_pairs = []
    for xi, x in enumerate(c["conflicts"]):
        want = [str(e) for e in x.get("entries") or []]
        a, b = side(x["a"]), side(x["b"]) if x.get("b") else (None, None)
        x = dict(x, after=after_of(x))
        S = snaps.get(x.get("after")) or end
        conf = x.get("confidence")
        ref = f"{x['a']}~{x['b']}" if x.get("b") else f"{x['a']}~{len(want)} entries"
        hit = S is not None and a[1] is not None and b[1] is not None and not want and found(S, a, b)
        why = "clash not flagged"
        if not hit and S is not None and a[1] is not None and (want or b[0] == "event"):
            g = group_found(S, a)      # 2.3: one grouped clash, whose entries must be the expected ones
            if g is not None and (want or b[1] in g["entries"]):
                want = want or [b[1]]
                gone, more = sorted(set(want) - set(g["entries"])), sorted(set(g["entries"]) - set(want))
                T.split["clash_groups.n"] += 1
                T.split["clash_groups." + ("partial" if gone or more else "exact")] += 1
                T.split["clash_groups.missing_entries"] += len(gone)
                T.split["clash_groups.extra_entries"] += len(more)
                expected_pairs.append(g["id"])   # never an extra clash, whatever its entries
                if gone or more:
                    why = f"group entries wrong: missing {gone}, extra {more}"
                    ref = f"{ref} (conflict {g['id']})"
                else:
                    hit = g
            elif want:
                T.split["clash_groups.n"] += 1
                T.split["clash_groups.not_flagged"] += 1
        within = hit and lat.get(x.get("after"), 0) <= (x.get("surface_within_s") or 300)
        T.rec(c, conf, "clashes", bool(within), why if not hit else "too slow", ref)
        T.out[("clash", c["case"], xi)] = bool(within)
        if hit:
            T.latency(c, conf, "clashes", lat.get(x.get("after"), 0))
            expected_pairs.append(hit["id"])
        if x.get("resolved_after"):
            R = snaps.get(x["resolved_after"])
            ok = R is not None and a[1] is not None and not still_open(R, a, b, bool(x.get("entries")))
            T.rec(c, conf, "clash_resolved", ok, "still open", ref)
    # follow-ups
    for f in c["follow_ups"]:
        f = dict(f, after=after_of(f))
        iid = look.get(f["eid"])
        S = snaps.get(f["after"])
        ok, why = False, "item missing"
        if iid is not None and S is not None:
            ids = chain(S, iid)
            raised = any(x["item"] in ids and x["state"] == "open" for x in S["followups"])
            ok, why = raised, "not raised"
            if raised:
                if f.get("cleared_after"):
                    C2 = snaps.get(f["cleared_after"])
                    ok = C2 is not None and not any(x["item"] in chain(C2, iid) and x["state"] == "open"
                                                    for x in C2["followups"])
                    why = "not cleared"
                elif end is not None:
                    ok = any(x["item"] in chain(end, iid) and x["state"] == "open" for x in end["followups"])
                    why = "cleared too early"
        T.rec(c, f.get("confidence"), "follow_ups", ok, None if ok else why, f["eid"])
    # links
    for lk in c["links"]:
        a, b = lk.get("a") or lk.get("eid"), lk.get("b") or lk.get("to")
        rel = lk.get("rel")
        if end is None or not a or not b or rel not in ("dedupe", "same_item", "part_of"):
            T.false["links_unscored"] += 1
            continue
        mids = {m["mid"] for m in c["messages"]}
        cal = [x for x in (a, b) if str(x).startswith(("cal:", "inv:"))]
        if rel == "same_item" or (rel == "dedupe" and a in mids and b in mids):
            ok = items_from(end, b) <= items_from(end, a) or items_from(end, a) <= items_from(end, b)
        elif rel == "dedupe" and cal:
            other = b if a in cal else a
            if other in mids:
                ok = all(end["items"][i]["cal_ref"] == cal[0] for i in items_from(end, other)
                         if end["items"][i]["type"] in ("event", "commitment"))
            else:
                iid = look.get(other)
                ok = iid is not None and (end["items"][iid]["cal_ref"] == cal[0] or
                                          end["items"][iid]["type"] not in ("event", "commitment"))
        elif rel == "dedupe":
            ok = look.get(a) is not None and look.get(b) is not None and look[b] in chain(end, look[a])
        else:                              # part_of: mid -> eid
            src, tgt = (a, b) if a in mids else (b, a)
            iid = look.get(tgt)
            ok = iid is not None and not any(
                i not in chain(end, iid) and end["items"][i]["type"] == end["items"][iid]["type"] and
                end["items"][i]["subtype"] == end["items"][iid]["subtype"] for i in items_from(end, src))
        T.rec(c, lk.get("confidence"), "dedupe", ok, f"{rel} not kept", f"{a}~{b}")
    return match, expected_pairs


CASE_KINDS = {True: "noise", False: "real", None: "unjudged"}   # no_items: true, false, null (README)


def _tier(tiers, mid):
    return (tiers or {}).get(mid, ("unread", ""))[0]


def false_items(cases, snaps, end, match_ids, tz, T, extra_pairs, tiers=None):
    """False items and extra clashes (scored), and their split (report only, `T.split`): false items in no-item cases
    (noise) or in cases with expectations (real threads), by the read tier of the message that made them; extra
    clashes with a false item on one side, between real items (expected items or calendar entries), or other (a side
    that is neither: an unjudged case's item, a hidden one)."""
    if end is None:
        return
    case_of = {m["mid"]: c for c in cases for m in c["messages"]}
    msg_of = {m["mid"]: m for c in cases for m in c["messages"]}
    false_ids, real_ids = set(T.leftover), set()
    for i in T.leftover:
        if i in end["items"]:
            T.split["false_items.real_threads"] += 1
            T.false_tier[_tier(tiers, end["items"][i]["created_mid"])] += 1
    for it in end["items"].values():
        if it["merged_into"] is not None or it["id"] in match_ids:
            continue
        c = case_of.get(it["created_mid"])
        if c is not None and it["cal_ref"] and any(lk.get("rel") == "dedupe" and it["cal_ref"] in (lk.get("a"), lk.get(
                "b")) for lk in c["links"]):
            real_ids.add(it["id"])
            continue                      # 0.22.0: it stands in for the calendar entry (Today shows one row)
        if c is None or c.get("no_items") is None:
            continue
        S = snaps.get(it["created_mid"]) or end
        if S["items"].get(it["id"], it)["section"] == "none":
            continue
        T.false["false_items"] += 1
        false_ids.add(it["id"])
        T.false_tier[_tier(tiers, it["created_mid"])] += 1
        if c.get("no_items") is True:
            T.false["in_no_items_cases"] += 1
            T.split["false_items.noise_cases"] += 1
        else:
            T.split["false_items.real_threads"] += 1
        T.fails.append({"case": c["case"], "metric": "false_item", "ref": f"item {it['id']}", "why": it["type"],
                        "read": _tier(tiers, it["created_mid"]),
                        "facts": _side_facts(end, msg_of, "item", it["id"], "false")})

    def side(kind, ref):
        if kind != "item":
            return "real"                 # a calendar entry
        if ref not in end["items"]:
            return "other"
        ids = {ref, _top(end, ref)}
        if ids & false_ids:
            return "false"
        return "real" if ids & (set(match_ids) | real_ids) else "other"
    for x in end["conflicts"]:
        if x["id"] in extra_pairs:
            continue
        c = case_of.get(x["created_mid"])
        T.false["extra_clashes"] += 1
        if c is not None and c.get("no_conflict"):
            T.false["no_conflict_cases_flagged"] += 1
        sides = [side(k, r) for k, r in x["sides"]]
        T.split["extra_clashes." + ("with_false_item" if "false" in sides else "between_real_items"
                                    if sides == ["real", "real"] else "other")] += 1
        T.fails.append({"case": c["case"] if c else None, "metric": "extra_clash", "ref": f"conflict {x['id']}",
                        "why": x["kind"], "confidence": x.get("confidence"), "entries": len(x.get("entries") or []),
                        "sides": [_side_facts(end, msg_of, k, r, s) for (k, r), s in zip(x["sides"], sides)]})


def _side_facts(end, msg_of, kind, ref, verdict):
    """An extra clash's side for the detail file: kinds and flags only, never words (which side, what kind of item,
    from whose message in which chat)."""
    if kind != "item" or ref not in end["items"]:
        return {"kind": kind, "verdict": verdict}
    it = end["items"][ref]
    m = msg_of.get(it.get("created_mid")) or {}
    return {"kind": kind, "verdict": verdict, "item": ref, "type": it.get("type"), "subtype": it.get("subtype"),
            "confidence": it.get("confidence"), "status": it.get("status"), "all_day": it.get("all_day"),
            "timed": bool(it.get("start_ts")) and not it.get("all_day"), "mid": it.get("created_mid"),
            "own": m.get("direction") == "out", "source": m.get("source"), "chat": m.get("chat") or m.get("thread")}


def tier_report(cases, tiers, match, snaps, T):
    """Report only: the read tier of each message per case kind (noise, real, unjudged), the expected items and asks
    found per tier of the message they come from (sure and likely apart), and the routing of each case that had a
    narrow read, with its expected items found."""
    case_of = {m["mid"]: c for c in cases for m in c["messages"]}
    for mid, c in case_of.items():
        T.reads[CASE_KINDS.get(c.get("no_items"), "unjudged")][_tier(tiers, mid)] += 1
    for c in cases:
        exp = [e for e in c["expect"]]
        for e in exp:
            tier = _tier(tiers, e["after"])
            conf = "sure" if e.get("confidence") == "sure" else "likely"
            got = match.get(e["eid"]) is not None and snaps.get(e["after"]) is not None
            for m in ("items",) + (("asks",) if "ask" in _types(e) else ()):
                T.recall[tier][conf][m + ".n"] += 1
                T.recall[tier][conf][m + ".found"] += 1 if got else 0
        mine = [m["mid"] for m in c["messages"]]
        if any(_tier(tiers, m) == X.TIER_NARROW for m in mine):
            why = sorted({tiers[m][1] for m in mine if _tier(tiers, m) == X.TIER_NARROW})
            T.narrow_cases[c["case"]] = {
                "kind": CASE_KINDS.get(c.get("no_items"), "unjudged"),
                "reads": dict(collections.Counter(_tier(tiers, m) for m in mine)), "why": why,
                "expected": len(exp), "found": sum(1 for e in exp if match.get(e["eid"]) is not None and
                                                   snaps.get(e["after"]) is not None),
                "expected_from_narrow": sum(1 for e in exp if _tier(tiers, e["after"]) == X.TIER_NARROW)}


def split_summary(T):
    """The report-only blocks: `false.split` and `tiers`."""
    def pct(n, k):
        return round(100.0 * k / n, 1) if n else None
    recall = {}
    for tier, confs in sorted(T.recall.items()):
        for conf, cnt in sorted(confs.items()):
            recall.setdefault(tier, {})[conf] = {m: {"n": cnt[m + ".n"], "found": cnt[m + ".found"],
                                                     "pct": pct(cnt[m + ".n"], cnt[m + ".found"])}
                                                 for m in ("items", "asks") if cnt[m + ".n"]}
    split = {"false_items": {"noise_cases": T.split["false_items.noise_cases"],
                             "real_threads": T.split["false_items.real_threads"],
                             "by_read_tier": dict(sorted(T.false_tier.items()))},
             "extra_clashes": {k: T.split["extra_clashes." + k] for k in ("with_false_item", "between_real_items",
                                                                          "other")},
             "reads_by_case_kind": {k: dict(sorted(v.items())) for k, v in sorted(T.reads.items())}}
    tiers = {"recall": recall, "narrow_cases": dict(sorted(T.narrow_cases.items()))}
    return split, tiers


# --- two annotators (scorer 2.4) -----------------------------------------------------------------------------------------
WEAK_TEST_BELOW = 30          # fewer required items than this: the two-annotator numbers are a weak test
KINDS = {"ask": "asks", "commitment": "commitments", "event": "events", "cleanup": "cleanups"}


def load_second_labels(path):
    """--second-labels: a jsonl with one line per case, the same schema as the cases (only `case`, `expect`,
    `conflicts`, `updates`, `follow_ups`, `links`, `no_items` and `unjudged` are read). The statuses read as
    load_cases reads them."""
    out = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        for x in list(b.get("expect") or []) + list(b.get("updates") or []):
            for k in ("status", "final_status"):
                if x.get(k) in STATUS_SAME:
                    x[k] = STATUS_SAME[x[k]]
        out[b["case"]] = b
    return out


def second_case(c, b):
    """The case as the second annotator labelled it: its messages, calendar and replay, B's labels."""
    return dict(c, expect=list(b.get("expect") or []), conflicts=list(b.get("conflicts") or []),
                updates=list(b.get("updates") or []), follow_ups=list(b.get("follow_ups") or []),
                links=list(b.get("links") or []), no_items=b.get("no_items", c.get("no_items")),
                unjudged=list(b.get("unjudged") or []))


def _kind(ea, eb):
    both = [t for t in _types(ea) if t in _types(eb)] or _types(ea)
    return KINDS.get("ask" if "ask" in both else both[0], "others")


def _when_key(w, tz):
    """A labelling's time, comparable across the two labellings (the phrase and tz aside)."""
    if not w or not any(w.get(k) for k in ("start", "start_date", "candidates", "time", "date")):
        return None
    if w.get("candidates"):
        return ("candidates", tuple(sorted(str(x) for x in w["candidates"])))
    if w.get("date_resolved") is False:
        return ("time", str(w.get("time") or "")[:5])
    if w.get("all_day") and w.get("start_date"):
        return ("days", w["start_date"], w.get("end_date") or w["start_date"])
    if w.get("start"):
        return ("at", ts_of(w["start"]), ts_of(w["end"]) if w.get("end") else None, bool(w.get("approx")))
    return ("date", str(w.get("date") or w.get("start_date") or ""))


def same_time(wa, wb, tz):
    """The two labellings give the same time: equal, or (either approximate) starts within 2 h on the same date."""
    ka, kb = _when_key(wa, tz), _when_key(wb, tz)
    if ka == kb:
        return True
    if ka and kb and ka[0] == kb[0] == "at" and (ka[3] or kb[3]):
        da, db = dt.datetime.fromtimestamp(ka[1], tz), dt.datetime.fromtimestamp(kb[1], tz)
        return abs(ka[1] - kb[1]) <= 7200 and da.date() == db.date()
    return False


def pair_items(ca, cb, tz):
    """[(A eid, B eid)]: each A expectation with at most one B expectation of the same type (or overlapping types),
    who and `after`; among several, the one whose time agrees, then the closest `what`."""
    pairs, used = [], set()
    for ea in ca["expect"]:
        cands = [eb for eb in cb["expect"] if eb["eid"] not in used and eb.get("after") == ea.get("after") and
                 set(_types(ea)) & set(_types(eb)) and L.norm_who(eb.get("who")) == L.norm_who(ea.get("who"))]
        if not cands:
            continue
        best = max(cands, key=lambda eb: (same_time(ea.get("when"), eb.get("when"), tz),
                                          L.similar(ea.get("what") or "", eb.get("what") or "")))
        used.add(best["eid"])
        pairs.append((ea["eid"], best["eid"]))
    return pairs


def _clash_key(x, to_a=None):
    """A clash as a comparable key: ("group", a) for a grouped clash, ("pair", {a, b}) for a pair; B's refs are read
    as A's through the item pairing (to_a); calendar refs are the same in both."""
    def ref(r):
        r = str(r)
        return (to_a or {}).get(r, r)
    if x.get("entries") is not None and not x.get("b"):
        return ("group", ref(x["a"])), {str(e) for e in x.get("entries") or []}
    return ("pair", frozenset({ref(x["a"]), ref(x.get("b"))})), set()


def pair_clashes(ca, cb, to_a):
    """[(A index, B index)] of the clashes both labellings give: the same pair, or the same group (a pair {a, b} and
    a group of `a` that lists b are one clash, as scorer 2.3 reads them)."""
    ka = [_clash_key(x) for x in ca["conflicts"]]
    kb = [_clash_key(x, to_a) for x in cb["conflicts"]]

    def same(x, y):
        (k1, e1), (k2, e2) = x, y
        if k1 == k2:
            return True
        for (g, ents), (p, _e) in ((x, y), (y, x)):
            if g[0] == "group" and p[0] == "pair" and g[1] in p[1] and (set(p[1]) - {g[1]}) <= ents:
                return True
        return False
    out, used = [], set()
    for i, x in enumerate(ka):
        j = next((j for j, y in enumerate(kb) if j not in used and same(x, y)), None)
        if j is not None:
            used.add(j)
            out.append((i, j))
    return out


class TwoAnn:
    """Scorer 2.4: the two-annotator tallies, over every replayed case the second labelling covers."""

    def __init__(self, labels, name):
        self.labels, self.name = labels, name
        self.c = collections.Counter()
        self.recall = collections.defaultdict(collections.Counter)
        self.fields = collections.defaultdict(collections.Counter)
        self.agree = collections.defaultdict(collections.Counter)
        self.cases, self.missing, self.seen = 0, [], set()

    def group(self, g, snaps, lat, end, tz, TA, matchA, pairsA):
        """One replay group, after it was scored with A's labels (TA: its Tally; matchA: eid -> item; pairsA: the
        conflict ids A's clashes met)."""
        scope = [c for c in g if c["case"] in self.labels]
        self.seen |= {c["case"] for c in scope}
        self.missing += [c["case"] for c in g if c["case"] not in self.labels]
        if not scope or end is None:
            return
        self.cases += len(scope)
        bcases = {c["case"]: second_case(c, self.labels[c["case"]]) for c in scope}
        TB = Tally()
        matchB, gexpB = {}, {e["eid"]: e for b in bcases.values() for e in b["expect"]}
        for b in bcases.values():
            matchB.update(match_case(b, snaps, tz, shared_of(b)))
        pairsB = []
        for b in bcases.values():
            _m, p = score_case(b, snaps, lat, end, tz, TB, False, matchB, gexpB)
            pairsB += p
        for c in scope:
            self._case(c, bcases[c["case"]], snaps, end, tz, TA, TB, matchA, matchB)
        self._false(scope, bcases, snaps, end, matchA, matchB, set(pairsA) | set(pairsB))

    def _case(self, ca, cb, snaps, end, tz, TA, TB, matchA, matchB):
        pairs = pair_items(ca, cb, tz)
        expA, expB = {e["eid"]: e for e in ca["expect"]}, {e["eid"]: e for e in cb["expect"]}
        ok_a = {e: TA.out.get(("items", e)) is True for e in expA}
        ok_b = {e: TB.out.get(("items", e)) is True for e in expB}
        req_ids = set()
        for a, b in pairs:
            ea, eb = expA[a], expB[b]
            hit = ok_a[a] or ok_b[b]
            for k in ("all", _kind(ea, eb)):
                self.recall[k]["n"] += 1
                self.recall[k]["found"] += 1 if hit else 0
            iid = matchA.get(a) if ok_a[a] else matchB.get(b) if ok_b[b] else None
            if iid is not None:
                req_ids.add(iid)
            if hit:                       # confidence, section and time: on required items only, either reading
                for m in ("times", "sections"):   # a time only one labelling gives is optional: not scored
                    if (m, a) in TA.out and (m, b) in TB.out:
                        got = [TA.out[(m, a)], TB.out[(m, b)]]
                        self.fields[m]["n"] += 1
                        self.fields[m]["ok"] += 1 if any(got) else 0
                S = snaps.get(ea["after"]) or end
                it = S["items"].get(iid) if iid is not None else None
                if it is not None:
                    self.fields["confidence"]["n"] += 1
                    self.fields["confidence"]["ok"] += 1 if it.get("confidence") in (
                        ea.get("confidence"), eb.get("confidence")) else 0
            # agreement between the two labellings, on the items both give
            self.agree["section"]["n"] += 1
            self.agree["section"]["agree"] += 1 if _sections(ea) == _sections(eb) else 0
            self.agree["confidence"]["n"] += 1
            self.agree["confidence"]["agree"] += 1 if ea.get("confidence") == eb.get("confidence") else 0
            self.agree["time"]["n"] += 1
            self.agree["time"]["agree"] += 1 if same_time(ea.get("when"), eb.get("when"), tz) else 0
            self.agree["final_status"]["n"] += 1
            self.agree["final_status"]["agree"] += 1 if (ea.get("final_status") or ea.get("status")) == (
                eb.get("final_status") or eb.get("status")) else 0
        paired_a, paired_b = {a for a, _ in pairs}, {b for _, b in pairs}
        self.c["required_items"] += len(pairs)
        self.c["a_items"] += len(expA)
        self.c["b_items"] += len(expB)
        for e in expA:
            if e not in paired_a:
                self.c["optional.a_only"] += 1
                if ok_a[e] and matchA.get(e) not in req_ids:
                    self.c["optional.produced.a_only"] += 1
        for e in expB:
            if e not in paired_b:
                self.c["optional.b_only"] += 1
                if ok_b[e] and matchB.get(e) not in req_ids:
                    self.c["optional.produced.b_only"] += 1
        self.agree["no_item"]["n"] += 1
        self.agree["no_item"]["agree"] += 1 if ca.get("no_items") == cb.get("no_items") else 0
        # clashes: required when both give it, optional when one does
        to_a = {b: a for a, b in pairs}
        cp = pair_clashes(ca, cb, to_a)
        self.c["a_clashes"] += len(ca["conflicts"])
        self.c["b_clashes"] += len(cb["conflicts"])
        self.c["required_clashes"] += len(cp)
        for i, j in cp:
            hit = TA.out.get(("clash", ca["case"], i)) or TB.out.get(("clash", cb["case"], j))
            self.recall["clashes"]["n"] += 1
            self.recall["clashes"]["found"] += 1 if hit else 0
        for side, conf, done, T, cid in (("a_only", ca["conflicts"], {i for i, _ in cp}, TA, ca["case"]),
                                         ("b_only", cb["conflicts"], {j for _, j in cp}, TB, cb["case"])):
            for i in range(len(conf)):
                if i not in done:
                    self.c["optional_clashes." + side] += 1
                    self.c["optional_clashes.produced." + side] += 1 if T.out.get(("clash", cid, i)) else 0

    def _false(self, scope, bcases, snaps, end, matchA, matchB, pairs):
        """FALSE = a model item outside the union of the two labellings (or a clash neither gives), in these cases."""
        mine = {m["mid"]: c for c in scope for m in c["messages"]}
        union = {v for v in list(matchA.values()) + list(matchB.values()) if v is not None}
        for it in end["items"].values():
            c = mine.get(it["created_mid"])
            if c is None or it["merged_into"] is not None:
                continue
            if c.get("no_items") is None and bcases[c["case"]].get("no_items") is None:
                continue
            if it["id"] in union:
                self.c["produced"] += 1
                self.c["produced.in_union"] += 1
                continue
            if it["cal_ref"] and any(lk.get("rel") == "dedupe" and it["cal_ref"] in (lk.get("a"), lk.get("b"))
                                     for lk in c["links"] + bcases[c["case"]]["links"]):
                continue                  # it stands in for the calendar entry (as false_items)
            S = snaps.get(it["created_mid"]) or end
            if S["items"].get(it["id"], it)["section"] == "none":
                continue
            self.c["produced"] += 1
            self.c["false_items"] += 1
        for x in end["conflicts"]:
            if mine.get(x["created_mid"]) is not None and x["id"] not in pairs:
                self.c["false_clashes"] += 1

    def summary(self):
        def pct(n, k):
            return round(100.0 * k / n, 1) if n else None

        def rate(cnt, key="found"):
            return {"n": cnt["n"], key: cnt[key], "pct": pct(cnt["n"], cnt[key])}

        def overlap(a, b, both):
            return {"a": a, "b": b, "both": both, "pct": pct(a + b - both, both)}
        req = self.c["required_items"]
        return {
            "labels_b": self.name, "cases": self.cases, "cases_without_b": len(self.missing),
            "b_cases_not_replayed": len(set(self.labels) - self.seen),
            "required": {"items": req, "clashes": self.c["required_clashes"], "weak_test": req < WEAK_TEST_BELOW,
                         "weak_test_below": WEAK_TEST_BELOW},
            "required_recall": {k: rate(self.recall[k]) for k in ("all", "asks", "commitments", "events", "cleanups",
                                                                  "clashes")},
            "required_fields": {k: rate(self.fields[m], "ok") for k, m in (("confidence", "confidence"),
                                                                             ("section", "sections"),
                                                                             ("time", "times"))},
            "union_precision": {"produced": self.c["produced"], "in_union": self.c["produced.in_union"],
                                "false": self.c["false_items"],
                                "pct": pct(self.c["produced"], self.c["produced.in_union"])},
            "false": {"items": self.c["false_items"], "clashes": self.c["false_clashes"]},
            "optional": {"items": self.c["optional.a_only"] + self.c["optional.b_only"],
                         "a_only": self.c["optional.a_only"], "b_only": self.c["optional.b_only"],
                         "produced": self.c["optional.produced.a_only"] + self.c["optional.produced.b_only"],
                         "produced_a_only": self.c["optional.produced.a_only"],
                         "produced_b_only": self.c["optional.produced.b_only"],
                         "clashes": self.c["optional_clashes.a_only"] + self.c["optional_clashes.b_only"],
                         "clashes_produced": self.c["optional_clashes.produced.a_only"] +
                         self.c["optional_clashes.produced.b_only"]},
            "agreement": {"overall": overlap(self.c["a_items"], self.c["b_items"], req),
                          **{k: rate(self.agree[k], "agree") for k in ("section", "confidence", "time",
                                                                       "final_status", "no_item")},
                          "clashes": overlap(self.c["a_clashes"], self.c["b_clashes"], self.c["required_clashes"])}}


def summarize(T, cases_scored, set_name, run, synthetic, set_version=None):
    def block(cnt, lat):
        out = {}
        for m in METRICS:
            n = cnt[m + ".n"]
            if n:
                ok = cnt[m + ".ok"]
                out[m] = {"n": n, "ok": ok, "pct": round(100.0 * ok / n, 1)}
        for k, v in lat.items():
            out.setdefault("p95_s", {})[k.split(".", 1)[1]] = _p95(v)
        return out
    res = {}
    for scope in sorted(T.t, key=lambda s: (s != "all", s)):
        res[scope] = {conf: block(T.t[scope][conf], {k: v for k, v in T.lat[scope].items() if k.startswith(conf)})
                      for conf in ("sure", "likely") if T.t[scope][conf] or any(k.startswith(conf) for k in T.lat[scope])}
    sure = res.get("all", {}).get("sure", {})

    def miss(m):
        return sure.get(m, {}).get("n", 0) - sure.get(m, {}).get("ok", 0)
    per20 = round(20.0 * T.false["false_items"] / cases_scored, 2) if cases_scored else 0.0
    split, tiers = split_summary(T)
    asks_n = sure.get("asks", {}).get("n", 0)
    bars = {"missed_asks": (miss("asks") == 0) if synthetic else (asks_n == 0 or miss("asks") / asks_n <= 0.10),
            "missed_clashes": miss("clashes") == 0 if synthetic else None,
            "wrong_times": miss("times") == 0,
            "false_items_per_20": per20 <= 1.0,
            "p95_asks_clashes_300s": all((v or 0) <= 300 for v in (sure.get("p95_s") or {}).values()),
            "updates_95": sure.get("updates", {}).get("pct", 100.0) >= 95.0,
            "follow_ups_90": sure.get("follow_ups", {}).get("pct", 100.0) >= 90.0,
            "dedupe_95": sure.get("dedupe", {}).get("pct", 100.0) >= 95.0,
            "never_hide_100": sure.get("never_hide", {}).get("pct", 100.0) == 100.0}
    gate = "pass" if all(v is not False for v in bars.values()) and not run.get("stopped") else "fail"
    return {"set": set_name, "set_version": set_version, "scorer_version": SCORER_VERSION, "gate": gate, "bars": bars, "missed": {"asks": miss("asks"), "clashes": miss("clashes"),
                                                                   "wrong_times": miss("times")},
            "false": dict(T.false, per_20_cases=per20, split=split), "metrics": res, "run": run, "tiers": tiers,
            # 2.3: the expected grouped trip clashes: exact, partial (an entry missing or extra: that clash is wrong)
            # or not flagged, and the entries missing or extra inside them
            "clash_groups": {k: T.split["clash_groups." + k] for k in ("n", "exact", "partial", "not_flagged",
                                                                       "missing_entries", "extra_entries")}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", required=True, help="cases.jsonl")
    ap.add_argument("--calendar", help="calendar.json for global cases (default: next to cases.jsonl)")
    ap.add_argument("--contacts", help="contacts.json: handles of known people (default: next to cases.jsonl, if any)")
    ap.add_argument("--extractor", choices=("oracle", "bad", "live", "raw"), default="oracle")
    ap.add_argument("--raw", help="raw: the model's answers kept by --raw-out")
    ap.add_argument("--raw-ids", help="raw: the item id map (--ids-out) of a replay of these answers with the code "
                    "they were read with: turns their ids into this replay's (exact re-score)")
    ap.add_argument("--ids-out", help="write the item id map for --raw-ids here")
    ap.add_argument("--raw-out", help="live: keep the model's answers here (mode 600; refused inside the repo)")
    ap.add_argument("--base", help="live: the model server's base URL (…/v1)")
    ap.add_argument("--model", help="live: the model name")
    ap.add_argument("--tz", default=os.environ.get("TZ") or "UTC", help="the zone (default: TZ, else UTC)")
    ap.add_argument("--owner", default="Alex", help="the owner's name when the cases don't give one")
    ap.add_argument("--case", action="append", default=[], help="only cases whose id starts with this (repeatable)")
    ap.add_argument("--on-block", choices=("wait", "stop"), default="wait")
    ap.add_argument("--quiet-times", default="", help='the model server\'s quiet times in --tz, "HH:MM-HH:MM,..."')
    ap.add_argument("--no-followups", action="store_true", help="oracle: leave every follow-up to the clash code")
    ap.add_argument("--out", help="write the JSON summary here too")
    ap.add_argument("--detail", help="write the failures here (mode 600; refused inside the repo)")
    ap.add_argument("--keep-db", help="keep the replayed ledger here for tools/replay_brief.py (refused inside the repo)")
    ap.add_argument("--second-labels", help="scorer 2.4: a second annotator's labels (jsonl, one line per case, the "
                    "cases' schema): adds the two_annotator block")
    ap.add_argument("--extra-body", default="", help='live: a JSON object merged at the top level into every request '
                    'body (model-specific fields, e.g. \'{"thinking_budget_tokens": 0}\'); never model, messages or '
                    'id_slot. Recorded in the summary (extra_body)')
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        args.extra_body_obj = parse_extra_body(args.extra_body)
    except ValueError as e:
        ap.error(str(e))
    if args.keep_db and pathlib.Path(args.keep_db).resolve().is_relative_to(ROOT):
        ap.error("--keep-db must be outside the repo")
    if args.extractor == "live" and not (args.base and args.model):
        ap.error("--extractor live needs --base and --model")
    if args.detail and pathlib.Path(args.detail).resolve().is_relative_to(ROOT):
        ap.error("--detail must be outside the repo")
    if args.ids_out and pathlib.Path(args.ids_out).resolve().is_relative_to(ROOT):
        ap.error("--ids-out must be outside the repo")
    if args.raw_out and pathlib.Path(args.raw_out).resolve().is_relative_to(ROOT):
        ap.error("--raw-out must be outside the repo")
    if args.extractor == "raw" and not args.raw:
        ap.error("--extractor raw needs --raw")

    def log(line):
        if not args.quiet:
            print(f"[replay] {line}", file=sys.stderr, flush=True)

    cases = load_cases(args.cases, args.case)
    errs = set_errors(cases)
    two = None
    if args.second_labels:
        two = TwoAnn(load_second_labels(args.second_labels), pathlib.Path(args.second_labels).name)
        errs += [f"second labels: {x}" for x in set_errors([second_case(c, two.labels[c["case"]]) for c in cases
                                                            if c["case"] in two.labels])]
    if errs:
        for x in errs:
            print(f"[replay] set error: {x}", file=sys.stderr, flush=True)
        return 2
    set_name = cases[0]["set"] if cases else "?"
    synthetic = set_name == "synthetic"
    tz = L.zone(args.tz)
    run = {"extractor": args.extractor, "cases": len(cases), "messages": 0, "requests": 0, "failed_reads": 0,
           "drops": 0, "stopped": None, "seconds": 0.0, "skipped": collections.Counter()}
    T = Tally()
    t0 = time.monotonic()
    groups = [[c] for c in cases if c["replay"] == "isolated"]
    glob = [c for c in cases if c["replay"] == "global"]
    if glob:
        groups.append(glob)
    for c in cases:
        if c["replay"] in ("facts_only", "calendar"):
            run["skipped"][c["replay"]] += 1
    cal_path = pathlib.Path(args.calendar) if args.calendar else pathlib.Path(args.cases).with_name("calendar.json")
    contacts = load_contacts(args.contacts or pathlib.Path(args.cases).with_name("contacts.json"))
    scored = 0
    idmaps = []
    for g in groups:
        if run["stopped"]:
            break
        if g is glob:
            cal = json.load(open(cal_path, encoding="utf-8")).get("events", []) if cal_path.exists() else []
        else:
            cal = g[0].get("calendar") or []
        fake = Oracle(g, followups=not args.no_followups) if args.extractor == "oracle" else \
            Bad(g) if args.extractor == "bad" else None
        call = fake if fake else raw_caller(args.raw, args.raw_ids) if args.extractor == "raw" else \
            live_caller(args, log)
        if args.raw_out and args.extractor == "live":
            call = raw_keeper(call, args.raw_out)
        last = g is glob or (not glob and g is groups[-1])
        tiers = {}
        msgs, snaps, lat = run_group(g, cal, call, fake, args, log, run, keep=args.keep_db if last else None,
                                     tiers=tiers, contacts=contacts)
        end = list(snaps.values())[-1] if snaps else None
        if args.ids_out:
            idmaps.append((msgs[0]["mid"] if msgs else "", list(snaps), end))
        matched, pairs = set(), []
        T.leftover = set()
        gmatch, gexp = {}, {e["eid"]: e for c in g for e in c["expect"]}
        for c in g:
            gmatch.update(match_case(c, snaps, tz, shared_of(c)))
        for c in g:
            m, p = score_case(c, snaps, lat, end, tz, T, synthetic, gmatch, gexp)
            matched |= {v for v in m.values() if v is not None}
            pairs += p
            scored += 1 if c.get("no_items") is not None else 0
        false_items(g, snaps, end, matched, tz, T, set(pairs), tiers)
        tier_report(g, tiers, gmatch, snaps, T)
        if two is not None:
            two.group(g, snaps, lat, end, tz, T, gmatch, pairs)
    run["seconds"] = round(time.monotonic() - t0, 1)
    run["skipped"] = dict(run["skipped"])
    if run["skipped"]:
        run["skipped_why"] = ("facts_only: no message text was stored before stage 1, so nothing can be replayed; "
                              "score them against the device's 30-day backfill. calendar: calendar-only expectations "
                              "for the stage-4 Coming-up view and RSVP-pending invites (not ledger items yet)")
    vers = sorted({str(c.get("set_version") or "1") for c in cases})
    summary = summarize(T, scored, set_name, run, synthetic, vers[0] if len(vers) == 1 else vers)
    if two is not None:
        summary["two_annotator"] = two.summary()
    summary["extra_body"] = args.extra_body_obj
    text = json.dumps(summary, indent=1, sort_keys=True)
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.ids_out:
        fd = os.open(args.ids_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ids_map(idmaps), f, ensure_ascii=False)
    if args.detail:
        fd = os.open(args.detail, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fails = T.fails if synthetic else [{k: v for k, v in x.items() if k != "words"} for x in T.fails]
            json.dump(fails, f, indent=1)
    return 0 if summary["gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
