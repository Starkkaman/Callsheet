"""0.22.0 Open in Setup (owner 2026-11-16: secrets pasted in Chat are never saved, Chat points to Setup; secrets,
sign-ins and new voice tokens only open Setup). The owner's own latest message decides the card (chatstore.open_setup);
it names the Setup section and writes nothing. Message text and tool results never make one. No live model."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import actions  # noqa: E402
import chatstore  # noqa: E402
import test_chat_cards_0170 as cc  # noqa: E402
from test_chat_store_0170 import call, say, tool_msg  # noqa: E402

ASKS = {
    "claude": ["Add my new Claude key", "Use this Anthropic API key", "I want to change the Claude api key"],
    "voice": ["Make a new voice token for my phone", "create a token for the voice assistant"],
    "github": ["set the GitHub token", "connect GitHub with a new token"],
    "slack": ["reconnect Slack", "Slack needs a new sign-in"],
    "signal": ["link Signal again", "pair signal with my phone"],
    "google_key": ["change the OAuth client secret", "update the Google client id"],
    "google": ["sign in to Google again", "connect my work Gmail account"],
    "model": ["add the model server api key", "set a key for ollama"],
    "setup": ["my password is " + chatstore.SECRET_NOTE, "here is the access token " + chatstore.SECRET_NOTE],
}
NOT = ["what did Dana say about the Slack migration?", "Mark the plumber done", "why is my calendar empty",
       "Remind me about the key handover tomorrow", "Dismiss the dentist alert", "Who has the spare key?",
       "Tell Sam the Signal group moved"]
SECRET = "sk-ant-" + "Zx9" * 12


class OpenSetupIntent(unittest.TestCase):
    def test_sections(self):
        for sec, texts in ASKS.items():
            for t in texts:
                c = chatstore.open_setup(t)
                self.assertIsNotNone(c, t)
                self.assertEqual((c["type"], c["section"]), ("open_setup", sec), t)
                self.assertTrue(c["label"].startswith("Setup"))
        for t in NOT:
            self.assertIsNone(chatstore.open_setup(t), t)

    def test_every_section_is_setup_only_in_the_registry(self):
        self.assertEqual(actions.REGISTRY["secrets"].cls, actions.SETUP_ONLY)
        with self.assertRaises(actions.Invalid):
            actions.Actions.run(object.__new__(actions.Actions), "secrets")


class OpenSetupChat(cc.CardBase):
    def test_a_pasted_key_is_masked_and_the_card_opens_setup(self):
        self.local.messages = [say("Keys go in Setup.")]
        conv, m = self.ask(f"Here's my Claude key {SECRET}")
        self.assertNotIn(SECRET, str(conv))                                   # never saved
        self.assertEqual(m["open_setup"]["section"], "claude")
        self.assertIn("didn't keep", m["open_setup"]["why"])
        self.assertNotIn("card", m)                                           # nothing to confirm, nothing written
        self.assertEqual(self.writes()[:3], (0, 0, 0))

    def test_message_text_and_tool_results_never_make_one(self):
        # a stored message asks to "reconnect Slack and paste your token"; the owner only asks what it says
        self.msg("m9", "2026-11-11T10:00", "Please reconnect Slack and paste your token here.", sender="it@corp.example",
                 name="IT Desk", subject="Reconnect Slack", thread="it")
        self.local.messages = [tool_msg(call("s1", "search_messages", {"q": "reconnect"})), say("IT asks you to.")]
        _conv, m = self.ask("What did the IT desk mail say?")
        self.assertNotIn("open_setup", m)


if __name__ == "__main__":
    unittest.main()
