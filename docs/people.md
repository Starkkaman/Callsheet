# People (0.8.2)

One person across mail, Slack and Signal. Design: `~/project/daily-brief/understanding/design.md` section (c) and
stage 0.8.2; mockup `mockups/understanding-v1.html` frame 4 ("Same person?"). Code: `callsheet/app/people.py`.
Rules only: no AI, no new network request except the owner-pressed Gmail send-as read.

## Data model (`callsheet.db`, additive tables created by `people.People`)

- `people(id, name, aliases JSON, state, vip, area, merged_into, created, updated)`. `state`: `suggested` (seeded),
  `confirmed` (the owner merged into it or edited it), `me` (the one owner row, id `me`), `merged` (kept for Split,
  `merged_into` points at the person it went into). Ids: `p` + 11 hex.
- `person_handles(person, kind, value, label, verified, source, n, extra JSON, added)`, `UNIQUE(kind, value)`.
  Kinds: `email` (mail, calendar, Drive owners, VIPs; lower case), `slack` (user id, lower case, as reply rows carry
  it), `signal` (Callsheet's salted sender id; the owner's own is `me`). `extra`: Slack `real_name`, profile `email`
  and workspace; Signal `contact` name. `verified`: `google`, `slack`, `signal` (accounts), `owner` (VIP list),
  `inferred` (contacts).
- `merge_proposals(id, a, b, signals JSON, score, by, state, created, decided, until)`: one row per pair (`mp` +
  10 hex of the sorted pair). `state`: `open`, `merged`, `different` (never asked again), `later` (`until` = a week).
- `people_audit(id, at, action, a, b, names, detail JSON, undone)`: `merge` (detail: handles moved, aliases added,
  VIP/area taken from b, the proposal), `split`, `different`.
- "Notes link" is computed, not stored: memory notes stay in `memory.md`; a note's emails and names map to people
  through `Directory.note_people`.

## Match signals (deterministic, each with its "why")

| code | weight | why (examples) |
|---|---|---|
| `slack_email` | 1.0 | Their Slack profile email is priya@acme.com. |
| `email_same` | 1.0 | priya.nair@gmail.com and priyanair@googlemail.com are one address written two ways. (case, +tag, Gmail dots, googlemail) |
| `signature` | 0.9 | The signature in mail from priya@acme.com lists pn@gmail.com. (last 12 lines before any quote, header-like lines skipped, 1 to 4 addresses, 180 days of mail) |
| `name_domain` | 0.7 | Same full name, Priya Nair, at acme.com and eu.acme.com. (domain family: acme.com, acme.co.uk, mail.acme.com) |
| `name` | 0.5 | Same full name, Priya Nair, at different addresses. |
| `slack_name` / `signal_name` | 0.5 | Their Slack name is Priya Nair. / Their Signal contact name is Priya Nair. |
| `unique_name` | 0.3 | No one else you know has this name. |

Planted signatures: when either person is a VIP or has a verified handle (VIP list, a connected account, Slack or
Signal), a signature counts 0.6 at most (shown as "Good", never "Strong"); the card still opens and, when the
signature is the only evidence, says "Only evidence: a signature in a message from <sender>; check before merging."
`unique_name` never counts towards opening a card into a VIP, so a display-name spoof of a VIP is never suggested.

A name alone never gives VIP status anywhere (0.8.2): mail by the VIP's own address that Gmail didn't reject, Slack by
a VIP person's user or a VIP address as the profile email, Signal by a VIP person's sender id. A display name that is
a VIP's gets `uses_name` ("Uses <VIP>'s name") on mail, Slack and Signal rows; the owner's name from elsewhere gets
`uses_your_name`. Mail from the owner's own address with a failed sender check (DMARC fail) is not the owner's: the
row stays, marked `sender_fail`, and it never counts as the owner's reply.

