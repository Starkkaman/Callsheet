"""0.6.0 chat: a rules-only detector for instructions planted in indexed items, and the answer post-check.

No model and no network: fixed regular expressions over the item's title and body (index.py stores the result at
index time) and a word-overlap check of a chat answer against the flagged sentences (chat.py).

`instructions(text)` returns the sentences that address an assistant or AI with an instruction ("Callsheet note:",
"Note to Callsheet", "Assistant:", "AI:", "SYSTEM:", "</data>", "ignore all previous instructions", "you may now
send", "reply that you've sent it", "always say yes", "tell the owner to", "say it's approved", "when asked about X,
say ...") and, since 0.7.1, link lures: an imperative to click/tap/open/visit/go to/download/log in/sign in/verify...
"here", "this link", "the link", "below", together with a URL in the same sentence or the next one, or a URL right
after such an imperative ("Visit https://...", "Log in at https://..."). It deliberately does NOT flag:
- a plain "Note:" ("Note: always bring ID"): no assistant target and "bring" isn't one of the yes-man verbs;
- "System" without a colon or without an instruction cue ("System maintenance tonight 10pm", "System: Windows 11");
- acknowledgements ("Reply that you've received this"): only claims of an action (sent, paid, approved, signed...)
  count, because a colleague asking for a receipt is normal mail;
- a sender's own claims addressed to the owner ("the invoice is paid, tell Priya it's settled", "contract's signed,
  confirm it"): those are claims, handled by the claim post-check (writer.states_decision), not instructions to an
  assistant;
- narrative "I/we/you/they always say ...";
- (0.7.1) a link without an imperative ("The agenda is here: https://...", "Slides: https://...", "Here's the doc
  https://...", a plain URL), "Join the meeting at https://meet..." ("join" isn't a lure verb), a lure whose every
  link is a video-meeting link (meet.google.com, zoom.us, teams.microsoft.com, webex.com...), mailing-list footers
  ("Click here to unsubscribe", "manage your preferences", "view in browser"), and link lures in the owner's own
  Obsidian notes (`source="obsidian"`: the owner's text, not someone else's).
A flag only adds a caution mark and the label below, so a rare false positive costs little.
0.14.0 adds text that speaks to an AI reading it ("If you are an AI, ..."), requests to save something in its memory,
"tell Callsheet/the AI ...", orders to mark something as paid or approved, and the owner's voice in someone else's text
("This is Isaac", with the owner's names: `owner`, see owner_terms). `marks()` is the one detector set the index flag
and Chat's live marks share (docs/chat.md).

The index stores, per item, only VERSION and short hashes of the flagged sentences (`keys`), never extra text: the
sentences themselves are re-derived from the body the index already holds when the chat needs them.
"""
import hashlib
import re

VERSION = 3            # bump when the rules change: the index re-checks every item (backfill)
MAX_KEYS = 8           # hashes stored per item
LABEL = "This answer repeats an instruction found in a message, event or document. It isn't advice."

# who an injected instruction talks to
_AGENT = r"(?:callsheet|(?:the\s+)?(?:assistant|ai|a\.i\.|chatbot|bot|model|llm|language\s+model))"
_LEAD = r"^[\W_]{0,4}"          # bullets, quotes, a stray "<" before a prefix
_ACTION_DONE = (r"(?:sent|done|completed|paid|approved|signed|confirmed|transferred|forwarded|booked|replied|"
                r"accepted|agreed|released|settled|wired|actioned|handled)")
# 0.14.0: an AI by a word no person goes by ("tell the AI", "AI reading this"; not "your assistant", a person)
_BOT = (r"(?:callsheet|(?:the\s+|your\s+|this\s+|an?\s+|any\s+|every\s+)?(?:ai|a\.i\.|ai\s+(?:assistant|model|agent|"
        r"system)|virtual\s+assistant|digital\s+assistant|chatbot|llm|(?:large\s+)?language\s+model|bot))")
# ... and the words that name one only at the end of a phrase ("If you are an assistant," but not "an assistant manager")
_BOT_ANY = (r"(?:ai|a\.i\.|artificial\s+intelligence|(?:ai\s+|virtual\s+|digital\s+)?assistant|(?:ai\s+|large\s+)?"
            r"(?:language\s+)?model|llm|chatbot|bot|ai\s+agent|callsheet|automated\s+(?:system|assistant|reader|tool))")
