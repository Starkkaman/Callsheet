"""0.24.0 (B2) the secret shopper's item view and Projects (pain points 8 and 10): a time only from the message's own
words, "No reply yet" only for mail a person sent, Projects' slipping list without digests, forums or list mail.
Fictional senders only; no model."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "callsheet" / "app"))

import filters as F  # noqa: E402
import projects as P  # noqa: E402
import today as T  # noqa: E402


def mail(subject="Lunch?", text="Can you make lunch Friday?", sender="kai@friends.example", **kw):
    return {"raw": dict({"source": "gmail", "direction": "in", "subject": subject, "text": text, "sender": sender,
                         "sender_name": "Kai Moss", "labels": "[]", "bulk": 0}, **kw)}


class RealTimeTest(unittest.TestCase):
    def test_vague_words_are_no_time(self):
        for ph in ("now", "Right now.", "ASAP", "as soon as possible", "soon", "whenever you can"):
            self.assertFalse(T.real_time({"phrase": ph}), ph)

    def test_a_guess_needs_a_day_or_time_word(self):
        self.assertFalse(T.real_time({"phrase": "when we talk", "approx": 1}))
        self.assertTrue(T.real_time({"phrase": "around 7 Friday", "approx": 1}))
        self.assertTrue(T.real_time({"phrase": "Friday at 7"}))
        self.assertTrue(T.real_time({"phrase": ""}))


class SlippingTest(unittest.TestCase):
    def test_digests_forums_and_list_mail_are_not_waiting_on_him(self):
        self.assertTrue(P._not_a_person(mail(subject="Weekly digest: 12 new posts"), None))  # noqa: SLF001
        self.assertTrue(P._not_a_person(mail(subject="Re: [Garden forum] Who has a tiller?"), None))  # noqa: SLF001
        self.assertTrue(P._not_a_person(mail(sender="news@mailer.shop.example", bulk=1), None))  # noqa: SLF001
        self.assertFalse(P._not_a_person(mail(), None))  # noqa: SLF001
        self.assertFalse(P._not_a_person(mail(direction="out"), None))  # noqa: SLF001

    def test_a_list_sender_he_never_answers(self):
        rep = F.Reputation({"deals@shop.example": {"n": 9, "unsub": 9, "bulk": 9, "promo": 0, "threads": 9,
                                                  "answered": 0}})
        hit = P._not_a_person(mail(sender="deals@shop.example", text="Can you rate your order?"), rep)  # noqa: SLF001
        self.assertEqual(hit, bool(rep.bulk("deals@shop.example")))


if __name__ == "__main__":
    unittest.main()
