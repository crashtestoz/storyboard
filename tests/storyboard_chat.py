"""Assistant context/proposal checks; no model or network required."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.storyboard_chat import chat, compact_board_context, validate_actions  # noqa: E402
from server.store import default_board, default_character, default_shot  # noqa: E402


class FakeLLM:
    label = "Test model"
    model = "test-1"

    def __init__(self, reply: str):
        self.reply = reply
        self.seen = None

    def health(self):
        return True, ""

    def complete(self, system, user, *, timeout=120.0):
        self.seen = (system, user, timeout)
        return self.reply


class StoryboardChatTests(unittest.TestCase):
    def setUp(self):
        self.board = default_board("Crossing")
        character = default_character("Mara", "Short silver hair, red coat.")
        shot = default_shot(self.board["defaults"])
        shot.update({"title": "At the gate", "prompt": "A wide shot.",
                     "characterIds": [character["id"]],
                     "outputs": ["large-output-that-must-not-enter-context"],
                     "logUrl": "/media/private/run.log"})
        self.board["characters"] = [character]
        self.board["shots"] = [shot]

    def test_context_omits_runtime_data(self):
        context = compact_board_context(self.board, self.board["shots"][0]["id"])
        encoded = json.dumps(context)
        self.assertIn("At the gate", encoded)
        self.assertIn("Mara", encoded)
        self.assertNotIn("large-output", encoded)
        self.assertNotIn("logUrl", encoded)

    def test_actions_are_allow_listed_and_ids_must_exist(self):
        sid = self.board["shots"][0]["id"]
        actions = validate_actions([
            {"tool": "update_shot", "shotId": sid,
             "fields": {"prompt": "Better", "outputs": ["bad"]}},
            {"tool": "update_shot", "shotId": "invented", "fields": {"prompt": "No"}},
            {"tool": "delete_board"},
            {"tool": "set_board_fields", "fields": {"name": "No", "soundscape": "Wind"}},
        ], self.board)
        self.assertEqual(actions, [
            {"tool": "update_shot", "shotId": sid, "fields": {"prompt": "Better"}},
            {"tool": "set_board_fields", "fields": {"soundscape": "Wind"}},
        ])

    def test_chat_sends_compact_context_and_parses_fenced_json(self):
        sid = self.board["shots"][0]["id"]
        model = FakeLLM("```json\n" + json.dumps({
            "message": "Ready to apply.",
            "actions": [{"tool": "update_shot", "shotId": sid,
                         "fields": {"title": "The threshold"}}],
        }) + "\n```")
        result = chat(model, self.board, "Make it stronger", selected_id=sid)
        self.assertEqual(result["message"], "Ready to apply.")
        self.assertEqual(result["actions"][0]["fields"]["title"], "The threshold")
        self.assertNotIn("large-output", model.seen[1])
        self.assertEqual(model.seen[2], 180.0)

    def test_plain_text_reply_remains_a_useful_chat_answer(self):
        result = chat(FakeLLM("The pacing works, but shot two needs an eyeline."),
                      self.board, "Review this")
        self.assertEqual(result["actions"], [])
        self.assertIn("pacing works", result["message"])


if __name__ == "__main__":
    unittest.main()