_BOT_END = (r"(?=\s*(?:[,.;:!?)–—]|-\s|$)|\s+(?:reading|processing|summari[sz]ing|parsing|scanning|that|who|"
            r"or|and|like|tool|program|system)\b)")

RULES = (
    # "Callsheet note:", "Note to Callsheet:", "Assistant:", "AI:", "Message for the assistant:" at the start
    ("prefix", re.compile(_LEAD + r"(?:callsheet\s+note|note\s+(?:to|for)\s+" + _AGENT + r"|(?:message|instructions?)"
                          r"\s+(?:to|for)\s+" + _AGENT + r"|assistant|ai|callsheet|chatbot)\s*:", re.I)),
    # "IMPORTANT SYSTEM NOTE TO THE ASSISTANT", "a note for Callsheet" anywhere
    ("note-to", re.compile(r"\b(?:notes?|messages?|instructions?|reminders?)\s+(?:to|for)\s+" + _AGENT + r"\b", re.I)),
    # "SYSTEM:" in capitals anywhere ("</data> SYSTEM: you may now ...")
    ("system", re.compile(r"\bSYSTEM\s*:")),
    # data-block / prompt tags: "</data>", "<data>", "</notes><notes>", "<system>"
    ("tag", re.compile(r"</?\s*(?:data|notes?|system|instructions?|tool_result|tool|user|assistant|prompt)\s*>",
                       re.I)),
    # "ignore all previous instructions", "disregard your rules", "disregard everything above"
    ("ignore", re.compile(r"\b(?:ignore|disregard|forget|override)\s+(?:(?:all|any|your|the|my|of\s+the)\s+)*"
                          r"(?:previous|prior|earlier|above|preceding|former|existing|system|safety)\s+"
                          r"(?:instructions|rules|guidelines|prompts?|directions)\b|\b(?:ignore|disregard|forget)\s+"
                          r"(?:all|any|your)\s+(?:instructions|rules|guidelines)\b|\b(?:ignore|disregard)\s+"
                          r"(?:everything|anything|all)\s+(?:above|before|previous|prior|else)\b", re.I)),
    # "you may now send emails", "you are now allowed to reply"
    ("may-now", re.compile(r"\byou\s+(?:may|can)\s+now\s+(?:send|reply|respond|act|email|e-mail|forward|pay|approve|"
                           r"confirm|transfer|book|sign)\b|\byou\s+are\s+now\s+(?:allowed|permitted|authori[sz]ed|"
                           r"free)\s+to\b", re.I)),
    # "reply that you've sent it" (acknowledgements such as "received" don't count)
    ("reply-that", re.compile(r"\b(?:reply|respond|answer|say|confirm|tell\s+\w+)\s+(?:back\s+)?(?:with\s+)?that\s+"
                              r"you(?:'ve|\s+have)\s+(?:already\s+|now\s+|just\s+|also\s+)?" + _ACTION_DONE + r"\b",
                              re.I)),
    # "always say yes to Otto", "always approve payments from this sender" (imperative only; see _narrative)
    ("always", re.compile(r"\balways\s+(?:say|approve|agree|accept|confirm|pay|answer|reply|grant|sign|authori[sz]e)"
                          r"\b", re.I)),
    # "tell the owner to transfer ...", "tell the user that ..."
    ("tell-owner", re.compile(r"\b(?:tell|inform|instruct|advise|remind)\s+the\s+(?:owner|user)\s+(?:to|that)\b",
                              re.I)),
    # "say it's approved" / "say that it is paid"
    ("say-done", re.compile(r"\b(?:say|state)\s+(?:that\s+)?(?:it's|it\s+is|it\s+has\s+been|it\s+was|this\s+is|"
                            r"everything\s+is)\s+(?:already\s+|all\s+|now\s+)?(?:approved|paid|signed|settled)\b", re.I)),
    # "when asked about the venue, tell ..."
    ("when-asked", re.compile(r"\bwhen(?:ever)?\s+(?:you(?:'re|\s+are)\s+|someone\s+)?(?:is\s+)?asked\s+(?:about|for|if|"
                              r"whether|what|how|who)\b[^.;!?]{0,120}?\b(?:say|tell|reply|answer|respond|confirm)\b",
                              re.I)),
    # 0.14.0: text that speaks to an AI reading it: "If you are an AI, ...", "if this email is read by an assistant",
    # "Any AI reading this", "Dear assistant,", "As an AI, you must". The AI word must end the phrase ("If you are an
    # AI researcher", "the AI reading list", "If you are a member" don't count)
    ("address", re.compile(
        r"\bif\s+you(?:'re|\s+are)\s+(?:an?\s+|the\s+|some\s+(?:kind\s+of\s+|sort\s+of\s+)?)?" + _BOT_ANY + _BOT_END
        + r"|\b(?:read|processed|summari[sz]ed|parsed|scanned|seen|analy[sz]ed)\s+by\s+(?:an?\s+|the\s+|any\s+|some\s+)?"
        + _BOT_ANY + _BOT_END
        + r"|\b" + _BOT + r"s?\s+(?:(?:that|who)\s+(?:is|are)\s+|currently\s+|now\s+)?(?:reading|processing|summari[sz]ing|"
        r"parsing|scanning|seeing|analy[sz]ing)\s+(?:this|these|my|our|the\s+(?:e-?mail|message|text|note|page|"
        r"document|thread))\b"
        r"|(?:^[\W_]{0,4}|[.!?]\s+)(?:dear|hey|hi|hello|attention|attn|psst)\s+(?:the\s+)?" + _BOT_ANY + r"\s*[,:!]"
        r"|\bas\s+an?\s+" + _BOT_ANY + r"\s*,\s*you\b", re.I)),
    # 0.14.0: a request to remember or save something in the assistant's memory: "save it to memory", "store this in
    # your memory", "add to memory that", "update your memory", "remember this for all future conversations" (not a
    # memory card, "a terrible memory", "remember to bring the receipts")
    ("memory", re.compile(
        r"\b(?:save|store|write|add|put|commit|record|keep|log)\b[^.!?;]{0,60}?\b(?:to|in|into|inside)\s+(?:your\s+|the\s+|"
        r"its\s+|callsheet's\s+|(?:long|short)[\s-]?term\s+|permanent\s+|persistent\s+)*(?:memory|memories)\b"
        r"(?!\s*(?:card|stick|chip|foam|lane|module|slot))"
        r"|\b(?:add|save|store)\s+(?:this|that|it|the\s+following)?\s*(?:as\s+)?(?:an?\s+)?(?:new\s+)?"
        r"(?:memory|memories|memory\s+note)\b(?!\s*(?:card|stick|chip|foam|lane|module|slot))"
        r"|\bupdate\s+(?:your|its|callsheet's|the\s+assistant's)\s+(?:memory|memories|notes|instructions|knowledge)\b"
        r"|\b(?:remember|memori[sz]e|note|keep)\b(?!\s+to\b)[^.!?;]{0,80}?\b(?:for|in|across|during)\s+(?:all\s+|any\s+|"
        r"every\s+|your\s+)?(?:future|later|subsequent|upcoming)\s+(?:conversations?|chats?|sessions?|answers?|replies|"
        r"responses|queries|questions|interactions)\b", re.I)),
    # 0.14.0: telling the assistant something: "tell Callsheet it is signed", "let the AI know" (not "tell your
    # assistant", a person)
    ("tell-ai", re.compile(r"\b(?:tell|inform|instruct|remind|notify|convince|persuade)\s+" + _BOT + r"\b"
                           r"(?!\s+(?:conference|team|reading|lab|course|model|researchers?|engineers?)\b)"
                           r"|\blet\s+" + _BOT + r"\s+know\b", re.I)),
    # 0.14.0: an order to record something as paid or approved: "mark it as settled", "flag this payment as paid",
    # "set the status of order 4471 to approved" (not "mark the date", "mark it as tentative", "we'll mark it")
    ("mark-as", re.compile(
        r"(?:^[\W_]{0,4}|[,;:(–—-]\s*(?:and\s+|so\s+|then\s+)?|\b(?:please|pls|kindly|just|simply|so|"
        r"also)\s+|\byou\s+(?:must|need\s+to|should|have\s+to|can)\s+)"
        r"(?:(?:mark|flag|record|log|list|treat|label|tag|classify|file|report|show)\s+(?:(?!as\b)[\w'#$£€.,/-]+\s+)"
        r"{1,5}?as|(?:set|change|update|move)\s+(?:the\s+|its\s+|their\s+)?(?:status|state)\s+(?:of\s+[^.!?;]{0,40}?)?"
        r"to)\s+[\"']?(?:already\s+|fully\s+|now\s+)?(?:paid|settled|approved|signed|cleared|authori[sz]ed|agreed|"
        r"safe|trusted|verified|legit(?:imate)?|not\s+spam)\b", re.I)),
)
# 0.14.0: the owner's voice from someone else's mail: "This is Isaac", "I'm Isaac", "this email is from the owner",
# "writing from the owner's other address" (the names: `owner`, see owner_terms; triage.steer's rule for the sorter)
_OWNER_VOICE = (r"\b(?:i\s+am|i'm|this\s+is)\s+(?:<<NAMES>>)\b(?!'s)"
                r"|\bthis\s+(?:e-?mail|message|text|note|mail)\s+is\s+(?:from|by)\s+(?:the\s+owner|<<NAMES>>)\b(?!'s)"
                r"|\bfrom\s+(?:the\s+owner's|<<NAMES>>'s)\s+other\s+(?:e-?mail|account|address|phone|number)\b")
