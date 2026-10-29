"""Writer evaluation (0.3.3): the owner compares local writer setups on today's real brief data, blind.

Runs only when the owner presses "Run evaluation" (POST api/eval/run); never on a schedule. It reads Google the
same way a sync does (Syncer.writer_data: same reads, rules, stripping, fit and row ids) but stores nothing live:
no cached brief, last sync, account status or last writer. The data goes only to the configured local model
server, one request at a time:

    G0     the production local request: Gemma, thinking off, the owner's local options (max tokens, timeout)
    Q-low  Qwen, thinking on, reasoning_effort "low",    reasoning_budget_tokens 2048, max_tokens 2048 + 4096, 450 s
    Q-med  Qwen, thinking on, reasoning_effort "medium", reasoning_budget_tokens 4096, max_tokens 4096 + 4096, 450 s
    reload one tiny chat request (max_tokens 8) to the Gemma model, so the brief's model is loaded again

All three get the same payload (fitted once for the largest max_tokens). Model ids can be changed in the
evaluation's Advanced settings (store.eval_settings) in case the server names them differently.

Results stay in <data>/writer-eval/<UTC timestamp>/ (dir 0700, files 0600): payload.txt, data.json,
responses.jsonl (one line per setup: request parameters without the messages, raw response, metrics, parsed
answer), metrics.json, run.json and, once the owner picks, choices.json. The last 5 runs are kept.
The page shows two blind pairs (G0 vs Q-med, Q-low vs Q-med) in an A/B order seeded per run; which setup was
A or B, and the metrics, are only returned after the owner submits choices. Logs: counts and times only.
"""
import datetime as dt
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

GEMMA_MODEL = "gemma-4-26b-a4b-it-qat-q4_0"
QWEN_MODEL = "Qwen3.8-27B-UD-Q3_K_XL"
QWEN_TIMEOUT = 450        # seconds per Qwen request
QWEN_ANSWER = 4096        # answer room on top of the reasoning budget
RELOAD_MAX_TOKENS = 8
CONFIGS = (
    {"name": "G0", "family": "gemma"},
    {"name": "Q-low", "family": "qwen", "effort": "low", "budget": 2048},
    {"name": "Q-med", "family": "qwen", "effort": "medium", "budget": 4096},
)
RELOAD = "Gemma reload"
STEPS = len(CONFIGS) + 1
PAIRS = (("G0", "Q-med"), ("Q-low", "Q-med"))
PICKS = ("A", "B", "same")
WINDOW_S = 10 * 60        # no run starts (and no setup starts) within 10 minutes of an enabled call time
KEEP = 5
DIRNAME = "writer-eval"
RUN_ID = r"\d{8}T\d{6}Z(?:-\d{1,2})?"
_RUN_RE = re.compile(RUN_ID)
OFF = "The evaluation tools are off. Turn them on in Setup, Delivery, Advanced."


def _stderr(line):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class Refused(Exception):
    """The run can't start now (HTTP 409). The message is a plain sentence."""


# --- requests ------------------------------------------------------------------
def config_body(cfg, model, payload_text, ids, opts):
    """(request body, options for writer._post) for one setup. opts: the owner's local_options()."""
    if cfg["family"] == "gemma":
        o = dict(opts, thinking=False)
        return writer.local_body(model, payload_text, ids, o), o
    o = dict(opts, thinking=True, reasoning_budget=cfg["budget"], max_tokens=QWEN_ANSWER, timeout=QWEN_TIMEOUT)
    body = writer.local_body(model, payload_text, ids, o)  # enable_thinking true, budget, max_tokens budget+4096
    body["reasoning_effort"] = cfg["effort"]
    return body, o


