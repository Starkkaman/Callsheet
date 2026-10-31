"""Writer evaluation (0.5.0, "contenders"): the owner compares writer setups on today's real brief data, blind.

The owner defines 2 to 4 contenders (store.eval_settings, validated here by check_contender), each:

    name         shown after the blind choices are submitted
    provider     "local" (the configured OpenAI-compatible server) or "claude" (needs the Anthropic key)
    model        a model id (the UI offers the local server's /models list, or the Claude models)
    thinking     on/off; effort low/medium/high (sent only with thinking on for local models; Claude's effort
                 is output_config.effort, default "medium" like the live writer); budget: the local reasoning
                 budget (with thinking on; default the owner's local setting)
    sampling     optional temperature, top_p and top_k (local models only; Claude takes none)
    profile      a writing profile (Default or one of the owner's)

and a mode: "each" (each contender against each other one) or "baseline" (every other contender against
the baseline). Old (0.3.3 to 0.4.x) settings are migrated to the three fixed setups they described: G0, Q-low
and Q-med as contenders, mode baseline with Q-med as the baseline (the old pairs were G0 vs Q-med and Q-low
vs Q-med).

Runs only when the owner presses "Run evaluation" (POST api/eval/run); never on a schedule. It reads Google the
same way a sync does (Syncer.writer_data: same reads, rules, stripping, fit and row ids) but stores nothing live:
no cached brief, last sync, account status or last writer. The data goes only to the local model server and,
for Claude contenders, to Anthropic, one request at a time, contenders in the owner's order. Every contender
gets what a live brief would send (the ask: area ids, the rules' top refs, the time format) with its own
writing profile; local contenders the local system message (writer.local_system) and schema, Claude
contenders the composed system prompt and the schema as output format. All get the same payload, fitted once
for the largest local answer room and the longest system message. Answers are parsed and scored the way the
brief reads them. Afterwards one tiny request loads the everyday local model again (the writer's local
model), when a local contender used another model. A run doesn't start, and stops before the next contender,
within 10 minutes of an enabled call time.

Results stay in <data>/writer-eval/<UTC timestamp>/ (dir 0700, files 0600): payload.txt, data.json,
responses.jsonl (one line per contender: request parameters without the messages, raw response, metrics,
parsed answer), metrics.json, run.json (with the contender snapshot, mode and pairs) and, once the owner
picks, choices.json. The last 5 runs are kept; runs from before 0.5.0 stay readable and downloadable.
The page shows the pairs in an A/B order seeded per run; which contender was A or B, the results and the
metrics are only returned after the owner submits choices. Logs: counts and times only.
"""
import datetime as dt
import itertools
import json
import os
import random
import re
import secrets
import shutil
import sys
import threading
import time

import evalscore
import rules
import writer

# --- the fixed setups before 0.5.0 (migration of old settings, reading old runs) ---------------
GEMMA_MODEL = "gemma-4-26b-a4b-it-qat-q4_0"
QWEN_MODEL = "Qwen3.8-27B-UD-Q3_K_XL"
LEGACY_CONFIGS = (
    {"name": "G0", "family": "gemma"},
    {"name": "Q-low", "family": "qwen", "effort": "low", "budget": 2048},
    {"name": "Q-med", "family": "qwen", "effort": "medium", "budget": 4096},
)
LEGACY_PAIRS = (("G0", "Q-med"), ("Q-low", "Q-med"))
# Qwen sampling as in the replay harness's Phase 2; G0 sent none, like the production request.
QWEN_SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20}

# --- contenders ----------------------------------------------------------------------------
MIN_CONTENDERS = 2
MAX_CONTENDERS = 4
MODES = ("each", "baseline")
PROVIDERS = ("local", "claude")
EFFORTS = ("low", "medium", "high")
NAME_MAX = 40
MODEL_MAX = 120
BUDGET = (256, 32768)
FIELDS = ("name", "provider", "model", "thinking", "effort", "budget", "temperature", "top_p", "top_k", "profile")
THINK_TIMEOUT = 450       # seconds at least per local request with thinking on (as the old Qwen setups)
CLAUDE_MAX_TOKENS = 16000  # as the live Claude writer
CLAUDE_EFFORT = "medium"   # as the live Claude writer
RELOAD = "Reload the everyday model"
RELOAD_MAX_TOKENS = 8
PICKS = ("A", "B", "same")
WINDOW_S = 10 * 60        # no run starts (and no contender starts) within 10 minutes of an enabled call time
KEEP = 5
DIRNAME = "writer-eval"
RUN_ID = r"\d{8}T\d{6}Z(?:-\d{1,2})?"
CONTENDER_ID = r"[\w-]{1,40}"
_RUN_RE = re.compile(RUN_ID)
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
OFF = "The evaluation tools are off. Turn them on in Setup, Delivery, Advanced."
GONE = "That contender doesn't exist."
BROKEN = "Something went wrong inside the evaluation. Check the app log."


def _stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class Refused(Exception):
    """The run can't start now (HTTP 409). The message is a plain sentence."""


