"""0.21.4 Tidy up: stricter sorting of what reaches Today, the owner's one-press re-sort with one Undo, and the
"Older, still open?" rule. Nothing is deleted or hidden: a moved item becomes a Clean up line with its reason (a check
with field "tidy" and the group it shows in), and Undo puts it back as it was.

The rules (docs/ledger.md, "Tidy up"):
1. Promo mail with no headers: mail imported or backfilled with no bulk flag or labels still reads as a list or
   machine sender (no-reply and role addresses, mailing-service domains, unsubscribe text) and promo cues
   ("% off", sale, offer ends, "chip in", donate, a gift card promotion, a subscription offer). Promo mail only gives
   a Clean up line: never a bill, ask, payment, event or clash. A real bill from machine mail needs an amount owed
   plus due-date or statement words from a billing-type sender (`bill_proof`).
2. Senders that were only suggested for People (added on their own, not confirmed by the owner) don't count as known
   people (extract.read_tier and `Known`).
3. Slack: a channel post that doesn't mention the owner, doesn't ask "you" directly (`asks_reader`: "can you", "can
   we", "let's", naming no one else) and isn't in a thread the owner wrote in goes to the Clean up group "From your
   channels", never Needs you. DMs, @mentions and direct asks stay.
4. Clashes: none where either side is a Clean up line, closed or already started (`clash_ok`); at most
   CLASH_PER_ITEM per item, the rest grouped (today.py).
5. Aging (`older_of`): open asks and items whose time passed more than AGE_DAYS ago, or that came from the first
   backfill with no activity since, show in the collapsed "Older, still open?" group on Today (still open, still in
   search), with Clear all (done, with Undo).
6. `Tidy.run` re-applies rules 1-3 to existing items when the owner presses Tidy up (Today's banner, or a Chat card he
   confirms); `Tidy.undo` puts the whole run back. Never automatic.

Data is text: message text is only matched here, never shown or sent anywhere.
"""
import datetime as dt
import json
import re

import ledger as L
import people

AGE_DAYS = 14
AGE_S = AGE_DAYS * 86400
CLASH_PER_ITEM = 3
HEAD = 600                    # promo cues count in the subject and this much of the text
BANNER_KEY = "tidy_banner_0214"

# ---- rule 1: promo mail without headers -------------------------------------------------------------------------------
# Mailing services' sending domains (a list sender, whatever the brand) and the marketing subdomains brands send from.
ESP_DOMAINS = re.compile(r"(?:^|\.)(?:mcsv\.net|mcdlv\.net|rsgsv\.net|mailchimpapp\.net|list-manage\.com|ccsend\.com|"
                         r"constantcontact\.com|sendgrid\.net|klaviyomail\.com|klaviyo\.com|exacttarget\.com|"
                         r"sailthru\.com|bmsend\.com|mailgun\.org|sparkpostmail\.com|cmail\d*\.com|"
                         r"createsend\d*\.com|hubspotemail\.net|hs-email\.net|em\.salesforce\.com|"
                         r"emarsys\.net|braze\.com|iterable\.com|customeriomail\.com|"
                         r"substack\.com|beehiiv\.com|convertkit-mail\d*\.com|e2ma\.net|myemma\.com)$", re.I)
MARKETING_SUB = re.compile(r"^(?:e|em|email|emails|mail|mailer|mailing|news|newsletters?|offers?|deals?|promo|promos|"
                           r"marketing|mktg|campaigns?|info|hello|shop|store|go|send|reply|engage|members?|"
                           r"rewards?|updates?|community|learn|events?)\d*\.", re.I)
LIST_LOCAL = re.compile(r"^(?:news(?:letters?)?|offers?|deals?|promos?|promotions?|marketing|sales|shop|store|rewards?|"
                        r"members?|membership|subscriptions?|donate|donations?|fundraising|campaigns?|community|"
                        r"events?|hello|hi|team|info|contact|updates?|email|mail|digest|specials?)\d*$", re.I)
