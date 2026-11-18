"""The background model (rework, 0.21.0): the one local model that reads each stored message into the ledger
(extract.py) and writes the talk-through brief (brief2.py), and the plumbing every background request goes through.

Carved out of the 0.8.4-0.14.4 sorter (triage.py, retired in the rework) with its behaviour unchanged: the model
setting (meta `sorting`: `model` and the per-model `ctx_sizes`), slot 1 (`slot_for`), the call-time guard, the
in-flight budget (80% of the model's context), the learned prefill and output pace and the whole-request cap (8 s by
day, 20 s in the night windows), Chat first (`chat_first`) and Chat's preemption (`post_bg` streams; a cut request
is counted and retried by its caller), the per-day request numbers, and the local model's health for Setup. Also the
owner's names for the detectors (`owner_names`) and the done-claim rule replies use (`claims`). Nothing here sorts.
"""
import collections
import datetime as dt
import re
import statistics
import sys
import threading
import time

import rules
import writer


def _stderr(line):
    print(line, file=sys.stderr, flush=True)


MODEL = re.compile(r"[\w.:/+@-]{1,120}")
# --- settings and the budget ----------------------------------------------------------------------------------------
NO_MODEL = ""   # no background model picked yet: Setup › AI shows "Pick a model" (or the server's first listed model)
# The llama.cpp slot: the resident model serves Chat on slot 0 and the background work (the ledger's read, the
# talk-through brief) on slot 1, whatever the background model's name; when Chat uses the same model they share it.
SLOT = 1
BATCH = 6                  # daytime batch, at most (0.14.1: sized by TARGET_S below it)
NIGHT_BATCH = 12           # inside the night windows, at most
# 0.14.1 (one resident model): a batch takes about TARGET_S at the model's measured median seconds per card (per
# model, the last 50 batches), from 1 card up to `batch` / `night_batch`, so Chat waits at most about that long behind
# one. A model with no measurement yet starts at FIRST_BATCH cards (a slow model's first batch stays short).
TARGET_S = 20
FIRST_BATCH = 2
PERF_MODELS = 8            # the measured pace is kept for this many models
PREFILL_OVER_S = 25.0
FIT_KEYS = ("requests", "split", "moved", "alone", "clip_notes", "clip_context", "clip_tasks", "over")
RUN_S = 180                # an incremental run: at most 3 minutes of model time
END_BEFORE_S = 10 * 60     # ... ending at least 10 minutes before a call time
AFTER_CALL_S = 10 * 60     # ... and starting no sooner than 10 minutes after one (the brief writer goes first)
MIN_RUN_S = 20             # not worth starting with less room than this
DAY_CARDS = 1200
DAY_GPU_MIN = 60           # model time a day: room for DAY_CARDS on a slow model server
TIMEOUT_S = 120
# The night windows: NIGHT (22:00-06:00) minus the owner's quiet times for the model server (settings
# model_quiet_times, Setup › AI › Advanced), at most MAX_WINDOWS of them; all belong to the same night (a night runs
# noon to noon). With quiet times 01:00-04:00 that is 22:00-01:00 and 04:00-06:00 (the 0.9.0 windows); with none it is
# the whole night. The night job resumes from its cursor every night until it has read `days` back (5 h at 21-36
# cards a minute is ~6,300-10,800 cards a night; a busy server reads less): `nights` is how many nights it may use.
NIGHT = ("22:00", "06:00")
MAX_WINDOWS = 3
# request extras per model family (thinking off; gpt-oss reasons at "low"); the screen picks the model
PROFILES = (("gpt-oss", {"reasoning_effort": "low"}),)
def _name_part(s, n=60):
    """A name for the fixed text: letters, spaces, dots, hyphens and apostrophes only (no quotes, brackets or
    newlines), clipped."""
    s = re.sub(r"[^\w .'-]", " ", str(s or ""), flags=re.UNICODE)
    return " ".join(s.split())[:n].strip()


def slot_for(model, background=None):
    """The background work's llama.cpp slot: SLOT (1) for the configured background model (the sorter's model setting),
    whatever its name; 0 for any other model. Chat keeps slot 0. `background` None: `model` is the configured one (the
    sorter, the reader and the picker only ever ask the sorter's model)."""
    return SLOT if background is None or str(model) == str(background) else 0


_OWNER_CLAIM = re.compile(r"\b(?:you)\s+(?:already\s+|has\s+|have\s+|had\s+|just\s+)*(?:agreed|promised|"
                          r"approved|confirmed|committed|signed\s+off|said\s+yes|okayed)\b", re.I)
