# Picker (0.13.0): What you should know

Design: "Wheatley" 2.4 E (the stage the design calls 0.10.2 Picker). Code: `callsheet/app/picker.py`.
Tests: `tests/test_picker_0130.py`, gold set `tests/picker_gold.py`.

## What it does
Today gets a list of things that don't need the owner but are worth a mention, grouped as Meetings, Money, Alerts,
Good to know, Launches, News and Channels (only groups with items). Sources:
- Meetings, Money, Alerts, Good to know (mail): the reader's kinds for surfaced rows (meeting requests and changes,
  bills, renewals, payments, security and service alerts, deliveries, info and documents) and open invites. Security
  rows and invites are locked: always shown, no More / Less.
- Launches, News, Good to know (reports), Channels: the candidates the picker judges: promos and updates the sorter
  sorted out, news stories, channel uploads and reports of the last 36 hours.

Nothing moves: a kept promo stays in Sorted for you (marked "Stays in Sorted for you"); the sorter's groups, sections
and overrides are never changed. Feed and report items said here are skipped in their own lists (said once). A group
shows at most 5 items and 3 per source; the rest are counted ("and N more in the lists below") and stay in those lists.

## The request
One request per batch of up to 12 cards, to the owner's local model with the sorter's model and slot, after the
reader's pass (`triage.Sorter.read_after`). A card: from (email promo, news feed, ...), source (the owner's label or
the sender), title (160), the first two sentences of the text (links out, 320), inside the escaped `<data>` block.
`<interests>` (trusted): the owner's topics as i1..iN, never their levels. `<notes>` (trusted): the owner's notes about
those senders. Strict json_schema, temperature 0, thinking off. The answer is enums only, per card: `interest`
(i1..iN or none), `strength` (strong, some, none), `kind` (launch, update, deal, news, video, event, other), `big`
(yes, no). Any free text, missing or extra key or value outside its enum makes the whole batch invalid (retried once,
then the rules-only `baseline` decides). The fixed texts are pinned by `prompt_hash()`.

Not sent: sources set to Only in the list; channel uploads when there are 5 or fewer in the window (all are
mentioned); cards with an instruction aimed at an assistant (inject rules: stored as flagged); anything without
interests.

## The rules (decide / classify)
Applied when the page is built, so a changed level or source choice counts at once. In order: Only in the list = no
mention; a Never topic named in the card, or picked, = no mention; an owner note that says the sender or domain isn't
important = no mention; channel uploads = mentioned; Always tell me sources = mentioned; instructions = no mention;
a deal only when the matched topic says deals, sales, discounts or offers; Always tell me topics with strength strong
or some; If it's big topics with big = yes and strength strong or some. A report's `importance` is never read.

## Steering
A card that asks to be shown first, says it is the most important item, claims to match the owner's interests, claims
`big: yes`, or claims a note from the owner (`picker.steer`, with the sorter's `triage.steer`) is sent without those
lines (`strip_steer`): the model never reads them, so they can't change the pick. On the page it is marked ("Asks to
be shown first · shown last") and ranked last in its group.

## More / Less like this
Per source: a connector (`c:<id>`), a sender's domain (`d:<domain>`) or, on public mail domains, the address
(`a:<address>`). `POST api/picker/prefs {target, way}`; `DELETE api/picker/prefs/<id>`; listed in Setup under What to
watch for with Forget. They only rank within a group; never hide; never apply to security alerts or invites.

## Load rules
As the reader: blocked windows 06:16-06:45, 12:16-12:45, 16:46-17:15 and 01:00-04:00 (owner's time zone), the
sorter's call guard and careful guard, Chat first (`writer.LOCAL_LOCK`, `Busy`), each request inside the in-flight
budget (`writer.INFLIGHT_MAX`), ContextFull waited out once (10 s) and retried at half size, a second time ends the
run with every card still queued. Day caps: 400 cards, 15 GPU minutes.

## Storage and logs
`picker_items` (enums and the owner's stored interest id; 7 days), `picker_queue` (references), `picker_prefs`.
Logs are counts only. `GET api/sorting` has `picking` (counts, p50/p95, schema-valid rate).

## The live check
Outside the repo (scratchpad harness): schema-valid rate (bar 98%), precision and recall per class on the gold set
with Wilson intervals against the rules-only baseline, overrides 20/20, paired injection runs (bar: at most 1 flip in
10 batches), owner notes respected 9/10, stability on a repeat at temperature 0 (95%), batch p95 30 s and Chat p95 10 s
during the run, ContextFull with 0 lost.
