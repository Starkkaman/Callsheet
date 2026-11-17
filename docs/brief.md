# Talk it through (0.21.0, brief2.py)

The morning brief as a short conversation: one step at a time, each a spoken turn and one picture.

## Steps
Order: your day, clashes, needs you, coming up, bills and trips, from your mail, from your channels, feeds and
reports, clean up (`brief2.ORDER`, at most `MAX_STEPS` = 8). `Planner.plan(now)` builds them from `today.Today.view`, `ledger.view(now, 30)` and `prefs.rules()`;
the same store, clock and preferences give the same plan (and the same `sig`). Each step has facts, refs
(`item:N`, `cal:N`, `clash:N`), the picture Callsheet would show (`visuals.py` blocks), quick replies and a template
sentence.

Preferences (set in Chat, prefs.py `brief`): skip (never-hide items such as security alerts and invites stay),
short (one sentence), first (moved to the front), note (passed to the model as the owner's words). A topic that is
not a standard step (for example "health messages") becomes part of "From your mail"; a health message shows sender
and time only. Digests: `always` is mentioned; `stands_out` only when the item is never-hide or from a VIP.
Security alerts (0.21.0, `ledger.view`'s `security` group) are never-hide facts in "From your mail".

The feeds step (0.21.0) is optional: it appears only when a connector item stands out (its source said always tell,
or it is marked urgent) or when the owner asked for feeds in the brief ("include/first/short feeds", words in
`TOPICS["feeds"]`: channels, reports, YouTube, news…); skip removes it. At most 3 items, title and source only, no
refs and no picture; an item that reads like instructions is never in the brief.

## Words
One local model request per step (`request_body`): no tools, the facts in `<facts>` as data, strict JSON
`{step, text, visual: {type, refs}, more: []}`. Checks:
- `parse`: JSON, the right step, text present, short, no markup; else (0.22.x) one more try with the answer and why
  it can't be used (`repair_body`, the step's word and character limits; `repaired` on the step), then the template.
  Round 6 on the real mail set: the local model's "needs" step was too long once and fell back.
- `pick_visual`: refs must be the step's and the type buildable; else the plan's picture (the reason is listed).
- `ground`: numbers, times and capitalized names not found in the step's facts or messages are marked
  (`segs`, `checks`) and shown as "<name>'s words, not found in these messages" (the assistant's configured name); nothing is removed.
- `writer.states_decision` adds the usual warning.
A busy model (Chat first) is retried for up to `WAIT_CHAT_S`, then the template is used.

## Runs
`brief_runs` in messages.db: written step by step in a thread (`start`), reused when the plan's sig is unchanged
(`REUSE_S`), kept `KEEP_DAYS`. Pictures are filled from the store at every GET (`visuals.compose`).
The scheduler writes a run after each scheduled sync and the push says "Your brief is ready" (`/<slug>#brief`).

## API
- `GET api/talk`: the run and the day's Morning brief conversation.
- `POST api/talk/start {fresh}`: Talk it through.
- `POST api/talk/reply {text, step, run}`: a Chat turn scoped to the step (scope from the cached plan); the answer
  carries `brief_step`, and action cards work as in Chat (`api/chat/apply`, `api/chat/undo`).

## Page
Design: the v4 brief frames. Desktop: the step list on the left; the assistant's mark, the date
and "Morning brief"; a segmented step bar; the assistant's words; replies (yours on the right, its answer with an action
card); the step's picture in a card headed by the step's name ("UPDATED" when a reply changed it); quick replies;
the reply field with Send; Back and Next (ink, never orange) with "STEP n OF N"; Show transcript; "Start talking"
(coming later). Phone: "‹ Today" first, "n OF N" in the date line, no step list.

## Replay
`tools/replay_ledger.py --cases … --keep-db DIR` keeps the replayed ledger; `tools/replay_brief.py --db DIR --base
<url>/v1 --model <name>` writes the brief on it (the clock is the replay's last message) and scores JSON validity,
grounding (flagged words, grounded steps), step coverage, refused pictures and latency. `--dry` uses a fake model,
`--fixture` the Today fixture; the `--quiet-times` wait (or `--on-block stop`), a server fault stops with a partial summary.