class Invalid(ValueError):
    """A contender or setting the owner sent isn't valid (HTTP 400). The message is a plain sentence."""


def default_settings():
    return {"enabled": False, "mode": "each", "baseline": None, "contenders": []}


def blank_contender():
    return {"id": None, "name": "", "provider": "local", "model": "", "thinking": False, "effort": None,
            "budget": None, "temperature": None, "top_p": None, "top_k": None, "profile": "default"}


def legacy_contenders(gemma=GEMMA_MODEL, qwen=QWEN_MODEL):
    """The three fixed setups of 0.3.3 to 0.4.x as contenders (ids fixed, so a migration needs no write)."""
    out = []
    for cfg in LEGACY_CONFIGS:
        c = dict(blank_contender(), id="legacy-" + cfg["name"].lower(), name=cfg["name"])
        if cfg["family"] == "gemma":
            c["model"] = gemma
        else:
            c.update(model=qwen, thinking=True, effort=cfg["effort"], budget=cfg["budget"], **QWEN_SAMPLING)
        out.append(c)
    return out


def _text(v, n):
    return v.strip()[:n] if isinstance(v, str) else ""


def settings_from(saved):
    """The evaluation settings from what the store keeps. A pre-0.5.0 value ({enabled, gemma_model,
    qwen_model}, no contenders) becomes the old three setups, compared against Q-med."""
    saved = saved if isinstance(saved, dict) else {}
    e = default_settings()
    e["enabled"] = saved.get("enabled") is True
    if "contenders" not in saved and ("gemma_model" in saved or "qwen_model" in saved):
        e["contenders"] = legacy_contenders(_text(saved.get("gemma_model"), MODEL_MAX) or GEMMA_MODEL,
                                            _text(saved.get("qwen_model"), MODEL_MAX) or QWEN_MODEL)
        e.update(mode="baseline", baseline="legacy-q-med")
        return e
    if saved.get("mode") in MODES:
        e["mode"] = saved["mode"]
    for c in saved.get("contenders") or []:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and len(e["contenders"]) < MAX_CONTENDERS:
            e["contenders"].append(dict(blank_contender(), **{k: c[k] for k in ("id",) + FIELDS if k in c}))
    ids = [c["id"] for c in e["contenders"]]
    e["baseline"] = saved.get("baseline") if saved.get("baseline") in ids else (ids[0] if ids else None)
    return e


def _number(v, lo, hi, whole, msg, low_open=False):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (whole and not isinstance(v, int)) \
            or v != v or v > hi or v < lo or (low_open and v <= lo):
        raise Invalid(msg)
    return int(v) if whole else round(float(v), 3)


