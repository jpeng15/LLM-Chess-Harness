import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chess
import httpx

from chess_harness.game import Recorder, run_game
from chess_harness.limits import PlayerFailure, context_error, require_supported_ollama
from chess_harness.players import OllamaPlayer, Reply
from chess_harness.viewer import snapshot

CONFIG = {"model": "test:latest", "url": "http://localhost:11434", "think": False,
          "context": 4096, "tokens": 64, "temperature": 0, "seed": 0, "seconds": 10}


def response(content="e2e4", reason="stop", count=5):
    return {"message": {"content": content}, "done": True, "done_reason": reason, "eval_count": count}


class LimitsTests(unittest.TestCase):
    def choose(self, data):
        async def post(*args):
            return data
        with patch.object(OllamaPlayer, "_post", post):
            return OllamaPlayer(CONFIG).choose(chess.Board())

    def test_both_truncation_controls_are_disabled(self):
        request = OllamaPlayer(CONFIG).request(chess.Board())
        self.assertIs(request["truncate"], False)
        self.assertIs(request["shift"], False)

    def test_versions_fail_closed(self):
        for value in (None, "unknown", "0.33.3", "0.34.0-rc1", 34):
            with self.subTest(value=value), self.assertRaises(PlayerFailure):
                require_supported_ollama(value)
        for value in ("0.34.0", "v0.34.1", "0.35.0", "1.0.0"):
            require_supported_ollama(value)

    def test_cutoff_rejects_even_a_complete_looking_legal_move(self):
        reply = self.choose(response(reason="length", count=64))
        self.assertEqual(reply.text, "e2e4")
        self.assertEqual(reply.failure_reason, "output_limit")

    def test_normal_stop_at_budget_is_not_assumed_truncated(self):
        self.assertIsNone(self.choose(response(count=64)).failure_reason)

    def test_ambiguous_length_is_not_assumed_output_budget(self):
        for count in (None, 10, True):
            self.assertEqual(self.choose(response(reason="length", count=count)).failure_reason, "generation_limit")

    def test_thinking_only_cutoff_preserves_thinking(self):
        raw = response(content="", reason="length", count=64)
        raw["message"]["thinking"] = "Still considering moves"
        reply = self.choose(raw)
        self.assertEqual(reply.failure_reason, "output_limit")
        self.assertEqual(reply.raw, raw)

    def test_incomplete_or_unknown_response_preserves_raw(self):
        for raw in ({"done": False, "message": {"content": "e2"}},
                    {"done": True, "message": None}, response(reason="mystery"),
                    {"done": True, "message": {"content": "e2e4"}}):
            with self.subTest(raw=raw), self.assertRaises(PlayerFailure) as caught:
                self.choose(raw)
            self.assertEqual(caught.exception.raw, raw)

    def test_nested_context_error_and_unrelated_error(self):
        error = {"error": json.dumps({"error": {"type": "exceed_context_size_error", "message": "too many tokens"}})}
        self.assertTrue(context_error(error))
        for raw in ({"error": "out of memory allocating context"}, {"error": "model not found"}, "invalid options"):
            self.assertFalse(context_error(raw))

    def test_http_error_classification(self):
        async def post(*args, **kwargs):
            return httpx.Response(400, json={"error": json.dumps({"error": {"type": "exceed_context_size_error"}})})
        with patch("httpx.AsyncClient.post", post), self.assertRaises(PlayerFailure) as caught:
            OllamaPlayer(CONFIG).choose(chess.Board())
        self.assertEqual(caught.exception.reason, "context_limit")
        self.assertEqual(caught.exception.raw["http_status"], 400)

    def test_non_json_and_network_failure(self):
        async def bad_json(*args, **kwargs):
            return httpx.Response(502, text="bad gateway")
        with patch("httpx.AsyncClient.post", bad_json), self.assertRaises(PlayerFailure) as caught:
            OllamaPlayer(CONFIG).choose(chess.Board())
        self.assertEqual(caught.exception.reason, "invalid_server_response")
        self.assertEqual(caught.exception.raw["body"], "bad gateway")
        async def disconnect(*args, **kwargs):
            raise httpx.ReadError("connection closed")
        with patch("httpx.AsyncClient.post", disconnect), self.assertRaises(PlayerFailure) as caught:
            OllamaPlayer(CONFIG).choose(chess.Board())
        self.assertEqual(caught.exception.reason, "ollama_transport_error")

    def test_effective_context_must_match(self):
        for size in (4096, 2048, None):
            raw = {"models": [{"name": "test:latest", "context_length": size}]}
            with self.subTest(size=size), patch("httpx.Client.get", return_value=httpx.Response(
                    200, json=raw, request=httpx.Request("GET", "http://localhost/api/ps"))):
                if size == 4096:
                    self.assertEqual(OllamaPlayer(CONFIG).verify_loaded_context()["context_length"], size)
                else:
                    with self.assertRaises(PlayerFailure):
                        OllamaPlayer(CONFIG).verify_loaded_context()

    def test_referee_records_each_failure_without_applying_move(self):
        class Fixed:
            name = "test"
            def __init__(self, value): self.value = value
            def choose(self, board):
                if isinstance(self.value, Exception): raise self.value
                return self.value
        cases = [
            (Reply("e2e4", 1, response(reason="length", count=64), "output_limit"), "forfeit", "output_limit", "0-1"),
            (Reply("e2e4", 1, response(reason="length", count=10), "generation_limit"), "truncated", "generation_limit", "*"),
            (PlayerFailure("context_limit", "input too large", raw={"error": "exceed_context_size_error"}), "truncated", "context_limit", "*"),
            (PlayerFailure("incomplete_response", "EOF", raw={"message": {"content": "e2"}}), "infrastructure_failure", "incomplete_response", "*"),
        ]
        for value, status, reason, result in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                recorder = Recorder(Path(temp) / "run")
                recorder.write("manifest.json", {"config": {"initial_fen": chess.STARTING_FEN}})
                with redirect_stdout(io.StringIO()):
                    summary = run_game(chess.Board(), {True: Fixed(value), False: Fixed(None)}, True, 10, recorder)
                self.assertEqual((summary["status"], summary["reason"], summary["result"]), (status, reason, result))
                self.assertEqual(summary["plies"], 0)
                self.assertEqual(summary["final_fen"], chess.STARTING_FEN)
                view = snapshot(recorder.directory)
                self.assertEqual(len(view["positions"]), 1)
                self.assertEqual(view["responses"][0]["failure_reason"], reason)
                self.assertFalse(view["responses"][0]["applied"])


if __name__ == "__main__":
    unittest.main()
