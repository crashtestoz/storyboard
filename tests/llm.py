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

from server.llm import (  # noqa: E402
    SCENE_SYSTEM_PROMPT, SOUND_ACCENT_SYSTEM_PROMPT, SOUNDSCAPE_SYSTEM_PROMPT,
    SYSTEM_PROMPT, AnthropicLLM, ClientGone, OllamaLLM, OpenAICompatLLM, board_outline,
    build_user_message, rewrite_dialogue, streaming,
)


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
            speaker={"name": "Jax", "description": "A cocky pilot."},
            shot_prompt="He studies the impossible route.",
            scene="A worn starship cockpit.",
            dialogue_style="Dry, dismissive confidence.",
            duration_seconds=4.0,
            research="Search result: terse, sarcastic, improvisational speech.",
        )
        self.assertEqual(result, "Never tell me the odds.")
        self.assertIn("original dialogue", service.system)
        self.assertIn("SELECTED SPEAKER: Jax", service.user)
        self.assertIn("terse, sarcastic", service.user)
        self.assertIn("SHOT DURATION: 4.00 seconds", service.user)


class H3AwareRewriteTests(unittest.TestCase):
    def test_h3_rules_reach_the_matching_system_prompts(self):
        for prompt in (SYSTEM_PROMPT, SCENE_SYSTEM_PROMPT):
            self.assertIn("No negative prompt exists", prompt)
            self.assertIn("frame terms", prompt)
        for prompt in (SOUNDSCAPE_SYSTEM_PROMPT, SOUND_ACCENT_SYSTEM_PROMPT):
            self.assertIn("No score, soundtrack or theme music", prompt)
            self.assertNotIn("frame terms", prompt)  # only the sound half
        self.assertNotIn("music or drone", SOUNDSCAPE_SYSTEM_PROMPT)

    def test_shot_rewrite_sees_its_dialogue_length_and_sound(self):
        text = build_user_message(
            "Ray by the car.", dialogue="Ray: Better, Sam.",
            duration_seconds=124 / 24, shot_sound="Rain on the roof.",
        )
        self.assertIn("Duration: 5.2 seconds.", text)
        self.assertIn("Ray: Better, Sam.", text)
        self.assertIn("Sound: Rain on the roof.", text)
        self.assertTrue(text.rstrip().endswith("Ray by the car."))

    def test_silent_shot_adds_no_facts_block(self):
        self.assertNotIn("length, dialogue and sound", build_user_message("A wide shot."))

    def test_board_outline_is_one_short_line_per_shot(self):
        board = {
            "characters": [{"id": "c1", "name": "Ray"}],
            "shots": [
                {"title": "Fly In", "prompt": "Camera Direction & Framing: " + "word " * 40},
                {"title": "Garage", "characterIds": ["c1"],
                 "prompt": "Camera Direction & Framing: Wide.\n\nPose / Action: Ray waits."},
            ],
        }
        lines = board_outline(board).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("1. Fly In: word"))
        self.assertTrue(lines[0].endswith("…"))
        self.assertLessEqual(len(lines[0].split()), 20)
        self.assertEqual(lines[1], "2. Garage [Ray]: Wide. Ray waits.")
        self.assertIn("Every shot in this board", build_user_message(
            "Scene.", outline="\n".join(lines)))

    def test_dialogue_rewrite_sees_surrounding_lines(self):
        class FakeService:
            label = "Fake"
            def health(self):
                return True, ""
            def complete(self, system, user, *, timeout=300.0, max_tokens=None):
                self.user = user
                return "Better."
        service = FakeService()
        rewrite_dialogue(service, "It is better.", speaker={"name": "Ray"},
                         neighbor_lines=["Before — Sam: Is that what I think it is?"])
        self.assertIn("SURROUNDING DIALOGUE", service.user)
        self.assertIn("Sam: Is that what I think it is?", service.user)


