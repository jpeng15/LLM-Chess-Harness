import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess
import httpx

from chess_harness import __main__ as single
from chess_harness import batch
from chess_harness.cli import add_game_arguments, game_config
from chess_harness.players import OllamaPlayer, Reply
from chess_harness.runner import run_match
from chess_harness.viewer import snapshot


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = self.root / "fake-engine"
        self.engine.write_bytes(b"test engine")
        self.output = self.root / "runs"
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        self.config = game_config(parser, parser.parse_args([
            "--engine", str(self.engine), "--max-plies", "2", "--seed", "17"]))
        self.addCleanup(patch.stopall)
        patch("chess_harness.batch.new_run_id", return_value="test-batch").start()

    def read(self, name):
        return json.loads((self.output / "batches/test-batch" / name).read_text())

    def run_batch(self, pairs=2):
        with redirect_stdout(io.StringIO()):
            return batch.run_batch(self.config, self.output, pairs)

    def test_schedule_pairs_colors_and_seeds_without_mutating_config(self):
        original = deepcopy(self.config)
        calls = []

        def play(config, directory, *, batch):
            # All games are planned and the current one is checkpointed before launch.
            plan = self.read("batch.json")
            progress = self.read("progress.json")
            self.assertEqual(len(plan["games"]), 4)
            self.assertEqual(progress["games"][len(calls)]["status"], "running")
            self.assertFalse(directory.exists())
            calls.append((deepcopy(config), directory.name, batch))
            config["llm"]["model"] = "adapter mutation must not leak"
            return {"status": "forfeit", "result": "0-1", "reason": "illegal_move"}

        with patch("chess_harness.batch.run_match", side_effect=play):
            result = self.run_batch()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.read("progress.json"), result)
        self.assertEqual(self.read("batch.json")["config"], original)
        self.assertEqual(self.config, original)
        self.assertEqual([(c[0]["llm_color"], c[0]["llm"]["seed"]) for c in calls],
                         [("white", 17), ("black", 17), ("white", 18), ("black", 18)])
        self.assertEqual(len({c[1] for c in calls}), 4)
        self.assertEqual([c[2]["pair"] for c in calls], [1, 1, 2, 2])
        self.assertTrue(all(c[0]["initial_fen"] == original["initial_fen"] for c in calls))

    def test_infrastructure_failure_stops_but_preserves_prior_results(self):
        summaries = [
            {"status": "truncated", "result": "*", "reason": "context_limit"},
            {"status": "infrastructure_failure", "result": "*", "reason": "ollama_transport_error"},
        ]
        with patch("chess_harness.batch.run_match", side_effect=summaries) as play:
            result = self.run_batch()
        self.assertEqual(play.call_count, 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "ollama_transport_error")
        self.assertEqual([g["status"] for g in result["games"]],
                         ["truncated", "infrastructure_failure", "pending", "pending"])
        self.assertEqual(result["games"][0]["summary"], summaries[0])
        self.assertEqual(self.read("progress.json"), result)

    def test_interrupted_game_stops_schedule(self):
        with patch("chess_harness.batch.run_match", return_value={
                "status": "interrupted", "result": "*", "reason": "user_interrupt"}) as play:
            result = self.run_batch()
        self.assertEqual(play.call_count, 1)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual([g["status"] for g in result["games"]],
                         ["interrupted", "pending", "pending", "pending"])

    def test_uncaught_interrupt_or_exception_is_checkpointed(self):
        for exc, status, game_status in [(KeyboardInterrupt(), "interrupted", "interrupted"),
                                         (OSError("disk failure"), "failed", "infrastructure_failure")]:
            with self.subTest(status=status):
                self.output = self.root / status
                with patch("chess_harness.batch.run_match", side_effect=exc):
                    result = self.run_batch()
                self.assertEqual(result["status"], status)
                self.assertEqual(result["games"][0]["status"], game_status)
                self.assertEqual(result["games"][1]["status"], "pending")
                self.assertEqual(self.read("progress.json"), result)

    def test_interrupt_between_games_preserves_finished_summary(self):
        from chess_harness.game import Recorder
        write = Recorder.write
        interrupted = False

        def checkpoint(recorder, name, data):
            nonlocal interrupted
            if name == "progress.json" and data["games"][0]["status"] == "completed" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return write(recorder, name, data)

        with patch.object(Recorder, "write", checkpoint), patch("chess_harness.batch.run_match", return_value={
                "status": "completed", "result": "1/2-1/2", "reason": "stalemate"}) as play:
            result = self.run_batch()
        self.assertEqual(play.call_count, 1)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["games"][0]["summary"]["reason"], "stalemate")
        self.assertEqual(self.read("progress.json")["games"][0]["status"], "completed")

    def test_existing_batch_is_never_overwritten(self):
        with patch("chess_harness.batch.run_match", return_value={
                "status": "completed", "result": "1-0", "reason": "checkmate"}) as play:
            self.run_batch(1)
            original = self.read("progress.json")
            with self.assertRaises(FileExistsError):
                self.run_batch(1)
        self.assertEqual(play.call_count, 2)
        self.assertEqual(self.read("progress.json"), original)

    def test_cli_validation_and_exit_codes(self):
        base = ["--engine", str(self.engine), "--output", str(self.output)]
        for flags in (["--pairs", "0"], ["--pairs", "-1"], ["--pairs", "1.5"],
                      ["--llm-color", "white"], ["--tokens", "4096", "--context", "4096"], ["--fen", "bad"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                batch.main(base + flags)
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse(self.output.exists())
        for status, code in (("completed", 0), ("failed", 1), ("interrupted", 1)):
            with patch("chess_harness.batch.run_batch", return_value={"status": status}) as run:
                self.assertEqual(batch.main(base + ["--pairs", "3", "--seed", "9"]), code)
                self.assertEqual(run.call_args.args[0]["llm"]["seed"], 9)
                self.assertEqual(run.call_args.args[2], 3)

    def test_real_referee_and_artifacts_with_fake_backends(self):
        engines, models = [], []

        class Engine:
            name = "fake-engine"
            def __init__(self, config):
                self.engine = SimpleNamespace(id={"name": self.name})
                self.closed = False
                self.moves = []
                engines.append(self)
            def choose(self, board):
                self.moves.append(len(board.move_stack))
                return Reply(next(iter(board.legal_moves)).uci(), 0, {})
            def close(self): self.closed = True

        class Model(OllamaPlayer):
            def __init__(self, config):
                super().__init__(config)
                models.append(self)
            def warmup(self): return {"done": True}
            def verify_loaded_context(self): return {"context_length": self.config["context"]}
            def choose(self, board):
                return Reply(next(iter(board.legal_moves)).uci(), 0, {})

        def get(client, path):
            return httpx.Response(200, json={"version": "0.34.0"} if path == "/api/version" else {"models": []},
                                  request=httpx.Request("GET", "http://localhost" + path))

        with patch("chess_harness.runner.EnginePlayer", Engine), patch("chess_harness.runner.OllamaPlayer", Model), \
                patch("httpx.Client.get", get):
            result = self.run_batch()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(engines), 4)
        self.assertTrue(all(engine.closed for engine in engines))
        self.assertEqual([engine.moves for engine in engines], [[1], [0], [1], [0]])
        self.assertEqual([model.config["seed"] for model in models], [17, 17, 18, 18])
        for job in self.read("batch.json")["games"]:
            directory = self.output / job["run_id"]
            manifest = json.loads((directory / "manifest.json").read_text())
            self.assertEqual(manifest["batch"], {"id": "test-batch", "index": job["index"], "pair": job["pair"]})
            self.assertEqual(manifest["config"]["llm_color"], job["llm_color"])
            self.assertEqual(manifest["config"]["llm"]["seed"], job["seed"])
            view = snapshot(directory)
            self.assertEqual(len(view["positions"]), 3)
            self.assertEqual(view["positions"][0]["fen"], chess.STARTING_FEN)
            self.assertEqual(view["summary"]["reason"], "max_plies")
            self.assertTrue((directory / "game.pgn").is_file())
            summary = json.loads((directory / "summary.json").read_text())
            self.assertEqual(summary, result["games"][job["index"] - 1]["summary"])

    def test_runner_setup_failure_has_manifest_and_summary(self):
        self.engine.unlink()
        with redirect_stdout(io.StringIO()):
            summary = run_match(self.config, self.output / "setup-failure")
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual(snapshot(self.output / "setup-failure")["summary"]["status"], "infrastructure_failure")

    def test_single_game_command_keeps_existing_options(self):
        with patch("chess_harness.__main__.run_match", return_value={"status": "forfeit"}) as run:
            code = single.main(["--engine", str(self.engine), "--llm-color", "black", "--seed", "42"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0]["llm_color"], "black")
        self.assertEqual(run.call_args.args[0]["llm"]["seed"], 42)


if __name__ == "__main__":
    unittest.main()
