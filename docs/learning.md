# Learning (0.8.5)

`callsheet/app/learn.py`, wired in `triage.py` (`Sorter._learn`, `learn_done`, `placed`, `forget`) and `api.py`
(`set_done`, `api/sorting/learned`, `api/sorting/forget`). Design: `~/project/daily-brief/understanding/design.md`
section 7 (e) and the S-L tests in section 6.

## What counts as teaching

Only the owner's own actions, and only while sorting is on or in its shadow week (never when it's off):

| Action | Learned label |
|---|---|
| Done on a reply row the sorter put in Needs you | needs a reply |
| Fix **Needs a reply** / **Not for me** / **This is a promo** | needs a reply / not for me / promo |
| Check the sorting **Right** | the AI's label (needs reply, fyi, promo, update, not for me) |
| Check the sorting **Wrong** + the right label | that label (meeting or date counts as fyi; security teaches nothing) |

Undo of a fix or of Done takes its event back. Done on a row outside Needs you teaches nothing (ticking a promo
away isn't a label). Messages never make an event, however many arrive (S-L2).

## What is stored (`learn_events` in callsheet.db)

id, time, action, learned label, the item's key (a hash), source kind, sender id, domain, People id, the sender's
display name, the correction id. Every value is checked against a pattern (address, Slack or Signal id, domain,
`p` + 11 hex, a name of letters, digits, spaces, dots, hyphens and apostrophes). **Never message text** (S-L1).

- **Sender**: the verified fields the brief and the sorter already use: the From address, Slack's user id,
  signal-cli's sender. Never a signature or the text, so a signature claiming someone else's address can't move
  learning to that address (S-L3).
- **Person**: the People id of that sender (People links addresses only by the owner's confirmation or verified
  handles), so two addresses of one person share what was learned.
- **Domain**: mail only; never a public mail domain (`people.PUBLIC`) or the owner's own domain.
- **Doubt**: an item marked sender not verified, look-alike domain, uses the owner's or a VIP's name, or hidden text
  teaches nothing and gets no prior.

## Priors

- Weights: an event counts 1 for 45 days, 0.5 from 45 to 90 days, then it is deleted (decay).
- A prior: 2 or more for one label and nothing against it (any other label from the same sender or person).
- Sender and person events decide first; the domain only when those don't disagree with it.
- The owner's fix of an item places that item (the strongest prior).

## Applied after the model (`learn.apply_prior`, deterministic, when the brief is read)

| Prior | Moves the row to |
|---|---|
| needs a reply | Needs you |
| fyi | For your info |
| promo / update / not for me | Sorted for you: Promos / Updates / Not for you |

- It never takes a row out of Needs you when an override (VIP, someone you know writing to you, your name in To,
  security, invite, steering text) or the rules' sure reply put it there: the row stays and Why says so
  (`prior_blocked`).
- Security alerts and invites never move. A row with a doubt mark gets no prior.
- Nothing is hidden: the row only changes section, and every row is in exactly one section.
- Shadow mode: events are recorded (from Check and Done), but the brief doesn't change.

**Why is this here?** shows "You taught it": "You marked Studio Supply as promo 2 times." (or "mail from
supply.example", or "You marked this: promo.").

## Few-shot examples: not added

The design allows feature-only examples in the triage prompt. 0.8.5 doesn't add them: the prompt `t083.1` is
pinned and was measured as is (0.8.3 screen), a change can't be measured here without live model calls, and an
example built from synthetic templates carries no sender signal that the priors don't already give,
deterministically and after the model. The request body is unchanged (tested).

## What it learned (Setup -> AI sorting)

A self-contained block (`srLearned` in app.js, `.ln*` in app.css, their own `0.8.5 Learning` sections). Each sender,
person and domain with its counts per label and what it learned ("Not sure yet" below the threshold), newest
first. **Forget** removes that identity from its events (an event with nothing left goes); **Forget all** (a second
press confirms) deletes every event.

## API

`GET api/sorting` has `learned` {items, names}; `GET api/sorting/learned`; `POST api/sorting/forget {id}` (`s:`,
`p:` or `d:` + value) or `{all: true}` returns the sorting view.

## Tests

`tests/test_learning_085.py` (fakes only; no model is ever called): S-L1 (labels and ids only; the prompt and
request body unchanged), S-L2 (50 promos make no prior; one fix doesn't; two do), S-L3 (unverified sender learns
nothing and gets no prior; a signature can't claim an address), overrides and sure replies never demoted, security
and invites never move, a prior moves a row and never hides it, an item fix places that item, conflict means no
prior, People links addresses, decay, Forget one / domain / all, Undo, Check and Done, routes, page block.
