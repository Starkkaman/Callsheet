# Date and amount rules (readrules.py)

The deterministic reading rules the ledger's read uses (`callsheet/app/readrules.py`, called from `extract.py`; see
`docs/ledger.md`). They were the 0.12.2 rules of the reader (`reader.py`, retired in the rework), kept unchanged when the
reader went: what follows is that reader's text for them, so "the item" is the ledger item and "the cited sentence"
the sentence the item came from.

    either of the next two Fridays;
  - a weekday written with a date ("Tue 10 Nov") must be that date's weekday; when they disagree, nothing is shown;
  - "EOW"/"end of (the) week": that week's Friday; "EOM"/"end of (the) month": the month's last day; "the 15th": the
    next 15th; "30/11": by the owner's date order (`date_order`, "dmy" or "mdy", meta `reader`, no control on the page
    yet), or when only one reading is a date; "03/04" with no date order shows nothing; a numeric date counts only
    after "on", "by", "due" and the like. "In two weeks" and other spans show nothing;
  - a time with a zone next to the date ("07:00 CET", "4pm PT", "23:59 AoE") moves the date into the owner's zone, so
    an invite across midnight shows the owner's date;
  - cancelled, called off, off, won't happen, void: the clause's dates are never due; moved, rescheduled, postponed,
    "now": the dates before the move word and after "was", "instead of" (and "from" when something moved) are old, the
    new date is shown only when a sentence says it. An old or cancelled date is never due anywhere in the item
    (`date_cancelled`);
- the amount is copied by Callsheet from the cited sentence, as written (`pick_amount`), only when that sentence holds
  exactly one amount (currency symbols or codes on either side, any separators, "5k" only in a sentence about money).
  Nothing is shown for: two amounts in one sentence or two different totals or a deposit next to a total
  (`amount_ambiguous`); a range, never one end of it ("$1–2k", "5–7k EUR", "between 500 and 800",
  `amount_range`); a line item or subtotal ("subtotal", "line", "item 2", "x2", "each", "per", VAT, quantity,
  `amount_line_item`); any other sentence when the message states a total (`amount_not_total`); savings and discounts
  (`amount_not_due`);
- signatures (`strip_signature`, 0.12.2): besides "-- ", a closing word (Thanks, Best, Regards, Cheers...) followed by
  a name and a few short lines, a last paragraph of short lines with a phone, email, address or company line, and a
  forwarded header are cut from the new message before anything is read, so no amount, date or name comes from them.