UNSUBSCRIBE = re.compile(r"\bunsubscrib\w*|\bopt[- ]out\b|manage (?:your )?(?:email |subscription |communication )?"
                         r"(?:preferences|settings|subscriptions?)|update your (?:email )?preferences|"
                         r"view (?:this email |it )?(?:in|on) (?:your |a )?(?:web )?browser|no longer (?:wish|want) to "
                         r"receive|you(?:'re| are) receiving this (?:email|message) because", re.I)
PROMO_CUE = re.compile(
    r"\b\d{1,2}\s?%\s?off\b|\bsave (?:up to )?\d{1,2}\s?%|\bup to \d{1,2}\s?% off|"
    r"\b(?:flash|big|huge|final|annual|holiday|summer|winter|spring|fall|clearance|semi-annual|warehouse|"
    r"black friday|cyber monday|labor day|memorial day)\s+sale\b|\bsale (?:ends|starts|is on|now on)\b|"
    r"\bon sale\b|\boffer (?:ends|expires)\b|\b(?:ends|expires) (?:tonight|today|soon|at midnight)\b|"
    r"\blimited[- ]time\b|\blast chance\b|\bdon[’']t miss (?:out|this)\b|"
    r"\bchip in\b|\bdonat(?:e|ion|ions)\b|\bpitch in \$|\bgive \$\d|\bmatch(?:ed|ing)? (?:gift|donation)s?\b|"
    r"\bfundrais\w*\b|\bcampaign deadline\b|"
    r"\b(?:free|bonus|\$\d+)\s+gift card\b|\bgift card (?:with|when you)\b|"
    r"\b(?:subscribe|subscription) (?:and|&) save\b|\b(?:join|start|renew) (?:now|today) and save\b|"
    r"\bfree (?:trial|shipping|gift|month)\b|\b(?:\d+|first) months? free\b|"
    r"\bspecial (?:rate|offer|price|pricing|deal)\b|\bexclusive (?:offer|deal|access|savings)\b|"
    r"\bcoupon\b|\bpromo code\b|\buse code\b|\bshop now\b|\bbuy now\b|\bbook now\b|\border now\b|"
    r"\bdeals? (?:of|for) the\b|\bdoorbuster|\bbogo\b|\bnew arrivals\b|\bwebinar\b|\bnewsletter\b", re.I)
TRANSACTIONAL = re.compile(
    r"\b(?:receipt|your order|order (?:#|no\.?|number|confirm\w*|status)|has shipped|shipped|out for delivery|"
    r"delivered|tracking (?:number|#)|statement (?:is )?(?:ready|available)|e-?statement|amount due|balance due|"
    r"minimum (?:payment )?due|payment (?:due|received|scheduled|confirm\w*|reminder|failed|declined)|invoice|"
    r"autopay|auto-pay|your (?:bill|account|reservation|booking|appointment|ticket)s?\b|confirmation (?:#|number|code)|"
    r"password|verification|sign[- ]?in|security|itinerary|check-in|arriving|arrives|on (?:its|the) way|package|"
    r"shipment|delivery|registration renewal|renewal notice)", re.I)
NEVER_HIDE = re.compile(r"(security alert|new sign[- ]?in|sign-in attempt|password (?:was )?(?:changed|reset)|"
                        r"verification code|one[- ]time (?:code|passcode)|\b2fa\b|login code|suspicious|"
                        r"invitation:|invited you|you(?:'re| are) invited)", re.I)   # extract.NEVER_HIDE
FREEMAIL = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "me.com",
                      "aol.com", "proton.me", "protonmail.com", "live.com", "msn.com", "fastmail.com"})

# ---- rule 1: what a real bill needs -----------------------------------------------------------------------------------
BILL_SUBS = frozenset({"bill", "payment", "invoice"})
MONEY = re.compile(r"(?:[$€£]\s?\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP|dollars)\b)", re.I)
DUE_CUE = re.compile(r"\b(?:amount due|balance due|total due|minimum (?:payment )?due|minimum payment|payment (?:is )?due|"
                     r"due (?:on|by|date)|"
                     r"pay by|past[- ]due|overdue|statement|invoice|bill is ready|new bill|your bill|autopay|auto-pay|"
                     r"renew(?:al|s)? on|will renew|charged on|amount owed|you owe|balance of|premium|rent (?:is )?due|"
                     r"make a payment|pay (?:your|now|online)|payment (?:is )?(?:due|scheduled|processed|posted))\b", re.I)