def check_contender(body, old, others, profiles, claude_key):
    """A contender after a change: body (a partial or full contender) on top of old (None for a new one).
    others: the other contenders (names must be unique), profiles: {profile id: name}, claude_key: whether an
    Anthropic key is saved. Returns the clean contender; raises Invalid with a plain sentence."""
    if not isinstance(body, dict):
        raise Invalid("Send the contender as a JSON object.")
    c = dict(old or blank_contender())
    for k, v in body.items():
        if k not in FIELDS:
            raise Invalid(f"Contenders can't change {str(k)[:40]}.")
        c[k] = v
    name = c["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX or _CTRL.search(name):
        raise Invalid(f"Give the contender a name of 1 to {NAME_MAX} characters.")
    c["name"] = name.strip()
    if any(o["name"].casefold() == c["name"].casefold() for o in others):
        raise Invalid(f"Another contender is already called {c['name']}.")
    if c["provider"] not in PROVIDERS:
        raise Invalid("Pick a local model or Claude.")
    model = c["model"]
    if not isinstance(model, str) or len(model.strip()) > MODEL_MAX or _CTRL.search(model):
        raise Invalid(f"The model name must be text of at most {MODEL_MAX} characters.")
    c["model"] = model.strip()
    if not c["model"]:
        raise Invalid(f"Pick a model for {c['name']}.")
    if not isinstance(c["thinking"], bool):
        raise Invalid("Thinking must be on or off.")
    if c["effort"] is not None and c["effort"] not in EFFORTS:
        raise Invalid("Effort must be low, medium or high.")
    c["budget"] = _number(c["budget"], *BUDGET, True,
                          f"The thinking budget must be a whole number from {BUDGET[0]} to {BUDGET[1]:,} tokens.")
    c["temperature"] = _number(c["temperature"], 0, 2, False, "The temperature must be a number from 0 to 2.")
    c["top_p"] = _number(c["top_p"], 0, 1, False, "Top p must be a number above 0 and at most 1.", low_open=True)
    c["top_k"] = _number(c["top_k"], 1, 1000, True, "Top k must be a whole number from 1 to 1,000.")
    if not isinstance(c["profile"], str) or c["profile"] not in profiles:
        raise Invalid("That writing profile doesn't exist.")
    if c["provider"] == "claude":
        if c["model"] not in writer.MODELS:
            raise Invalid("Pick one of the Claude models.")
        if not claude_key:
            raise Invalid("Add an Anthropic API key in Setup, Delivery to use Claude.")
        if any(c[k] is not None for k in ("budget", "temperature", "top_p", "top_k")):
            raise Invalid("Claude doesn't take a thinking budget or sampling settings. Use effort instead.")
    elif not c["thinking"]:
        c["effort"] = c["budget"] = None  # nothing to spend them on
    if not c.get("id"):
        c["id"] = "con_" + secrets.token_hex(4)
    return {k: c[k] for k in ("id",) + FIELDS}


def patch_settings(e, patch):
    """enabled, mode and baseline on top of the settings e (a copy is changed and returned)."""
    if not isinstance(patch, dict):
        raise Invalid("Send the evaluation settings as a JSON object.")
    e = dict(e)
    ids = [c["id"] for c in e["contenders"]]
    for key, val in patch.items():
        if key == "enabled":
            if not isinstance(val, bool):
                raise Invalid("enabled must be true or false.")
            e["enabled"] = val
        elif key == "mode":
            if val not in MODES:
                raise Invalid("The mode must be each against each, or against a baseline.")
            e["mode"] = val
        elif key == "baseline":
            if val not in ids:
                raise Invalid("Pick one of the contenders as the baseline.")
            e["baseline"] = val
        else:
            raise Invalid(f"The evaluation settings can't change {str(key)[:40]}.")
    return e


def pairings(keys, mode, baseline=None):
    """The pairs to compare: each against each (in list order), or every other contender against the
    baseline (other, baseline), as the old G0/Q-low vs Q-med pairs."""
    keys = list(keys)
    if mode == "baseline":
        return [(k, baseline) for k in keys if k != baseline] if baseline in keys else []
    return list(itertools.combinations(keys, 2))


def pair_orders(seed, pairs=LEGACY_PAIRS):
    """[(a, b)] per pair, each in a random order that only depends on the run's seed."""
    rng = random.Random(seed)
    return [(x, y) if rng.random() < 0.5 else (y, x) for x, y in pairs]


def tally(keys, picks):
    """{key: {wins, losses, ties}} from the submitted picks ({pair: {a, b, pick}})."""
    out = {k: {"wins": 0, "losses": 0, "ties": 0} for k in keys}
    for p in (picks or {}).values():
        a, b, pick = p.get("a_key", p.get("a")), p.get("b_key", p.get("b")), p.get("pick")
        if a not in out or b not in out:
            continue
        if pick == "same":
            out[a]["ties"] += 1
            out[b]["ties"] += 1
        elif pick in ("A", "B"):
            win, lose = (a, b) if pick == "A" else (b, a)
            out[win]["wins"] += 1
            out[lose]["losses"] += 1
    return out


# --- requests ------------------------------------------------------------------
def contender_body(c, payload_text, ids, opts, ask=None):
    """(request body, options for writer._post) for a local contender. opts: the owner's local_options().
    Answer room: the owner's max tokens, plus the budget with thinking on."""
    o = dict(opts, thinking=bool(c["thinking"]))
    if o["thinking"]:
        o["reasoning_budget"] = c.get("budget") or opts["reasoning_budget"]
        o["timeout"] = max(opts["timeout"], THINK_TIMEOUT)
    body = writer.local_body(c["model"], payload_text, ids, o, ask)
    if o["thinking"] and c.get("effort"):
        body["reasoning_effort"] = c["effort"]
    for k in ("temperature", "top_p", "top_k"):
        if c.get(k) is not None:
            body[k] = c[k]
    return body, o


def claude_params(c, payload_text, ids, ask=None):
    """The messages.create arguments for a Claude contender: the composed system prompt (no tools), the
    per-request schema as output format, effort, thinking adaptive or off. No sampling: the Messages API (and
    the pinned SDK) has no temperature or top_p for these models."""
    p = {"model": c["model"], "max_tokens": CLAUDE_MAX_TOKENS,
         "output_config": {"effort": c.get("effort") or CLAUDE_EFFORT,
                           "format": {"type": "json_schema", "schema": writer.build_schema(ids, ask)}},
         "thinking": {"type": "adaptive"} if c["thinking"] else {"type": "disabled"},
         "system": writer.compose_system(None, ids, ask), "messages": [{"role": "user", "content": payload_text}]}
    return p


def claude_call(key, params):
    """(the answer in the local server's shape for score(), the raw response as JSON). Raises WriterError."""
    if not key:
        raise writer.WriterError("Add an Anthropic API key in Setup.")
    sdk = writer._sdk()
    try:
        resp = writer._client(sdk, key).messages.create(**params)
    except sdk.APIError as e:
        raise writer.WriterError(writer._claude_error(sdk, e)) from None
    if resp.stop_reason == "refusal":
        raise writer.WriterError("Claude declined to write this brief.")
    blocks = resp.content or []
    text = next((b.text for b in blocks if getattr(b, "type", "") == "text"), None)
    thinking = "".join(getattr(b, "thinking", "") or "" for b in blocks if getattr(b, "type", "") == "thinking")
    usage = getattr(resp, "usage", None)
    finish = {"end_turn": "stop", "max_tokens": "length"}.get(resp.stop_reason, resp.stop_reason)
    res = {"model": str(resp.model),
           "choices": [{"message": {"role": "assistant", "content": text, "reasoning_content": thinking or None},
                        "finish_reason": finish}],
           "usage": {"prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                     "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0)}}
    try:
        raw = resp.model_dump(mode="json")
    except Exception:  # noqa: BLE001 (an SDK without pydantic dumps: keep the converted answer)
        raw = res
    return res, raw


