# Writer evaluation with contenders (0.5.0)

The generalised writer evaluation (roadmap "Evaluation, generalised"). It replaces the fixed G0 / Q-low / Q-med
setups of 0.3.3 to 0.4.x. Code: `callsheet/app/evaluate.py` (validation, requests, runs, files),
`store.py` (the `eval` meta value and contender CRUD), `api.py` (routes), `app.js` (the `#eval` page).
Tests: `tests/test_eval.py` (fakes only: fake Google, a fake local server answering per model id, the fake
Anthropic API).

Everything the 0.3.3 evaluation guaranteed is kept: owner-run only; refused within 10 minutes of an enabled
call time (and stops before the next contender when one comes near); refused while a sync or another run is
busy, and manual Refresh is refused while it runs; the live brief, last sync, last writer and account status
never change; one request at a time with one retry after a 503/500 "Loading model"; blind pairs in an A/B
order seeded per run, names revealed only after submitting; files 0700/0600, last 5 runs kept; NDJSON
download; counts-only logs; the everyday local model is loaded again afterwards.

## Data model

Store meta key `eval` (shown in `GET api/state` as `eval`, no secrets):

```json
{"enabled": false, "mode": "each" | "baseline", "baseline": "<contender id>" | null,
 "contenders": [
   {"id": "con_1a2b3c4d", "name": "Qwen thinking", "provider": "local" | "claude", "model": "<model id>",
    "thinking": true, "effort": "low" | "medium" | "high" | null, "budget": 4096 | null,
    "temperature": 1.0 | null, "top_p": 0.95 | null, "top_k": 20 | null, "profile": "default" | "<prof id>"}]}
```

Validation (`evaluate.check_contender`, plain-word `InputError`s, HTTP 400): name 1-40 characters, unique
(case-insensitive); provider local or claude; model text up to 120 characters (Claude: one of
`writer.MODELS`, and a saved Anthropic key); thinking a boolean; effort low/medium/high or null; budget
256-32,768; temperature 0-2; top_p above 0 and at most 1; top_k 1-1,000; profile an existing writing profile.
Thinking off clears effort and budget for local models. Claude takes no budget and no sampling (the pinned
SDK's `messages.create` has no temperature/top_p): any of those is refused. At most 4 contenders; a run
needs at least 2. The first contender becomes the baseline until the owner picks one; removing the
baseline moves it to the first remaining contender.

Migration: a pre-0.5.0 value (`{enabled, gemma_model, qwen_model}`, no `contenders`) reads as the three old
setups with fixed ids `legacy-g0`, `legacy-q-low`, `legacy-q-med` (same models, thinking, effort, budget and
the Qwen sampling 1.0 / 0.95 / 20), mode baseline with Q-med as the baseline: exactly the old pairs (G0 vs
Q-med, Q-low vs Q-med). The value is rewritten in the new shape on the first change.

## API

| Route | |
|---|---|
| `PATCH api/eval/settings` | `{enabled, mode, baseline}`; any other key is refused (also the old `gemma_model`) |
| `GET api/eval/models` | `{local: [ids from the server's /models], local_error, everyday, claude: [{id, label}] (only with a key)}` |
| `POST api/eval/contenders` | add one; returns the settings |
| `PATCH api/eval/contenders/<id>` | change fields; returns the settings |
| `DELETE api/eval/contenders/<id>` | remove one; returns the settings |
| `GET api/eval/status` | as before + `settings`, `limits {min, max}`; `progress.steps` = contenders (+1 for the reload) |
| `POST api/eval/run`, `GET api/eval/<run>`, `POST api/eval/<run>/choices`, `GET api/eval/<run>/export` | as before |

All but `PATCH api/eval/settings` answer 404 with the "tools are off" sentence while the evaluation is off.

## Requests per contender

All contenders get the same payload, fitted once (`writer.fit_local`) for the largest local answer room and the
longest system message among the contenders' profiles (Claude-only runs: `writer.build_payload`). Each gets the
live brief's ask (areas, the rules' top refs, time format) with its own profile's texts.

- Local: `writer.local_body` (local system message with the profile, schema, LOCAL_RULE last) with
  `enable_thinking`; with thinking on `reasoning_budget_tokens` = budget (default: the owner's local setting),
  `max_tokens` = the owner's max tokens + budget, `reasoning_effort` when set, timeout at least 450 s; sampling
  keys only when set; the owner's `id_slot` when set. Sent through `writer._post` (the loading retry).
- Claude: `messages.create` with the composed system prompt, `output_config` = {effort (default medium), the
  JSON schema}, `thinking` = adaptive or disabled, `max_tokens` 16,000, no tools. The answer is converted to
  the local shape for scoring (`usage.input_tokens` → prompt tokens, thinking blocks → reasoning).
- Reload: one 8-token request to the writer's local model when any local contender that ran used another
  model (none when the everyday model isn't set).

Pairs: each-vs-each = all combinations in the owner's order (2 → 1, 3 → 3, 4 → 6); baseline = `(other,
baseline)` for every other contender (n-1). A/B order per pair from `random.Random(seed)`, as before.

## Run files

`writer-eval/<UTC time>/run.json` gains `format: 2`, `contenders` (the snapshot at start: keys `c1`..`c4` in
order, settings, `profile_name`, the profile texts), `mode`, `baseline` (key), `pairs` (by key) and
`everyday_model`; later edits to contenders or profiles never change a stored run. `responses.jsonl` lines
carry `contender` (key), `config` (name), `model`, `settings`, `profile`, `request` (without messages/system),
`response`, `metrics`, `parsed`, `error`. `metrics.json` is keyed by name. `choices.json` picks hold names
(`a`, `b`, `better`) and keys (`a_key`, `b_key`).

Old runs (no `contenders` in run.json) are read as the three fixed setups keyed by name with the old pairs, so
they open, take choices and download exactly as before (`config` lines).

## Results

The blind view shows only pairs (summary and drafts as A and B) and the rows. After the choices are submitted
(or when a run has nothing to compare) it adds per pair `a_name`, `b_name`, `pick`, and `results`: per
contender name, profile, settings, `ok`, `error`, `schema_valid`, `wall_s`, `prompt_tokens`,
`completion_tokens`, `empties`, `flagged` (drafts `writer.states_decision` flags), `wins`/`losses`/`ties`
from the picks, and the full `metrics`. The download has a `payload` line (+ mode, baseline, pairs by name),
one `contender` line per contender (settings, profile and its texts, request, metrics, answer) and the
`choices` line with a `tally` per contender.
