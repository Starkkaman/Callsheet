"""0.24.2: the two leftover promotion senders (a pet-supplies retailer and a software company's newsletter).

Why they slipped through: the retailer mails as brand@<sub>.brand.com (no marketing subdomain word, no mailing
service) and its address had become a confirmed People row; the newsletter mails from reply@, webinars@ and
community@ on a domain the owner once wrote to (its support desk), which made the whole domain "his", and its words
(webinars, product update, customer stories, "how is your pup liking …?") were no cue. General rules, no names: a
brand-named address on the brand's own subdomain is a list sender; list-style local parts (reply@, webinars@,
community@ …) are lists whoever they are filed under and whatever domain he writes to; more retail, software-newsletter
and review-request cues. Real mail from the same senders stays: order shipped, the pharmacy desk, a charge with a
date, an invoice, a support reply, a colleague. Fictional senders and mail only."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))

import tidy  # noqa: E402

SHOP = "kibblecart@paws.kibblecart.example"
SAAS = "signloop.example"


def mail(sender, subject, text="Hi there.", labels=(), bulk=0):
    return {"source": "gmail", "direction": "in", "sender": sender, "subject": subject, "text": text,
            "labels": json.dumps(list(labels)), "bulk": bulk, "account": "a", "thread": "t-" + subject}


def ask(what, sub=""):
    return {"type": "ask", "subtype": sub, "what": what}


def known():
    """His circle as in his data: the shop's and the newsletter's addresses became confirmed People, and he once
    wrote to both companies' help desks (and to a colleague)."""
    return tidy.Known(confirmed={SHOP, "community@" + SAAS, "webinars@" + SAAS, "dana@work.example"},
                      wrote_to={"help@kibblecart.example", "support@" + SAAS, "dana@work.example"})


class TestShop(unittest.TestCase):
    def judge(self, it, row):
        r = tidy.judge_mail(it, row, known())
        return r[0] if r else None

    def test_marketing_is_a_promotion(self):
        self.assertTrue(tidy.brand_sender(SHOP))
        self.assertIn("brand list sender", tidy.list_signals(mail(SHOP, "x")))
        self.assertFalse(known().protects(mail(SHOP, "x")))
        for subj, it in (("Autoship & Save 35% on food and litter", ask("start Autoship")),
                         ("New deals on treats and toys this week", ask("shop the deals")),
                         ("Stock up on cat litter before winter", ask("stock up on litter")),
                         ("How is your pup liking the Dental Chews?", ask("review the dental chews")),
                         ("Rate your order #4412: write a quick review", ask("rate the order"))):
            self.assertEqual(self.judge(it, mail(SHOP, subj)), "promo", subj)

    def test_real_mail_from_the_same_shop_stays(self):
        shipped = mail(SHOP, "Your KibbleCart order #4412 has shipped", "Tracking number 1Z999. Arriving Thursday.")
        self.assertIsNone(self.judge({"type": "event", "subtype": "delivery", "what": "order arrives"}, shipped))
        charge = mail("orders@kibblecart.example", "Your Autoship order will be charged $54.20 on Nov 20",
                      "We'll charge $54.20 on Nov 20 for your Autoship.")
        self.assertIsNone(self.judge({"type": "event", "subtype": "payment", "what": "Autoship charge $54.20",
                                      "amount": "$54.20"}, charge))
        charge2 = mail(SHOP, "Your Autoship order will be charged $54.20 on Nov 20",
                       "We'll charge $54.20 on Nov 20 for your Autoship.")
        self.assertIsNone(self.judge({"type": "event", "subtype": "payment", "what": "Autoship charge $54.20",
                                      "amount": "$54.20"}, charge2))
        rx = mail("rx@kibblecart.example", "Action needed: your vet's approval for Rx #5531")
        self.assertIsNone(self.judge(ask("call the vet about the prescription"), rx))
        help_reply = mail("help@kibblecart.example", "Re: refund for the damaged bag",
                          "Could you send a photo of the bag?")
        self.assertIsNone(self.judge(ask("send a photo of the bag"), help_reply))

    def test_brand_sender_shape(self):
        for a in ("kibblecart@paws.kibblecart.example", "kibble-cart@e.kibblecart.example",
                  "brightway@mail.brightway.co.uk"):
            self.assertTrue(tidy.brand_sender(a), a)
        for a in ("kibblecart@kibblecart.example", "orders@paws.kibblecart.example", "dana@work.example",
                  "kc@paws.kc.example", "brightway@brightway.co.uk", "nobody"):
            self.assertFalse(tidy.brand_sender(a), a)


class TestNewsletter(unittest.TestCase):
    def judge(self, it, row):
        r = tidy.judge_mail(it, row, known())
        return r[0] if r else None

    def test_newsletter_mail_is_a_promotion_though_he_wrote_to_their_desk(self):
        k = known()
        self.assertFalse(k.protects(mail("reply@" + SAAS, "x")))
        self.assertFalse(k.protects(mail("community@" + SAAS, "x")))       # a confirmed People row too
        for sender, subj, it in (
                ("reply@" + SAAS, "SignLoop product update: smarter playlists", ask("try smarter playlists")),
                ("webinars@" + SAAS, "Webinars this month: digital signage for schools",
                 {"type": "event", "subtype": "meeting", "what": "SignLoop webinar"}),
                ("community@" + SAAS, "Join us live: customer stories from three school districts",
                 {"type": "event", "subtype": "", "what": "SignLoop community session"}),
                ("reply@" + SAAS, "Introducing SignLoop Studio", ask("try SignLoop Studio")),
                ("webinars@" + SAAS, "Save your seat: live demo Thursday",
                 {"type": "event", "subtype": "meeting", "what": "SignLoop live demo"})):
            self.assertEqual(self.judge(it, mail(sender, subj)), "promo", subj)

    def test_real_mail_from_the_same_company_stays(self):
        inv = mail("billing@" + SAAS, "Your SignLoop invoice INV-2041", "Amount due $120.00 by Dec 1.")
        self.assertIsNone(self.judge({"type": "event", "subtype": "bill", "what": "SignLoop invoice $120.00",
                                      "amount": "$120.00"}, inv))
        ticket = mail("support@" + SAAS, "Re: ticket #889 screen sync", "Can you send us the player log?")
        self.assertIsNone(self.judge(ask("send the player log"), ticket))
        ana = mail("ana@" + SAAS, "Introducing our new account manager", "Hi Isaac, can we meet Tuesday?")
        self.assertTrue(known().protects(ana))                               # a person there: his domain
        self.assertIsNone(self.judge(ask("meet the account manager"), ana))

    def test_colleagues_and_code_hosts_keep_their_words(self):
        colleague = mail("dana@work.example", "Introducing the new feature for the lobby screens")
        self.assertIsNone(self.judge(ask("review the lobby feature"), colleague))
        stranger = mail("sam@studio.example", "New feature idea for the signage", "Could you look at this?")
        self.assertIsNone(self.judge(ask("look at the idea"), stranger))
        code = mail("notifications@codehost.example", "[signage] Add new feature flag for playlists (#212)",
                    "Review requested.")
        self.assertIsNone(self.judge(ask("review pull request 212"), code))   # no-reply alone: a notice
        team = mail("team@work.example", "Introducing our new IT lead")      # a desk on his domain stays his
        self.assertTrue(known().protects(team))


if __name__ == "__main__":
    unittest.main()