def reload_body(model, opts=None):
    """The tiny request that loads the everyday model again; opts carries the owner's server slot, if any."""
    body = {"model": model, "stream": False, "max_tokens": RELOAD_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Reply with OK."}]}
    return writer.with_slot(body, opts or {})


def fit_opts(opts, contenders):
    """The options the shared payload is fitted with: the owner's context, the largest local answer room."""
    budgets = [c.get("budget") or opts["reasoning_budget"] for c in contenders
               if c["provider"] == "local" and c["thinking"]]
    return dict(opts, thinking=bool(budgets), reasoning_budget=max(budgets) if budgets else opts["reasoning_budget"])


def needs_reload(contenders, everyday):
    return bool(everyday) and any(c["provider"] == "local" and c["model"] != everyday for c in contenders)


# --- metrics -------------------------------------------------------------------
def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def thread_texts(data):
    """{id: the thread text sent for that row} (the scorer's needs_placeholder reads it)."""
    return {r["id"]: "\n\n".join(m.get("text") or "" for m in r.get("messages") or [])
            for r in data.get("replies") or [] if r.get("suggest")}


def score(res, ids, threads, wall, ask=None, payload_text=None):
    """(metrics, parsed) for one raw /chat/completions answer. parsed: as the brief would show it
    (writer.parse_output with the ask and the payload sent): {summary, replies} and, with an ask, areas, top
    and gists (details that fail the verbatim check dropped); None when the answer isn't usable."""
    res = res if isinstance(res, dict) else {}
    choices = res.get("choices") if isinstance(res.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
    timings = res.get("timings") if isinstance(res.get("timings"), dict) else {}
    content = msg.get("content")
    reasoning = msg.get("reasoning_content") if isinstance(msg.get("reasoning_content"), str) else msg.get("reasoning")
    out, valid, rescued = None, False, False
    if isinstance(content, str) and content.strip():
        try:
            out = json.loads(re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content))
            valid = isinstance(out, dict)
        except ValueError:
            out = None
    elif isinstance(reasoning, str):
        out = next((o for o in writer._json_in(reasoning) if writer._complete(o, ids, ask)), None)
        rescued = out is not None
    parsed, p = None, None
    if isinstance(out, dict):
        try:
            p = writer.parse_output(out, ids, ask, writer.seen_texts(payload_text) if payload_text else None)
            parsed = {"summary": p["summary"], "replies": p["replies"]}
            if ask is not None:
                parsed.update(areas=p["areas"], top=p["top"], gists=p["gists"])
        except writer.WriterError:
            parsed, p = None, None
    texts = evalscore.reply_texts(out)
    returned = [i for i in ids if isinstance(texts.get(i), str) and texts[i].strip()]
    drafts = parsed["replies"] if parsed else {}
    flagged = [i for i in ids if i in drafts and writer.states_decision(drafts[i])]
    sc = evalscore.score_drafts(drafts, threads)
    metrics = {
        "valid_json": valid, "schema_valid": evalscore.schema_valid(out, ids, ask),
        "rescued_from_thinking": rescued,
        "finish_reason": str(choice.get("finish_reason"))[:20] if choice.get("finish_reason") else None,
        "wall_s": round(wall, 1),
        "prompt_tokens": _num(usage.get("prompt_tokens")), "completion_tokens": _num(usage.get("completion_tokens")),
        "prompt_n": _num(timings.get("prompt_n")), "cache_n": _num(timings.get("cache_n")),
        "predicted_per_second": _num(timings.get("predicted_per_second")),
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
        "ids_expected": len(ids), "ids_returned": len(returned), "empties": len(ids) - len(returned),
        "flagged": len(flagged), "flagged_ids": flagged,
        "commitments": sc["drafts_flagged"], "commitment_ids": sc["flagged_ids"],
        "placeholder_used": sc["placeholder_used"], "placeholder_needed": sc["placeholder_needed"],
        "placeholder_used_when_needed": sc["placeholder_used_when_needed"],
        "invented_when_needed": sc["invented_when_needed"], "error": None}
    if ask is not None:
        metrics.update(evalscore.shape_040(out, ids, ask, p))
    return metrics, parsed


# --- files ---------------------------------------------------------------------
def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _settings_of(c):
    """The contender's request settings, for the run snapshot, results and export."""
    return {k: c.get(k) for k in ("provider", "model", "thinking", "effort", "budget", "temperature", "top_p", "top_k")}


def _legacy_snapshot(info):
    """The contenders of a pre-0.5.0 run (its fixed setups), keyed by their names."""
    models = info.get("models") or {}
    out = []
    for c in legacy_contenders(models.get("gemma") or GEMMA_MODEL, models.get("qwen") or QWEN_MODEL):
        out.append(dict(c, key=c["name"], profile_name=None))
    return out


class Evaluator:
    def __init__(self, store, syncer, clock=time.time, log=_stderr, monotonic=time.monotonic):
        self.store = store
        self.syncer = syncer
        self.clock = clock
        self.log = log
        self.monotonic = monotonic
        self.root = store.dir / DIRNAME
        self.lock = threading.Lock()
        self.job = None     # while running: {run, step, steps, config, t0}
        self.thread = None

    # --- checks --------------------------------------------------------------
    def enabled(self):
        return self.store.eval_settings()["enabled"]

    def near_call(self):
        """'<label> at HH:MM' when an enabled call time is within 10 minutes before or after now, else None."""
        s = self.store.settings()
        now = dt.datetime.fromtimestamp(self.clock(), rules.tzinfo(s))
        nxt, call = rules.next_call(s, now - dt.timedelta(seconds=WINDOW_S + 1))
        if nxt is not None and nxt <= now + dt.timedelta(seconds=WINDOW_S):
            return f"{call.get('label') or 'A brief'} at {rules.hhmm(nxt)}"
        return None

    def _setup_problem(self, es, wcfg):
        """Why the contenders can't run as set up (a plain sentence), or None."""
        cons = es["contenders"]
        if len(cons) < MIN_CONTENDERS:
            return "Add at least two contenders first."
        if es["mode"] == "baseline" and es["baseline"] not in [c["id"] for c in cons]:
            return "Pick the baseline contender first."
        if any(c["provider"] == "local" for c in cons) and not wcfg["local"]["base_url"]:
            return "Add the local model's server address in Setup, Delivery first."
        if any(c["provider"] == "claude" for c in cons) and not wcfg["claude"]["api_key"]:
            return "Add an Anthropic API key in Setup, Delivery first, or remove the Claude contenders."
        profiles = {p["id"] for p in self.store.writer_public()["prompt_profiles"]}
        for c in cons:
            if c["profile"] not in profiles:
                return f"The writing profile of {c['name']} was deleted. Pick another one."
        return None

    def reason(self):
        """Why a run can't start now (a plain sentence), or None."""
        if self.job is not None:
            return "An evaluation is already running."
        problem = self._setup_problem(self.store.eval_settings(), self.store.writer_config())
        if problem:
            return problem
        if not self.store.has_accounts():
            return "Connect a Google account first."
        near = self.near_call()
        if near:
            return f"The {near} is too close. Run the evaluation more than 10 minutes before or after a brief."
        if self.syncer.lock.locked():
            return "A sync is running. Try again in a minute."
        return None

    def _snapshot(self, es, wcfg):
        """The run's contenders as they are now (keys c1.. in the owner's order, the profile texts resolved),
        the pairs (by key) and the everyday local model to load again afterwards."""
        profiles = {p["id"]: p for p in self.store.writer_public()["prompt_profiles"]}
        cons = []
        for i, c in enumerate(es["contenders"], 1):
            p = profiles[c["profile"]]
            cons.append(dict(c, key=f"c{i}", profile_name=p["name"],
                             prompts={k: p[k] for k in writer.PROMPT_KEYS}))
        keys = {c["id"]: c["key"] for c in cons}
        pairs = pairings([c["key"] for c in cons], es["mode"], keys.get(es["baseline"]))
        return {"contenders": cons, "mode": es["mode"], "baseline": keys.get(es["baseline"]) if es["mode"] ==
                "baseline" else None, "pairs": [list(p) for p in pairs], "everyday_model": wcfg["local"]["model"]}

    # --- run -----------------------------------------------------------------
    def start(self):
        with self.lock:
            why = self.reason()
            if why:
                raise Refused(why)
            snap = self._snapshot(self.store.eval_settings(), self.store.writer_config())
            steps = len(snap["contenders"]) + (1 if needs_reload(snap["contenders"], snap["everyday_model"]) else 0)
            if not self.syncer.lock.acquire(blocking=False):  # held until the Google reads are done
                raise Refused("A sync is running. Try again in a minute.")
            try:
                run_id = self._new_dir()
                self.job = {"run": run_id, "step": 0, "steps": steps, "config": "Reading mail and calendar",
                            "t0": self.monotonic()}
            except Exception:
                self.syncer.lock.release()
                raise
        self.thread = threading.Thread(target=self._run, args=(run_id, snap), name="callsheet-eval", daemon=True)
        self.thread.start()
        return self.status()

    def _new_dir(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        base = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id, i = base, 1
        while (self.root / run_id).exists():
            i += 1
            run_id = f"{base}-{i}"
        (self.root / run_id).mkdir(mode=0o700)
        os.chmod(self.root / run_id, 0o700)
        self._prune(keep=run_id)
        return run_id

    def _prune(self, keep):
        runs = self._run_ids()
        for old in runs[:-KEEP]:
            if old != keep:
                shutil.rmtree(self.root / old, ignore_errors=True)

    def _run_ids(self):
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and _RUN_RE.fullmatch(p.name))

    def _save_run(self, d, info):
        _write(d / "run.json", _json(info))

    def _ask(self, ask, c):
        return dict(ask, prompts=writer.effective_prompts(c["prompts"]))

    def _run(self, run_id, snap):
        d = self.root / run_id
        info = dict({"id": run_id, "format": 2, "started": dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
                     .isoformat(timespec="seconds"), "finished": None, "status": "running", "errors": [],
                     "seed": secrets.randbits(32)}, **snap)
        t0 = self.monotonic()
        cons = snap["contenders"]
        held = True  # the sync lock, taken in start(): released as soon as the Google reads are done
        try:
            self._save_run(d, info)
            wcfg = self.store.writer_config()
            loc = wcfg["local"]
            try:
                data, ids, ask, read_errors = self.syncer.writer_data(wcfg)
            finally:
                held = False
                self.syncer.lock.release()
            if read_errors:
                info["errors"].append("Some accounts couldn't be read, so the data may be incomplete.")
            opts = writer.local_options(loc)
            asks = {c["key"]: self._ask(ask, c) for c in cons}
            longest = max(asks.values(), key=lambda a: len(writer.local_system(ids, a)))
            payload = writer.fit_local(data, ids, fit_opts(opts, cons), self.log, ask=longest) \
                if any(c["provider"] == "local" for c in cons) else writer.build_payload(data)
            info["ids"] = list(ids)
            info["ask"] = {"areas": ask["areas"], "refs": ask["refs"], "time_format": ask["time_format"]}
            _write(d / "payload.txt", payload)
            _write(d / "data.json", _json(data))
            self._save_run(d, info)
            threads = thread_texts(data)
            metrics, lines, ran = {}, [], []
            for n, c in enumerate(cons, 1):
                near = self.near_call()
                if near:
                    info["errors"].append(f"Stopped before {c['name']}: the {near} is too close.")
                    info["status"] = "stopped"
                    break
                self.job.update(step=n, config=c["name"])
                ran.append(c)
                line = self._one(c, payload, ids, asks[c["key"]], opts, loc, wcfg, threads)
                if line["error"]:
                    info["errors"].append(f"{c['name']}: {line['error']}")
                metrics[c["name"]] = line["metrics"]
                lines.append(line)
                _write(d / "responses.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
                _write(d / "metrics.json", _json(metrics))
                m = line["metrics"]
                self.log(f"Callsheet eval: contender {n} " + (f"failed, {m['wall_s']}s" if line["error"] else
                         f"finish {m['finish_reason'] or 'unknown'}, {m['ids_returned']} of {m['ids_expected']} "
                         f"drafts, {m['flagged']} flagged, {m['wall_s']}s"))
            if needs_reload(ran, snap["everyday_model"]):
                self.job.update(step=self.job["steps"], config=RELOAD)
                try:
                    writer._post(loc["base_url"], loc["api_key"], reload_body(snap["everyday_model"], opts),
                                 {"timeout": opts["timeout"]}, self.log, self.monotonic)
                except writer.WriterError as e:
                    info["errors"].append(f"{RELOAD}: {e}")
            if info["status"] == "running":
                info["status"] = "done"
        except Exception as e:  # noqa: BLE001
            info["status"] = "failed"
            info["errors"].append(str(e) if isinstance(e, writer.WriterError) else BROKEN)
            self.log(f"Callsheet eval: failed ({type(e).__name__})")
        finally:
            if held:
                self.syncer.lock.release()
            info["finished"] = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="seconds")
            try:
                self._save_run(d, info)
            except OSError:
                pass
            self.log(f"Callsheet eval: {info['status']}, {len(info['errors'])} problems, "
                     f"{self.monotonic() - t0:.1f}s")
            with self.lock:
                self.job = None

    def _one(self, c, payload, ids, ask, opts, loc, wcfg, threads):
        """One contender's request and its scored answer (a responses.jsonl line). Never raises."""
        line = {"contender": c["key"], "config": c["name"], "model": c["model"], "settings": _settings_of(c),
                "profile": c["profile_name"], "request": None, "response": None, "metrics": None, "parsed": None,
                "error": None}
        s0 = self.monotonic()
        try:
            if c["provider"] == "claude":
                params = claude_params(c, payload, ids, ask)
                line["request"] = {k: v for k, v in params.items() if k not in ("messages", "system")}
                res, line["response"] = claude_call(wcfg["claude"]["api_key"], params)
            else:
                body, o = contender_body(c, payload, ids, opts, ask)
                line["request"] = {k: v for k, v in body.items() if k != "messages"}
                res = line["response"] = writer._post(loc["base_url"], loc["api_key"], body, o, self.log,
                                                      self.monotonic)
            line["metrics"], line["parsed"] = score(res, ids, threads, self.monotonic() - s0, ask, payload)
        except writer.WriterError as e:
            line["error"] = str(e)
        except Exception as e:  # noqa: BLE001 (one contender failing never stops the others)
            line["error"] = BROKEN
            self.log(f"Callsheet eval: {c['key']} unexpected {type(e).__name__}")
        if line["error"]:
            line["metrics"] = {"error": line["error"], "wall_s": round(self.monotonic() - s0, 1)}
        return line

    # --- models --------------------------------------------------------------
    def models(self):
        """What the contender form offers: the local server's model ids (GET /models) and, with an Anthropic
        key saved, the Claude models. A local server that can't be reached is a plain sentence, not an error."""
        wcfg = self.store.writer_config()
        loc = wcfg["local"]
        out = {"local": [], "local_error": None, "everyday": loc["model"] or None,
               "claude": [{"id": k, "label": v[0]} for k, v in writer.MODELS.items()] if wcfg["claude"]["api_key"]
               else []}
        if not loc["base_url"]:
            out["local_error"] = "Add the local model's server address in Setup, Delivery to pick a local model."
            return out
        try:
            res = writer._local("GET", loc["base_url"], "/models", loc["api_key"], timeout=writer.LOCAL_TEST_TIMEOUT)
        except writer.WriterError as e:
            out["local_error"] = str(e)
            return out
        items = res.get("data") if isinstance(res, dict) else None
        if not isinstance(items, list):
            items = res.get("models") if isinstance(res, dict) else None
        names = [str(m.get("id") or m.get("name") or "")[:MODEL_MAX] for m in items or [] if isinstance(m, dict)]
        out["local"] = list(dict.fromkeys(n for n in names if n and not _CTRL.search(n)))
        if not out["local"]:
            out["local_error"] = "The local model server lists no models."
        return out

    # --- reads ---------------------------------------------------------------
    def _info(self, run_id):
        try:
            info = json.loads((self.root / run_id / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(info, dict):
            return None
        if info.get("status") == "running" and (self.job or {}).get("run") != run_id:
            info["status"] = "interrupted"
        return info

    def runs(self):
        out = []
        for run_id in reversed(self._run_ids()):
            info = self._info(run_id) or {"status": "unreadable", "started": None}
            out.append({"id": run_id, "at": info.get("started"), "status": info.get("status"),
                        "contenders": len(info.get("contenders") or []) if "contenders" in info else
                        (len(LEGACY_CONFIGS) if "models" in info else None),
                        "chosen": (self.root / run_id / "choices.json").exists()})
        return out

    def status(self):
        job = self.job
        last = None
        ids = self._run_ids()
        if ids:
            info = self._info(ids[-1]) or {}
            last = {"id": ids[-1], "status": info.get("status"), "errors": info.get("errors") or []}
        return {"running": job is not None,
                "progress": {"run": job["run"], "step": job["step"], "steps": job["steps"], "config": job["config"],
                             "elapsed_s": int(self.monotonic() - job["t0"])} if job else None,
                "reason": self.reason(), "last": last, "runs": self.runs(),
                "settings": self.store.eval_settings(), "limits": {"min": MIN_CONTENDERS, "max": MAX_CONTENDERS}}

    def _load(self, run_id):
        """(info, contenders, pairs, {key: line}, data, choices) for a run, old (pre-0.5.0) runs included:
        their fixed setups become contenders keyed by name."""
        if not _RUN_RE.fullmatch(run_id or "") or not (self.root / run_id).is_dir():
            return None
        info = self._info(run_id)
        if info is None:
            return None
        d = self.root / run_id
        lines = []
        try:
            for raw in (d / "responses.jsonl").read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    lines.append(json.loads(raw))
        except (OSError, ValueError):
            lines = []
        try:
            data = json.loads((d / "data.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        try:
            choices = json.loads((d / "choices.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            choices = None
        if isinstance(info.get("contenders"), list):
            cons = [c for c in info["contenders"] if isinstance(c, dict) and c.get("key")]
            pairs = [tuple(p) for p in info.get("pairs") or [] if isinstance(p, list) and len(p) == 2]
        else:
            cons, pairs = _legacy_snapshot(info), list(LEGACY_PAIRS)
        by_key = {}
        for x in lines:
            if isinstance(x, dict):
                by_key[x.get("contender") or x.get("config")] = x
        return info, cons, pairs, by_key, data if isinstance(data, dict) else {}, choices

    def view(self, run_id):
        """The blind page for a run: pairs with summary and drafts as A and B. Contender names, settings,
        results and metrics only appear after choices were submitted (or when there is nothing to compare)."""
        loaded = self._load(run_id)
        if loaded is None:
            return None
        info, cons, pairs, lines, data, choices = loaded
        out = {"id": run_id, "at": info.get("started"), "status": info.get("status"),
               "errors": info.get("errors") or [], "pairs": [], "revealed": choices is not None}
        if info.get("status") == "running":
            return out
        rows = [{"id": r["id"], "who": r.get("who", ""), "subject": r.get("subject", "")}
                for r in data.get("replies") or [] if isinstance(r, dict) and r.get("suggest")]
        name = {c["key"]: c.get("name") or c["key"] for c in cons}

        def side(key):
            p = (lines.get(key) or {}).get("parsed") or {}
            return {"summary": p.get("summary"), "drafts": {r["id"]: (p.get("replies") or {}).get(r["id"])
                                                            for r in rows}}
        picks = (choices or {}).get("picks") or {}
        for i, (a, b) in enumerate(pair_orders(info.get("seed", 0), pairs), 1):
            if a not in lines and b not in lines:
                continue
            pair = {"id": f"p{i}", "a": side(a), "b": side(b)}
            if choices is not None:
                pair.update(a_name=name.get(a, a), b_name=name.get(b, b), pick=(picks.get(pair["id"]) or {}).get("pick"))
            out["pairs"].append(pair)
        out["rows"] = rows
        out["revealed"] = choices is not None or not out["pairs"]
        if out["revealed"]:
            counts = tally([c["key"] for c in cons], picks)
            out["mode"] = info.get("mode") or "baseline"
            out["baseline"] = name.get(info["baseline"]) if info.get("baseline") else \
                (None if info.get("contenders") else "Q-med")
            out["results"] = [self._result(c, lines.get(c["key"]), counts[c["key"]]) for c in cons if c["key"] in lines]
        return out

    @staticmethod
    def _result(c, line, counts):
        m = (line or {}).get("metrics") or {}
        return dict({"name": c.get("name") or c["key"], "profile": c.get("profile_name"), "ok": not m.get("error"),
                     "error": m.get("error"), "schema_valid": m.get("schema_valid"), "wall_s": m.get("wall_s"),
                     "prompt_tokens": m.get("prompt_tokens"), "completion_tokens": m.get("completion_tokens"),
                     "empties": m.get("empties"), "flagged": m.get("flagged"), "metrics": m},
                    **_settings_of(c), **counts)

    def choose(self, run_id, body):
        loaded = self._load(run_id)
        if loaded is None:
            return None
        info, cons, pairs, lines, data, choices = loaded
        if choices is not None:
            raise Refused("Choices for this run were already submitted.")
        if info.get("status") == "running":
            raise Refused("This run hasn't finished yet.")
        view = self.view(run_id)
        picks = body.get("picks") if isinstance(body, dict) else None
        want = [p["id"] for p in view["pairs"]]
        if not want:
            raise Refused("This run has nothing to compare.")
        if not isinstance(picks, dict) or set(picks) != set(want) or any(picks[k] not in PICKS for k in want):
            raise ValueError("Pick A, B or Same for every pair.")
        orders = dict((f"p{i}", ab) for i, ab in enumerate(pair_orders(info.get("seed", 0), pairs), 1))
        name = {c["key"]: c.get("name") or c["key"] for c in cons}
        rec = {"at": dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="seconds"),
               "picks": {}}
        for k in want:
            a, b = orders[k]
            rec["picks"][k] = {"pick": picks[k], "a": name.get(a, a), "b": name.get(b, b), "a_key": a, "b_key": b,
                               "better": name.get(a, a) if picks[k] == "A" else name.get(b, b) if picks[k] == "B"
                               else "same"}
        _write(self.root / run_id / "choices.json", _json(rec))
        return self.view(run_id)

    def export(self, run_id):
        """NDJSON text: the payload line, one line per contender (per setup for runs before 0.5.0), then the
        owner's choices (with the wins, losses and ties per contender) when submitted."""
        loaded = self._load(run_id)
        if loaded is None:
            return None
        info, cons, pairs, lines, data, choices = loaded
        try:
            payload = (self.root / run_id / "payload.txt").read_text(encoding="utf-8")
        except OSError:
            payload = None
        legacy = not isinstance(info.get("contenders"), list)
        head = {"type": "payload", "run": run_id, "started": info.get("started"), "status": info.get("status"),
                "ids": info.get("ids") or [], "payload": payload}
        if not legacy:
            name = {c["key"]: c.get("name") or c["key"] for c in cons}
            head.update(mode=info.get("mode"), baseline=name.get(info.get("baseline")),
                        pairs=[[name.get(a, a), name.get(b, b)] for a, b in pairs])
        out = [head]
        for c in cons:
            x = lines.get(c["key"])
            if x is None:
                continue
            p = x.get("parsed") or {}
            row = {"type": "config", "config": c["name"]} if legacy else \
                dict({"type": "contender", "contender": c["name"], "profile": c.get("profile_name"),
                      "prompts": c.get("prompts")}, **_settings_of(c))
            row.update(model=x.get("model"), request=x.get("request"), metrics=x.get("metrics"),
                       summary=p.get("summary"), drafts=p.get("replies") or {}, areas=p.get("areas") or {},
                       top=p.get("top") or [], gists=p.get("gists") or {}, error=x.get("error"))
            out.append(row)
        if choices is not None:
            rec = dict({"type": "choices"}, **choices)
            if not legacy:
                counts = tally([c["key"] for c in cons], choices.get("picks"))
                rec["tally"] = {c["name"]: counts[c["key"]] for c in cons}
            out.append(rec)
        return "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out)