def claims(text, owner_first=()):
    """A sentence that says the owner agreed, promised or approved, or that states a decision."""
    if _OWNER_CLAIM.search(text) or writer.states_decision(text):
        return True
    for n in owner_first or ():
        if re.search(r"\b" + re.escape(n) + r"\s+(?:already\s+|has\s+|had\s+|just\s+)*(?:agreed|promised|approved|"
                     r"confirmed|committed|signed\s+off|said\s+yes|okayed)\b", text, re.I):
            return True
    return False


def owner_names(store, people=None):
    """0.14.0: the owner's names for the instruction and claim detectors (inject.owner_terms takes the first and full
    names from them): the About you name and the names People knows the owner by, the sources of context()'s
    owner_first. Never from content."""
    out = []
    try:
        full = _name_part(store.settings().get("name"))
    except Exception:  # noqa: BLE001 (no settings yet: "the owner" still counts)
        full = ""
    if full:
        out.append(full)
    if people is not None:
        try:
            out += sorted(n for n in people.owner_names() if n)
        except Exception:  # noqa: BLE001
            pass
    return tuple(out)


def background_model(store):
    """The configured background model (Setup › AI); Chat shares its server and slot when it uses the same model."""
    saved = store._get_meta("sorting", None)  # noqa: SLF001
    m = saved.get("model") if isinstance(saved, dict) else None
    return m if isinstance(m, str) and MODEL.fullmatch(m) else NO_MODEL


def _ctx_sizes(v):
    """0.14.1: the owner's per-model "Context size" ({model: tokens}), only valid entries."""
    if not isinstance(v, dict):
        return {}
    lo, hi = writer.CTX_LIMITS
    return {str(m): n for m, n in list(v.items())[-PERF_MODELS:] if isinstance(m, str) and MODEL.fullmatch(m)
            and isinstance(n, int) and not isinstance(n, bool) and lo <= n <= hi}


# Extra request fields per model (Setup › AI › Advanced; meta `sorting`, key model_extra_body {model: {key: number}}):
# model-specific sampling or reasoning fields some models need (LFM2.5: thinking_budget_tokens 0), added at the top
# level of every background request (the reader's, the brief's) for that model. None by default; keys only from
# EXTRA_KEYS, numbers only, so they can never change the model, the messages, the slot or the answer's format.
EXTRA_KEYS = ("thinking_budget_tokens", "top_k", "top_p", "min_p", "repeat_penalty", "presence_penalty",
              "reasoning_budget")
EXTRA_MAX = 1_000_000


def extra_error(v):
    """Why `v` isn't a valid set of extra request fields (None when it is)."""
    if not isinstance(v, dict):
        return "Extra request fields are a JSON object, like {\"top_k\": 20}."
    for k, n in v.items():
        if k not in EXTRA_KEYS:
            return f"{k} isn't allowed here. Use only {', '.join(EXTRA_KEYS)}."
        if isinstance(n, bool) or not isinstance(n, (int, float)) or n != n or abs(n) > EXTRA_MAX:
            return f"{k} needs a number."
    return None


def _extra_bodies(v):
    """The owner's per-model extra request fields ({model: {key: number}}), only valid, non-empty entries."""
    if not isinstance(v, dict):
        return {}
    return {m: dict(x) for m, x in list(v.items())[-PERF_MODELS:] if isinstance(m, str) and MODEL.fullmatch(m)
            and x and extra_error(x) is None}


def size_for(card_s, cap, target=None):
    """0.14.1: a batch's size: as many cards as take about `target` (TARGET_S) at `card_s` seconds a card, from 1 to
    `cap`; FIRST_BATCH (at most cap) while the model's pace isn't measured yet (card_s None)."""
    cap = max(1, int(cap))
    if not card_s or card_s <= 0:
        return min(cap, FIRST_BATCH)
    return max(1, min(cap, int((TARGET_S if target is None else target) / card_s)))


def perf_get(store, key, model):
    """0.14.1: a model's measured seconds per card (the last 50 batches) from meta `key` ({"models": {model: [...]}}).
    A list from before 0.14.1 ({"card_s": [...]}, one list for every model) counts for the model set now."""
    p = store._get_meta(key, {}) or {}  # noqa: SLF001
    p = p if isinstance(p, dict) else {}
    models = p.get("models") if isinstance(p.get("models"), dict) else None
    xs = models.get(str(model)) if models is not None else p.get("card_s")
    return [float(x) for x in xs or [] if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0][-50:]


