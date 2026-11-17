#!/usr/bin/env python3
"""Generate the talk-through brief (brief2.py, docs/brief.md) on a replayed ledger and score it.

Ledger: --db DIR, as kept by `tools/replay_ledger.py --keep-db DIR` (DIR/messages.db and DIR/replay.json with the
replay's tz, owner and clock), or --fixture (the seeded Today ledger of tests/today_fixtures.py, stage 0840).

Model: live (--base and --model; the key, if any, comes from the LOCAL_MODEL_KEY environment variable and is never
printed) or --dry (a deterministic fake that says each step's template as strict JSON: should score 100 %).

Scores, per step and in all:
- json: the model's answer parsed as the step's strict JSON (brief2.parse), of the answers received;
- grounding: numbers, times and names in the words not backed by the step's facts or messages (brief2.ground):
  flagged words, and steps with any;
- coverage: planned steps written by the model, and of those, the ones whose words name something of the step's
  facts (a who, a what, a number; a step without such facts counts);
- pictures: the model's picture refused (brief2.pick_visual) and the plan's used instead;
- latency: seconds per step (p50, p95, max) and in all.

Live runs honour the model server's quiet times (--quiet-times, as in replay_ledger.py, by the real clock in --tz,
default the TZ environment variable, else UTC): they wait them out, or stop with a partial summary (--on-block stop). A server fault stops the
run with a partial summary. The summary holds step keys and counts, no message words; --detail (mode 600, never
inside the repo) adds the flagged words and each step's text.
"""
import argparse
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
sys.path[:0] = [str(ROOT / "callsheet" / "app"), str(ROOT / "tools")]

import brief2  # noqa: E402
import extract as X  # noqa: E402
import msgstore  # noqa: E402
import today as today_mod  # noqa: E402
import writer  # noqa: E402
from replay_ledger import KEY_ENV, Stop, guard, parse_extra_body, with_extra  # noqa: E402

GATES = {"json": 0.95, "grounded_steps": 0.9, "coverage": 0.9}


def dry_call(body):
    """The fake model: each step's template, as the strict JSON the schema asks for (the plan's own picture)."""
    key = body["response_format"]["json_schema"]["schema"]["properties"]["step"]["enum"][0]
    return {"choices": [{"message": {"content": json.dumps({"step": key, "text": dry_call.templates.get(key, ""),
                                                             "visual": {"type": "none", "refs": []}, "more": []})}}]}


dry_call.templates = {}


class Live:
    """One request per step on the live server; a fault (or a blocked window with --on-block stop) stops the run."""

    def __init__(self, args, log, run):
        self.args, self.log, self.run, self.key = args, log, run, os.environ.get(KEY_ENV, "")
        self.extra = getattr(args, "extra_body_obj", None) or {}

    def __call__(self, body):
        body = with_extra(body, self.extra)
        if self.run["stopped"]:
            raise Stop(self.run["stopped"])
        try:
            guard(self.args, self.log)
        except Stop as e:
            self.run["stopped"] = str(e)
            raise
        try:
            return writer._post(self.args.base, self.key, body, {"timeout": brief2.STEP_TIMEOUT_S}, None,  # noqa: SLF001
                                time.monotonic, budget=writer.INFLIGHT_MAX)
        except writer.ServerFault as e:
            self.run["stopped"] = f"server fault: {e}"
            raise


def _p(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))], 2)


_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]{2,}|\d+")


def _fact_words(st):
    out = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k not in ("ref", "why", "source", "never_hide", "vip", "likely"):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            out.update(w.lower() for w in _WORD.findall(str(x)))
    walk(st.get("facts"))
    if st.get("count"):
        out.add(str(st["count"]))
        if st["count"] < len(brief2._NUMW):  # noqa: SLF001
            out.add(brief2._NUMW[st["count"]])  # noqa: SLF001
    return {w for w in out if w not in brief2.COMMON}