BILLING_SENDER = re.compile(r"(?:^|[._+-])(?:bill|billing|bills|ebill|statements?|invoices?|invoice|payments?|pay|"
                            r"accounts?|account-?services|customer-?service|service|alerts?|notify|notifications?|"
                            r"no-?reply|do-?not-?reply|receivables?|ar|finance|collections?|autopay)(?:$|[._+-])", re.I)
# The strict billing addresses (billing@, invoices@, statements@, receivables@ …): their own mail is a bill without due
# words unless it carries a promo cue. Not no-reply, alerts, service or accounts: shops and campaigns send from those.
BILLING_LOCAL = re.compile(r"(?:^|[._+-])(?:bill|billing|bills|ebill|e-bill|statements?|invoices?|invoicing|payments?|"
                           r"receivables?|ar|collections?|autopay|accounts-?(?:payable|receivable))(?:$|[._+-])", re.I)
BOOKING = re.compile(r"\b(?:confirm(?:ed|ation)|reservation|reserved|booking|booked|ticket|your appointment|itinerary|"
                     r"check-?in|scheduled for|see you (?:on|at)|registration confirmed|you(?:'re| are) registered)\b", re.I)
LIST_EVENT_SUBS = frozenset({"appointment", "meeting", "party", "job", "gig", ""})


def _labels(row):
    v = row.get("labels")
    if isinstance(v, str):
        try:
            v = json.loads(v or "[]")
        except ValueError:
            v = []
    return {str(x) for x in v or [] if x}


def address(row):
    s = str(row.get("sender") or "").strip().lower()
    m = re.search(r"<([^>]+)>", s)
    return (m.group(1) if m else s).strip()


def domain_of(addr):
    return addr.rsplit("@", 1)[1] if "@" in addr else ""


def list_signals(row):
    """The list or machine signals of one stored mail that work without headers: the stored bulk flag and Gmail tab
    (live mail), an automated or role local part, a mailing-service or marketing subdomain, a list-style local part,
    unsubscribe text in the body."""
    out = []
    labels = _labels(row)
    if row.get("bulk"):
        out.append("bulk")
    if "CATEGORY_PROMOTIONS" in labels:
        out.append("promotions tab")
    a = address(row)
    local, dom = a.split("@", 1)[0], domain_of(a)
    if local and people.AUTOMATED_SENDER.search(local):
        out.append("no-reply sender")
    elif local and (people.ROLE.match(local) or LIST_LOCAL.match(local)):
        out.append("role sender")
    if dom and (ESP_DOMAINS.search(dom) or MARKETING_SUB.match(dom)):
        out.append("mailing-service sender")
    if UNSUBSCRIBE.search(str(row.get("text") or "")):
        out.append("unsubscribe text")
    return out


def by_name(row, names):
    """A person writing to the owner through a mailing tool: a personal (not no-reply or role) address that greets the
    owner by name and asks something (extract.asks_by_name, the read tier's "asks you by name")."""
    import extract                                     # late: extract imports tidy
    a = address(row)
    local = a.split("@", 1)[0]
    if not names or not local or people.AUTOMATED_SENDER.search(local) or people.is_role(a):
        return False
    return extract.asks_by_name(row, {"owner_names": list(names)})