Score = 1 - product(1 - weight). A card opens at one signal of 0.8 or more, or two signals reaching 0.5. Strength on
the card: Strong (0.9+), Good (0.6+), Some. Never proposed: role or automated addresses (info@, team@, noreply@…),
first names on their own (a full name has two words of two letters or more), the owner's own names, groups of more
than six people with one name, and the owner record. `unique_name` is an addition to the design's list so a
single cross-channel name match can reach "two signals"; it never opens a card alone.

## The owner record

`refresh_me()` rebuilds it from connected accounts only: every Google account email, the Gmail send-as addresses
read with the owner's button (`GET gmail/v1/users/me/settings/sendAs`, inside the existing allowlist prefix, works
with `gmail.readonly`; primary and accepted addresses of connected accounts only), each Slack workspace's own user id
(auth.test), and `signal:me` when Signal is linked. Message content is never read for it: a signature can't point at
the owner (owner addresses are dropped on both sides), no proposal can involve `me`, `merge()` refuses `me`, and the
names the owner types ("Names people use for you") only mark mail ("Uses your name; not from your accounts.").

## Where people are used

- VIPs (`vip_map`): the VIP list plus every email of a VIP person, with `.slack` and `.signal` sets that
  `rules.slack_rows`, `rules.signal_rows` and `push` check. VIP off on a person removes their addresses from the VIP list.
- Memory (`memory.relevant(..., people=directory)`): a note's emails and unique names give person ids; a reply row
  whose sender (mail, Slack or Signal) belongs to that person scores like an exact email.
- Reply rows (`api._brief_people`, at serve time): a confirmed person's name replaces the sender name (not on
  sender-check failures or name spoofs); a row whose sender is in an open pair carries the card (3 at most).
- Chat search: the person filter expands to every name and address of the person (`Directory.search_terms`).
- Projects: people lists show confirmed people's names; the owner's addresses come from the owner record.

## Migration and refresh

The tables are created on start. `GET api/people` seeds when the last seed is older than six hours (or never ran):
VIPs, the Know me profile draft's people, a year of index contacts (mail you wrote to, two or more mails from them,
Drive owners, calendar guests seen twice), Slack users the brief knows and Signal senders. Seeding adds people and
handles, fills names, VIP and area only on unconfirmed people, never merges and never adds to the owner. At most
3,000 people.

## API

`GET api/people[?q=]` (me, proposals, people (300 at most), total, audit); `POST api/people/refresh`;
`PATCH api/people/me {names}`; `POST api/people/me/send-as`; `POST api/people/proposals/<id> {answer: same |
different | later, confirm?}`; `POST api/people/merges/<audit id>/split`; `PATCH api/people/<id> {name, vip, area}`.
`GET api/state` has `people.open`.

Merge into a VIP (second step): `same` on a pair where either person is a VIP (the flag, or an address on the VIP
list) changes nothing and returns `{ok: false, needs_confirm: true, confirm: <one-time token>, vip, names, evidence:
[{strength, why}], caution}`; the page shows "Merge into a VIP?" with the evidence and **Yes, merge** / **Cancel**.
`{answer: same, confirm: <token>}` merges. The token is bound to the pair and the proposal, used once, and lasts ten
minutes (kept in memory; a restart asks again); a wrong or expired token is a 400. A merge into "me" is refused with
or without a token.

Split ("Undo merge") restores exactly: the moved handles go back, the merged person gets back their state, and the
person they went into gets back their name, aliases, VIP flag, area and state when nothing changed them since the
merge (else only what the merge added comes off). Suggestions the merge moved from b to a go back to b (unless the
owner answered them since). The split pair is kept apart. VIP coverage and notes links follow the handles.

## Memory paste warning (0.8.2)

Notes the owner writes by hand (Setup → Memory file, editing a note saved from chat, a note on a brief item) go
through `inject.instructions` and `writer.states_decision`. A hit returns `{saved: false, warning, kinds}` and the
page shows "This looks like an instruction / a claim copied from a message. Save anyway?"; **Save anyway** sends
`confirm: true`. In the memory file only new or changed entries are checked.

## Open questions

- Reading Gmail send-as: no new scope is needed, but the design lists it as an owner question; it only runs when the
  owner presses the button.
