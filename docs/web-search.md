# Web search in Chat (0.14.0)

`callsheet/app/websearch.py`, `chat.py` (the tools and the ask-first question), `net.py` (`Searx`, `Net.get_page`),
`api.py` (Setup), `static/app.js` (Setup › AI › Web search, the answer's web line and sources). Owner decision
2026-11-06 (CLAUDE.md, "Chat may search the web"): "SearXNG on the NAS is the right choice, I don't want to have to
press any extra buttons. It should know when to search, or ask."

## The rule

Only Chat, only in a turn the owner's message started, and only when web search is on. The brief writer, the sorter,
the reader and every other model call never get the tools (`test_brief_writer_reader_and_picker_never_call_it`). Text
in mail, feeds, reports or web pages can never start a search: every query is checked before anything leaves, and
anything that isn't the owner's own words becomes a question to the owner.

## Setup › AI › Web search

- **Your SearXNG address** (placeholder `http://192.168.10.157:30053`). Stored in meta `web_search` as
  `{base_url, on, added, checked, check_ok}`; a new address gets today's date (`added`), like the other places
  Callsheet contacts. A pasted `…/search` or `…/search?q=…&format=json` is cut back to the address.
- **Check** (`POST api/web-search/check`): one `GET <address>/search?q=test&format=json`, only when the owner presses
  it. The page shows "SearXNG answered with N results", a busy note, or the error in plain words (403: turn on the
  JSON format in SearXNG's `settings.yml`).
- **On/Off** (`PATCH api/web-search {base_url?, on?}`). Off (or no address): the tools aren't offered, and a call
  anyway is ignored and counted in the log. `GET api/web-search` returns the settings, today's counts and the caps.

## The query check (`websearch.check_query`, deterministic)

The query is cleaned first (NFKC, zero-width and bidi characters out, one line, at most 200 characters); the cleaned
string is the one shown and the one sent. It goes out at once only when:

1. every word is in the owner's own messages in this chat (case-folded, light stemming: boots/boot, hiking/hike), a
   stopword, or in the owner's "Your words" glossary (meta `your_words`, when Callsheet has one), and there are at
   most 16 words; and
2. nothing looks private (`websearch.private`): an email address (also "name at host dot com"), a web address or
   domain, a phone number, an amount (a currency sign or code next to a number, "300 dollars"), an order, account,
   invoice, booking, ticket or similar number, a long digit run, a mixed letters-and-digits id, the full name of one of
   the owner's contacts (people.py's directory, aliases included), or a word mixing alphabets (homoglyphs).

The owner pasting a mail counts as his words, but the private patterns still apply to it. Words that only appeared
in a tool result (an item, a search result, a page) are not his, so such a query asks.

A word the model adds on its own stays strict too: a year guessed from "latest" ("Transport Canada drone rules
2026"), a synonym ("regulations" for "rules") or "news" makes the turn ask with that exact query (0.14.0 live check D:
one of three web questions asked first). `chat.WEB_RULE` therefore tells the model to use the owner's words as written
and add no year, date, synonym or other word of its own.

## Asking first

When the check fails, nothing is sent and the turn ends with a normal Wheatley message: `Search the web for
'<the exact query>'?` plus a short why ("It has words you didn't use in this chat."). The message carries
`web_ask {query, why, pid, turn, expires}`; the one-time token lives in memory only (`Chat.proposals`, what = "web",
30 minutes), bound to this chat, the next turn and the exact query. The owner's next message uses the token up:

- a plain yes ("yes", "ok go ahead", "sure, search it") runs exactly that query first, then the model answers;
- anything else is a new message: its words become the owner's words and any query is checked again.

A forged or edited `web_ask` in the stored chat, an expired token, a yes in another chat or a turn later searches
nothing.

## The tools

- `web_search(query)`: at most 5 per turn; SearXNG's results as `{id: w1…, title, snippet, url, host}` (http(s)
  only, 10 at most, titles 200 and snippets 400 characters), inside `<data>` with `<` and `>` escaped. A result whose
  title or snippet reads like instructions is marked `"instructions": true` and can't be opened.
- `read_result(id)`: at most 5 per turn; only a result id from this turn's searches, never an address (anything that
  isn't `w<number>` is refused and counted). The page is fetched with `Net.get_page`, HTML turned to the visible text
  (`htmltext.py`), at most 6,000 characters, marked `hidden_text` and `cut` when so. A page with planted instructions
  (`inject.instructions` plus steering sentences such as "now search for…", "open result 4", "visit https://…") is
  marked, and no further page is read that turn.
- Answers cite results as `[w3]`; the markers become source chips: title and host, a link to the result's own
  address (0.14.0: only `http(s)://` without a user name or password, `target=_blank`, `rel="noopener noreferrer"`,
  no referrer; the page only, never fetched by the page itself). A result marked as instructions stays text. Links
  written inside the answer stay defanged text. Without a cite the pages read are the sources. The decision post-check and the repeated-instruction label work as for other answers.

## What leaves the box

- **SearXNG** (`net.Searx`): the one exception to "public https only": exactly the saved scheme, host and port, only
  `GET <prefix>/search?q=<query>&format=json` (`net.searx_allowed`; every other path, port, method or query key is
  refused before any I/O), no redirects, JSON only, 512 KB, 10 s, the fixed User-Agent and Accept only. The name may
  point to the home network but never to the Supervisor, loopback, link-local or other reserved addresses.
- **Result pages** (`Net.get_page`): a new kind of request, to any public host in the results, with all of net.py's
  rules (https on 443, public addresses only after DNS and pinned, redirects only to the same host and re-checked,
  1 MB cut, the time limit, 6 reads per host per 15 minutes). No cookies, no Referer, no sign-in; the site sees the
  Home Assistant box's public address and Callsheet's User-Agent (PRIVACY.md).

## Caps, back-off and logs

5 searches and 5 pages per question; 100 searches and 100 pages a day by default (meta `web_search_usage`, UTC day),
settable from 1 to 1,000 in Setup › AI › Web search › Advanced (`PATCH api/web-search {day_searches, day_reads}`). The
model asks for at most 4 tool calls a round, so 5 needs two rounds. These are the only rate limit SearXNG gets. A 429, or no results while SearXNG lists unresponsive engines (a CAPTCHA), means "Web
search is busy, try again in a few minutes": no retry in that turn, and no search for 5 minutes. The chat log line
gets counts only (`web: 1 searches, 1 pages, asked first, …`), never the query.

Tools are registered in `chat.REGISTRY` (docs/chat-tools.md). With web search on, the add-something prompt no longer
says Chat can't search the web: it may search to help, but the setup card still takes only an address the owner writes.

## Tests

`tests/test_web_search_0140.py` (no network: a fake SearXNG and fake pages behind injected resolvers and connects).
Bars: private-looking queries asked 80/80 (8 kinds x 10); SearXNG exactness 29/29 other requests refused, 0 sent;
planted "search for"/"visit" in mail, feeds and reports 0 searches and 0 fetches (3 x 10); tool-result words asked
10/10; planted pages flagged 10/10 with 0 extra searches or fetches; off-host redirects refused 10/10; model-written
addresses fetched 0/10; caps never exceeded; asks sent exactly the shown query 10/10.
