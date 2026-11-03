# Projects (0.7.0): the planning assistant

Owner decisions, 2026-11-03: the tab is **Projects** (replaces "Tasks · coming later"); projects, tasks and notes
live in Callsheet only (never written to Obsidian or any account); GitHub read-only; the weekly review runs on
Monday morning by default (Monday morning / Friday afternoon / Sunday evening / off); "gone quiet" defaults to
7 days; slipped items also show in the Briefing. Design: `~/project/daily-brief/mockups/planning-v1.html`.

## Data model (`callsheet.db`, 0600, additive tables)
- `projects(id, dkey, state, name, area, status, next_step, next_due, people, notes, draft, ai, created, updated,
  accepted)`: `state` is `suggested | active | not | hidden`; `status` is `on | behind | wait | paused | done`;
  `dkey` is the stable draft key; `draft` holds the drafted fields (sources, milestones, links, quotes) as JSON;
  `ai` holds the AI's suggestion for a suggested row only.
- `project_tasks(id, project, text, due, state open|done|dismissed, origin owner|note, skey, created, done_at)`.
- `slipped_marks(key, action, until, note, at)`: done, fine (dismiss), snooze (7 days), note, reopen.
- Meta `projects`: `find {notes, calendar, threads, github}`, `quiet_days` (3/5/7/10/14, default 7), `owe_days`
  (1/2/3/5, default 2), `review` (`mon_am | fri_pm | sun_pm | off`). Meta `projects_review`: last review state.
- Meta `github`: token (never returned by the API), followed repos. Index meta `github_etags:<repo>`: ETags.

## Drafting (`projects.draft`, deterministic)
Sources, merged in priority order note > Know me profile > threads > calendar series > GitHub (candidates that
share a word of 4+ letters are one project):
- Obsidian notes tagged `#project`, in a `Projects` folder, or with project front matter. Note checkboxes
  (`- [ ]`) become suggested tasks; `(due YYYY-MM-DD)`, a calendar emoji date or `[due:: ]` make milestones.
- Thread clusters from Know me (only threads the owner wrote in).
- Calendar series: 3+ events with the same title, one in the last 45 days or upcoming; dates are milestones.
- Projects saved in Know me, and followed GitHub repos.
Next step: the earliest dated open task, else the first open task, else the next milestone. Status: behind when a
note or promise date was missed, paused after 60 days without activity. `build()` keeps accepted owner fields,
never brings back a project marked "not a project" and removes stale suggestions.

## Slipped (`projects.detect`, cached 5 minutes)
Per thread over the last 45 days, automated senders skipped:
- **Date passed**: the owner promised something by a date ("I'll send it by Friday") or an incoming ask named a
  date, the date is gone, and the owner hasn't written since.
- **Follow-up owed**: an incoming ask unanswered for `owe_days` up to `quiet_days`.
- **Gone quiet**: an incoming ask unanswered for `quiet_days` or more; "I'll get back to you" with nothing since;
  the owner's own ask or promise with no answer for `quiet_days`.
- Active projects: past-due note lines and owner tasks, a past-due next step (deduplicated against its note line),
  and no activity for twice `quiet_days`.
Each item quotes the exact sentence with a link to its source. Done items hide after a day.

## AI pass (optional, `start_ai`)
Off unless the writer is set up; skipped near a call time, during a sync, or with no suggestions. Exactly one
request (local: thinking off, no tools, JSON schema; or Claude). Input is aggregates only (key, words, area,
counts, at most 4 titles, people, dates, open tasks) inside the escaped `<data>` block. The schema's enum limits
answers to the suggested keys; unknown keys are ignored. A name or next step with links or markup, or flagged by
`inject.instructions`, is dropped; one flagged by `states_decision` is kept with "Check this suggestion: it states
an approval, payment or agreement you may not have made." Results stay "Suggested" until the owner adds them.

## Weekly review
Steps per the mockup (slipped, then each project, then suggestions) with a "This review" side column. Slot:
Monday at the morning call time (08:00 if the morning brief is off), Friday 16:00, Sunday 18:00. When the slot
has passed (within 12 hours), push targets exist and Home Assistant answers, one push per week: "Your weekly
review is ready: N projects, M slipped." (counts only, tag `callsheet-review`). The Briefing shows a Slipped line
(count + top item, links to Projects) and, on review day, a review card.

## GitHub (read-only, `github.py`)
A fine-grained token (Contents, Issues, Pull requests: Read); classic tokens are refused. The owner picks repos.
The (method, URL) allowlist permits only `GET` on `api.github.com` for `/user/repos`, `/repos/{o}/{r}`,
`/repos/{o}/{r}/commits`, `/repos/{o}/{r}/issues` and `/repos/{o}/{r}/pulls`, with allowlisted query keys; no `#`,
`//` or `%` in paths, no redirects. Pagination follows `Link: next` (each URL checked again); `If-None-Match` with
stored ETags; it stops when fewer than 5 requests remain and tries again after the reset. Commits, issues and pull
requests are indexed with source `github`. Unfollowing a repo or forgetting the token deletes its items.

## Ask about this project
Chat with `project_id` (new chats only) stores a scope `{project, name, ids}` on the conversation. The scope is an
id filter on the existing `search`, `get_item` and `list_events` tools (no new tool); `get_item` outside it says
"Item N isn't part of this project." The system prompt adds a scope sentence before the safety text, which stays
last. Since 0.7.1 the scope also stores the project's status name, and the sentence names the project:
`This chat is about the project "<name>" (status: <status>): search only finds its linked items.` followed by the
fixed filter rule. Name and status are owner-accepted data, treated as data: one line, clipped (60 / 20
characters), `"` turned into `'` and `<` `>` escaped as `\u003c` `\u003e`. Chats without a name keep the 0.7.0 fixed
sentence. A fixed nudge follows in every scoped chat: "For questions about this project, search its items before
answering." The unscoped prompt is unchanged byte for byte.

**First lookup (0.7.1).** The nudge alone didn't make Gemma search (projinj071: "What should I do next?" made no tool
call in 10 of 10 and answered "couldn't find any tasks"). So before the model's first request of every question in a
project chat, Callsheet runs the first lookup itself and sends it as the first `search` call with its `<data>` result:
the project's own summary (`Projects.chat_summary`: name, status, next step and date, open tasks (the owner's, then
the note's not yet added), up to 8 open milestones, up to 5 open slipped items of this project) and the top 8 results
of the same scoped `index.search` for the question (the project's newest linked items when nothing matches). Entries
that came from an index item outside the scope are dropped. Details and the token cap: `docs/chat.md`.

## API
`GET/POST api/projects`, `POST api/projects/build`, `POST api/projects/suggest`, `GET/PATCH api/projects/settings`,
`POST api/projects/review`, `POST api/projects/slipped/<key>`, `PATCH api/projects/<id>`,
`POST api/projects/<id>/accept|not|undo|hidden`, `POST/PATCH/DELETE api/projects/<id>/tasks[/<tid>]`,
`GET/DELETE api/github`, `PUT api/github/token`, `POST api/github/list`, `PUT api/github/repos`. The GitHub
token is accepted by `PUT` only and never returned.
