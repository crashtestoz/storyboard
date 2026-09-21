"""LLM service compatibility checks; no model or network required."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.llm import OpenAICompatLLM, rewrite_dialogue  # noqa: E402


class _Response:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class OpenAICompatTests(unittest.TestCase):
    def test_retries_without_audio_when_server_only_accepts_text_and_images(self):
        service = OpenAICompatLLM(
            "lmstudio", "LM Studio", "http://localhost:11434", "vision-model"
        )
        rejection = HTTPError(
            service.url + "/v1/chat/completions",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({
                "error": "Invalid content: type must be text or image_url."
            }).encode()),
        )
        success = _Response({
            "choices": [{"message": {"content": "Portrait description"}}]
        })

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "portrait.png"
            audio = Path(directory) / "voice.mp3"
            image.write_bytes(b"image")
            audio.write_bytes(b"audio")
            with patch("server.llm.urllib.request.urlopen",
                       side_effect=[rejection, success]) as request:
                result = service.complete_with_media(
                    "system", "user", images=[image], audio=audio
                )

        self.assertEqual(result, "Portrait description")
        self.assertEqual(request.call_count, 2)
        first = json.loads(request.call_args_list[0].args[0].data)
        second = json.loads(request.call_args_list[1].args[0].data)
        first_content = first["messages"][1]["content"]
        second_content = second["messages"][1]["content"]
        self.assertTrue(any(block["type"] == "input_audio" for block in first_content))
        self.assertFalse(any(block["type"] == "input_audio" for block in second_content))
        self.assertTrue(any(block["type"] == "image_url" for block in second_content))

    def test_dialogue_rewrite_uses_speaker_context_and_research(self):
        class FakeService:
            label = "Test LLM"
            model = "test"

            def health(self):
                return True, ""

            def complete(self, system, user, *, timeout=300.0):
                self.system = system
                self.user = user
                return '"Never tell me the odds."'

        service = FakeService()
        result = rewrite_dialogue(
            service,
            "That is unlikely to work.",
            speaker={"name": "Han Solo", "description": "A cocky pilot."},
            shot_prompt="He studies the impossible route.",
            scene="A worn starship cockpit.",
            dialogue_style="Dry, dismissive confidence.",
            duration_seconds=4.0,
            research="Search result: terse, sarcastic, improvisational speech.",
        )
        self.assertEqual(result, "Never tell me the odds.")
        self.assertIn("original dialogue", service.system)
        self.assertIn("SELECTED SPEAKER: Han Solo", service.user)
        self.assertIn("terse, sarcastic", service.user)
        self.assertIn("SHOT DURATION: 4.00 seconds", service.user)


if __name__ == "__main__":
    unittest.main()