def perf_add(store, key, model, secs, cards):
    """Add one batch (seconds for `cards` cards) to the model's measured pace; keeps PERF_MODELS models."""
    p = store._get_meta(key, {}) or {}  # noqa: SLF001
    p = p if isinstance(p, dict) else {}
    models = dict(p["models"]) if isinstance(p.get("models"), dict) else {}
    if not models and p.get("card_s"):   # the list from before 0.14.1 belongs to the model set now
        models[str(model)] = perf_get(store, key, model)
    xs = perf_get(store, key, model) + [round(secs / max(1, cards), 3)]
    models.pop(str(model), None)
    models[str(model)] = xs[-50:]
    store._set_meta(key, {"models": dict(list(models.items())[-PERF_MODELS:])})  # noqa: SLF001


def pace(xs):
    """The median seconds per card of a measured list, else None."""
    return statistics.median(xs) if xs else None


def _vals_get(store, key, model):
    """0.14.2: a model's measured values from meta `key` ({"models": {model: [numbers]}}), the last PREFILL_KEEP."""
    p = store._get_meta(key, {}) or {}  # noqa: SLF001
    xs = (p.get("models") or {}).get(str(model)) if isinstance(p, dict) and isinstance(p.get("models"), dict) else None
    return [float(x) for x in xs or [] if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0][
        -writer.PREFILL_KEEP:]


def _vals_add(store, key, model, value):
    p = store._get_meta(key, {}) or {}  # noqa: SLF001
    models = dict(p["models"]) if isinstance(p, dict) and isinstance(p.get("models"), dict) else {}
    xs = _vals_get(store, key, model) + [float(value)]
    models.pop(str(model), None)
    models[str(model)] = xs[-writer.PREFILL_KEEP:]
    store._set_meta(key, {"models": dict(list(models.items())[-PERF_MODELS:])})  # noqa: SLF001


def _hhmm(v):
    return isinstance(v, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v) is not None


def _windows(v):
    """Night windows as [[start, end], ...] (1 to MAX_WINDOWS, HH:MM, start != end, may cross midnight), else None."""
    if not isinstance(v, (list, tuple)) or not 1 <= len(v) <= MAX_WINDOWS:
        return None
    out = []
    for w in v:
        if isinstance(w, dict):
            w = (w.get("start"), w.get("end"))
        if not isinstance(w, (list, tuple)) or len(w) != 2 or not all(_hhmm(x) for x in w) or w[0] == w[1]:
            return None
        out.append([w[0], w[1]])
    return out


def quiet_times(store):
    """The owner's quiet times for the model server ([[start, end], ...], local time) from Store.settings; none when
    they're missing or unreadable."""
    try:
        v = store.settings().get("model_quiet_times")
    except Exception:  # noqa: BLE001 (no settings yet: no quiet times)
        return []
    return [list(w) for w in v if isinstance(w, (list, tuple)) and len(w) == 2 and all(_hhmm(x) for x in w)
            and w[0] != w[1]] if isinstance(v, list) else []