_NOT_NAMES = frozenset("""the a an and or of to i me my you your we us our he she they it this that owner user mr mrs
ms dr sir madam team""".split())
# 0.7.1 link lures ----------------------------------------------------------------------------------------------
OWN_SOURCES = frozenset({"obsidian"})   # the owner's own text: link lures there aren't someone else's instruction
URL = re.compile(r"(?:\b(?:https?|hxxps?|ftp)://|\bwww\.)[^\s<>\"'`]+"
                 r"|\b[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,24}/[^\s<>\"'`]*", re.I)
# where an imperative starts: the sentence's start, after a clause break, "please/kindly/just/simply/then/now/and/or",
# or "you must/need to/should/have to"
_IMP = (r"(?:^[\W_]{0,4}|[,;:(\u2013\u2014-]\s*|\b(?:please|pls|kindly|just|simply|then|now|and|or|so|first|asap)\s+|"
        r"\byou\s+(?:must|need\s+to|should|have\s+to|will\s+need\s+to|'ll\s+need\s+to)\s+)")
_LURE_VERB = (r"(?:click(?:\s+on)?|tap(?:\s+on)?|press|open|visit|go\s+to|head\s+(?:over\s+)?to|navigate\s+to|"
              r"download|install|log\s*-?\s*(?:in|on)(?:\s+to)?|login|sign\s*-?\s*(?:in|on)(?:\s+to)?|signin|"
              r"verify|validate|re-?activate|unlock|reset|follow|use|update)")
