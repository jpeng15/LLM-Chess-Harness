import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import chess

from chess_harness.cli import add_game_arguments, game_config
from chess_harness.experiment import condition_config
from chess_harness.game import Recorder, run_game
from chess_harness.players import OllamaPlayer, legal_move_schema, prompt_version
from chess_harness.suites import load_suite, position_config, initial_board
from chess_harness.viewer import snapshot


CONFIG = dict(model="test", url="http://localhost:11434", mode="constrained-legal",
              think=False, tokens=64, context=4096, seconds=60, seed=0, temperature=0)


def response(content, reason="stop", count=10):
    return {"done": True, "done_reason": reason, "eval_count": count,
            "message": {"content": content}}


class ConstrainedTests(unittest.TestCase):
    def test_exact_schema_for_special_positions_and_after_moves(self):
        player = OllamaPlayer(CONFIG)
        suite = load_suite(split="all")
        for position in suite["positions"]:
            with self.subTest(position=position["id"]):
                board = initial_board(position_config({}, position))
                original = board.fen()
                schema = player.request(board)["format"]
                self.assertEqual(schema["properties"]["move"]["enum"], sorted(m.uci() for m in board.legal_moves))
                self.assertEqual(schema["required"], ["move"])
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(board.fen(), original)
        board = chess.Board()
        before = player.request(board)["format"]
        self.assertNotIn("e1g1", before["properties"]["move"]["enum"])
        board.push_uci("e2e4")
        after = player.request(board)["format"]
        self.assertNotIn("e2e4", after["properties"]["move"]["enum"])
        self.assertIn("e7e5", after["properties"]["move"]["enum"])
        pinned = chess.Board("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
        self.assertNotIn("e5d6", legal_move_schema(pinned)["properties"]["move"]["enum"])

    def test_adapter_sends_schema_and_preserves_raw_json(self):
        raw = response('  {"move": "e2e4"}\n')
        with patch.object(OllamaPlayer, "_post", new_callable=AsyncMock, return_value=raw) as post:
            reply = OllamaPlayer(CONFIG).choose(chess.Board())
        self.assertEqual(reply.text, "e2e4")
        self.assertIsNone(reply.failure_reason)
        self.assertIs(reply.raw, raw)
        post.assert_awaited_once()
        request = post.call_args.args[1]
        self.assertEqual(request["format"], legal_move_schema(chess.Board()))
        self.assertIn("JSON schema:", request["messages"][1]["content"])
        self.assertNotIn("Return only that UCI move", request["messages"][1]["content"])

    def test_invalid_responses_forfeit_without_repair_or_retry(self):
        malformed = ['e2e4', '```json\n{"move":"e2e4"}\n```', '{}', '[]', 'null',
                     '{"move":null}', '{"move":42}', '{"move":["e2e4"]}',
                     '{"move":"e2e4","why":"good"}', '{"move":"e2e4","move":"d2d4"}',
                     '{"move":"e2e4"} trailing', '{"move":', '{"move":NaN}']
        illegal = [json.dumps({"move": m}) for m in ("e2e5", "e1g1", " e2e4 ", "E2E4", "O-O", "")]
        for content, reason in [(s, "malformed_response") for s in malformed] + [(s, "illegal_move") for s in illegal]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                player = OllamaPlayer(CONFIG)
                recorder = Recorder(Path(temp), exist_ok=True, mode="constrained-legal")
                with patch.object(player, "_post", new_callable=AsyncMock, return_value=response(content)) as post, redirect_stdout(io.StringIO()):
                    summary = run_game(chess.Board(), {True: player, False: player}, True, 2, recorder)
                self.assertEqual((summary["status"], summary["reason"], summary["plies"]), ("forfeit", reason, 0))
                post.assert_awaited_once()
                saved = [json.loads(line) for line in (Path(temp) / "events.jsonl").read_text().splitlines()]
                self.assertEqual(next(e for e in saved if e["type"] == "move_response")["raw"]["message"]["content"], content)

    def test_cutoff_takes_precedence_over_even_valid_json(self):
        for content in ('{"move":"e2e4"}', '{"move":', ''):
            for count, expected in ((64, "output_limit"), (10, "generation_limit")):
                with self.subTest(content=content, count=count), patch.object(OllamaPlayer, "_post", new_callable=AsyncMock, return_value=response(content, "length", count)):
                    reply = OllamaPlayer(CONFIG).choose(chess.Board())
                self.assertEqual(reply.failure_reason, expected)
                self.assertEqual(reply.raw["message"]["content"], content)

    def test_terminal_board_never_calls_model(self):
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        with self.assertRaises(ValueError):
            legal_move_schema(board)
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            player = OllamaPlayer(CONFIG)
            with patch.object(player, "_post", new_callable=AsyncMock) as post:
                result = run_game(board, {True: player, False: player}, False, 2,
                                  Recorder(Path(temp), exist_ok=True, mode="constrained-legal"))
            post.assert_not_awaited()
        self.assertEqual(result["reason"], "stalemate")

    def test_cli_referee_pgn_and_viewer_keep_mode_and_original_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = root / "engine"
            engine.write_bytes(b"test")
            parser = argparse.ArgumentParser()
            add_game_arguments(parser)
            config = game_config(parser, parser.parse_args(["--mode", "constrained-legal", "--no-think", "--engine", str(engine)]))
            self.assertEqual(config["prompt_version"], "constrained-legal-v1")
            self.assertFalse(config["llm"]["think"])
            recorder = Recorder(root / "game", mode=config["mode"])
            recorder.write("manifest.json", {"config": config})
            player = OllamaPlayer({**config["llm"], "mode": config["mode"]})
            with patch.object(player, "_post", new_callable=AsyncMock, return_value=response('{"move":"e2e4"}')), redirect_stdout(io.StringIO()):
                result = run_game(chess.Board(), {True: player, False: player}, True, 1, recorder)
            self.assertEqual(result["plies"], 1)
            self.assertEqual(result["reason"], "max_plies")
            view = snapshot(recorder.directory)
            self.assertEqual(view["responses"][0]["text"], "e2e4")
            self.assertEqual(view["responses"][0]["content"], '{"move":"e2e4"}')
            self.assertTrue(view["responses"][0]["applied"])
            self.assertIn("Schema-constrained legal", (recorder.directory / "game.pgn").read_text())

    def test_existing_modes_have_no_schema_and_experiment_can_select_constrained(self):
        for mode in ("unassisted", "legal-moves"):
            self.assertNotIn("format", OllamaPlayer({**CONFIG, "mode": mode}).request(chess.Board()))
        config = condition_config({"llm": {"think": True, "tokens": 128}}, "constrained")
        self.assertEqual(config["mode"], "constrained-legal")
        self.assertEqual(config["prompt_version"], prompt_version("constrained-legal"))
        self.assertFalse(config["llm"]["think"])
        self.assertEqual(config["llm"]["tokens"], 128)