def _mins(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def night_windows(quiet):
    """NIGHT minus the quiet times, as [[start, end], ...] in order through the night (at most MAX_WINDOWS, the
    longest kept). [] when the quiet times cover the whole night."""
    start, end = _mins(NIGHT[0]), _mins(NIGHT[1])
    span = (end - start) % 1440
    free = [True] * span
    for qs, qe in quiet:
        a, b = _mins(qs), _mins(qe)
        n = (b - a) % 1440
        for k in range(n):
            off = (a + k - start) % 1440
            if off < span:
                free[off] = False
    runs, i = [], 0
    while i < span:
        if free[i]:
            j = i
            while j < span and free[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    runs = sorted(sorted(runs, key=lambda r: r[0] - r[1])[:MAX_WINDOWS])
    hm = lambda off: "%02d:%02d" % divmod((start + off) % 1440, 60)  # noqa: E731
    return [[hm(a), hm(b)] for a, b in runs]


def in_window(t, start, end):
    """Is the local time t inside [start, end) (HH:MM; the window may cross midnight)?"""
    st, en = dt.time.fromisoformat(start), dt.time.fromisoformat(end)
    return st <= t < en if st < en else (t >= st or t < en)


class Busy(writer.Busy):
    """LOCAL_LOCK was taken (Chat or the brief writer): the sorter yields. (0.12.0: a writer.Busy, like ContextFull.)"""


class InputError(ValueError):
    """A plain sentence for the page (api.py turns it into a 400)."""



def date_order(store):
    """0.12.2: how the owner writes numeric dates ("dmy" = 30/11, "mdy" = 11/30, None = read 3/4 only when one reading
    is a date), kept in meta `reader` (the 0.12.2 key, so a saved choice still holds). The ledger's date rules use it."""
    saved = store._get_meta("reader", None)  # noqa: SLF001
    v = saved.get("date_order") if isinstance(saved, dict) else None
    return v if v in ("dmy", "mdy") else None


def set_date_order(store, order):
    saved = store._get_meta("reader", None)  # noqa: SLF001
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved["date_order"] = order if order in ("dmy", "mdy") else None
    store._set_meta("reader", saved)  # noqa: SLF001
    return saved["date_order"]

class Background:
    def __init__(self, store, syncer=None, clock=time.time, monotonic=time.monotonic, post=None, log=_stderr,
                 models=None):
        self.store = store
        self.syncer = syncer
        self._models_fn = models            # 0.14.1: fetch(base, key) -> GET /models (tests); else the server's
        self.clock = clock
        self.monotonic = monotonic
        self.log = log
        self._post_fn = post
        self.lengths = 0                    # 0.14.1: answers that stopped at max_tokens (finish "length")
        self._fit = collections.Counter()   # 0.14.2: the last request's fitting

    # settings ------------------------------------------------------------------------------------------------------
    def settings(self):
        """The background model and its context size (meta `sorting`, the 0.8.4 key kept so installs keep their
        model), with the fixed batch limits and night windows the pace and cap rules use."""
        quiet = quiet_times(self.store)
        s = {"model": self.first_listed(), "batch": BATCH, "night_batch": NIGHT_BATCH, "day_cards": DAY_CARDS,
             "day_gpu_min": DAY_GPU_MIN, "backfill": {"windows": night_windows(quiet)}, "ctx_sizes": {},
             "quiet_times": quiet, "extra_bodies": {}}
        saved = self.store._get_meta("sorting", None)  # noqa: SLF001
        if isinstance(saved, dict):
            if isinstance(saved.get("model"), str) and MODEL.fullmatch(saved["model"]):
                s["model"] = saved["model"]
            s["ctx_sizes"] = _ctx_sizes(saved.get("ctx_sizes"))
            s["extra_bodies"] = _extra_bodies(saved.get("model_extra_body"))
            if isinstance(saved.get("backfill"), dict) and _windows(saved["backfill"].get("windows")):
                s["backfill"]["windows"] = _windows(saved["backfill"]["windows"])
        s["date_order"] = date_order(self.store)
        return s

    def extra_for(self, model):
        """The owner's extra request fields for `model` (Setup › AI › Advanced), {} when none."""
        if not model:
            return {}
        try:
            saved = self.store._get_meta("sorting", None)  # noqa: SLF001
        except Exception:  # noqa: BLE001 (no settings yet: none)
            return {}
        return dict(_extra_bodies((saved or {}).get("model_extra_body") if isinstance(saved, dict) else None)
                    .get(model) or {})

    def first_listed(self):
        """No background model picked: the first model the owner's server listed (aisettings' remembered GET /models
        answer, see refresh_models), else NO_MODEL (Setup › AI says "Pick a model")."""
        import aisettings   # imports this module: loaded on first use, not at import
        base = self._server()[0]
        hit = aisettings.cached_list(base) if base else None
        ids = [m.get("id") for m in (hit or {}).get("models") or [] if isinstance(m, dict)]
        return next((m for m in ids if isinstance(m, str) and MODEL.fullmatch(m)), NO_MODEL)

    def refresh_models(self, every_s=600):
        """With no pick and no remembered list: ask the owner's server for its models (aisettings.list_models, the
        one allowed GET /models, 8 s at most), at most once every `every_s` seconds. The background model (from then
        on its first listed model) or NO_MODEL."""
        s = self.settings()
        if s["model"] or not self._server()[0] or (self._models_fn is None and self._post_fn is not None):
            return s["model"]
        now = self.monotonic()
        if now - getattr(self, "_listed_at", -1e9) < every_s:
            return s["model"]
        self._listed_at = now
        import aisettings
        try:
            aisettings.list_models(self.store, self.clock, fetch=self._models_fn)
        except Exception:  # noqa: BLE001 (an unreachable server: the gate says no_model)
            pass
        return self.settings()["model"]

    def update_settings(self, patch):
        """Setup › AI: {model} and/or {ctx_size} (the model's context size; None = ask the server) and/or {extra_body}
        (the model's extra request fields, keys from EXTRA_KEYS; None or {} = none)."""
        if not isinstance(patch, dict):
            raise InputError("Send a JSON object.")
        saved = self.store._get_meta("sorting", None)  # noqa: SLF001
        saved = dict(saved) if isinstance(saved, dict) else {}
        s = self.settings()
        for k, v in patch.items():
            if k == "model":
                if not isinstance(v, str) or not MODEL.fullmatch(v.strip()):
                    raise InputError("Type the model's name as your model server lists it.")
                s["model"] = v.strip()
            elif k == "date_order":
                if v not in ("dmy", "mdy", None):
                    raise InputError("Pick dmy (30/11), mdy (11/30) or none.")
            elif k == "extra_body":
                why = None if v is None else extra_error(v)
                if why:
                    raise InputError(why)
            elif k != "ctx_size":
                raise InputError("Unknown setting.")
        if "ctx_size" in patch:
            v = patch["ctx_size"]
            lo, hi = writer.CTX_LIMITS
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi):
                raise InputError(f"Pick a context size from {lo:,} to {hi:,} tokens.")
            sizes = dict(s["ctx_sizes"])
            sizes.pop(s["model"], None)
            if v is not None:
                sizes[s["model"]] = v
            s["ctx_sizes"] = _ctx_sizes(sizes)
        if "extra_body" in patch:
            extra = dict(s["extra_bodies"])
            extra.pop(s["model"], None)
            if patch["extra_body"] and s["model"]:
                extra[s["model"]] = dict(patch["extra_body"])
            saved["model_extra_body"] = _extra_bodies(extra)
        if "model" in patch or saved.get("model"):   # no pick stays no pick (the server's first model is used)
            saved["model"] = s["model"]
        saved["ctx_sizes"] = s["ctx_sizes"]
        self.store._set_meta("sorting", saved)  # noqa: SLF001
        if "date_order" in patch:
            set_date_order(self.store, patch["date_order"])
        return self.view()

    def view(self):
        """Setup › AI's background model block: the model, its context size, the measured pace and the health."""
        s = self.settings()
        return {"settings": {"model": s["model"], "ctx_size": s["ctx_sizes"].get(s["model"]), "date_order": s["date_order"],
                             "extra_body": s["extra_bodies"].get(s["model"]), "extra_keys": list(EXTRA_KEYS),
                             "ctx_default": writer.CTX_DEFAULT, "ctx_limits": list(writer.CTX_LIMITS)},
                "status": {"server": bool(self._post_fn is not None or self._server()[0]),
                           "capacity": self.capacity(s)},
                "local_health": self.local_health()}

    def _day(self):
        d = self.store._get_meta("sorting_day", {}) or {}  # noqa: SLF001
        today = self._today().isoformat()
        if d.get("day") != today:
            d = {"day": today, "cards": 0, "gpu_s": 0.0, "runs": 0, "fallback": 0}
        return d

    def _save_settings(self, s):
        self.store._set_meta("sorting", {k: v for k, v in s.items() if not k.startswith("_")})  # noqa: SLF001

    # time ------------------------------------------------------------------------------------------------------------
    def _tz(self):
        return rules.tzinfo(self.store.settings())

    def _now(self):
        return dt.datetime.fromtimestamp(self.clock(), self._tz())

    def _today(self):
        return self._now().date()

    def call_guard(self, now=None):
        """(seconds a run may take from now, reason). reason None: go. Never while a Careful brief runs or is due
        within CAREFUL_BEFORE_S ("careful"), within AFTER_CALL_S after a call time or END_BEFORE_S before the next one,
        never while a sync or the brief writer runs."""
        settings = self.store.settings()
        now = now or self._now()
        sy = getattr(self, "syncer", None)
        if sy is not None and (sy.writing is not None or sy.lock.locked()):
            return 0, "brief"
        prev, _ = rules.next_call(settings, now - dt.timedelta(seconds=AFTER_CALL_S + 1))
        if prev is not None and prev <= now:
            return 0, "after_call"
        nxt, _ = rules.next_call(settings, now)
        room = RUN_S if nxt is None else min(RUN_S, (nxt - now).total_seconds() - END_BEFORE_S)
        if room < MIN_RUN_S:
            return 0, "before_call"
        return room, None

    def _server(self):
        loc = (self.store.writer_config().get("local") or {})
        return loc.get("base_url") or "", loc.get("api_key") or ""

    # 0.14.1: the model's context and the in-flight budget ------------------------------------------------------------
    def ctx_size(self, s=None, ask=True):
        """(tokens, source): the background model's context: what the server's GET /models says for it ("server"),
        else the owner's "Context size" for that model ("setting"), else writer.CTX_DEFAULT ("default"). ask False:
        no request (the page), only what the server said last."""
        s = s or self.settings()
        base, key = self._server()
        n = None
        if base and s["model"] and (self._models_fn is not None or self._post_fn is None):
            n = writer.server_ctx(base, key, s["model"], self._models_fn, self.monotonic) if ask else \
                writer.cached_ctx(base, s["model"])
        if n:
            return n, "server"
        n = s["ctx_sizes"].get(s["model"])
        return (n, "setting") if n else (writer.CTX_DEFAULT, "default")

    def budget(self, s=None):
        """The background work's in-flight budget (sorter, reader, picker): 80% of the model's context."""
        return writer.budget_for(self.ctx_size(s)[0])

    def post_bg(self, body, timeout, post_fn=None, budget=None, kind="sorter", units=None, stream=None):
        """0.14.1: one background request (the sorter's, the reader's or the picker's): through `post_fn` (tests) or
        writer._post under the in-flight budget (80% of the model's context). 0.14.2: the answer's prefill timings
        teach the model's pace (writer._post learns from every local answer; a test's post_fn stands in for it) and
        the request's size and measured prefill are counted for `kind` (numbers only). 0.14.3: the answer's output
        pace and its output tokens per unit (`units`: the cards or items it answers) are learned too; while Chat is
        active on this server nothing is sent (Busy("chat"), see writer.chat_active)."""
        base, key = self._server()
        model = body.get("model") if isinstance(body, dict) else None
        extra = self.extra_for(model)
        if extra:   # the owner's extra request fields for this model (keys from EXTRA_KEYS only)
            body = dict(body, **extra)
        if post_fn is not None:
            if writer.chat_active(base):
                raise Busy("chat")
            try:
                res = post_fn(body, timeout)
            except writer.Busy as e:   # (a test's fake server cut by Chat)
                if str(e) == writer.PREEMPTED:
                    self.count_preempt(kind)
                raise
            writer.learn_prefill(base, model, body, res)
        else:
            def notify(event):
                if event == "lock":   # Chat or the brief writer has the server: background work yields (Chat first)
                    raise Busy("The model server is busy.")
            # 0.14.4: streamed, so Chat can cut it (llama-server stops a streamed request when the client goes)
            sbody = dict(body, stream=True, stream_options={"include_usage": True})
            try:
                res = writer._post(base, key, sbody, {"timeout": timeout}, None, self.monotonic,  # noqa: SLF001
                                   notify=notify, budget=self.budget() if budget is None else budget,
                                   stream=stream)   # 0.24.0 (B1): stream(kind, piece) hears the words as they come
            except writer.Busy as e:
                if str(e) == writer.PREEMPTED:
                    self.count_preempt(kind)
                raise
        try:
            if units:
                self.learn_out(kind, units, res, model)
            self._note_request(kind, body, res)
        except Exception as e:  # noqa: BLE001 (counting never breaks the work)
            self.log(f"Callsheet sorting: a request wasn't counted ({type(e).__name__})")
        return res

    # 0.14.2: the prompt's time (prefill) ---------------------------------------------------------------------------------
    def prefill(self, s=None, now=None):
        """{tps, measured, scale, cap_s, cap_tokens, gen_tps, out}: the background model's prefill pace (what writer
        learned from the server's timings this session, else what this store measured before, else
        writer.PREFILL_TPS_DEFAULT), the token estimate's correction, 0.14.3: the output pace (gen_tps, None until
        measured: writer.GEN_TPS_DEFAULT is used), the expected output tokens per unit and kind, the request time cap
        now (cap_s: writer.BG_DAY_S, writer.BG_NIGHT_S in a night window) and the estimated prompt tokens one sorter
        card's request may have inside it (cap_tokens)."""
        s = s or self.settings()
        base = self._server()[0]
        tps = writer.prefill_rate(base, s["model"]) or pace(_vals_get(self.store, "prefill_perf", s["model"]))
        scale = writer.token_scale(base, s["model"])
        if scale == 1.0:
            m = pace(_vals_get(self.store, "prefill_scale", s["model"]))
            scale = min(writer.TOKEN_SCALE[1], max(writer.TOKEN_SCALE[0], m)) if m else 1.0
        gen = writer.gen_rate(base, s["model"]) or pace(_vals_get(self.store, "gen_perf", s["model"]))
        out = {k: round(writer.out_rate(base, s["model"], k)   # 0.14.4: the 80th percentile (writer.upper)
                        or writer.upper(_vals_get(self.store, f"out_{k}", s["model"])) or d, 1)
               for k, d in writer.OUT_TOKENS.items()}
        cap_s = self.cap_s(s, now)
        return {"tps": round(tps, 1) if tps else None, "measured": bool(tps), "scale": round(scale, 3),
                "cap_s": cap_s, "default_tps": writer.PREFILL_TPS_DEFAULT,
                "gen_tps": round(gen, 2) if gen else None, "default_gen_tps": writer.GEN_TPS_DEFAULT, "out": out,
                "cap_tokens": writer.cap_tokens(tps, scale, cap_s, out["sorter"] if gen else 0, gen)}

    def cap_s(self, s=None, now=None):
        """0.14.3: one background request's time cap now: writer.BG_NIGHT_S inside a night window, else BG_DAY_S."""
        s = s or self.settings()
        return writer.BG_NIGHT_S if self.night_of(now or self._now(), s) is not None else writer.BG_DAY_S

    def time_cap(self, s, kind, units, pf=None):
        """0.14.3: the estimated prompt tokens a `kind` request answering `units` cards or items may have so that its
        prefill and its expected output take about cap_s (at least writer.PREFILL_FLOOR_S of prefill). Until this
        model's output pace is measured (any local answer teaches it) the prefill alone counts: the first batch is
        FIRST_BATCH cards anyway."""
        pf = pf or self.prefill(s)
        out = max(1, units) * pf["out"].get(kind, 0) if pf.get("gen_tps") else 0   # once the output pace is known
        return writer.cap_tokens(pf["tps"], pf["scale"], pf["cap_s"], out, pf.get("gen_tps"), floor=units <= 1)

    def learn_out(self, kind, units, res, model=None):
        """0.14.3: an answer's output tokens per card or item for `kind` (writer.learn_output; kept in callsheet.db for
        the next start). Never fails the work."""
        try:
            model = model or self.settings()["model"]
            per = writer.learn_output(self._server()[0], model, kind, units, res)
            if per is not None:
                _vals_add(self.store, f"out_{kind}", model, per)
            return per
        except Exception:  # noqa: BLE001
            return None

    def chat_first(self):
        """0.14.3: Chat is active on the background's server (writer.chat_active): background work starts nothing."""
        return writer.chat_active(self._server()[0])

    # 0.14.4: Chat preempts background work ---------------------------------------------------------------------------
    def night_now(self, s=None):
        """Inside one of the night windows now (where items cut by Chat too often are read)."""
        try:
            return self.night_of(self._now(), s or self.settings()) is not None
        except Exception:  # noqa: BLE001
            return False

    def count_preempt(self, kind):
        """One `kind` request cut by Chat (counted per day and kind; numbers only)."""
        try:
            d = self._bg_day()
            self._kind_day(d, kind)["preempted"] += 1
            self.store._set_meta("bg_day", d)  # noqa: SLF001
        except Exception:  # noqa: BLE001 (counting never breaks the work)
            pass

    def fits(self, body, cap, ptok):
        """(over the in-flight budget, over the prefill cap): a request's two limits."""
        return writer.estimate_tokens(body) > cap, writer.prompt_tokens(body) > ptok

    def _bg_day(self):
        d = self.store._get_meta("bg_day", {}) or {}  # noqa: SLF001
        today = self._today().isoformat()
        if not isinstance(d, dict) or d.get("day") != today:
            d = {"day": today, "kinds": {}}
        return d

    def _kind_day(self, d, kind):
        k = d["kinds"].setdefault(kind, {})
        for f in FIT_KEYS:
            k.setdefault(f, 0)
        for f in ("est_max_s", "prefill_max_s", "prefill_s", "tokens_max", "preempted", "night_only"):
            k.setdefault(f, 0)
        return k

    def fit_add(self, kind, counts):
        """0.14.2: what a request's fitting did (split, moved, alone, clip_*), counted per day and kind."""
        counts = {k: int(v) for k, v in dict(counts or {}).items() if k in FIT_KEYS and v}
        if not counts:
            return
        d = self._bg_day()
        k = self._kind_day(d, kind)
        for f, v in counts.items():
            k[f] += v
        self.store._set_meta("bg_day", d)  # noqa: SLF001

    def _note_request(self, kind, body, res):
        """One background request: its estimated size and prefill, the server's measured prefill (prompt_ms), and the
        pace it teaches (kept in callsheet.db for the next start). Numbers only."""
        s = self.settings()
        pf = self.prefill(s)
        smp = writer.prefill_sample(body, res)
        d = self._bg_day()
        k = self._kind_day(d, kind)
        k["requests"] += 1
        k["tokens_max"] = max(k["tokens_max"], writer.prompt_tokens(body))
        k["est_max_s"] = max(k["est_max_s"], writer.prefill_secs(body, pf["tps"], pf["scale"]))
        if smp and smp.get("prompt_ms"):
            secs = round(smp["prompt_ms"] / 1000, 1)
            k["prefill_max_s"] = max(k["prefill_max_s"], secs)
            k["prefill_s"] = round(k["prefill_s"] + secs, 1)
            k["over"] += secs > PREFILL_OVER_S
        self.store._set_meta("bg_day", d)  # noqa: SLF001
        if smp and smp.get("tps"):
            _vals_add(self.store, "prefill_perf", s["model"], smp["tps"])
        if smp and smp.get("scale"):
            _vals_add(self.store, "prefill_scale", s["model"], smp["scale"])
        if smp and smp.get("gen"):
            _vals_add(self.store, "gen_perf", s["model"], smp["gen"])

    def bg_numbers(self):
        """0.14.2 "Copy the numbers": the background model's prefill pace and cap, and today's requests per kind
        (counts, sizes and seconds only)."""
        s = self.settings()
        d = self._bg_day()
        return {"model": s["model"], "prefill": self.prefill(s), "today": d["kinds"],
                "preempt": writer.preempt_view()}

    def _post(self, body, timeout=TIMEOUT_S):
        """One request. timeout: the room left in the run (a batch never runs past the budget). The sorter never
        waits behind Chat or the brief writer: when LOCAL_LOCK is taken it gives up (Busy)."""
        timeout = max(5, min(TIMEOUT_S, int(timeout)))
        res = self.post_bg(body, timeout, self._post_fn)
        if writer.finished_long(res):   # 0.14.1: counted; handled as before (an unusable answer: rules labels)
            self.lengths += 1
        return res

    def _perf(self, model=None):
        """The last 50 batches' seconds per card (0.9.0: per card, as day and night batches differ in size; 0.14.1:
        per model, the sorter's model setting unless named)."""
        return perf_get(self.store, "sorting_perf", model or self.settings()["model"])

    def _add_perf(self, secs, cards, model=None):
        perf_add(self.store, "sorting_perf", model or self.settings()["model"], secs, cards)

    def capacity(self, s=None, d=None):
        """0.14.1: the measured pace on the background model and what it allows: seconds per card, cards an hour and a
        day (inside the day caps), the batch sizes it picks, the model's context and the in-flight budget."""
        s = s or self.settings()
        d = d or self._day()
        card_s = pace(self._perf(s["model"]))
        ctx, src = self.ctx_size(s, ask=False)
        out = {"model": s["model"], "card_s": round(card_s, 2) if card_s else None, "per_hour": None, "per_day": None,
               "batch": size_for(card_s, s["batch"]), "night_batch": size_for(card_s, s["night_batch"]),
               "ctx": ctx, "ctx_from": src, "budget": writer.budget_for(ctx), "lengths": int(d.get("length") or 0)}
        if card_s:
            hour = 3600 / card_s
            out["per_hour"] = int(round(hour, -1)) if hour >= 100 else max(1, int(round(hour)))
            out["per_day"] = int(min(s["day_cards"], s["day_gpu_min"] * 60 / card_s))
        return out

    def local_health(self):
        """0.8.5: the local model's health for Setup: {healthy} or {healthy: false, since (seconds), since_text (the
        owner's time format, with the day when it isn't today), next_check}."""
        v = writer.health_view()
        if v.get("healthy"):
            return {"healthy": True}
        settings = self.store.settings()
        tz, fmt = rules.tzinfo(settings), rules.time_format(settings)
        d = dt.datetime.fromtimestamp(v["since"], tz)
        text = rules.clock(d, fmt)
        if d.date() != self._today():
            text = f"{d:%b} {d.day}, {text}"
        return {"healthy": False, "since": int(v["since"]), "since_text": text, "next_check": int(v["next_check"])}

    def check_model(self):
        """0.8.5 Setup "Check again": a real health probe now. Returns view()."""
        writer.check_local(self.log)
        return self.view()

    def night_of(self, now, s):
        """The night when now is inside one of the night windows, else None. A night runs noon to noon, so
        22:00-01:00 and the 04:00-06:00 after it are the same night (the evening's date)."""
        t = now.time()
        if not any(in_window(t, st, en) for st, en in s["backfill"]["windows"]):
            return None
        return (now - dt.timedelta(hours=12)).date()

