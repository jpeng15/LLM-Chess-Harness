from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import chess

from chess_harness.batch import new_progress, schedule
from chess_harness.game import Recorder
from chess_harness.report import build_report, markdown
from chess_harness.validator_reporting import (
    aggregate_validator_metrics, measurement, validator_cost_report, validator_metrics,
    validator_metrics_from_records,
)


ARTIFACT = {
    "artifact_id": "artifact-a", "source_sha256": "a" * 64,
    "source_text": "def validate(position, history, candidate):\n    return {}\n",
    "setup_costs": {"generation": {"attempts": 2, "prompt_tokens": 100, "output_tokens": 50,
                                  "wall_seconds": 3.0},
                    "development_execution": {"cpu_seconds": 0.3},
                    "freeze": {"cpu_seconds": 0.1}},
    "manifest": {"contract_version": "authored-validator-v1"},
}


def trace(*, cpu=0.1, status="ok", reason="completed"):
    return [
        {"type": "validator_preflight", "report": {"status": "ok"}, "elapsed_seconds": 1.5},
        {"type": "move_requested", "fen": chess.STARTING_FEN, "ply": 1, "player": "Qwen"},
        {"type": "model_call_response", "call": 1, "raw": {"prompt_eval_count": 10, "eval_count": 4},
         "elapsed_seconds": 0.2},
        {"type": "validator_requested", "call": 1, "artifact_id": "artifact-a", "candidate": "e2e4"},
        {"type": "validator_result", "call": 1, "artifact_id": "artifact-a", "candidate": "e2e4",
         "execution": {"status": status, "reason": reason, "cpu_seconds": cpu, "total_wall_seconds": 0.8,
                       "execution_wall_seconds": 0.6, "worker_wall_seconds": 0.3,
                       "cleanup_seconds": 0.1, "runtime_check_seconds": 0.1,
                       "peak_memory_bytes": 1024, "stdout_bytes": 75, "stderr_bytes": 0}},
        # The final reply repeats the model's last raw response. It is not a new
        # call and must not be charged again by the validator cost breakdown.
        {"type": "move_response", "raw": {"prompt_eval_count": 10, "eval_count": 4}, "elapsed_seconds": 2},
        {"type": "move_applied", "uci": "g1f3"},
    ]


