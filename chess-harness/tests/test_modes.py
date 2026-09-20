import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chess
import httpx

from chess_harness.batch import run_batch
from chess_harness.cli import add_game_arguments, game_config
from chess_harness.game import Recorder, run_game
from chess_harness.players import OllamaPlayer, Reply, observation, prompt_version
from chess_harness.report import build_report, markdown
from chess_harness.storage import read_json
from chess_harness.viewer import snapshot

CONFIG = {"model": "test", "think": False, "context": 4096, "tokens": 64,
          "temperature": 0, "seed": 0, "seconds": 60, "url": "http://localhost:11434"}


def listed_moves(board):
    text = observation(board, "legal-moves")
    return text.split(" moves):\n", 1)[1].split("\n", 1)[0].split()


class ModeTests(unittest.TestCase):
    def test_thinking_defaults_and_explicit_baseline_reach_ollama(self):
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        with tempfile.TemporaryDirectory() as temp:
            engine_path = Path(temp) / "engine"
            engine_path.write_bytes(b"test")
            for flags, expected in [([], (True, 4096, 8192, 180)),
                                    (["--no-think", "--tokens", "64", "--context", "4096",
                                      "--move-seconds", "60"], (False, 64, 4096, 60))]:
                with self.subTest(flags=flags):
                    config = game_config(parser, parser.parse_args(["--engine", str(engine_path), *flags]))
                    self.assertEqual(tuple(config["llm"][key] for key in ("think", "tokens", "context", "seconds")), expected)
                    request = OllamaPlayer(config["llm"]).request(chess.Board())
                    self.assertEqual(request["think"], expected[0])
                    self.assertEqual(request["options"]["num_predict"], expected[1])
                    self.assertEqual(request["options"]["num_ctx"], expected[2])
        self.assertTrue(parser.parse_args(["--think"]).think)
        self.assertEqual(parser.parse_args(["--no-think"]).tokens, 4096)

    def test_unassisted_prompt_matches_stage1_baseline(self):
        messages = OllamaPlayer(CONFIG).request(chess.Board())["messages"]
        # Captured from the Stage 1 baseline opening request, before assisted mode.
        self.assertEqual(hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
                         "f66bb3d9696611c825f476b9fd9523d6983247ce26991bbe5d1588fc26cbf957")
        with patch.object(chess.Board, "legal_moves", new_callable=unittest.mock.PropertyMock,
                          side_effect=AssertionError("unassisted observation must not enumerate legal moves")):
            OllamaPlayer(CONFIG).request(chess.Board())

    def test_complete_sorted_lists_for_special_positions(self):
        cases = [
            (chess.STARTING_FEN, {"e2e4", "g1f3"}),
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", {"e1g1", "e1c1"}),
            ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", {"e8g8", "e8c8"}),
            ("7k/P7/8/8/8/8/8/7K w - - 0 1", {"a7a8" + p for p in "qrbn"}),
            ("7k/8/8/8/8/8/p7/7K b - - 0 1", {"a2a1" + p for p in "qrbn"}),
            ("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", {"e5d6"}),
            ("4r2k/8/8/8/8/8/8/4K3 w - - 0 1", set()),
            ("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", set()),
        ]
        for fen, required in cases:
            with self.subTest(fen=fen):
                board = chess.Board(fen)
                self.assertTrue(board.is_valid())
                moves = listed_moves(board)
                self.assertEqual(moves, sorted({m.uci() for m in board.legal_moves}))
                self.assertTrue(required <= set(moves))
                self.assertEqual(board.fen(), chess.Board(fen).fen())
        pinned = chess.Board("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
        self.assertNotIn("e5d6", listed_moves(pinned))

    def test_list_refreshes_after_moves_without_changing_generation_options(self):
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("e7e5")
        player = OllamaPlayer({**CONFIG, "mode": "legal-moves"})
        request = player.request(board)
        self.assertIn("1. e4 1... e5", request["messages"][1]["content"])
        self.assertNotIn("e2e4", listed_moves(board))
        self.assertIn("g1f3", listed_moves(board))
        plain = OllamaPlayer(CONFIG).request(board)
        self.assertEqual({k: v for k, v in request.items() if k != "messages"},
                         {k: v for k, v in plain.items() if k != "messages"})
        with self.assertRaises(ValueError):
            OllamaPlayer({**CONFIG, "mode": "unknown"})

    def test_assisted_invalid_answers_still_forfeit_without_retry(self):
        cases = [(Reply("e2e5", 0, {}), "illegal_move"),
                 (Reply("I choose e2e4", 0, {}), "malformed_response"),
                 (Reply("e2e4", 0, {}, "output_limit"), "output_limit")]
        for reply, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                player = OllamaPlayer({**CONFIG, "mode": "legal-moves"})
                recorder = Recorder(Path(temp) / "run", mode="legal-moves")
                with patch.object(player, "choose", return_value=reply) as choose, redirect_stdout(io.StringIO()):
                    result = run_game(chess.Board(), {True: player, False: player}, True, 10, recorder)
                self.assertEqual((result["status"], result["reason"], result["plies"]), ("forfeit", reason, 0))
                choose.assert_called_once()
                records = [json.loads(line) for line in (recorder.directory / "events.jsonl").read_text().splitlines()]
                self.assertIn("Legal moves", records[0]["request"]["messages"][1]["content"])
                self.assertIn("Legal-move-assisted", (recorder.directory / "game.pgn").read_text())

    def test_assisted_batch_wires_prompts_manifests_viewer_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine_path = root / "engine"
            engine_path.write_bytes(b"test")
            parser = argparse.ArgumentParser()
            add_game_arguments(parser)
            config = game_config(parser, parser.parse_args(["--engine", str(engine_path), "--mode", "legal-moves", "--max-plies", "2"]))
            self.assertEqual(config["prompt_version"], prompt_version("legal-moves"))

            def get(client, path):
                return httpx.Response(200, json={"version": "0.34.1"} if path == "/api/version" else {"models": []},
                                      request=httpx.Request("GET", "http://localhost" + path))

            def choose(player, board):
                self.assertEqual(player.mode, "legal-moves")
                return Reply(next(iter(board.legal_moves)).uci(), 0, {})

            with patch("httpx.Client.get", get), patch("chess_harness.runner.EnginePlayer") as engine, \
                    patch.object(OllamaPlayer, "warmup", return_value={"done": True}), \
                    patch.object(OllamaPlayer, "verify_loaded_context", return_value={"context_length": config["llm"]["context"]}), \
                    patch.object(OllamaPlayer, "choose", choose), redirect_stdout(io.StringIO()):
                engine.return_value.name = "test-engine"
                del engine.return_value.request
                engine.return_value.engine.id = {"name": "test-engine"}
                engine.return_value.choose.side_effect = lambda board: Reply(next(iter(board.legal_moves)).uci(), 0, {})
                result = run_batch(config, root, 1)
            self.assertEqual(result["status"], "completed")
            for job in result["games"]:
                directory = root / job["current_run_id"]
                self.assertEqual(read_json(directory / "manifest.json")["config"]["mode"], "legal-moves")
                self.assertEqual(snapshot(directory)["config"]["prompt_version"], "legal-moves-v2")
                records = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
                requests = [e["request"] for e in records if e["type"] == "move_requested" and e["request"] is not None]
                self.assertEqual(len(requests), 1)
                self.assertIn("Legal moves", requests[0]["messages"][1]["content"])
            report = build_report(root / "batches" / result["batch_id"])
            self.assertIn("Mode: legal-moves | Prompt: legal-moves-v2", markdown(report))


if __name__ == "__main__":
    unittest.main()
