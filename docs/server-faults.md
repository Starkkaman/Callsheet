# Local server faults (0.8.5)

A llama.cpp server can break in ways that look like a model answering: Gemma streaming `<unused32>` tokens, special
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

- **No retries on a fault.** The existing one retry after "Loading model" stays (a fault on the retry still counts).
- **Marked unhealthy** per (server address, model), in memory (a restart forgets it), with the time it started.
- **Backoff** `FAULT_BACKOFF_S` = 15 minutes: `_post` raises `ServerFault` at once and sends nothing.
- **Health probe** before the next real request once the backoff is over: one request (`writer.probe_body`:
  max_tokens 160, temperature 0, thinking off, `reasoning_effort: low` for gpt-oss, the faulting request's slot),
  "Reply with this sentence and nothing else: The quick brown fox jumps over the lazy dog." It passes only with no
  junk, finish `stop`, 4+ of quick/brown/fox/jumps/lazy/dog and mostly letters. Pass: the mark goes and the real
  request follows. Fail: nothing else is sent and the backoff starts again (the start time stays).

## Effects

- **Brief writer** (`sync._write`): the rules brief with the Heads up line "The local model isn't healthy right now, so
  this brief was made by the rules." No Fast-preset fallback, never Claude as a fallback (Claude writes only when the
  owner picked Claude as the writer), no Fast reload afterwards. The writer's request bodies are unchanged.
- **AI sorting** (`Sorter._run`): the run stops at the first fault (reason `unhealthy`); the batch stays queued and its
  items keep their rules labels (nothing is hidden, nothing is labelled from garbage). While in backoff `run_pass` and
  the night step return `unhealthy` without sending.
- **Chat**: "The local model isn't working right now. Try again in a few minutes." (with Try again); nothing of the
  garbage is stored or goes into history.
- **Setup -> AI sorting**: "Local model unhealthy since 14:05" (the owner's time format; with the date when it isn't
  today) and **Check again**, which runs the probe now (`POST api/sorting/model-check`, returns the sorting view; 409
  when Callsheet's own requests keep the server busy for 30 s). `GET api/sorting` has `local_health`:
  `{healthy: true}` or `{healthy: false, since, since_text, next_check}`. No addresses or keys.
- Projects, Know me and the evaluation get the same `ServerFault` (a plain error).

## Logs (codes and counts only)

- `Callsheet writer: local server fault (<rule>), not retried; the local model is marked unhealthy (N faults), next
  check in 15 min`
- `Callsheet writer: the local model passed its health check (Xs); using it again` / `... failed its health check
  (<rule>, Xs); next check in 15 min`
- `Callsheet sorting: the local model isn't healthy (<rule>); rules-only labels this run, N cards sorted, M waiting`
- `Callsheet chat: local failed: the local model isn't healthy (<rule>) (...)`

## Known limits

- Text an attacker puts in a mail can't call a tool, but a model asked to repeat 25 copies of a word or `<unused1>`
  tokens could trip a rule; the cost is the rules brief and plain-word errors for 15 minutes, never a wrong label.
- Health is kept in memory: after a restart the first request finds a still-broken server again (one request).