def covers(text, st):
    """Do the words name something of the step's facts (a who, a what, a number)?"""
    said, have = {w.lower() for w in _WORD.findall(text or "")}, _fact_words(st)
    return bool(said & have) if have else True     # a step with nothing to name ("Nothing else is on…") is covered


def open_ledger(args, tmp):
    """(MessageStore, ledger view, Today, clock, tz, owner) for --db or --fixture."""
    if args.fixture:
        sys.path.insert(0, str(ROOT / "tests"))
        import today_fixtures as F
        now = [F.ts(F.STAGES["0840"])]
        ms = msgstore.MessageStore(tmp, clock=lambda: now[0])
        ex = X.Extractor(ms, F.TZ, F.OWNER, None, clock=lambda: now[0])
        td = today_mod.Today(ms, ex.lv, lambda: now[0], alerter=F.FakeAlerter())
        F.seed(ms, ex, now, td, "0840")
        return ms, ex.lv, td, now, F.TZ, F.OWNER
    meta_p = pathlib.Path(args.db) / "replay.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    tz = meta.get("tz") or args.tz
    owner = args.owner or meta.get("owner") or "Alex"
    at = args.now or meta.get("now")
    now = [dt.datetime.fromisoformat(at).timestamp() if isinstance(at, str) else float(at or time.time())]
    ms = msgstore.MessageStore(args.db, clock=lambda: now[0])
    ex = X.Extractor(ms, tz, owner, None, clock=lambda: now[0])
    td = today_mod.Today(ms, ex.lv, lambda: now[0])
    return ms, ex.lv, td, now, tz, owner


