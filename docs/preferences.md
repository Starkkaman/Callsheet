# Preferences set by chat (0.17.0)

Approved 2026-11-11 (chat on the store and configuring by chat). The owner says what they want in
Chat. When the owner's own latest message says exactly the change, Callsheet applies it at once and the card says "Done: …"
with Undo · See all rules; anything filled in, taken from earlier messages or broad waits on the card for the owner's Confirm.
Everything applied can be undone (decided 2026-11-12, as the v4 mockup shows).

## Where they live

`prefs.py`, in `messages.db` next to the ledger:

- `preferences(id, kind, fields JSON, label, before, ref, created_at, undone_at, deleted_at)`: one row per change.
- `owner_actions(id, item, action, fields, label, before, until_ts, ref, at, undone_at, fired_at)`: the owner's
  actions on ledger items.

`ref` is `<chat id>:<turn>` for changes from a card.

## Kinds

| kind | fields | applied by |
|---|---|---|
| `vip` | person, sources, dm_only, alerts, needs_you, through_quiet | `conflicts.Alerter` (VIP asks), `Ledger.view` (VIPs on top of Needs you) |
| `alert_rule` | sources, what (clashes, asks, all), mode (never, quiet_hours with start/end, always), except | `Alerter._decide` |
| `quiet_hours` | start, end | the push settings (`Store.update_push`) |
| `vip_through_quiet` | on | the push settings |
| `alerts` | what (clashes, asks), on | the push settings |
| `cleanup` | match (sender, words), value, counts | `Ledger.view`: each clean-up row gets `cleanup` = {decision: clean_up or keep, rule, label} from the newest matching rule (`Rules.cleanup_rule`: address, domain, name, or words with plurals folded); nothing moves or hides |
| `digest` | name, mention (always, stands_out, when_asked, never) | stored for the brief: `Rules.digest(name)` (unnamed digests: `DIGEST_DEFAULT`, stands_out) |
| `brief` | topic, choice (include, skip, short, first), note | stored for the brief (rework stage 6): `Rules.brief()` |
| `brief_schedule` | call (morning, midday, afternoon), time, days (null: the days it has now) | the brief times themselves (`Store.update_settings` call_times; turns that call on) |
| `assistant_name` | name (1-40 characters, `assistant.clean`) | the assistant's name itself (`Store.update_settings` assistant_name) |
| `model_quiet_times` | start, end, on (true adds the time, false removes it) | the model server's quiet times themselves (`Store.update_settings` model_quiet_times, Setup › AI › Advanced); both times must be in the owner's latest message |

The three push kinds change the existing phone-notification settings, `brief_schedule` changes Setup › Your day ›
Brief times, and `assistant_name` and `model_quiet_times` change those settings, so each setting has one home; the row keeps `before` and Undo or Delete puts it back (`PUSH_DEFAULTS` when
it was never set). `Prefs.for_brief()` (in `GET api/preferences` as `for_brief`) is what stage 6 reads: the digests'
flags, the default and the brief preferences.

Matching: a rule's person matches a sender when every word of the rule's name is in one of the sender's names (the
message's name and the People directory), or the rule names the address itself. The newest matching VIP rule wins over
the VIP list. Alert rules: the newest matching rule decides (`off`, `quiet` inside its own window, which holds VIPs too,
`through`); a rule whose `except` names the sender is skipped.

## Actions

`done` (status done, history kind `owner`), `snooze` (hidden in a Snoozed group until the time), `remind` (hidden until
the time, then a phone reminder from `Alerter.flush` with the person and source only) and `note` (a note to the assistant in
the item's history). Undo restores the status or ends the snooze.

## Cards

`chatstore.setting_intent` / `action_intent` read the owner's own latest message (quoted text removed); only then is
`propose_setting` / `propose_action` offered. `build_setting_card` / `build_action_card` keep only what the owner's
words support: names the owner wrote, sources the owner named, on/off from the owner's own negation words, times the owner said (a time the owner didn't
say is filled from the current quiet hours and marked "Guessed"), items a ledger tool showed in that chat (more than
one only when the owner said so), a note in the owner's words that passes `note_check`. A ready card gets a one-time token
(`Chat._issue`, 30 minutes, bound to the chat and the turn); the change is kept with it on the server.

The owner's-words gate (`chatstore.setting_mode` / `action_mode`, deterministic) decides how a ready card is applied:

- **At once** when the change is all in the owner's own latest message: the card's subject (person, value, digest name, topic,
  call) and its words (sources, DMs, times, days, except names, note, the action's item words) are in it, and nothing
  was dropped or overridden from what the model asked for (`changed`). The token is marked used and the change is
  written; the tool result tells the model `{"ok": true, "done": …}`. The card reads "Done: <label>" with Undo (the
  same token, `POST api/chat/undo`, for 30 minutes after the change, once) · See all rules.
- **Confirm** otherwise, with a reason: the model filled in words the owner didn't say (`WHY_FILLED`), the words come from an
  earlier message (`WHY_EARLIER`), or the change is broad, like alerts off, VIPs held in quiet hours, "never" for every
  source or more than one item (`WHY_BROAD`). A turn that ended in an error also waits (`WHY_FAILED`).
- A guessed quiet-hours time still applies at once (the card shows the "Guessed" row), as in the mockup.

Tool results and other people's messages still put up no card and write nothing (the injection suite in
`tests/test_chat_cards_0170.py`). Undo in Setup › Your rules, or a Delete there, marks the card "Undone" too (`ref`
finds it after a restart).

## API

- `POST api/chat/apply {chat, turn, token}`: Confirm; applies the kept change; anything else in the body is refused.
- `POST api/chat/undo {chat, turn, token}`: Undo on an applied card (its own token, once).
- `GET api/preferences`: `{rules, undone, actions, count, for_brief}`; each rule and action has `chat`, `turn` and
  `chat_title` (null when the chat is gone).
- `POST api/preferences/<id>/undo`, `DELETE api/preferences/<id>`, `POST api/ledger/actions/<id>/undo`.
- `POST api/chat/brief {text}`: a reply to the brief, in the day's "Morning brief" chat.

## The page

- Chat: the card (`ruleCard` in app.js): applied ("Done: …", the rows, Undo · See all rules), Confirm (the card's one
  loud button, Dismiss, the reason), undone, expired and refused. An open Confirm card quiets the Ask button.
- Setup › Your day › Your rules (`dayRules`): Alerts, VIPs, Quiet hours, Clean up, Digests, Brief, then the actions
  done to items; each with Undo, Delete (asks once) and its source chat. Brief times say "Or ask in Chat" with a button
  that starts a chat with the question filled in.