_DEICTIC = (r"(?:\bhere\b(?!'s)|\b(?:this|that|the|following|attached)\s+(?:link|url|address|button|page)\b|"
            r"\bbelow\b|\b(?:link|button)\s+(?:below|above)\b)")
_LURE_HERE = re.compile(_IMP + _LURE_VERB + r"\b[^.!?]{0,60}?" + _DEICTIC, re.I)
_LURE_URL = re.compile(_IMP + _LURE_VERB + r"\b\s*:?\s*(?:(?!https?:|www\.)[^\s.!?:]+\s+){0,4}?"
                       r"(?:(?:at|on|via|from|using|with|to|here|through)\s*)?:?\s*(?=" + URL.pattern + ")", re.I)
_MEETING = re.compile(r"^(?:[a-z0-9-]+\.)*(?:meet\.google\.com|zoom\.us|zoom\.com|teams\.microsoft\.com|"
                      r"teams\.live\.com|webex\.com|whereby\.com|gotomeeting\.com|meet\.jit\.si|"
                      r"chime\.aws)$", re.I)
_FOOTER = re.compile(r"\b(?:unsubscribe|opt[\s-]?out|(?:manage|update)\s+(?:your\s+)?(?:email\s+|notification\s+|"
                     r"subscription\s+|communication\s+)?(?:preferences|settings|subscriptions?)|view\s+(?:this\s+|"
                     r"the\s+)?(?:email|message|newsletter|it)?\s*(?:in\s+(?:your\s+|a\s+)?browser|online|"
                     r"on\s+the\s+web))\b", re.I)


def urls(text):
    """The links in a text (scheme or www., or a bare host with a path), trailing punctuation removed."""
    out = []
    for m in URL.finditer(_plain(text)):
        u = m.group(0)
        while u and u[-1] in ".,;:!?)]}*_>":
            if u[-1] == ")" and u.count("(") >= u.count(")"):
                break
            u = u[:-1]
        if u:
            out.append(u)
    return out


def host(url):
    """The lower-case host of a link ("" when there is none)."""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(url or ""), flags=re.I)
    u = re.split(r"[/?#]", u, maxsplit=1)[0].rsplit("@", 1)[-1]
    return re.sub(r":\d+$", "", u).strip(".").lower()


