import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chess

from chess_harness.batch import run_batch, resume_batch, schedule
from chess_harness.benchmark import run_benchmark
from chess_harness.cli import add_game_arguments, game_config
from chess_harness.compare import compare
from chess_harness.game import Recorder, run_game
from chess_harness.players import OllamaPlayer, Reply
from chess_harness.report import build_report
from chess_harness.storage import events, read_json
from chess_harness.suites import initial_board, load_suite, position_config, validate_positions
from chess_harness.viewer import snapshot


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        engine = self.root / "engine"
        engine.write_bytes(b"fake engine")
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        self.config = game_config(parser, parser.parse_args(["--engine", str(engine), "--mode", "legal-moves"]))

    def fake_match(self, config, directory, *, expected_identity=None, batch=None):
        recorder = Recorder(directory, mode=config["mode"])
        recorder.write("manifest.json", {"config": config, "batch": batch,
                       "loaded_model": {"digest": "test-model"}, "engine_sha256": "test-engine",
                       "ollama_version": {"version": "0.34.1"}, "packages": {}, "python": "test"})
        player = OllamaPlayer({**config["llm"], "mode": config["mode"]})
        def choose(board):
            return Reply(next(iter(board.legal_moves)).uci(), 0.5, {"prompt_eval_count": 100, "eval_count": 5})
        with patch.object(player, "choose", side_effect=choose):
            return run_game(initial_board(config), {True: player, False: player},
                            config["llm_color"] == "white", config["max_plies"], recorder)

    def test_suite_history_annotations_and_single_decision_reports(self):
        suite = load_suite(split="all")
        self.assertEqual(len(suite["positions"]), 12)
        with patch("chess_harness.benchmark.run_match", side_effect=self.fake_match), redirect_stdout(io.StringIO()):
            report = run_benchmark(self.config, self.root, suite)
        self.assertTrue(report["complete"])
        self.assertEqual((report["tested"], report["legal"], report["expected_move_cases"]), (12, 12, 2))
        self.assertEqual(report["output_tokens"], 60)
        history_case = next(r for r in report["cases"] if r["id"] == "recent-reversal")
        requests = [e for e in events(self.root / history_case["run_id"]) if e["type"] == "move_requested"]
        self.assertEqual(len(requests), 1)
        self.assertIn("2. Ng1", requests[0]["request"]["messages"][1]["content"])
        for row in report["cases"]:
            settings = read_json(self.root / row["run_id"] / "manifest.json")["config"]
            self.assertEqual(settings["llm_color"] == "white", initial_board(settings).turn)
            self.assertNotIn("expected_moves", json.dumps(settings))
        self.assertEqual(len(compare(report, report)["paired_positions"]), 12)
        changed = deepcopy(report)
        changed["config"]["llm"]["tokens"] = 2048
        result = compare(report, changed)
        self.assertIn("llm.tokens", result["settings_differences"])
        changed["suite"]["sha256"] = "different"
        with self.assertRaisesRegex(ValueError, "suite"):
            compare(report, changed)
        changed = deepcopy(report)
        changed["complete"] = False
        with self.assertRaisesRegex(ValueError, "complete"):
            compare(report, changed)

    def test_fixture_validation_rejects_terminal_duplicate_and_illegal_history(self):
        base = load_suite()["positions"][0]
        for positions in ([base, base], [{**base, "moves": ["e2e5"]}],
                          [{**base, "fen": "7k/8/8/8/8/8/8/K7 w - - 0 1", "moves": []}]):
            with self.assertRaises(ValueError):
                validate_positions(positions)

    def test_annotated_mates_are_exhaustive(self):
        for position in load_suite(split="all")["positions"]:
            if not position.get("expected_moves"):
                continue
            board = initial_board(position_config(self.config, position))
            mates = []
            for move in list(board.legal_moves):
                board.push(move)
                if board.is_checkmate():
                    mates.append(move.uci())
                board.pop()
            self.assertEqual(set(mates), set(position["expected_moves"]))

    def test_varied_start_batch_resume_and_report_preserve_fixtures(self):
        positions = load_suite()["positions"][:2]
        jobs = schedule("test", 3, 42, positions)
        self.assertEqual([j["position"]["id"] for j in jobs],
                         [positions[0]["id"]] * 2 + [positions[1]["id"]] * 2 + [positions[0]["id"]] * 2)
        self.config["max_plies"] = 1
        with patch("chess_harness.batch.run_match", side_effect=self.fake_match), patch("chess_harness.batch.new_run_id", return_value="test"), redirect_stdout(io.StringIO()):
            run_batch(self.config, self.root, 2, positions)
        directory = self.root / "batches" / "test"
        with patch("chess_harness.batch.run_match") as run, redirect_stdout(io.StringIO()):
            result = resume_batch(directory)
        run.assert_not_called()
        self.assertEqual(result["status"], "completed")
        report = build_report(directory)
        self.assertEqual(report["games"][2]["position"], positions[1])
        config = read_json(self.root / "test-000001" / "manifest.json")["config"]
        self.assertEqual(config["initial_moves"], positions[0]["moves"])
        self.assertEqual(snapshot(self.root / "test-000001")["positions"][0]["fen"], initial_board(config).fen())

    def test_infrastructure_failure_stops_suite_with_partial_report(self):
        def failed(config, directory, **kwargs):
            rec = Recorder(directory)
            rec.write("manifest.json", {"config": config})
            return {"status": "infrastructure_failure", "reason": "unavailable"}
        with patch("chess_harness.benchmark.run_match", side_effect=failed) as run, redirect_stdout(io.StringIO()):
            report = run_benchmark(self.config, self.root, load_suite())
        self.assertFalse(report["complete"])
        self.assertEqual(report["tested"], 1)
        self.assertIsNone(report["cases"][0]["elapsed_seconds"])
        self.assertEqual(run.call_count, 1)
