from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import chess

from chess_harness.batch import schedule
from chess_harness.report import duration_stats
from chess_harness.validator_artifacts import FrozenValidator
from chess_harness.validator_experiment import (
    CONDITIONS, condition_config, heldout_cases, load_heldout_suite, main, run_experiment,
    validate_heldout_suite,
)


SOURCE = b'def validate(position, history, candidate):\n return {}\n'
MANIFEST = {"image": "sha256:" + "b" * 64, "docker_context": "desktop-linux",
            "setup_costs": {"generation_development": {"generation_tokens": 123},
                            "freeze_validation": {"cpu_seconds": 0.1}}}
CONFIG = {
    "mode": "authored-validator", "prompt_version": "authored-validator-v1",
    "validator": {"enabled": True, "artifact": "unused-frozen-path", "artifact_id": "frozen-a"},
    "tools": {"version": "authored-tools-v1", "calls": 4, "depth": 4, "minimum_calls": 1, "output_budget": "per-turn"},
    "llm": {"seed": 2300, "model": "test", "think": False, "context": 16384, "tokens": 1024, "seconds": 180},
    "engine": {"path": "unused-engine", "skill": 0, "nodes": 10000},
    "initial_fen": chess.STARTING_FEN, "llm_color": "white", "max_plies": 300,
}


class ValidatorExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frozen = FrozenValidator("frozen-a", SOURCE, MANIFEST)
        self.suite = {"schema_version": 1, "name": "small-heldout", "split": "validation",
                      "cases": heldout_cases()[:2]}
        self.loader = Mock(return_value=self.frozen)
        self.backend = Mock()
        self.backend.prepare.return_value = {"status": "ok", "reason": "ready"}
        self.backend.run_prepared.side_effect = self.execute_fixture
        self.factory = Mock(side_effect=self.make_backend)
        self.batches = []
        self.reports = {}
        self.mismatch_condition = None
        self.failed_condition = None

    def make_backend(self, config):
        # Preparation cannot precede the complete predeclared plan and statuses.
        paths = list((self.root / "validator-experiments").iterdir())
        self.assertEqual(len(paths), 1)
        plan = json.loads((paths[0] / "plan.json").read_text())
        self.assertEqual([row["name"] for row in plan["conditions"]], list(CONDITIONS))
        report = json.loads((paths[0] / "experiment.json").read_text())
        self.assertEqual([row["status"] for row in report["conditions"]], ["not_run"] * 3)
        self.assertTrue(config.enabled)
        return self.backend

    def execute_fixture(self, source, request):
        self.assertEqual(source, SOURCE)
        self.assertEqual(set(request), {"contract_version", "rules_api_version", "position", "history", "candidate"})
        case = next(case for case in self.suite["cases"] if case["request"] == request)
        return {"status": "ok", "reason": "completed", "cpu_seconds": 0.1,
                "findings": {"contract_version": "authored-validator-v1",
                             "facts": [{"id": f"observed-{index}", **fact}
                                       for index, fact in enumerate(case["required_facts"])], "heuristics": []}}

    def batch(self, config, output, pairs, positions):
        self.batches.append(deepcopy(config))
        name = config["mode"]
        directory = output / "batches" / name
        directory.mkdir(parents=True)
        identity = {"model_digest": "changed" if name == self.mismatch_condition else "same-model",
                    "engine_sha256": "same-engine", "python": "same-python"}
        if name == "authored-validator":
            identity["validator_artifact_id"] = "frozen-a"
        jobs = schedule(name, pairs, config["llm"]["seed"], positions)
        failure = name == self.failed_condition
        report = {"batch_id": name, "config": deepcopy(config), "scheduled_games": len(jobs),
                  "finalized_games": 0 if failure else len(jobs), "scored_games": 0 if failure else len(jobs),
                  "score_rate": None if failure else 0.5, "legal_move_rate": 1,
                  "results": {"wins": 0 if failure else pairs, "draws": 0, "losses": 0 if failure else pairs},
                  "llm_latency_seconds": duration_stats([1, 2]),
                  "recorded_token_usage": {"prompt_tokens": 100, "output_tokens": 10},
                  "games": jobs, "runtime_identities": [identity], "warnings": []}
        self.reports[name] = report
        return {"batch_id": name, "status": "failed" if failure else "completed",
                "reason": "validator_timeout" if failure else "schedule_exhausted"}

    def run_comparison(self, **kwargs):
        return run_experiment(deepcopy(CONFIG), self.root, 1, heldout=self.suite,
                              backend_factory=self.factory, artifact_loader=self.loader,
                              batch_runner=self.batch,
                              report_builder=lambda directory: deepcopy(self.reports[directory.name]), **kwargs)

    def test_heldout_cases_are_true_distinct_evaluation_inputs(self):
        suite = load_heldout_suite()
        self.assertEqual(suite["split"], "validation")
        self.assertEqual(len(suite["cases"]), 9)
        self.assertEqual(len(suite["sha256"]), 64)
        self.assertEqual(len({row["id"] for row in suite["cases"]}), 9)
        invalid = deepcopy(self.suite)
        invalid["split"] = "development"
        with self.assertRaises(ValueError):
            validate_heldout_suite(invalid)
        invalid = deepcopy(self.suite)
        invalid["cases"][0]["required_facts"][0]["line"] = ["f2e3"]
        with self.assertRaises(ValueError):
            validate_heldout_suite(invalid)

    def test_disabled_library_and_cli_never_read_files_or_start_backend(self):
        disabled = deepcopy(CONFIG)
        disabled["validator"]["enabled"] = False
        with patch("pathlib.Path.open", side_effect=AssertionError("disabled read")):
            with self.assertRaisesRegex(ValueError, "explicit enablement"):
                run_experiment(disabled, self.root, backend_factory=self.factory, artifact_loader=self.loader)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--validator-artifact", "unused", "--output", str(self.root)])
        self.assertEqual(error.exception.code, 2)
        self.loader.assert_not_called()
        self.factory.assert_not_called()

    def test_all_conditions_use_matched_budgets_and_no_answers_enter_execution(self):
        with patch("chess.engine.SimpleEngine.popen_uci", side_effect=AssertionError("No evaluation engine calls")):
            report = self.run_comparison()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["heldout"]["passed"], 2)
        self.assertEqual([row["status"] for row in report["conditions"]], ["completed"] * 3)
        self.assertEqual(len(report["comparisons"]), 3)
        self.assertEqual(len(self.batches), 3)
        for config in self.batches:
            self.assertEqual(config["llm"], CONFIG["llm"])
            self.assertEqual(config["engine"], CONFIG["engine"])
            self.assertEqual(config["max_plies"], 300)
        self.assertNotIn("validator", self.batches[0])
        self.assertNotIn("tools", self.batches[0])
        self.assertNotIn("validator", self.batches[1])
        self.assertEqual(self.batches[1]["tools"]["calls"], self.batches[2]["tools"]["calls"])
        self.assertEqual(report["costs"]["artifact_setup"]["setup_costs"], MANIFEST["setup_costs"])
        self.assertEqual(report["costs"]["heldout_evaluation"]["execution"]["cpu_seconds"]["total"], 0.2)
        self.assertEqual(self.backend.prepare.call_count, 1)
        self.assertEqual(self.backend.run_prepared.call_count, 2)
        self.assertEqual(self.loader.call_count, 6)  # Initial, two cases, three conditions.

    def test_failed_preflight_keeps_every_condition_and_never_runs_source(self):
        self.backend.prepare.return_value = {"status": "error", "reason": "runtime_unavailable"}
        report = self.run_comparison()
        self.assertEqual((report["status"], report["reason"]), ("failed", "runtime_unavailable"))
        self.assertEqual([row["status"] for row in report["conditions"]], ["not_run"] * 3)
        self.assertEqual([row["reason"] for row in report["conditions"]], ["experiment_stopped"] * 3)
        self.assertEqual(report["heldout"]["tested"], 0)
        self.backend.run_prepared.assert_not_called()
        self.assertEqual(self.batches, [])

    def test_execution_failure_stops_without_repair_and_preserves_partial_costs(self):
        self.backend.run_prepared.side_effect = [self.execute_fixture(SOURCE, self.suite["cases"][0]["request"]),
                                               {"status": "error", "reason": "timeout", "cpu_seconds": None,
                                                "stdout_hex": b"partial".hex()}]
        report = self.run_comparison()
        self.assertEqual((report["status"], report["reason"]), ("failed", "timeout"))
        self.assertEqual(report["heldout"]["passed"], 1)
        self.assertEqual(report["heldout"]["tested"], 2)
        self.assertEqual(report["costs"]["heldout_evaluation"]["execution"]["cpu_seconds"],
                         {"samples": 1, "missing": 1, "total": 0.1})
        self.assertEqual(self.backend.run_prepared.call_count, 2)
        self.assertEqual(self.batches, [])
        self.assertEqual(self.frozen.source, SOURCE)

    def test_missing_required_findings_evaluate_all_checks_then_block_games(self):
        self.backend.run_prepared.side_effect = None
        self.backend.run_prepared.return_value = {"status": "ok", "reason": "completed",
            "findings": {"contract_version": "authored-validator-v1", "facts": [], "heuristics": []}}
        report = self.run_comparison()
        self.assertEqual(report["reason"], "heldout_accuracy_failure")
        self.assertEqual(report["heldout"]["tested"], 2)
        self.assertEqual(report["heldout"]["passed"], 0)
        self.assertEqual(self.batches, [])

    def test_false_reported_fact_is_independently_rejected(self):
        self.backend.run_prepared.side_effect = None
        self.backend.run_prepared.return_value = {"status": "ok", "reason": "completed",
            "findings": {"contract_version": "authored-validator-v1", "facts": [
                {"id": "false", "kind": "candidate_checkmate", "line": ["f2e3"]}], "heuristics": []}}
        report = self.run_comparison()
        self.assertEqual(report["reason"], "invalid_finding")
        self.assertEqual(report["heldout"]["tested"], 1)
        self.assertEqual(report["heldout"]["cases"][1]["status"], "not_run")
        self.assertEqual(self.batches, [])
        costs = report["costs"]["heldout_evaluation"]
        self.assertEqual((costs["successes"], costs["errors"]), (0, 1))
        self.assertEqual(costs["failure_reasons"], {"invalid_finding": 1})
        execution = report["heldout"]["cases"][0]["execution"]
        self.assertEqual(execution["reported_status"], "ok")
        self.assertNotIn("findings", execution)
        self.assertIsNotNone(execution["rejected_findings"])

    def test_backend_exception_marks_attempted_case_failed_with_explicit_result(self):
        self.backend.run_prepared.side_effect = RuntimeError("transport failed")
        report = self.run_comparison()
        self.assertEqual(report["reason"], "runtime_error")
        self.assertEqual([row["status"] for row in report["heldout"]["cases"]], ["failed", "not_run"])
        costs = report["costs"]["heldout_evaluation"]
        self.assertEqual((costs["requests"], costs["results"], costs["errors"], costs["unanswered"]), (1, 1, 1, 0))
        self.assertEqual(costs["execution"]["cpu_seconds"]["missing"], 1)

    def test_interrupted_execution_is_explicit(self):
        self.backend.run_prepared.side_effect = KeyboardInterrupt()
        report = self.run_comparison()
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual([row["status"] for row in report["heldout"]["cases"]], ["interrupted", "not_run"])
        self.assertEqual(report["costs"]["heldout_evaluation"]["failure_reasons"], {"cancelled": 1})

    def test_malformed_backend_return_is_rejected(self):
        self.backend.run_prepared.side_effect = None
        self.backend.run_prepared.return_value = None
        report = self.run_comparison()
        self.assertEqual(report["reason"], "runtime_protocol_error")
        self.assertEqual(report["heldout"]["cases"][0]["status"], "failed")

    def test_measured_cancelled_report_preserves_costs_and_interrupts(self):
        self.backend.run_prepared.side_effect = None
        self.backend.run_prepared.return_value = {"status": "error", "reason": "cancelled", "cpu_seconds": 0.25,
                                                 "cleanup_seconds": 0.1, "cleanup_confirmed": True}
        report = self.run_comparison()
        self.assertEqual((report["status"], report["reason"]), ("interrupted", "user_interrupt"))
        self.assertEqual([row["status"] for row in report["heldout"]["cases"]], ["interrupted", "not_run"])
        self.assertEqual(report["costs"]["heldout_evaluation"]["execution"]["cpu_seconds"]["total"], 0.25)
        self.assertEqual(report["heldout"]["cases"][0]["execution"]["cleanup_seconds"], 0.1)

    def test_unhashable_failure_reason_is_rejected_before_cost_aggregation(self):
        self.backend.run_prepared.side_effect = None
        self.backend.run_prepared.return_value = {"status": "error", "reason": []}
        report = self.run_comparison()
        self.assertEqual(report["reason"], "runtime_protocol_error")
        self.assertEqual(report["costs"]["heldout_evaluation"]["failure_reasons"], {"runtime_protocol_error": 1})

    def test_unexpected_backend_exception_is_recorded(self):
        self.backend.run_prepared.side_effect = AttributeError("unexpected transport response")
        report = self.run_comparison()
        self.assertEqual(report["reason"], "runtime_error")
        self.assertEqual(report["heldout"]["cases"][0]["status"], "failed")

    def test_model_drift_is_an_error_but_intentional_artifact_identity_is_not(self):
        self.mismatch_condition = "rules-tools"
        report = self.run_comparison()
        self.assertEqual(report["reason"], "environment_mismatch")
        self.assertEqual([row["status"] for row in report["conditions"]], ["completed", "failed", "not_run"])

    def test_failed_game_condition_preserves_its_report_and_later_unrun_rows(self):
        self.failed_condition = "rules-tools"
        report = self.run_comparison()
        self.assertEqual(report["reason"], "validator_timeout")
        self.assertEqual(report["conditions"][1]["report"]["scored_games"], 0)
        self.assertEqual([row["status"] for row in report["conditions"]], ["completed", "failed", "not_run"])

    def test_source_is_rechecked_before_each_condition(self):
        self.loader.side_effect = [self.frozen, self.frozen, self.frozen,
                                   FrozenValidator("frozen-a", b"changed", MANIFEST)]
        report = self.run_comparison()
        self.assertEqual(report["reason"], "artifact_mismatch")
        self.assertEqual(self.batches, [])

    def test_baseline_condition_removes_authored_capabilities_without_changing_common_config(self):
        for mode in CONDITIONS:
            with self.subTest(mode=mode):
                config = condition_config(CONFIG, mode)
                self.assertEqual(config["llm"], CONFIG["llm"])
                self.assertEqual("validator" in config, mode == "authored-validator")
                self.assertEqual("tools" in config, mode != "constrained-legal")


if __name__ == "__main__":
    unittest.main()
