# Writer presets per call time (0.6.1)

Started as the design sketch `model-per-call-time.md` (2026-10-29, written when Qwen was the careful candidate).
The blind ranking then put **Swift-1.5-Qwen3.8-27B-Q3_K_M** (thinking on, reasoning budget 2048, ~180 s per brief,
timeout ≥ 900 s) first on quality; **Gemma** (`gemma-4-26b-a4b-it-qat-q4_0`, thinking off, ~18 s) is the fast model.
The owner hasn't chosen yet, so 0.6.1 ships the mechanism with safe defaults: every call time uses the owner's
current writer settings until the owner maps a call time to another preset.

## Idea
Each call time (06:30 morning brief, 12:30 and 17:00 check-ins, manual Refresh) can use its own writer preset.
Example: the morning brief, written while the owner is still asleep, uses the slow, careful preset; check-ins and
Refresh, when someone is waiting, use Fast. Numbers are from the NAS llama-server (Radeon VII, Vulkan, router mode,
one model loaded at a time): a swap takes ~7–8 s.

## Data model (store meta `writer`)
- `local` (unchanged) is also the built-in **Fast** preset: `model`, `thinking`, `reasoning_budget`, `max_tokens`,
  `timeout`, `id_slot`, plus 0.6.1's `effort` (null) and `kwargs` ({}), sent only when set. `base_url`, `api_key`
  and `context` are the server's and shared by every preset.
- `presets`: the owner's own presets, at most 4: `{id: "pre_<8 hex>", name, model, thinking, effort, kwargs,
  reasoning_budget, max_tokens, timeout, id_slot}`.
- `call_presets`: `{morning, midday, afternoon, refresh: preset id}`, all `"fast"` by default; an id that no
  longer exists reads as `"fast"`. `refresh` also covers the start-up catch-up sync.
- `reload_fast`: true by default.

Migration: none on disk. A 0.6.0 store has no `presets`/`call_presets`/`reload_fast`, so it reads as Fast for every
call time with the owner's settings as Fast: the request body is byte-identical to 0.6.0's.

## Validation (`presets.py`, store.py)
Name 1–40 characters, unique (case-insensitive, "Fast" reserved); model 1–120 characters without control
characters; thinking true/false; effort low/medium/high or empty (sent as `reasoning_effort` with thinking on
only); kwargs (llama.cpp `chat_template_kwargs`) at most 4 keys `[A-Za-z_][A-Za-z0-9_]*`, values true/false,
whole numbers or text up to 40 characters, never `enable_thinking` (the Thinking switch wins); budget, max tokens
and timeout in the local writer's limits (256–32768, 512–32768, 30–1800 s); server slot 0–63 or empty. A new
preset copies Fast's numbers and slot for fields it doesn't send. A preset in use can't be deleted; Fast can't be
deleted or renamed. `call_presets` takes only the four kinds and existing preset ids (the urgent check is not a
kind: it never uses the writer).

The model id is checked by `api.py` against the server's `GET /models`: an id the server doesn't list is refused
(400, with the server's ids); when the server can't be reached the preset is saved and the answer carries a
`model_check` sentence.

### The suggestion
`Careful (Swift, thinking)`: name "Careful", model `Swift-1.5-Qwen3.8-27B-Q3_K_M`, thinking on, budget 2048,
max tokens 4096 (answer room 6144), timeout 900 s, effort and kwargs empty, Fast's server slot. One click posts
`{"suggestion": "careful"}`; if the server lists the model under another id, the page opens the form with the
suggestion filled in and the error, so the owner edits the id.

## API
- `GET api/writer` adds `presets` (Fast first, `builtin: true`), `call_presets`, `reload_fast`, `call_kinds`
  (`[{id, label}]`), `preset_suggestion`, `preset_max`, `urgent_note`.
- `PATCH api/writer` takes `call_presets` (partial map) and `reload_fast`.
- `POST api/writer/presets` (`{name, model, …}` or `{"suggestion": "careful", …overrides}`),
  `PATCH api/writer/presets/<fast|pre_x>`, `DELETE api/writer/presets/<pre_x>`: the writer view plus
  `preset_id` and `model_check`.
- `GET api/writer/models`: `{models: [ids], error}` for the form's list.
- `GET api/brief` adds `writing: {text: "Writing the summary…", since, preset, poll_s: 10}` while the writer
  works on the cached brief.

## Run flow
1. The scheduler passes the call time's id (`morning`, `midday`, `afternoon`) to `Syncer.run_now(kind=…)`; the
   Refresh button and the start-up sync are `refresh`.
2. The rules brief is built and cached first (as before); `Syncer.writing` is set before it is stored, so
   `api/brief` shows it with `writing`. The page shows a quiet "Writing the summary…" line in place of the summary
   and polls `api/brief` every 10 s; it re-renders only when the writer is done. A Refresh while a writer runs
   returns at once. When Refresh itself maps to another preset than Fast, `POST api/sync` returns after the rules
   brief and the writer finishes in a background thread (the sync lock is held until it is done).
3. The writer request uses the preset's model and options; the router swaps models; a "Loading model" 500/503 is
   retried once after 10 s (as before).
4. The answer goes through `parse_output` and `sync.guard_claims` (0.5.3) exactly like before; the prompt texts
   are unchanged.
5. If another preset than Fast fails or times out: once to Fast for this brief, with Heads up "Written by the fast
   model: the careful one didn't answer in time." (timeout) or "…didn't work." (other failures). Never to Claude,
   even with "If the local model fails, use Claude instead" on (that setting still applies to Fast's own runs).
   If Fast fails too, Heads up says the writer didn't run and why.
6. After a successful run with another preset, when `reload_fast` is on and the models differ: one tiny request
   (max_tokens 8, thinking off, Fast's slot) loads Fast again. Not sent after a fallback (Fast is loaded already).

## Logs (counts only)
- `Callsheet writer: preset <id> for <kind>, model change yes|no|unknown, <s>s` per writer request (the model of
  Callsheet's previous brief request; unknown after a restart or a failure).
- `Callsheet writer: swap and queue about <s>s before the server worked <s>s` when llama.cpp sends
  `timings.prompt_ms`/`predicted_ms`: wall time minus the server's own work, i.e. the swap and any queueing.
- `Callsheet writer: fast model loaded again, <s>s` (or `loading the fast model again failed`).
- The existing per-request and per-run lines are unchanged.

## Costs and risks
- **Wait:** a Swift brief is ~180 s plus a swap; fine at 06:30, too slow for Refresh (keep Refresh on Fast).
- **Swaps:** two per careful run (there and back), ~7–8 s each; another client during that time waits or hits the
  loading retry. Chat questions wait while the brief writer holds the sync lock (as in 0.6.0).
- **Cache:** each swap loses the other model's prompt cache; the next Fast run pays full prefill.
- The writer evaluation's contenders are separate and unchanged; its "everyday model" is Fast's model.

## Files
`presets.py` (pure: defaults, suggestion, validation, mapping, options, reload body), `store.py` (storage and
validation), `sync.py` (kind → preset, writing state, fallback, reload, logs), `scheduler.py` (call kind),
`writer.py` (`LocalTimeout`, effort/kwargs in the body only when set, `local_models`, wall/server time),
`api.py` (routes, model check, `writing`), `app.js`/`app.css` (Setup table and form, the brief's line and polling),
`tests/test_presets_061.py`.