class HttpErrorMessageTests(unittest.TestCase):
    def _fail(self, service, code, body):
        err = HTTPError(service.url, code, "Bad Request", {}, io.BytesIO(body.encode()))
        with patch("server.llm.urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                service.complete("system", "user")
        return str(ctx.exception)

    def test_ollama_context_overflow_names_the_window(self):
        service = OllamaLLM("g", "Gemma4 26b", "http://studio:11434", "gemma4:26b")
        inner = json.dumps({"error": {
            "code": 400, "type": "exceed_context_size_error",
            "message": "request (23968 tokens) exceeds the available context size (8192 tokens)",
            "n_prompt_tokens": 23968, "n_ctx": 8192}})
        message = self._fail(service, 400, json.dumps({"error": inner}))
        self.assertIn("23,968", message)
        self.assertIn("8,192", message)
        self.assertIn("Context window", message)
        self.assertNotIn("Bad Request", message)

    def test_other_errors_carry_the_server_reason(self):
        service = OllamaLLM("g", "Gemma4 26b", "http://studio:11434", "gemma4:26b")
        message = self._fail(service, 400, json.dumps({"error": "invalid think value"}))
        self.assertEqual(message, "Gemma4 26b answered HTTP 400: invalid think value")

    def test_openai_style_context_error(self):
        service = OpenAICompatLLM("l", "LM Studio", "http://localhost:1234", "m")
        message = self._fail(service, 400, json.dumps({"error": {
            "message": "maximum context length is 4096 tokens"}}))
        self.assertIn("context window", message)


class AnthropicThinkingTests(unittest.TestCase):
    def _run(self, thinking):
        from types import SimpleNamespace as NS
        sent = {}

        class Messages:
            def create(self, **kw):
                sent.update(kw)
                return NS(stop_reason="end_turn", content=[
                    NS(type="thinking", text=None), NS(type="text", text=" Hi ")])

        service = AnthropicLLM("c", "Claude", "https://api.anthropic.com", "claude-opus-5-5")
        service.thinking = thinking
        fake = NS(messages=Messages())
        with patch.object(service, "_client", return_value=(NS(), fake)):
            reply = service.complete("system", "user", max_tokens=8192)
        return sent, reply

    def test_off_sends_no_thinking_controls(self):
        sent, reply = self._run("none")
        self.assertEqual(reply, "Hi")
        self.assertNotIn("extra_body", sent)
        self.assertEqual(sent["max_tokens"], 8192)

    def test_level_sets_effort_and_lifts_the_reply_cap(self):
        sent, reply = self._run("medium")
        self.assertEqual(reply, "Hi")
        self.assertEqual(sent["extra_body"], {"output_config": {"effort": "medium"}})
        self.assertEqual(sent["max_tokens"], AnthropicLLM.DEFAULT_MAX_TOKENS)


class _Lines:
    """A streamed HTTP body: iterates lines, read() would be the whole thing."""

    def __init__(self, lines: list[str]):
        self.lines = [l.encode() + b"\n" for l in lines]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self.lines)


class StreamingTests(unittest.TestCase):
    def test_not_streamed_outside_streaming(self):
        service = OllamaLLM("o", "Ollama", "http://x", "m")
        with patch("server.llm.urllib.request.urlopen",
                   return_value=_Response({"message": {"content": "Hi"}})) as call:
            self.assertEqual(service.complete("s", "u"), "Hi")
        self.assertFalse(json.loads(call.call_args.args[0].data)["stream"])

    def test_ollama_streams_thinking_and_reply(self):
        service = OllamaLLM("o", "Ollama", "http://x", "m")
        body = _Lines([
            json.dumps({"message": {"thinking": "hm"}, "done": False}),
            json.dumps({"message": {"thinking": "m", "content": ""}, "done": False}),
            json.dumps({"message": {"content": "Hel"}, "done": False}),
            json.dumps({"message": {"content": "lo"}, "done": True}),
        ])
        seen = []
        with patch("server.llm.urllib.request.urlopen", return_value=body) as call:
            with streaming(lambda kind, text: seen.append((kind, text))):
                reply = service.complete("s", "u")
        self.assertEqual(reply, "Hello")
        self.assertTrue(json.loads(call.call_args.args[0].data)["stream"])
        self.assertEqual(seen, [("thinking", "hm"), ("thinking", "m"),
                                ("reply", "Hel"), ("reply", "lo")])

    def test_ollama_error_line_raises(self):
        service = OllamaLLM("o", "Ollama", "http://x", "m")
        body = _Lines([json.dumps({"error": "model crashed"})])
        with patch("server.llm.urllib.request.urlopen", return_value=body):
            with streaming(lambda *_: None):
                with self.assertRaisesRegex(RuntimeError, "model crashed"):
                    service.complete("s", "u")

    def test_openai_streams_reasoning_and_inline_think(self):
        service = OpenAICompatLLM("l", "LM", "http://x", "m")

        def sse(delta):
            return "data: " + json.dumps({"choices": [{"delta": delta}]})

        body = _Lines([
            sse({"reasoning_content": "plan"}),
            "",
            sse({"content": "<think>inl"}),
            sse({"content": "ine</think>"}),
            sse({"content": "Answer"}),
            "data: [DONE]",
        ])
        seen = []
        with patch("server.llm.urllib.request.urlopen", return_value=body) as call:
            with streaming(lambda kind, text: seen.append((kind, text))):
                reply = service.complete("s", "u")
        self.assertEqual(reply, "Answer")
        self.assertTrue(json.loads(call.call_args.args[0].data)["stream"])
        self.assertEqual([k for k, _ in seen],
                         ["thinking", "thinking", "thinking", "reply"])
        self.assertEqual(seen[-1], ("reply", "Answer"))

    def test_aborting_closes_the_connection(self):
        service = OllamaLLM("o", "Ollama", "http://x", "m")
        closed = []

        class Body(_Lines):
            def __exit__(self, *_args):
                closed.append(True)
                return False

        body = Body([json.dumps({"message": {"content": "a"}, "done": False})] * 3)

        def gone(kind, text):
            raise ClientGone

        with patch("server.llm.urllib.request.urlopen", return_value=body):
            with streaming(gone):
                with self.assertRaises(ClientGone):
                    service.complete("s", "u")
        self.assertEqual(closed, [True])

    def test_whole_run_is_bounded(self):
        service = OllamaLLM("o", "Ollama", "http://x", "m")
        body = _Lines([json.dumps({"message": {"content": "a"}, "done": False})] * 3)
        with patch("server.llm.urllib.request.urlopen", return_value=body):
            with streaming(lambda *_: None, max_seconds=-1):
                with self.assertRaisesRegex(RuntimeError, "still generating"):
                    service.complete("s", "u")


if __name__ == "__main__":
    unittest.main()