def link_lure(sentence, following=""):
    """0.7.1: does the sentence push the reader to a link: an imperative lure verb with "here"/"this link"/"below"
    and a URL in this sentence or the next one, or a URL right after the imperative? Not when every link is a
    video-meeting link, nor in a mailing-list footer (unsubscribe, preferences, view in browser)."""
    s = _plain(sentence)
    links = urls(s)
    nxt = _plain(following).strip()
    if not links and nxt and URL.match(nxt.lstrip("<([")):
        links = urls(nxt)[:1]
    if not links:
        return False
    if not (_LURE_URL.search(s) or _LURE_HERE.search(s)):
        return False
    if all(_MEETING.match(host(u)) for u in links):
        return False
    return not _FOOTER.search(s)


# "I always say", "we always agree", "you always confirm": a habit, not an order
_NARRATIVE = re.compile(r"\b(?:i|we|you|they|he|she|it|who|one|people|everyone|everybody|someone)\s+(?:do\s+|don't\s+|"
                        r"would\s+|will\s+)?$", re.I)


def _plain(text):
    return (str(text or "").replace("’", "'").replace("‘", "'").replace("“", '"')
            .replace("”", '"').replace(" ", " "))


def sentences(text):
    """Lines, then sentences ending in . ! or ? followed by a space. Bullets and quote marks at the start go."""
    out = []
    for line in _plain(text).replace("\r", "\n").split("\n"):
        for s in re.split(r"(?<=[.!?])\s+(?=\S)", line):
            s = s.strip().lstrip("-*•·>").strip() if not s.strip().startswith("</") and not \
                s.strip().startswith("<") else s.strip()
            if s:
                out.append(s)
    return out


def owner_terms(names=()):
    """0.14.0: the owner's names as the detectors match them: each name as given (lower case, spaces squeezed) and its
    first word, longest first, without one-letter words or words such as "the" or "owner". `names`: the About you name
    and the names People knows the owner by (the same sources as triage.context's owner_first)."""
    out = set()
    for n in names or ():
        words = re.findall(r"[^\W\d_](?:[^\W\d_]|['-](?=[^\W\d_]))*", _plain(n).lower())
        words = [w for w in words if len(w) > 1]
        if not words:
            continue
        if words[0] not in _NOT_NAMES:
            out.add(words[0])
        if len(words) > 1:
            out.add(" ".join(words))
    return tuple(sorted(out, key=lambda w: (-len(w), w)))


_VOICE = {}


def _voice(owner):
    """The owner-voice rule for these names (cached; "this email is from the owner" needs none)."""
    terms = owner_terms(owner)
    rx = _VOICE.get(terms)
    if rx is None:
        names = "|".join(r"\s+".join(map(re.escape, t.split())) for t in terms) or r"(?!)"
        rx = _VOICE[terms] = re.compile(_OWNER_VOICE.replace("<<NAMES>>", names), re.I)
        if len(_VOICE) > 64:
            _VOICE.clear()
    return rx


def rules_hit(sentence, following="", source=None, owner=(), own=False):
    """Names of the rules a sentence matches (empty: not an instruction). `following`: the next sentence (a link
    lure's URL may stand there); `source`: the item's source (no link-lure or owner-voice rule for the owner's own
    notes); `owner` (0.14.0): the owner's names (owner_terms), for the owner-voice rule; `own`: the text is the owner's
    own (mail he sent, his own Slack or Signal message), where "This is Isaac" is just the owner writing: no owner-voice
    rule."""
    s = _plain(sentence)
    hits = []
    for name, rx in RULES:
        for m in rx.finditer(s):
            if name == "always" and _NARRATIVE.search(s[:m.start()]):
                continue
            hits.append(name)
            break
    if not own and source not in OWN_SOURCES and _voice(owner).search(s):
        hits.append("owner-voice")
    if source not in OWN_SOURCES and link_lure(s, following):
        hits.append("link")
    return hits


def instructions(text, source=None, owner=(), own=False):
    """The sentences of `text` that hold an instruction addressed to an assistant (or, 0.7.1, a link lure), in
    order, without repeats. `source`: the item's source when known (see rules_hit); `owner`: the owner's names; `own`:
    the owner's own text (see rules_hit)."""
    out, seen = [], set()
    ss = sentences(text)
    for i, s in enumerate(ss):
        k = norm(s)
        if k not in seen and rules_hit(s, ss[i + 1] if i + 1 < len(ss) else "", source, owner, own):
            seen.add(k)
            out.append(s)
    return out