def score(plan, outs, secs, run):
    steps, flagged, grounded, json_ok, answered, model_steps, covered, pics = [], 0, 0, 0, 0, 0, 0, 0
    for st in plan["steps"]:
        o = outs.get(st["key"])
        if o is None:
            steps.append({"key": st["key"], "written": False})
            continue
        got_answer = o["by"] == "model" or o["why"] in ("not JSON",) or not any(
            w in o["why"] for w in ("no local model", "busy", "ServerFault", "Stop", "Error", "Timeout"))
        if got_answer:
            answered += 1
            json_ok += o["by"] == "model"
        row = {"key": st["key"], "by": o["by"], "why": o["why"] or None, "secs": secs.get(st["key"]),
               "flagged": len(o["checks"]), "picture_refused": len(o["visual_checks"]), "warning": bool(o["warning"])}
        if o["by"] == "model":
            model_steps += 1
            flagged += len(o["checks"])
            grounded += not o["checks"]
            row["covers"] = covers(o["text"], st)
            covered += row["covers"]
            pics += bool(o["visual_checks"])
        steps.append(row)
    planned = len(plan["steps"])
    lat = [v for v in secs.values() if v is not None]
    rate = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    out = {"planned_steps": planned, "written_steps": len(outs), "model_steps": model_steps,
           "json": {"ok": json_ok, "answers": answered, "rate": rate(json_ok, answered)},
           "grounding": {"flagged_words": flagged, "grounded_steps": grounded, "rate": rate(grounded, model_steps)},
           "coverage": {"by_model": rate(model_steps, planned), "names_a_fact": covered,
                        "rate": rate(covered, planned)},
           "pictures_refused": pics,
           "latency_s": {"p50": _p(lat, .5), "p95": _p(lat, .95), "max": max(lat) if lat else None,
                         "total": round(sum(lat), 2)},
           "steps": steps, "run": run}
    ok = (not run["stopped"] and (out["json"]["rate"] or 0) >= GATES["json"]
          and (out["grounding"]["rate"] or 0) >= GATES["grounded_steps"]
          and (out["coverage"]["rate"] or 0) >= GATES["coverage"])
    out["gate"] = "pass" if ok else ("partial" if run["stopped"] else "fail")
    out["gates"] = GATES
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="a ledger kept by replay_ledger.py --keep-db (the directory)")
    src.add_argument("--fixture", action="store_true", help="the seeded Today ledger of tests/today_fixtures.py")
    ap.add_argument("--dry", action="store_true", help="a fake model (each step's template as JSON); no server")
    ap.add_argument("--base", help="live: the model server's base URL (…/v1)")
    ap.add_argument("--model", help="live: the model name")
    ap.add_argument("--tz", default=os.environ.get("TZ") or "UTC", help="the quiet times' zone (and the ledger's, if not kept)")
    ap.add_argument("--owner", help="the owner's name (default: from replay.json)")
    ap.add_argument("--now", help="the brief's clock, ISO (default: the replay's last message)")
    ap.add_argument("--on-block", choices=("wait", "stop"), default="wait")
    ap.add_argument("--quiet-times", default="", help='the model server\'s quiet times in --tz, "HH:MM-HH:MM,..."')
    ap.add_argument("--out", help="write the JSON summary here too")
    ap.add_argument("--detail", help="flagged words and each step's text (mode 600; refused inside the repo)")
    ap.add_argument("--extra-body", default="", help='live: a JSON object merged at the top level into every step '
                    'request (e.g. \'{"thinking_budget_tokens": 0}\'); never model, messages or id_slot. Recorded in '
                    'the summary (extra_body)')
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        args.extra_body_obj = parse_extra_body(args.extra_body)
    except ValueError as e:
        ap.error(str(e))
    if not args.dry and not (args.base and args.model):
        ap.error("live runs need --base and --model (or --dry)")
    if args.detail and pathlib.Path(args.detail).resolve().is_relative_to(ROOT):
        ap.error("--detail must be outside the repo")
    if args.db and not (pathlib.Path(args.db) / msgstore.DB_NAME).exists():
        ap.error(f"no {msgstore.DB_NAME} in --db")

    def log(line):
        if not args.quiet:
            print(f"[replay-brief] {line}", file=sys.stderr, flush=True)

    run = {"model": "dry" if args.dry else "live", "stopped": None, "seconds": 0.0}
    with tempfile.TemporaryDirectory(prefix="callsheet-brief-") as tmp:
        ms, lv, td, now, tz, owner = open_ledger(args, tmp)
        try:
            call = dry_call if args.dry else Live(args, log, run)
            b = brief2.Brief(ms, lv, td, None, clock=lambda: now[0], call=call,
                             model=lambda: args.model or "replay-dry", owner=lambda: [owner])
            plan = b.plan()
            dry_call.templates = {s["key"]: s["template"] for s in plan["steps"]}
            outs, secs, t0 = {}, {}, time.monotonic()
            log(f"{len(plan['steps'])} steps planned ({', '.join(s['key'] for s in plan['steps'])})")
            for st in plan["steps"]:
                if run["stopped"]:
                    break
                t = time.monotonic()
                try:
                    o = b.write_step(st, len(plan["steps"]), args.model or "replay-dry", [owner])
                except Stop as e:   # a blocked window with --on-block stop
                    run["stopped"] = run["stopped"] or str(e)
                    break
                if run["stopped"]:   # this step met the fault or the window: not scored
                    break
                secs[st["key"]] = round(time.monotonic() - t, 2)
                outs[st["key"]] = o
                log(f"{st['key']}: {o['by']} {o['why'] or ''} {secs[st['key']]}s")
            run["seconds"] = round(time.monotonic() - t0, 1)
        finally:
            ms.close()
    summary = score(plan, outs, secs, run)
    summary["extra_body"] = args.extra_body_obj
    text = json.dumps(summary, indent=1, sort_keys=True)
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.detail:
        fd = os.open(args.detail, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([{"key": k, "by": o["by"], "text": o["text"], "flagged": [c.get("what") for c in o["checks"]],
                        "visual_checks": o["visual_checks"]} for k, o in outs.items()], f, indent=1)
    return 0 if summary["gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