class ValidatorReportingTests(unittest.TestCase):
    def test_cost_buckets_remain_separate_and_final_reply_is_not_double_counted(self):
        records = trace()
        before = deepcopy(records)
        metrics = validator_metrics_from_records({"validator_artifact": ARTIFACT}, records)
        self.assertEqual((metrics["requests"], metrics["results"], metrics["successes"]), (1, 1, 1))
        self.assertEqual(metrics["execution"]["cpu_seconds"], {"samples": 1, "missing": 0, "total": 0.1})
        self.assertEqual(metrics["execution"]["total_wall_seconds"]["total"], 0.8)
        self.assertEqual(metrics["initialization"]["elapsed_seconds"]["total"], 1.5)
        self.assertEqual(metrics["model_inference"]["elapsed_seconds"]["total"], 0.2)
        self.assertEqual(metrics["model_inference"]["prompt_tokens"]["total"], 10)
        self.assertEqual(metrics["model_inference"]["output_tokens"]["total"], 4)
        self.assertEqual(metrics["choice_observations"], {
            "eligible_turns": 1, "different_from_last_validated_candidate": 1})
        self.assertEqual(records, before)
        metrics["artifacts"][0]["setup_costs"]["generation"]["output_tokens"] = 999
        self.assertEqual(ARTIFACT["setup_costs"]["generation"]["output_tokens"], 50)

    def test_unknown_measurements_are_not_zero_and_peak_memory_is_not_added(self):
        first = validator_metrics_from_records({"validator_artifact": ARTIFACT}, trace(cpu=None))
        self.assertEqual(first["execution"]["cpu_seconds"], {"samples": 0, "missing": 1, "total": None})
        second = validator_metrics_from_records({"validator_artifact": ARTIFACT}, trace(cpu=0.4))
        combined = aggregate_validator_metrics([first, second])
        self.assertEqual(combined["execution"]["cpu_seconds"], {"samples": 1, "missing": 1, "total": 0.4})
        self.assertEqual(combined["execution"]["peak_memory_bytes"], {"samples": 2, "missing": 0, "max": 1024})
        self.assertEqual(measurement([None, True, -1, float("nan"), float("inf")]),
                         {"samples": 0, "missing": 5, "total": None})

    def test_unanswered_calls_and_failure_reasons_are_preserved_across_turns(self):
        records = trace(status="error", reason="timeout")[:-2]
        records += [
            {"type": "move_requested", "ply": 3},
            {"type": "validator_requested", "call": 1, "artifact_id": "artifact-a", "candidate": "d2d4"},
        ]
        metrics = validator_metrics_from_records({"validator_artifact": ARTIFACT}, records)
        self.assertEqual((metrics["requests"], metrics["results"], metrics["errors"], metrics["unanswered"]), (2, 1, 1, 1))
        self.assertEqual(metrics["failure_reasons"], {"timeout": 1})
        self.assertEqual(metrics["execution"]["cpu_seconds"]["missing"], 1)
        self.assertEqual(metrics["choice_observations"]["eligible_turns"], 0)
        self.assertEqual(metrics["warnings"], [])

    def test_artifact_setup_is_deduplicated_and_conflicting_ledgers_fail(self):
        metrics = validator_metrics_from_records({"validator_artifact": ARTIFACT}, trace())
        report = validator_cost_report([metrics, metrics], [metrics, metrics, metrics])
        self.assertEqual(len(report["artifacts"]), 1)
        self.assertEqual(report["artifacts"][0]["setup_costs"], ARTIFACT["setup_costs"])
        self.assertEqual(report["latest_attempts"]["results"], 2)
        self.assertEqual(report["all_attempts"]["results"], 3)
        for field in ("setup_costs", "source_sha256"):
            with self.subTest(field=field):
                changed = deepcopy(metrics)
                changed["artifacts"][0][field] = {} if field == "setup_costs" else "b" * 64
                with self.assertRaisesRegex(ValueError, "Contradicting"):
                    aggregate_validator_metrics([metrics, changed])

    def test_mismatched_event_artifact_is_rejected_and_old_runs_remain_optional(self):
        records = trace()
        records[3]["artifact_id"] = "different-artifact"
        with self.assertRaisesRegex(ValueError, "manifest artifact identity"):
            validator_metrics_from_records({"validator_artifact": ARTIFACT}, records)
        self.assertIsNone(validator_metrics_from_records({"config": {"mode": "unassisted"}}, [
            {"type": "move_requested"}, {"type": "move_response", "raw": {"eval_count": 3}},
        ]))

    def test_recovered_attempt_costs_include_failed_work_but_setup_only_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "batches" / "test-costs"
            batch = Recorder(directory)
            config = {"mode": "authored-validator", "prompt_version": "authored-validator-v1",
                      "llm": {"seed": 7, "model": "test"}, "initial_fen": chess.STARTING_FEN,
                      "llm_color": "white"}
            plan = {"schema_version": 1, "batch_id": "test-costs", "config": config,
                    "scheduling": {"pairs": 1}, "games": schedule("test-costs", 1, 7)}
            batch.write("batch.json", plan)
            batch.write("progress.json", new_progress(plan))
            job = plan["games"][0]
            for number, cpu in ((1, 2.0), (2, 3.0)):
                run_id = job["run_id"] + ("-attempt-0002" if number == 2 else "")
                recorder = Recorder(root / run_id)
                recorder.write("manifest.json", {"config": config, "batch": {"id": "test-costs"},
                                                   "validator_artifact": ARTIFACT})
                rows = trace(cpu=cpu, status="error" if number == 1 else "ok",
                             reason="timeout" if number == 1 else "completed")
                if number == 1:
                    rows = rows[:-2] + [{"type": "move_failed", "reason": "validator_timeout",
                                        "elapsed_seconds": 2, "raw": {}}]
                for row in rows:
                    row = dict(row)
                    recorder.event(row.pop("type"), **row)
                summary = {"status": "infrastructure_failure" if number == 1 else "truncated",
                           "reason": "validator_timeout" if number == 1 else "max_plies",
                           "result": "*", "plies": 0 if number == 1 else 1}
                recorder.event("game_finished", **summary)
                recorder.write("summary.json", summary)
            report = build_report(directory)
            costs = report["validator_costs"]
            self.assertEqual(costs["latest_attempts"]["execution"]["cpu_seconds"]["total"], 3)
            self.assertEqual(costs["all_attempts"]["execution"]["cpu_seconds"]["total"], 5)
            self.assertEqual(costs["latest_attempts"]["errors"], 0)
            self.assertEqual(costs["all_attempts"]["failure_reasons"], {"timeout": 1})
            self.assertEqual(costs["all_attempts"]["initialization"]["invocations"], 2)
            self.assertEqual(len(costs["artifacts"]), 1)
            self.assertEqual(report["recorded_token_usage"]["output_tokens"], 4)
            self.assertEqual(costs["all_attempts"]["model_inference"]["output_tokens"]["total"], 8)
            self.assertIn("do not add them together", markdown(report))
            self.assertIn("All attempts, including superseded", markdown(report))
            self.assertEqual(report["attempts"]["superseded"], 1)

    def test_incomplete_event_append_does_not_invent_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = Recorder(Path(temporary) / "run")
            recorder.write("manifest.json", {"validator_artifact": ARTIFACT})
            recorder.event("validator_requested", call=1, artifact_id="artifact-a", candidate="e2e4")
            with (recorder.directory / "events.jsonl").open("ab") as output:
                output.write(b'{"type":"validator_result",')
            costs = validator_metrics(recorder.directory)
            self.assertEqual((costs["requests"], costs["results"], costs["unanswered"]), (1, 0, 1))
            self.assertIsNone(costs["execution"]["cpu_seconds"]["total"])
            self.assertEqual(costs["execution"]["cpu_seconds"]["missing"], 1)

    def test_unanswered_model_calls_report_unknown_cost_instead_of_zero(self):
        metrics = validator_metrics_from_records({"validator_artifact": ARTIFACT}, [
            {"type": "move_requested", "ply": 1},
            {"type": "model_call_requested", "call": 1},
            {"type": "move_failed", "reason": "timeout", "elapsed_seconds": 2},
        ])
        self.assertEqual(metrics["model_inference"]["unanswered"], 1)
        self.assertEqual(metrics["model_inference"]["output_tokens"],
                         {"samples": 0, "missing": 1, "total": None})


if __name__ == "__main__":
    unittest.main()
