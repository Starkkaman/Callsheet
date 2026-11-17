# Visual blocks (0.19.0, rework stage 6a)

The talk-through brief speaks in turns: a few words, and now and then a picture. The pictures are **visual blocks**:
the brief model emits a small JSON block, Callsheet checks it, fills in the facts from its own store and draws it in
the current look (Day or Night). The model never draws anything and never supplies a number, a name
or a time that Callsheet can look up itself.

Code: `callsheet/app/visuals.py` (schemas, checker, filling), `callsheet/app/visuals_sample.py` (the gallery's sample
store), `callsheet/app/static/visuals.js` and `visuals.css` (the renderer). The brief UI that shows them in turns is
stage 4; this stage is the renderer, the schemas and the plumbing.

## How a model uses it

A turn's JSON carries a `show` list next to its words, at most **3** blocks:

```json
{"say": "Tomorrow evening clashes: Dana and Marco both want 7.",
 "show": [{"type": "clash", "ref": "clash:2"}]}
```

- **Point, don't copy.** Every block names things by id: `msg:N` (a message in `messages.db`), `item:N` (a ledger
  item), `cal:N` (a calendar event), `clash:N` (a clash the ledger found). Callsheet fills in who, what, when, how
  long ago, the source and the counts from the store.
- **Quotes for what the ledger doesn't hold yet.** (0.20.0: amounts and package stages come from the ledger when the
  item holds them; see Open edges.) Amounts, balances, places, tracking numbers, prices, levels and the
  like are *quotes*: the model copies them from the referenced message (`"amount": "$1,240"`). Callsheet keeps one only
  when it is found in that message (case and spacing ignored, numbers whole: `$124` is not in `$1,240`) and then uses
  the message's own text. A quote that isn't there is left out and listed in the block's `checks`; the renderer shows a
  quiet "Left out: an amount (not found in the store)" line rather than dropping it silently.
- **Words still play.** A block that fails is dropped, never drawn raw; the turn's words are unaffected.
- `GET api/visuals/schema` returns every type's JSON Schema and the limits: this is what the brief model is shown.

## Limits and the checker

| Limit | Value |
|---|---|
| Blocks in a turn | 3 (more are dropped as `too_many`) |
| One block's JSON | 4096 bytes (`oversized`) |
| Composite nodes / depth | 40 / 4 |
| Schema version | `"v": 1` (optional; another version is `invalid`) |

Every schema is strict (`additionalProperties: false`, required keys, string lengths, enums, patterns, array bounds;
no control characters). `$id` is `callsheet:visual/<type>/1`. `compose(show, facts)` returns
`{"blocks": [...filled], "dropped": {"unknown", "invalid", "oversized", "unfilled", "too_many"}, "problems": [...]}`:
`unknown` is a type Callsheet doesn't have, `invalid` fails its schema, `unfilled` points at nothing in the store.

## The types

Bold keys are required. `items`/`legs`/`sides` are lists of refs; the rest are options or quotes.

| Type | Block | Keys |
|---|---|---|
| `day` | Day timeline | date, from, to, items, free |
| `clash` | Clash diagram | ref, sides |
| `asks` | Needs-you stack | order, items, max |
| `upcoming` | Coming-up strip | days, items |
| `money` | Money card | **bills** |
| `trip` | Trip card | **to**, **legs** |
| `person` | Person and thread snippet | **ref**, mark |
| `cleanup` | Clean-up summary | kinds |
| `bills` | Bills ladder | days, **items** |
| `ledger` | Money in and out | range, **items** |
| `package` | Package tracker | **name**, **steps**, arrives, tracking |
| `ticket` | Event ticket | **ref**, place, state, register_by |
| `tasks` | Account tasks | order, **items** |
| `health` | Health inbox | **ref** |
| `stat` | Weekly stat | **ref**, **metric**, **values**, goal, average, range, change |
| `local` | Local alert | **items** |
| `price` | Price watch | **ref**, **name**, range, new, **matches** |
| `leads` | Leads digest | **ref**, count, what, top |
| `series` | Recurring series | **ref**, **name**, **dates**, time |
| `invite` | Invite card | **ref**, what, app, expires |
| `subs` | Subscriptions and renewals | days, **items** |
| `composite` | Composite | **name**, title, ref, **body** |