def promo_of(row, known=None, names=()):
    """("promo", why) for promo mail, else ("", ""). Incoming Gmail only; never mail that is a security alert, a code or
    an invite, from a confirmed person, in a thread the owner wrote in, or from a domain the owner writes to (his work
    groups). Promo = a list or machine signal plus a promo cue in the subject or the start of the text, with no
    transactional words in the subject; or Gmail's Promotions tab with no transactional words, unless a person asks
    the owner by name through a mailing tool (`by_name`, names: the owner's names)."""
    if row.get("source") != "gmail" or row.get("direction") != "in":
        return "", ""
    subj, text = str(row.get("subject") or ""), str(row.get("text") or "")
    if NEVER_HIDE.search(subj) or NEVER_HIDE.search(text[:HEAD]):
        return "", ""
    if known is not None and known.protects(row):
        return "", ""
    sig = list_signals(row)
    if not sig:
        return "", ""
    if TRANSACTIONAL.search(subj):
        return "", ""
    # a cue only in the text counts with a list signal beyond a no-reply address (a pharmacy's delivery notice from
    # no-reply mentions free shipping in its footer)
    cue = PROMO_CUE.search(subj) or (PROMO_CUE.search(text[:HEAD]) if set(sig) - {"no-reply sender"} else None)
    if cue:
        return "promo", f"promo mail ({', '.join(sig)}; “{cue.group(0).strip()[:30]}”)"
    if "promotions tab" in sig and not by_name(row, names):
        return "promo", f"promo mail ({', '.join(sig)})"
    return "", ""


def billing_sender(row):
    a = address(row)
    local, dom = a.split("@", 1)[0], domain_of(a)
    return bool(BILLING_SENDER.search(local) or re.match(r"(?:bill|billing|pay|payments|accounts?|statements?|"
                                                          r"service|servicing|alerts?|notify)\.", dom))


def bill_proof(it, row):
    """A bill or payment from machine mail is real only with an amount owed and due-date or statement words, from a
    billing-type sender (a billing, payments, accounts or no-reply address, or statement words in the subject). Mail from
    a strict billing address (BILLING_LOCAL: billing@, invoices@ …) with no promo cue is a bill without due words."""
    hay = " ".join(str(x or "") for x in (it.get("amount"), it.get("what"), it.get("purpose"), it.get("phrase"),
                                         row.get("subject"), str(row.get("text") or "")[:4000]))
    if not DUE_CUE.search(hay):
        return bool(BILLING_LOCAL.search(address(row).split("@", 1)[0])) and not PROMO_CUE.search(hay)
    # an amount owed with due words; or due or statement words from a billing-type sender or in the subject (a
    # statement notice that names no amount: the amount is behind the sign-in)
    return bool(it.get("amount") or MONEY.search(hay)) or billing_sender(row) or \
        bool(DUE_CUE.search(str(row.get("subject") or ""))) or not PROMO_CUE.search(hay)


def machine_mail(row):
    """Mail no person stands behind: an automated or role address, a mailing service, the bulk flag or a tab."""
    if row.get("source") != "gmail" or row.get("direction") != "in":
        return False
    return bool(list_signals(row)) or bool(people.is_role(address(row)))


def judge_mail(it, row, known=None, names=()):
    """For an item made from incoming Gmail: (group, why) when rule 1 moves it to Clean up, else None."""
    if it.get("type") == "cleanup" or it.get("subtype") in ("security", "invite") or \
            NEVER_HIDE.search(f"{it.get('what') or ''} {row.get('subject') or ''}"):
        return None
    if known is not None and known.protects(row):
        return None
    kind, why = promo_of(row, known, names)
    if kind:
        return "promo", why
    if not machine_mail(row):
        return None
    sub = it.get("subtype") or ""
    if sub in BILL_SUBS and not bill_proof(it, row):
        return "promo", "not a bill: no amount owed with a due date or statement from a billing sender"
    if it.get("type") == "event" and sub in LIST_EVENT_SUBS and ("bulk" in list_signals(row) or
                                                                 UNSUBSCRIBE.search(str(row.get("text") or ""))) \
            and not BOOKING.search(f"{row.get('subject') or ''} {str(row.get('text') or '')[:2000]}"):
        return "promo", "list mail with no booking words: not your appointment"
    return None


# ---- rule 3: Slack channel posts ----------------------------------------------------------------------------------------
SLACK_WHY = "a channel post that doesn’t mention you or ask you, not in a thread you wrote in"


