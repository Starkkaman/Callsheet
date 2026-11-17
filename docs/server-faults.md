# Local server faults (0.8.5)

Status (0.21.0): the AI sorting, the rules brief and the writer evaluation this page first described went with the
rework; the detection, backoff and probe below still apply to every local request.

A llama.cpp server can break in ways that look like a model answering: the local model streaming `<unused32>` tokens, special
tokens as text, control characters, one word over and over, or a 500 "Unexpected empty grammar stack after accepting
piece". A one-token canary ("Reply with OK") can still pass while every real generation fails. Since 0.8.5 Callsheet
treats these as a **server fault**, not a model mistake. Code: the "0.8.5 server faults" section of `writer.py`
(every local POST goes through `writer._post`); tests in `tests/test_server_fault_085.py` (fake servers only).

## Detection (`writer.junk_rule`, `writer.fault_of`, `_local`)

Checked on every answer's `content`, `reasoning_content` (or `reasoning`) and tool-call arguments, first 100,000
characters each. Rule codes (the only thing logged):

| Rule | Fires when |
|---|---|
| `unused` | 3+ `<unusedNN>` tokens, or 1+ when the text holds nothing else |
| `special` | 3+ `<\|...\|>` tokens (`<\|im_end\|>`, `<\|channel\|>`...), or 1+ when the text holds nothing else |
| `control` | 8+ control or non-printable characters in a row (Unicode Cc except tab and line breaks, Cf, Cs, Co, Cn, U+FFFD) |
| `repeat` | one whitespace-separated token with a letter or digit 25+ times in a row, or a 2-12 character unit repeated 25+ times without spaces ("nanana...") |
| `empty_length` | finish `length` and the answer and thinking hold junk and nothing else |
| `grammar` | HTTP 5xx whose body says "empty grammar stack", "grammar stack after accepting" or `GGML_ASSERT` |

Not faults: a single stray token in normal text, `-----` or `=====` lines, a plain empty answer cut at the limit (still
"ran out of room"), other HTTP errors (unchanged), malformed JSON (the writer's one retry and the sorter's rules
fallback are unchanged).

## What happens

- **Content-provokable rules get an immediate probe.** `repeat` and `control` can be caused by text in a message (a mail
  asking for one word thirty times), so the health probe runs at once. If it passes, only that request failed
  (`ModelGarbage`, "The local model's answer wasn't usable."): no mark, no backoff, the next request goes straight to
  the model. If it fails, the server fault below. `unused`, `special`, `grammar` and `empty_length` go straight to it.
- **No retries on a fault.** The existing one retry after "Loading model" stays (a fault on the retry still counts).
- **Marked unhealthy** per (server address, model), in memory (a restart forgets it), with the time it started.
- **Backoff** `FAULT_BACKOFF_S` = 15 minutes: `_post` raises `ServerFault` at once and sends nothing.
- **Health probe** before the next real request once the backoff is over: one request (`writer.probe_body`:
  max_tokens 160, temperature 0, thinking off, `reasoning_effort: low` for gpt-oss, the faulting request's slot),
  "Reply with this sentence and nothing else: The quick brown fox jumps over the lazy dog." It passes only with no
  junk, finish `stop`, 4+ of quick/brown/fox/jumps/lazy/dog and mostly letters. Pass: the mark goes and the real
  request follows. Fail: nothing else is sent and the backoff starts again (the start time stays).

## Effects

- **Background reading and the brief** (the rework): the ledger's read and Talk it through get the same plain
  error; Talk it through then uses Callsheet's own sentence for the step (`docs/brief.md`).
- **Chat**: "The local model isn't working right now. Try again in a few minutes." (with Try again); nothing of the
  garbage is stored or goes into history.
- **Setup → AI → Background model**: the health line ("Local model unhealthy since 14:05", the owner's time format;
  with the date when it isn't today).
- **One unusable answer** (`ModelGarbage`): Chat answers "The chat model's answer wasn't usable. Try again, or ask in other words."
- Projects and Know me get the same `ServerFault` (a plain error).

## Logs (codes and counts only)

- `Callsheet writer: local server fault (<rule>), not retried; the local model is marked unhealthy (N faults), next
  check in 15 min`
- `Callsheet writer: the local model passed its health check (Xs); using it again` / `... failed its health check
  (<rule>, Xs); next check in 15 min`
- `Callsheet writer: local answer tripped <rule>; the health check passed, so it counts as a model error for this
  request (no backoff)`
- `Callsheet chat: local failed: the local model isn't healthy (<rule>) (...)`

## Known limits

- A mail that gets the model to repeat a word costs one rules brief or one batch of rules labels, not an outage (the
  probe passes). A model talked into writing `<unusedNN>` or `<|...|>` tokens three times would still trip a fault
  rule; the cost is the rules brief and plain-word errors until the next probe passes, never a wrong label.
- Health is kept in memory: after a restart the first request finds a still-broken server again (one request).
