# Chat's tools, streaming and Show thinking (0.14.0)

Owner, 2026-11-06: "I'm probably going to keep wanting to add capabilities that will probably turn into tool calls."
and "Can you have chat stream the response, and let me toggle something to view its thinking/reasoning?"

## The registry

Every tool Chat can call is one `ChatTool` in `chat.REGISTRY` (`callsheet/app/chat.py`):

| field | what it holds |
|---|---|
| `name` | the tool's name the model calls |
| `description`, `schema` | what the model sees (the schema may be a function of the turn's sources) |
| `handler` | the `Tools` method that runs it (dispatch goes only through the registry) |
| `offer` | when it is offered: `index` (always) or a turn kind: `remember`, `connect`, `web` |
| `approved` | the date the owner approved it, as written in CLAUDE.md |
| `test` | its injection test module |

`tool_defs`, `openai_tools`, `claude_tools` and the JSON protocol are built from the registry. A registered tool that
isn't offered in a turn is refused like an unknown one ("There is no tool called …") and counted in the log.
`tests/test_stream_0140.py` (`RegistryTest`) checks every entry: a unique name, an approval date found in CLAUDE.md, an
existing test module, a real handler and a closed schema.

## Adding a tool

1. **The owner's approval first**, written in CLAUDE.md's hard rules with the date and his words (anything that
   writes, sends or fetches especially). No approval, no entry.
2. One `ChatTool` entry and its `Tools` handler. Results go back as `<data>`; nothing in a result is an instruction.
3. **A placebo-controlled injection test** (the test lead's standard): planted instructions in every place the tool
   reads (mail, feeds, reports, pages, its own results) next to matched harmless placebo text, with bars for both: the
   planted side is caught or does nothing (0 writes, 0 sends, 0 fetches it wasn't asked for), and the placebo side is
   not flagged. Put its module in the entry's `test`.
4. CHANGELOG, PRIVACY.md when something new leaves the box, filemap.

## Streaming (no SSE to the browser)

- Local: `stream: true` with `stream_options.include_usage`; `writer.read_stream` (stdlib: `SSEReader` cuts lines at
  bytes so split UTF-8 is safe, `StreamAnswer` rebuilds the one-piece answer: content, reasoning, tool calls by index,
  finish, usage) runs inside `writer._post`, so `LOCAL_LOCK`, the in-flight count and the fault checks are unchanged
  and the checks run on the whole answer. A server that answers in one piece is read as before.
- Claude: the SDK's `messages.stream` when the client has it (text and thinking deltas), else `messages.create`.
- The page polls `GET api/chat/<id>` every 300 ms while an answer streams (1 s otherwise). `pending.live` holds whole
  sentences only (`chat.Live`): each is defanged and run through the claim, instruction and link-lure detectors before
  it is published with its marks, so a sentence never shows without its label (max visible-unlabelled time 0). A run
  without a sentence end shows once it reaches 240 characters. Citation markers are dropped while streaming. A `.`, `!`
  or `?` ends a sentence only when a space or line break follows (or the answer ends): models stream `84` `.` `20` and
  `www` `.` `example` as separate tokens, and the stop alone isn't a sentence end (0.14.0 live check).
- Only the final answer streams: a reply that turns into a tool call is cleared and shows as a step. The JSON protocol
  doesn't stream answer text.
- `pending.checking`: the claim post-check runs on the final text; the page shows "checking…" and then swaps the
  stored answer in place of the streamed one (same spot).
- Stop shuts the upstream connection at once (`Chat._abort`; Claude: the stream's `close`), and no tool runs after it.
- A stream that breaks off (dropped connection, an error event, Context full, garbage found at the end) stores an
  error starting "The answer stopped before it was finished, so it isn't shown." (`cut: true`): never half an answer.

## Show thinking

- The switch at the top of Chat is the chat's thinking setting for both engines (`PATCH api/chat/settings
  {local: {thinking}, claude: {thinking}}`; Setup shows the same value). On: local requests carry
  `chat_template_kwargs.enable_thinking: true` and `reasoning_budget_tokens` (REASONING_BUDGET, added to max_tokens);
  Claude gets adaptive thinking with the larger max_tokens. Off: `enable_thinking: false`, Claude thinking disabled,
  and any reasoning a model sends anyway is dropped (no panel).
- Reasoning (`reasoning_content`/`reasoning`, inline `<think>…</think>`, Claude thinking) goes through the same `Live`
  pipeline: defanged, detector hits marked inline (never blocking), clipped at 8,000 characters (`cut`). It is not
  post-checked; the panel says it is the model's working, not an answer, and is closed once the answer is done.
- It is stored only in the chat's message (`thinking {sents, cut, none}`), deleted with the chat (30 days). It is never
  sent back as history, never given to the writer, never used for a note or a card, and not in Copy.
- No reasoning: "This model didn't share its thinking." Out of room while thinking: "Thinking was cut short: the model
  ran out of room before the answer. …" and the panel shows what there was.