def slack_channel(row):
    """An incoming Slack post in a channel (public or private), not a DM or group DM."""
    if row.get("source") != "slack" or row.get("direction") != "in":
        return False
    if any(isinstance(r, dict) and r.get("address") == "me" for r in row.get("recipients") or ()):
        return False                                   # sent to him by name (live channel posts list only the channel)
    th = str(row.get("thread") or "")
    labels = _labels(row)
    return bool(labels & {"channel", "private"}) or str(row.get("channel") or "").startswith("#") or th[:1] in ("C",)


def mentions_owner(row, names):
    text = str(row.get("text") or "")
    if "mentions_me" in _labels(row) or re.search(r"(?<!\w)@(?:you|here|channel)\b", text, re.I):
        return True
    names = sorted((n for n in names or () if len(n) > 1), key=len, reverse=True)
    return bool(names) and re.search(r"(?<!\w)@?(?:" + "|".join(re.escape(n) for n in names) + r")\b", text,
                                     re.I) is not None


# A direct ask of the reader in a channel ("can you be in early Thu?", "can we do the review at 8:30?", "let's sync
# Monday"): the second person, with no one else addressed (an @mention of someone else, "someone", "anyone", "all",
# "team", or a post that opens with another name: "Kofi, can you…"). "Can someone check…" stays gated.
DIRECT_ASK = re.compile(r"\b(?:can|could|would|will|do|are)\s+(?:you|u|we)\b|\blet[’']?s\b|\bwould you mind\b", re.I)
OTHERS = re.compile(r"\b(?:some|any|every)(?:one|body)\b|\b(?:you|u)\s+(?:all|guys|two|both)\b|\by[’']?all\b|"
                    r"\b(?:folks|team|everybody|all of you|whoever)\b", re.I)
OPENERS = frozenset("hey hi hello ok okay so also and but quick reminder thanks thank morning afternoon evening yo "
                    "sorry fyi update heads please actually btw great perfect cool sure yes yeah no".split())


def asks_reader(row, names=()):
    """A Slack channel post that asks the reader directly in the second person and addresses no one else."""
    text = str(row.get("text") or "")[:400]
    if not DIRECT_ASK.search(text) or OTHERS.search(text):
        return False
    own = {str(n).lower() for n in names or () if n}
    if any(m.lower() not in own for m in re.findall(r"(?<!\w)@([A-Za-z][\w.'-]*)", text)):
        return False                                   # @someone else
    m = re.match(r"\s*([A-Z][\w'-]+)\s*[,:]", text)
    return not (m and m.group(1).lower() not in own and m.group(1).lower() not in OPENERS)   # "Kofi, can you…"


# ---- the owner's own circle ---------------------------------------------------------------------------------------------
class Known:
    """Who counts as known for the rules: confirmed People (not suggested ones), addresses and domains the owner has
    written to (not free-mail domains), threads the owner wrote in."""

    def __init__(self, confirmed=(), wrote_to=(), threads=()):
        self.confirmed = {str(x).lower() for x in confirmed}
        self.wrote_to = {str(x).lower() for x in wrote_to}
        self.domains = {domain_of(a) for a in self.wrote_to} - FREEMAIL - {""}
        self.threads = set(threads)

    @classmethod
    def load(cls, ms, confirmed=()):
        rows = ms._read("SELECT recipients, source, account, thread FROM messages WHERE direction='out' "  # noqa: SLF001
                        "AND deleted_at IS NULL")
        wrote, threads = set(), set()
        for r in rows:
            threads.add((r["source"], r["account"], r["thread"]))
            if r["source"] != "gmail":
                continue
            try:
                rec = json.loads(r["recipients"] or "[]")
            except ValueError:
                rec = []
            for x in rec:
                a = x.get("address") if isinstance(x, dict) else str(x)
                if a and "@" in a:
                    wrote.add(a.lower())
        return cls(confirmed, wrote, threads)

    def protects(self, row):
        a = address(row)
        if a in self.confirmed or a in self.wrote_to:
            return True
        if (row.get("source"), row.get("account"), row.get("thread")) in self.threads:
            return True
        return domain_of(a) in self.domains