Notes:
- `day`: `from`/`to` are window hints ("7 am"); the window grows to fit what's shown. Free gaps are worked out.
- `clash`: either `ref` (a clash the ledger found) or `sides` (two refs).
- `asks`, `upcoming`, `cleanup`: with no `items` they read the ledger's own Needs you / Coming up / Clean up.
- `health`: sender and time only. The message's subject and text are never read into the block, whatever it says.
- `local`: the level word (Info, Advisory, Warning) must appear in the message.
- `composite`: a tree of safe parts, the path for model-defined types later (options B and C): `row`, `column`
  (`c`: children), `text` (`text`, `style` body|dim|label|big), `number` (`value`, `unit`), `time_axis` (`from`, `to`,
  `c` of `range_bar`/`dot` with refs), `list`, `icon` (a fixed set: mail, signal, slack, text, cal, box, lock, repeat,
  plane, alert, check, news), `progress` (`value` of `of`), `table` (`head`, `rows`). Any text with a digit must carry a
  `ref` and be found in that message. No SVG, HTML, images or URLs from the model.

## What visuals.js gets

A filled block: `{"v": 1, "type", "label", "alt", "d": {...}, "checks": [...]}`. `label` is the block's accessible
name, `alt` its full text alternative (read instead of the drawing), `d` the drawing's data (positions as hours or
percentages, words already worked out), `checks` what was left out. `window.CallsheetVisuals.render(block)` returns a
`<figure>` (or nothing for a type it doesn't know); `gallery()` returns the Setup gallery.

The renderer's rules: every string goes in with `textContent`; axes, bars, dots and the sparkline are built by code
(positioned boxes and inline SVG from `createElementNS`); links only `https://`, opened with `noopener noreferrer`;
no requests except the gallery's relative `api/visuals/gallery`; the drawing is `aria-hidden` and the figure carries
`aria-label` plus a `figcaption` with the text alternative; links are 44 px tall; no motion (and reduced motion is
respected for anything a look adds). Layout follows the block's own width (container queries), so a block in a narrow
column reads like a phone.

## API

- `GET api/visuals/schema`: the catalogue (version, limits, ref kinds, every type's schema).
- `POST api/visuals/preview` `{"block": {...}}` or `{"show": [...]}`, `"store": "sample"` (default) or `"live"`: what
  would be drawn, with what was dropped and why. Read-only; for tests and tools.
- `GET api/visuals/gallery`: every type drawn from the sample store, plus a dropped example.

## The gallery

Setup › Advanced › Visuals shows every block with sample data (a fixed store as of Wed Nov 11, 9:00 am), the JSON the
model would write under each, and two blocks that are dropped. It uses the same `compose` path as the brief will.

## Tests

- `tests/test_visuals_0190.py`: schemas (good, bad, unknown, oversized, composite limits, too many), quotes, filling
  from fakes and from a real `messages.db` with ledger items and a clash, health without content, the API routes, and
  the renderer's static rules (no HTML insertion, relative URLs, loaded before app.js).
- A Playwright page check (outside the suite): every block at 400 and 1280 px in Day and Night, no
  overflow, AA contrast, no foreign requests, a label and text alternative each, 44 px targets on phones.

## Open edges

- Since 0.20.0 the ledger keeps an item's amount (+ currency) and stage (`docs/ledger.md`): `money`, `bills` and
  `ledger` show the ledger's amount and `package` the ledger's stages (out for delivery is drawn as on the way); the
  block's own `amount`/`stage` is then optional and only compared (a difference is listed in `checks`). They stay
  quotes for plain messages, items read before 0.20.0 and a bill's minimum. Levels, places, prices and tracking
  numbers are still quotes.
- `ledger` is the type name for money in and out (from the mockup); it is not the ledger itself.