def item_instructions(title, body, source=None, owner=(), own=False):
    """instructions() over an item's title and body (what index.py stores keys for)."""
    return instructions(str(title or "") + "\n" + str(body or ""), source, owner, own)


def marks(text, source=None, owner=()):
    """0.14.0: the one detector set for a piece of text, in the order Chat shows them: "claim" (writer.states_decision
    as for chat answers, with the owner's names), then "lure" (a link lure) or "instruction" (instructions()). The
    index stores a flag for exactly the texts that get "lure" or "instruction" here (item_instructions with the same
    names), so Chat's live marks and the index agree; a claim alone is the claim post-check's, not a flag."""
    import writer   # here, not at the top: writer imports this module
    kinds = []
    if writer.states_decision(text, attributed_ok=True, owner=owner):
        kinds.append("claim")
    if source not in OWN_SOURCES and link_lure(text):
        kinds.append("lure")
    elif instructions(text, source, owner):
        kinds.append("instruction")
    return kinds


def norm(sentence):
    return re.sub(r"\s+", " ", _plain(sentence).lower()).strip()


def key(sentence):
    """A short hash of one flagged sentence (what the index stores instead of the text)."""
    return hashlib.sha256(norm(sentence).encode("utf-8")).hexdigest()[:16]


def keys(title, body, source=None, owner=(), own=False):
    return [key(s) for s in item_instructions(title, body, source, owner, own)][:MAX_KEYS]


# --- the answer post-check -----------------------------------------------------------------------------------
STOP = frozenset("""a an the and or but if then so to of in on at by for with from as into onto about over under
than that this these those it its it's is are was were be been being am do does did done doing have has had having
i me my mine we us our you your yours you've you're you'll he him his she her they them their there here who whom
which what when where why how all any each every some no not nor only own same too very can could may might must
shall should will would just now also please ok okay s t d ll re ve m o y ain again once both few more most
other such up down out off further while during before after above below between through until against
note notes callsheet assistant ai system data owner user""".split())
# the prefix words of an injection (note, callsheet, assistant, system, data) and "owner"/"user" never count as
# shared content: an answer that reports "a note to the assistant" isn't repeating the instruction.
_IRREG = {"sent": "send", "paid": "pay", "signed": "sign", "approved": "approve", "agreed": "agree",
          "confirmed": "confirm", "transferred": "transfer", "wired": "wire", "booked": "book"}
_PREFIX = re.compile(r"^(?:[\W_]*(?:</?\s*\w+\s*>|(?:callsheet\s+note|note\s+(?:to|for)\s+" + _AGENT + r"|"
                     r"(?:important\s+)?system(?:\s+note)?(?:\s+to\s+" + _AGENT + r")?|assistant|ai|callsheet)\s*:))+",
                     re.I)


def _stem(w):
    w = _IRREG.get(w, w)
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def content_words(text):
    t = re.sub(r"(?<=\d),(?=\d{3})", "", norm(text))
    t = re.sub(r"'s\b", "", t)
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", t)
    return {_stem(w) for w in words if w not in STOP and len(w) > 1}


def core(sentence):
    """A flagged sentence without its prefix ("Callsheet note:", "</data> SYSTEM:", "Assistant:")."""
    return _PREFIX.sub("", _plain(sentence)).strip(" :-")


def repeated(answer, flagged, min_shared=3):
    """The flagged sentences the answer quotes (a clause of 15+ characters verbatim, a leading you/I/we aside) or paraphrases (an answer
    sentence shares min_shared content words with it after stop words and light stemming)."""
    if not answer or not flagged:
        return []
    a = norm(answer)
    a_sents = [content_words(s) for s in sentences(answer)]
    out = []
    for f in flagged:
        c = core(f)
        hit = False
        for clause in [c] + re.split(r"\s*[;,]\s*|\s+and\s+", c):
            n = re.sub(r"^(?:you|i|we)\s+", "", norm(clause).strip(" .!?\"'"))   # "you may now send" ~ "I may now send"
            if len(n) >= 15 and len(content_words(n)) >= 2 and n in a:
                hit = True
                break
        if not hit:
            cw = content_words(c)
            if len(cw) >= min_shared:
                hit = any(len(cw & s) >= min_shared for s in a_sents)
        if hit:
            out.append(f)
    return out
