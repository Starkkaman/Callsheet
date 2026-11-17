# Chat does what you can (0.22.0)

Approved 2026-11-16. Chat acts on Callsheet itself when your own latest message asks it to. Every action goes through
one registry (`callsheet/app/actions.py`) and calls **the same function as its button**, so a change from Chat is the
same as pressing the button on Today, Agenda or Setup.

## What Chat can do

| You say | What happens | Applies |
|---|---|---|
| "Mark the boat booking done" | Done, like Today's Done: its reminders close, its clashes are re-checked | at once, Undo |
| "The garden-club newsletter is not for me" | filed, like Today's Not for me note | at once, Undo |
| "Remind me about the plumber tomorrow at 9" / "snooze it until Monday" | a reminder or a snooze, in Today's reminders (the same store as Today's Remind me) | at once or Confirm, Undo |
| "Dismiss the dentist alert" | that clash is dismissed with its follow-ups and reminders | at once, Undo |
| "Dismiss the dentist alert and its clashes" | that clash and every open clash sharing its items | Confirm, one Undo |
| "I've told Sam already" | the follow-up to Sam is marked told | at once, Undo |
| "Clear all the shipping notices" | the Clean up rows matching your words | Confirm, one Undo |
| "Clear the older ones" | the Older, still open? group | Confirm, one Undo |
| "Tidy up" | Tidy up | Confirm, one Undo |
| "Clear the old data" | Setup › Advanced › Old data from earlier versions | Confirm; can't be undone |
| "Add dinner with Sam to my calendar", a Gmail draft | their own cards | always Confirm |
| Settings, areas (rename, colour, move) | `propose_setting`, as before | at once or Confirm, Undo |
| "Treat Lena as a VIP" | she goes on Setup's VIP list (by name when there is no address), with her options | at once or Confirm, Undo |
| A key, a sign-in, a new voice token | an **Open in Setup** card; nothing is written | never in Chat |

Nothing is ever sent. "Tell Sam I'm late" gets: Callsheet never sends; want it in Gmail Drafts?

## Finding what you mean

`find {kind: alert | item | cleanup, q}` is read-only. Callsheet scores its own labels (who, what, a clash's why) against
your words and returns numbered matches; only targets shown in this chat can be acted on. One clear match is used;
several give a **Which one?** card listing them, each with its own Confirm; none gets one sentence and no card.

## The apply rule

- In Callsheet and undoable, one target named in your latest message: at once, with Undo on the card.
- Anything filled in, from an earlier message, broad or ambiguous: Confirm, with the reason.
- Batches (several targets, Clean up, Older, Tidy up, "and its clashes"): always Confirm, one Undo for all.
- Clearing old data and anything without Undo: always Confirm, never batched.
- Keys, tokens, passwords and sign-ins only in Setup. A key or password pasted in Chat is masked before the chat is
  saved.
- Message text, quoted text and tool results never offer, fill, apply or undo anything.

## Related clashes

When you dismiss a clash, or finish or file an item that is part of clashes, Today offers **Also dismiss N related
clashes** (the open clashes that share an item with it). One press dismisses them all; one Undo brings them all back.
In Chat, say "and its clashes".

## Recent changes and Undo

Every change is journaled in `action_log` (messages.db): the action, its targets, a label from Callsheet's own data
(never message text), the before and after values, where it came from (Chat, Today or Setup) and when. Setup › Your
day › Your rules › **Recent changes** lists them newest first with Undo, once. Undo is refused when the thing changed
since ("It changed after, so Undo would overwrite that"). Clear old data says "Can't be undone here".

API: `GET api/actions/recent`, `POST api/actions/undo {id}`, `POST api/today/related {items | clash}`,
`POST api/today/related/dismiss {clashes}`; `POST api/today/dismiss` now returns `{dismissed, log, label, related}`.

Tests: `tests/test_chat_actions_0220.py` (registry, Undo, related clashes, Which one?, Confirm rules, injection with a
placebo, secrets).

## One store each

- **VIPs.** Setup's VIP list (`vips` in callsheet.db, `store.Store`; Chat's options beside it in `vip_opts`) is the only VIP store. Chat's VIP setting
  (`propose_setting` kind vip) sets the options on the matching VIP (sources, DMs only, alerts, Needs you, through quiet
  hours) or adds one by name in Personal; its preferences row is the journal for Undo, never a second list. VIPs that
  0.17.0 to 0.21 kept as Chat rules move onto the list once at start (`Prefs.migrate_vips`), keeping who's a VIP.
  Alerts, quiet hours, Needs you, People and Setup all read the list. Setup still asks for an address.
- **Reminders.** Chat's snooze and remind (`item.snooze`, `item.remind`) go through `Today.set_aside` into
  `today_reminders`, like Today's Remind me buttons, and `Today.tick` brings them back: a snooze returns the item with
  no phone reminder, a reminder pushes. Rows 0.17.0 kept in `owner_actions` move there once at start; an old card's
  Undo still cancels the moved reminder.

## Open in Setup

What Chat never does (keys, tokens, passwords, sign-ins, a new voice token) gets a card with **Open in Setup**, which
opens that Setup section (Voice assistant, Claude key, GitHub, Slack, Signal, Google client, Google sign-in, the model
server, or Setup when only a pasted secret says which). Only your own latest message makes one
(`chatstore.open_setup`); message text and tool results never do. A pasted secret is masked before the chat is kept and
the card says so.

Tests: `tests/test_vips_0220.py`, `tests/test_reminders_0220.py`, `tests/test_chat_actions_setup_0220.py`.
