import argparse
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest

import chess

from chess_harness.batch import new_progress, schedule
from chess_harness.cli import add_game_arguments, game_config
from chess_harness.game import Recorder
from chess_harness.report import build_report, duration_stats, main, markdown
from chess_harness.storage import batch_lock, read_json


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "batches" / "test-batch"
        self.recorder = Recorder(self.directory)
        engine = self.root / "engine"
        engine.write_bytes(b"test")
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        config = game_config(parser, parser.parse_args(["--engine", str(engine)]))
        self.plan = {"schema_version": 1, "batch_id": "test-batch", "config": config,
                     "scheduling": {"pairs": 4}, "games": schedule("test-batch", 4, 0)}
        self.recorder.write("batch.json", self.plan)
        self.recorder.write("progress.json", new_progress(self.plan))

    def game(self, index, status, reason, result, seconds=1, attempt=1):
        job = self.plan["games"][index]
        name = job["run_id"] + (f"-attempt-{attempt:04d}" if attempt > 1 else "")
        recorder = Recorder(self.root / name)
        config = deepcopy(self.plan["config"])
        config["llm_color"], config["llm"]["seed"] = job["llm_color"], job["seed"]
        recorder.write("manifest.json", {"config": config, "batch": {"id": "test-batch"}})
        board = chess.Board()
        board.turn = job["llm_color"] == "white"
        recorder.event("move_requested", fen=board.fen(), ply=1, player="same-name")
        if reason == "timeout":
            recorder.event("move_timeout", elapsed_seconds=seconds)
        elif status == "infrastructure_failure":
            recorder.event("move_failed", elapsed_seconds=seconds, raw={})
        else:
            recorder.event("move_response", elapsed_seconds=seconds, text="e2e4",
                           raw={"prompt_eval_count": 20, "eval_count": 4})
            if status == "completed":
                recorder.event("move_applied", uci="e2e4")
        # Same display name, opposite color: never include the engine's 999 seconds.
        board.turn = not board.turn
        recorder.event("move_requested", fen=board.fen(), ply=2, player="same-name")
        recorder.event("move_response", elapsed_seconds=999, raw={"eval_count": 999}, text="e7e5")
        recorder.event("move_applied", uci="e7e5")
        summary = {"status": status, "reason": reason, "result": result, "plies": 2}
        recorder.event("game_finished", **summary)
        recorder.write("summary.json", summary)

    def test_results_denominators_color_and_llm_only_metrics(self):
        cases = [("completed", "checkmate", "1-0", 1),
                 ("completed", "checkmate", "0-1", 2),
                 ("completed", "stalemate", "1/2-1/2", 3),
                 ("forfeit", "illegal_move", "1-0", 4),
                 ("truncated", "context_limit", "*", 5),
                 ("infrastructure_failure", "service_failure", "*", 6),
                 ("forfeit", "timeout", "0-1", 63)]
        for index, args in enumerate(cases):
            self.game(index, *args)
        report = build_report(self.directory)
        self.assertEqual(report["scheduled_games"], 8)
        self.assertEqual(report["finalized_games"], 6)
        self.assertEqual(report["scored_games"], 5)
        self.assertEqual(report["unscored_games"], 3)
        self.assertEqual(report["results"], {"wins": 2, "draws": 1, "losses": 2})
        self.assertEqual(report["results_by_color"]["black"], {"wins": 1, "draws": 0, "losses": 1, "scored": 2})
        self.assertEqual(report["score_rate"], 0.5)
        self.assertEqual(report["llm_turns"], {"requested": 7, "responses": 5, "applied": 3,
                                            "timeouts": 1, "failed": 1, "unanswered": 0})
        self.assertEqual(report["legal_move_rate"], 3 / 7)
        self.assertEqual(report["llm_latency_seconds"]["mean"], 12)
        self.assertEqual(report["llm_latency_seconds"]["p95"], 63)
        self.assertEqual(report["recorded_token_usage"], {"prompt_tokens": 100, "output_tokens": 20, "responses_with_usage": 5})
        self.assertIn("forfeits included", markdown(report))

    def test_retry_attempt_does_not_duplicate_game_or_latency(self):
        self.game(0, "infrastructure_failure", "service_failure", "*", 60)
        self.game(0, "completed", "checkmate", "1-0", 2, attempt=2)
        report = build_report(self.directory)
        self.assertEqual(report["scored_games"], 1)
        self.assertEqual(report["results"]["wins"], 1)
        self.assertEqual(report["attempts"], {"total": 2, "by_status": {"infrastructure_failure": 1, "completed": 1}, "superseded": 1})
        self.assertEqual(report["llm_latency_seconds"]["total"], 2)
        self.assertEqual(report["games"][0]["attempt_count"], 2)

    def test_no_scores_or_latencies_are_null_not_zero(self):
        report = build_report(self.directory)
        self.assertIsNone(report["score_rate"])
        self.assertIsNone(report["legal_move_rate"])
        self.assertIsNone(report["llm_latency_seconds"]["mean"])
        self.assertEqual(report["attempts"]["total"], 0)
        self.assertEqual(report["status_counts"]["pending"], 8)
        self.assertEqual(duration_stats([4])["p95"], 4)

    def test_report_writes_json_and_markdown_without_changing_progress(self):
        self.game(0, "forfeit", "malformed_response", "0-1")
        before = (self.directory / "progress.json").read_bytes()
        destination = self.root / "export"
        with redirect_stdout(io.StringIO()):
            code = main(["--batch", str(self.directory), "--output", str(destination)])
        self.assertEqual(code, 0)
        self.assertEqual(read_json(destination / "report.json")["reason_counts"]["malformed_response"], 1)
        self.assertIn("Metric definitions", (destination / "report.md").read_text())
        self.assertEqual((self.directory / "progress.json").read_bytes(), before)

    def test_report_refuses_running_batch_and_inconsistent_results(self):
        with batch_lock(self.directory), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--batch", str(self.directory)]), 1)
        self.game(0, "truncated", "max_plies", "1/2-1/2")
        with self.assertRaisesRegex(ValueError, "Unscored"):
            build_report(self.directory)


if __name__ == "__main__":
    unittest.main()
