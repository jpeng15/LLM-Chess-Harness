from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chess_harness.experiment import condition_config, main, run_experiment
from chess_harness.suites import load_suite


class ExperimentTests(unittest.TestCase):
    def test_cli_keeps_controlled_defaults_despite_game_thinking_default(self):
        with tempfile.TemporaryDirectory() as temp, patch("chess_harness.experiment.run_experiment", return_value={"status": "completed"}) as run:
            engine = Path(temp) / "engine"
            engine.write_bytes(b"test")
            self.assertEqual(main(["--engine", str(engine)]), 0)
            config = run.call_args.args[0]
            self.assertEqual(tuple(config["llm"][key] for key in ("think", "tokens", "context", "seconds")),
                             (False, 1024, 4096, 180))
            self.assertEqual(run.call_args.args[3], ["unassisted", "assisted", "assisted-thinking"])

    def test_conditions_isolate_assistance_and_thinking_with_fixed_budgets(self):
        base = {"mode": "unassisted", "prompt_version": "unassisted-v2", "max_plies": 300,
                "llm": {"model": "test", "tokens": 1024, "context": 4096, "seconds": 180, "seed": 42, "think": False}}
        original = deepcopy(base)
        configs = [condition_config(base, name) for name in ("unassisted", "assisted", "assisted-thinking")]
        self.assertEqual(base, original)
        self.assertEqual([c["llm"]["think"] for c in configs], [False, False, True])
        self.assertEqual([c["mode"] for c in configs], ["unassisted", "legal-moves", "legal-moves"])
        for c in configs:
            self.assertEqual(c["max_plies"], 1)
            for key in ("tokens", "context", "seconds", "seed", "model"):
                self.assertEqual(c["llm"][key], base["llm"][key])

    def test_failure_stops_later_conditions_and_saves_partial_experiment(self):
        result = {"complete": False, "legal": 0, "tested": 1, "expected_move_hits": 0,
                  "expected_move_cases": 0, "latency_seconds": {"median": None}, "output_tokens": 0}
        with tempfile.TemporaryDirectory() as root, patch("chess_harness.experiment.run_benchmark", return_value=result) as run, patch("chess_harness.experiment.analyze.main") as analyze, redirect_stdout(io.StringIO()):
            report = run_experiment({"llm": {}}, Path(root), load_suite(), ["unassisted", "assisted"], 100)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(run.call_count, 1)
            analyze.assert_not_called()
            self.assertEqual(len(list(Path(root).glob("experiments/*/experiment.md"))), 1)