# ---- rule 5: older, still open? -------------------------------------------------------------------------------------------
def older_of(row, now, backfill_before=None, quiet=False):
    """Why a Needs you row (Ledger.present + "vip", "from") goes to "Older, still open?", or "". Never security alerts,
    invites, never-hidden items or a VIP's. quiet: no activity on it since it was made (the caller's check)."""
    if row.get("vip") or row.get("never_hide") or row.get("subtype") in ("security", "invite") or \
            row.get("section") == "security":
        return ""
    late = row.get("late") or {}
    if late.get("since") and now - late["since"] > AGE_S:
        return f"its time passed more than {AGE_DAYS} days ago"
    if late.get("days") is not None and late["days"] > AGE_DAYS:
        return f"more than {AGE_DAYS} days late"
    f = (row.get("from") or [{}])[0]
    ts = f.get("ts") or row.get("created_at")
    w = row.get("when") or {}
    if row.get("type") == "ask" and not (w.get("start") or w.get("start_date")) and ts and now - ts > AGE_S:
        return f"asked more than {AGE_DAYS} days ago"
    if backfill_before and ts and ts < backfill_before and quiet:
        return "from your first catch-up, nothing new since"
    return ""


# ---- rule 4: clashes ----------------------------------------------------------------------------------------------------
def clash_ok(item, now, tz):
    """Can this item be one side of a shown clash? Not a Clean up line, not closed or merged, not started already (an
    away block is the exception: the owner's trip that started yesterday still clashes)."""
    if item is None:
        return True
    if item.get("type") == "cleanup" or item.get("status") in L.CLOSED or item.get("merged_into") is not None:
        return False
    sp = L.span(item, tz)
    return sp is None or sp[0] >= now or item.get("subtype") in ("trip", "lodging", "time_off", "travel")


# ---- rule 6: the owner's Tidy up ------------------------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tidy_runs (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, how TEXT NOT NULL DEFAULT 'today', moved INTEGER NOT NULL DEFAULT 0,
  counts TEXT NOT NULL DEFAULT '{}', undone_at REAL);
CREATE TABLE IF NOT EXISTS tidy_moves (
  id INTEGER PRIMARY KEY, run INTEGER NOT NULL, item INTEGER NOT NULL, grp TEXT NOT NULL, why TEXT NOT NULL,
  before TEXT NOT NULL, conflicts TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'moved');