def reload_body(model):
    return {"model": model, "stream": False, "max_tokens": RELOAD_MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Reply with OK."}]}


def fit_opts(opts):
    """The options the shared payload is fitted with: the owner's context, the largest answer room."""
    budget = max(c.get("budget", 0) for c in CONFIGS)
    return dict(opts, thinking=True, reasoning_budget=budget, max_tokens=max(opts["max_tokens"], QWEN_ANSWER))


# --- metrics -------------------------------------------------------------------
def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def thread_texts(data):
    """{id: the thread text sent for that row} (the scorer's needs_placeholder reads it)."""
    return {r["id"]: "\n\n".join(m.get("text") or "" for m in r.get("messages") or [])
            for r in data.get("replies") or [] if r.get("suggest")}


def score(res, ids, threads, wall):
    """(metrics, parsed) for one raw /chat/completions answer. parsed: {summary, replies} as the brief would
    show them (writer.parse_output), or None when the answer isn't usable."""
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
        out = next((o for o in writer._json_in(reasoning) if writer._complete(o, ids)), None)
        rescued = out is not None
    parsed = None
    if isinstance(out, dict):
        try:
            p = writer.parse_output(out, ids)
            parsed = {"summary": p["summary"], "replies": p["replies"]}
        except writer.WriterError:
            parsed = None
    texts = evalscore.reply_texts(out)
    returned = [i for i in ids if isinstance(texts.get(i), str) and texts[i].strip()]
    drafts = parsed["replies"] if parsed else {}
    flagged = [i for i in ids if i in drafts and writer.states_decision(drafts[i])]
    sc = evalscore.score_drafts(drafts, threads)
    metrics = {
        "valid_json": valid, "schema_valid": evalscore.schema_valid(out, ids), "rescued_from_thinking": rescued,
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
    return metrics, parsed


def pair_orders(seed):
    """[(a_config, b_config)] per pair in PAIRS, each in a random order that only depends on the run's seed."""
    rng = random.Random(seed)
    return [(x, y) if rng.random() < 0.5 else (y, x) for x, y in PAIRS]


# --- files ---------------------------------------------------------------------
def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)


class Evaluator:
    def __init__(self, store, syncer, clock=time.time, log=_stderr, monotonic=time.monotonic):
        self.store = store
        self.syncer = syncer
        self.clock = clock
        self.log = log
        self.monotonic = monotonic
        self.root = store.dir / DIRNAME
        self.lock = threading.Lock()
        self.job = None     # while running: {run, step, config, t0}
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

    def reason(self):
        """Why a run can't start now (a plain sentence), or None."""
        if self.job is not None:
            return "An evaluation is already running."
        if not self.store.writer_config()["local"]["base_url"]:
            return "Add the local model's server address in Setup, Delivery first."
        if not self.store.has_accounts():
            return "Connect a Google account first."
        near = self.near_call()
        if near:
            return f"The {near} is too close. Run the evaluation more than 10 minutes before or after a brief."
        if self.syncer.lock.locked():
            return "A sync is running. Try again in a minute."
        return None

    # --- run -----------------------------------------------------------------
    def start(self):
        with self.lock:
            why = self.reason()
            if why:
                raise Refused(why)
            if not self.syncer.lock.acquire(blocking=False):  # held until the Google reads are done
                raise Refused("A sync is running. Try again in a minute.")
            try:
                run_id = self._new_dir()
                self.job = {"run": run_id, "step": 0, "config": "Reading mail and calendar", "t0": self.monotonic()}
            except Exception:
                self.syncer.lock.release()
                raise
        self.thread = threading.Thread(target=self._run, args=(run_id,), name="callsheet-eval", daemon=True)
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

    def _run(self, run_id):
        d = self.root / run_id
        info = {"id": run_id, "started": dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
                .isoformat(timespec="seconds"), "finished": None, "status": "running", "errors": [],
                "seed": secrets.randbits(32)}
        t0 = self.monotonic()
        held = True  # the sync lock, taken in start(): released as soon as the Google reads are done
        try:
            self._save_run(d, info)
            es = self.store.eval_settings()
            wcfg = self.store.writer_config()
            loc = wcfg["local"]
            info["models"] = {"gemma": es["gemma_model"], "qwen": es["qwen_model"]}
            try:
                data, ids, read_errors = self.syncer.writer_data(wcfg)
            finally:
                held = False
                self.syncer.lock.release()
            if read_errors:
                info["errors"].append("Some accounts couldn't be read, so the data may be incomplete.")
            opts = writer.local_options(loc)
            payload = writer.fit_local(data, ids, fit_opts(opts), self.log)
            info["ids"] = list(ids)
            _write(d / "payload.txt", payload)
            _write(d / "data.json", _json(data))
            self._save_run(d, info)
            threads = thread_texts(data)
            metrics, lines, qwen_ran = {}, [], False
            for n, cfg in enumerate(CONFIGS, 1):
                near = self.near_call()
                if near:
                    info["errors"].append(f"Stopped before {cfg['name']}: the {near} is too close.")
                    info["status"] = "stopped"
                    break
                self.job.update(step=n, config=cfg["name"])
                model = es["gemma_model"] if cfg["family"] == "gemma" else es["qwen_model"]
                body, o = config_body(cfg, model, payload, ids, opts)
                qwen_ran = qwen_ran or cfg["family"] == "qwen"
                line = {"config": cfg["name"], "model": model,
                        "request": {k: v for k, v in body.items() if k != "messages"},
                        "response": None, "metrics": None, "parsed": None, "error": None}
                s0 = self.monotonic()
                try:
                    res = writer._post(loc["base_url"], loc["api_key"], body, o, self.log, self.monotonic)
                    line["response"] = res
                    line["metrics"], line["parsed"] = score(res, ids, threads, self.monotonic() - s0)
                except writer.WriterError as e:
                    line["error"] = str(e)
                except Exception as e:  # noqa: BLE001 (one setup failing never stops the others)
                    line["error"] = "Something went wrong inside the evaluation. Check the app log."
                    self.log(f"Callsheet eval: {cfg['name']} unexpected {type(e).__name__}")
                if line["error"]:
                    line["metrics"] = {"error": line["error"], "wall_s": round(self.monotonic() - s0, 1)}
                    info["errors"].append(f"{cfg['name']}: {line['error']}")
                metrics[cfg["name"]] = line["metrics"]
                lines.append(line)
                _write(d / "responses.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
                _write(d / "metrics.json", _json(metrics))
                m = line["metrics"]
                self.log(f"Callsheet eval: {cfg['name']} " + (f"failed, {m['wall_s']}s" if line["error"] else
                         f"finish {m['finish_reason'] or 'unknown'}, {m['ids_returned']} of {m['ids_expected']} "
                         f"drafts, {m['flagged']} flagged, {m['wall_s']}s"))
            if qwen_ran:
                self.job.update(step=STEPS, config=RELOAD)
                try:
                    writer._post(loc["base_url"], loc["api_key"], reload_body(es["gemma_model"]),
                                 {"timeout": opts["timeout"]}, self.log, self.monotonic)
                except writer.WriterError as e:
                    info["errors"].append(f"{RELOAD}: {e}")
            if info["status"] == "running":
                info["status"] = "done"
        except Exception as e:  # noqa: BLE001
            info["status"] = "failed"
            msg = str(e) if isinstance(e, writer.WriterError) else \
                "Something went wrong inside the evaluation. Check the app log."
            info["errors"].append(msg)
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

    # --- reads ---------------------------------------------------------------
    def _info(self, run_id):
        try:
            info = json.loads((self.root / run_id / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if info.get("status") == "running" and (self.job or {}).get("run") != run_id:
            info["status"] = "interrupted"
        return info

    def runs(self):
        out = []
        for run_id in reversed(self._run_ids()):
            info = self._info(run_id) or {"status": "unreadable", "started": None}
            out.append({"id": run_id, "at": info.get("started"), "status": info.get("status"),
                        "chosen": (self.root / run_id / "choices.json").exists()})
        return out

    def status(self):
        es = self.store.eval_settings()
        job = self.job
        last = None
        ids = self._run_ids()
        if ids:
            info = self._info(ids[-1]) or {}
            last = {"id": ids[-1], "status": info.get("status"), "errors": info.get("errors") or []}
        return {"running": job is not None,
                "progress": {"run": job["run"], "step": job["step"], "steps": STEPS, "config": job["config"],
                             "elapsed_s": int(self.monotonic() - job["t0"])} if job else None,
                "reason": self.reason(), "last": last, "runs": self.runs(),
                "models": {"gemma": es["gemma_model"], "qwen": es["qwen_model"]}}

    def _load(self, run_id):
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
        return info, {x.get("config"): x for x in lines}, data, choices

    def view(self, run_id):
        """The blind page for a run: pairs with summary and drafts as A and B. Config names, models and
        metrics only appear after choices were submitted."""
        loaded = self._load(run_id)
        if loaded is None:
            return None
        info, lines, data, choices = loaded
        out = {"id": run_id, "at": info.get("started"), "status": info.get("status"),
               "errors": info.get("errors") or [], "pairs": [], "revealed": choices is not None}
        if info.get("status") == "running":
            return out
        rows = [{"id": r["id"], "who": r.get("who", ""), "subject": r.get("subject", "")}
                for r in data.get("replies") or [] if r.get("suggest")]

        def side(name):
            p = (lines.get(name) or {}).get("parsed") or {}
            return {"summary": p.get("summary"), "drafts": {r["id"]: (p.get("replies") or {}).get(r["id"])
                                                            for r in rows}}
        for i, (a, b) in enumerate(pair_orders(info.get("seed", 0)), 1):
            if a not in lines and b not in lines:
                continue
            pair = {"id": f"p{i}", "a": side(a), "b": side(b)}
            if choices is not None:
                pair.update(a_config=a, b_config=b, pick=(choices.get("picks", {}).get(pair["id"]) or {}).get("pick"))
            out["pairs"].append(pair)
        out["rows"] = rows
        if choices is not None:
            out["metrics"] = [{"config": c["name"], "model": (lines.get(c["name"]) or {}).get("model"),
                               "metrics": (lines.get(c["name"]) or {}).get("metrics")}
                              for c in CONFIGS if c["name"] in lines]
        return out

    def choose(self, run_id, body):
        loaded = self._load(run_id)
        if loaded is None:
            return None
        info, lines, data, choices = loaded
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
        orders = dict((f"p{i}", ab) for i, ab in enumerate(pair_orders(info.get("seed", 0)), 1))
        rec = {"at": dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat(timespec="seconds"),
               "picks": {}}
        for k in want:
            a, b = orders[k]
            rec["picks"][k] = {"pick": picks[k], "a": a, "b": b,
                               "better": a if picks[k] == "A" else b if picks[k] == "B" else "same"}
        _write(self.root / run_id / "choices.json", _json(rec))
        return self.view(run_id)

    def export(self, run_id):
        """NDJSON text: the payload line, one line per setup, then the owner's choices when submitted."""
        loaded = self._load(run_id)
        if loaded is None:
            return None
        info, lines, data, choices = loaded
        try:
            payload = (self.root / run_id / "payload.txt").read_text(encoding="utf-8")
        except OSError:
            payload = None
        out = [{"type": "payload", "run": run_id, "started": info.get("started"), "status": info.get("status"),
                "ids": info.get("ids") or [], "payload": payload}]
        for c in CONFIGS:
            x = lines.get(c["name"])
            if x is None:
                continue
            p = x.get("parsed") or {}
            out.append({"type": "config", "config": c["name"], "model": x.get("model"), "request": x.get("request"),
                        "metrics": x.get("metrics"), "summary": p.get("summary"), "drafts": p.get("replies") or {},
                        "error": x.get("error")})
        if choices is not None:
            out.append(dict({"type": "choices"}, **choices))
        return "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out)