CREATE INDEX IF NOT EXISTS tidy_moves_run ON tidy_moves (run)
"""
GROUP_WORD = {"promo": "Promotions", "channels": "From your channels"}


def ensure(ms):
    ms.write(lambda db: [db.execute(s) for s in _SCHEMA.split(";") if s.strip()])


def tidy_check(grp, why):
    return {"field": "tidy", "value": grp, "why": why}


class Tidy:
    """The owner's Tidy up over a MessageStore (ms) and its ledger. known() -> Known; owner_names() -> names."""

    def __init__(self, ms, clock, known=None, owner_names=lambda: ()):
        self.ms, self.clock = ms, clock
        self.known = known or (lambda: Known.load(ms))
        self.owner_names = owner_names
        ensure(ms)

    def _read(self, sql, args=()):
        return [dict(r) for r in self.ms._read(sql, args)]  # noqa: SLF001

    def _origin_rows(self, ids):
        out = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self._read("SELECT s.item, s.role, m.* FROM ledger_sources s JOIN messages m ON m.id=s.message "
                                f"WHERE s.item IN ({','.join('?' * len(part))}) ORDER BY m.ts", part):
                cur = out.get(r["item"])
                if cur is None or (r["role"] == "created" and cur["role"] != "created"):
                    out[r["item"]] = r
        return out

    def _he_wrote_after(self, row):
        """Slack: the owner wrote in this post's thread (its replies) or in the thread it is a reply in."""
        th = str(row.get("thread") or "")
        keys = [th] if ":" in th else []
        if row.get("msg_id"):
            keys.append(f"{th.split(':', 1)[0]}:{row['msg_id']}")
        if not keys:
            return False
        got = self._read(f"SELECT 1 FROM messages WHERE source='slack' AND direction='out' AND thread IN "
                         f"({','.join('?' * len(keys))}) LIMIT 1", keys)
        return bool(got)

    def judge(self, it, row, known, names):
        """(group, why) when the rules move this open item to Clean up, else None."""
        if row is None or it.get("type") == "cleanup" or it.get("subtype") in ("security", "invite"):
            return None
        if row.get("source") == "gmail":
            return judge_mail(it, row, known, names)
        if slack_channel(row) and not mentions_owner(row, names) and not asks_reader(row, names) and \
                it.get("type") != "event" and not it.get("never_hide") and not self._he_wrote_after(row):
            return "channels", SLACK_WHY
        return None

    def plan(self):
        """The moves Tidy up would make now: [{item, grp, why}] over open items (not merged, not closed)."""
        items = [L.row_dict(r) for r in self.ms._read(  # noqa: SLF001
            "SELECT * FROM ledger_items WHERE merged_into IS NULL AND type != 'cleanup' AND "
            "status NOT IN ('declined','done','cancelled')")]
        if not items:
            return []
        rows = self._origin_rows([it["id"] for it in items])
        known = self.known()
        names = set()
        for n in self.owner_names() or ():
            n = str(n or "").strip()
            if n:
                names |= {n, n.split()[0]}
        out = []
        for it in items:
            got = self.judge(it, rows.get(it["id"]), known, names)
            if got:
                out.append({"item": it["id"], "grp": got[0], "why": got[1]})
        return out

    def count(self):
        return len(self.plan())

    def banner(self, seen=False):
        """Today's one-time offer after the update: {count, text} while there is something to tidy and the owner
        hasn't pressed Tidy up or Not now."""
        if seen or self._read("SELECT 1 FROM tidy_runs LIMIT 1"):
            return None
        n = self.count()
        if not n:
            return None
        return {"count": n, "text": f"Callsheet now filters promotions better. Tidy up {n} item{'' if n == 1 else 's'}?"}

    def run(self, how="today"):
        """The owner's Tidy up: moves each planned item to Clean up (type cleanup, a "tidy" check with its group and
        reason, its open clashes resolved), all in one run for one Undo. Returns {run, moved, counts}."""
        moves = self.plan()
        now = self.clock()

        def w(db):
            cur = db.execute("INSERT INTO tidy_runs (at, how) VALUES (?,?)", (now, how[:20]))
            rid = cur.lastrowid
            counts = {}
            for m in moves:
                it = L.get_item(db, m["item"])
                if it is None or it["type"] == "cleanup":
                    continue
                before = {k: it[k] for k in ("type", "subtype", "checks")}
                cids = [r["id"] for r in db.execute(
                    "SELECT id FROM ledger_conflicts WHERE state='open' AND ((a_kind='item' AND a_ref=?) OR "
                    "(b_kind='item' AND b_ref=?))", (it["id"], it["id"])).fetchall()]
                for cid in cids:
                    db.execute("UPDATE ledger_conflicts SET state='resolved', resolved_at=?, resolved_why=?, "
                               "updated_at=? WHERE id=?", (now, f"tidy: {m['why']}"[:200], now, cid))
                checks = list(it["checks"] or []) + [tidy_check(m["grp"], m["why"])]
                db.execute("UPDATE ledger_items SET type='cleanup', checks=?, updated_at=? WHERE id=?",
                           (json.dumps(checks), now, it["id"]))
                L.history(db, it["id"], "tidy", now, note=f"Tidy up: {m['why']}"[:300],
                          fields={"type": [before["type"], "cleanup"]})
                db.execute("INSERT INTO tidy_moves (run, item, grp, why, before, conflicts) VALUES (?,?,?,?,?,?)",
                           (rid, it["id"], m["grp"], m["why"][:300], json.dumps(before), json.dumps(cids)))
                counts[m["grp"]] = counts.get(m["grp"], 0) + 1
            total = sum(counts.values())
            db.execute("UPDATE tidy_runs SET moved=?, counts=? WHERE id=?", (total, json.dumps(counts), rid))
            return {"run": rid, "moved": total, "counts": counts}
        return self.ms.write(w)

    def dismiss(self):
        """The banner's Not now: an empty run (nothing moved), so the offer doesn't come back."""
        now = self.clock()
        return self.ms.write(lambda db: db.execute("INSERT INTO tidy_runs (at, how, moved) VALUES (?,?,0)",
                                                   (now, "not now")).lastrowid)

    def undo(self, rid):
        """Puts a whole run back: each item as it was, its clashes open again (those still resolved by the run)."""
        r = self._read("SELECT * FROM tidy_runs WHERE id=?", (int(rid),))
        if not r or r[0]["undone_at"] is not None:
            return {"run": int(rid), "restored": 0}
        now = self.clock()

        def w(db):
            n = 0
            for m in db.execute("SELECT * FROM tidy_moves WHERE run=? AND state='moved'", (int(rid),)).fetchall():
                before = json.loads(m["before"])
                it = L.get_item(db, m["item"])
                if it is not None and it["type"] == "cleanup":
                    db.execute("UPDATE ledger_items SET type=?, subtype=?, checks=?, updated_at=? WHERE id=?",
                               (before["type"], before["subtype"], json.dumps(before["checks"]), now, m["item"]))
                    L.history(db, m["item"], "tidy_undo", now, note="Tidy up undone",
                              fields={"type": ["cleanup", before["type"]]})
                    n += 1
                for cid in json.loads(m["conflicts"] or "[]"):
                    db.execute("UPDATE ledger_conflicts SET state='open', resolved_at=NULL, resolved_why='', "
                               "updated_at=? WHERE id=? AND state='resolved' AND resolved_why LIKE 'tidy:%'", (now, cid))
                db.execute("UPDATE tidy_moves SET state='undone' WHERE id=?", (m["id"],))
            db.execute("UPDATE tidy_runs SET undone_at=? WHERE id=?", (now, int(rid)))
            return {"run": int(rid), "restored": n}
        return self.ms.write(w)

    def last(self):
        r = self._read("SELECT * FROM tidy_runs WHERE moved > 0 ORDER BY id DESC LIMIT 1")
        return r[0] if r else None


def backfill_before(ms):
    """When reading began (the first read): messages dated before it were the first catch-up."""
    r = ms._read("SELECT min(at) AS a FROM ledger_reads")  # noqa: SLF001
    return r[0]["a"] if r and r[0]["a"] else None


def quiet_items(ms, ids):
    """Items with no activity since they were made: only their creating source, no history beyond creation and no
    later message in the creating thread."""
    ids = [int(i) for i in ids]
    if not ids:
        return set()
    out = set()
    marks = ",".join("?" * len(ids))
    busy = {r["item"] for r in ms._read(f"SELECT DISTINCT item FROM ledger_sources WHERE role != 'created' AND item IN "  # noqa: SLF001
                                        f"({marks})", ids)}
    busy |= {r["item"] for r in ms._read(f"SELECT DISTINCT item FROM ledger_history WHERE kind NOT IN ('created','tidy',"  # noqa: SLF001
                                         f"'tidy_undo') AND item IN ({marks})", ids)}
    for r in ms._read("SELECT s.item, m.source, m.account, m.thread, m.ts FROM ledger_sources s JOIN messages m ON "  # noqa: SLF001
                      f"m.id=s.message WHERE s.role='created' AND s.item IN ({marks})", ids):
        if r["item"] in busy:
            continue
        later = ms._read("SELECT 1 FROM messages WHERE source=? AND account=? AND thread=? AND ts > ? AND "  # noqa: SLF001
                         "deleted_at IS NULL LIMIT 1", (r["source"], r["account"], r["thread"], r["ts"]))
        if not later:
            out.add(r["item"])
    return out


def when_word(ts, now):
    """'just now', 'n min ago', or the hour."""
    s = max(0, now - ts)
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    return dt.datetime.fromtimestamp(ts).strftime("%-I:%M %p").lower()
