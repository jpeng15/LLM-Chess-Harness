"""Opt-in, frozen-validator held-out checks followed by matched game batches."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import chess

from .batch import run_batch, schedule
from .cli import add_game_arguments, game_config, positive
from .compare import compare, markdown as comparison_markdown
from .game import Recorder
from .players import prompt_version
from .report import build_report
from .runner import new_run_id
from .storage import atomic_write, batch_lock
from .suites import load_suite, validate_positions
from .validator_contract import CONTRACT_VERSION, RULES_API_VERSION, ValidatorContractError, validate_request
from .validator_player import configured_artifact
from .validator_reporting import validator_cost_report, validator_metrics_from_records
from .validator_rules import RulesBoard
from .validator_sandbox import DockerSandbox, SandboxConfig
from .validator_verify import verify_result


CONDITIONS = ("constrained-legal", "rules-tools", "authored-validator")
MAX_SUITE_BYTES = 2 * 1024 * 1024
MAX_CASES = 128


class EvaluationFailure(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _request(candidate, fen=chess.STARTING_FEN, moves=()):
    board = chess.Board(fen)
    for move in moves:
        board.push_uci(move)
    return {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "position": {"fen": board.fen(en_passant="fen")},
            "history": {"initial_fen": fen, "moves": list(moves)}, "candidate": candidate}


def _capture(candidate, reply, attacker, victim, square):
    return {"kind": "capture_available", "line": [candidate, reply],
            "capturing_piece": attacker, "captured_piece": victim, "capture_square": square}


def heldout_cases():
    """Evaluation-only fixtures; never used to generate, repair, or freeze source."""
    return [
        {"id": "black-candidate-mate",
         "request": _request("f2h2", "8/8/8/8/8/6k1/5q2/7K b - - 0 1"),
         "required_facts": [{"kind": "candidate_checkmate", "line": ["f2h2"]}]},
        {"id": "white-reply-mate",
         "request": _request("g7g5", moves=["e2e4", "f7f6", "d2d4"]),
         "required_facts": [{"kind": "reply_checkmate", "line": ["g7g5", "d1h5"]}]},
        {"id": "black-queen-offer",
         "request": _request("d8h4", moves=["g2g3", "e7e5", "e2e4"]),
         "required_facts": [_capture("d8h4", "g3h4", "P", "q", "h4")]},
        {"id": "white-en-passant",
         "request": _request("d7d5", "7k/3p4/8/4P3/8/8/7P/7K b - - 0 1"),
         "required_facts": [_capture("d7d5", "e5d6", "P", "p", "d5")]},
        {"id": "black-capturing-promotion",
         "request": _request("h1g1", "7k/8/8/8/8/8/p7/1R5K w - - 0 1"),
         "required_facts": [_capture("h1g1", "a2b1q", "p", "R", "b1")]},
        {"id": "pinned-rook-pinner-capture",
         "request": _request("h8g8", "3r3k/8/8/8/8/8/3R3q/3K4 b - - 0 1"),
         "required_facts": [_capture("h8g8", "d2d8", "R", "r", "d8")]},
        {"id": "black-queenside-castle",
         "request": _request("e8c8", "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1"),
         "required_facts": [_capture("e8c8", "h1h8", "R", "r", "h8")]},
        {"id": "alternate-repetition-history",
         "request": _request("h3g1", moves=["g1h3", "g8h6", "h3g1", "h6g8", "g1h3", "g8h6"]),
         "required_facts": []},
        {"id": "black-candidate-stalemate",
         "request": _request("f2e3", "8/8/8/8/8/6k1/5q2/7K b - - 0 1"),
         "required_facts": []},
    ]


def validate_heldout_suite(suite):
    if (type(suite) is not dict or set(suite) != {"schema_version", "name", "split", "cases"}
            or type(suite["schema_version"]) is not int or suite["schema_version"] != 1 or suite["split"] != "validation"
            or type(suite["name"]) is not str or not suite["name"]
            or type(suite["cases"]) is not list or not 1 <= len(suite["cases"]) <= MAX_CASES
            or len(_canonical(suite)) > MAX_SUITE_BYTES):
        raise ValueError("Expected a bounded, validation-only authored-validator suite")
    identifiers = set()
    for case in suite["cases"]:
        if (type(case) is not dict or set(case) != {"id", "request", "required_facts"}
                or type(case["id"]) is not str or not case["id"] or case["id"] in identifiers
                or type(case["required_facts"]) is not list):
            raise ValueError("Invalid held-out case or duplicate case ID")
        identifiers.add(case["id"])
        request = validate_request(case["request"])
        RulesBoard.from_request(request)
        expected = []
        for index, fact in enumerate(case["required_facts"]):
            if type(fact) is not dict or "id" in fact:
                raise ValueError("Expected facts omit report-local fact IDs")
            expected.append({"id": f"expected-{index}", **fact})
        # Trusted answers must themselves have legal, true witnesses. This never
        # discovers answers with an engine and never sends them to the worker.
        verify_result(request, {"contract_version": CONTRACT_VERSION, "facts": expected, "heuristics": []})
    return deepcopy(suite)


def load_heldout_suite(path=None):
    if path is None:
        suite = {"schema_version": 1, "name": "authored-validator-heldout-v1",
                 "split": "validation", "cases": heldout_cases()}
        raw = _canonical(suite)
    else:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_SUITE_BYTES + 1)
        if len(raw) > MAX_SUITE_BYTES:
            raise ValueError("Held-out suite exceeds its byte limit")

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate held-out suite JSON key")
                value[key] = item
            return value

        suite = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    suite = validate_heldout_suite(suite)
    return {**suite, "sha256": hashlib.sha256(raw).hexdigest()}


def condition_config(config, mode):
    if mode not in CONDITIONS:
        raise ValueError("Unknown frozen comparison condition")
    result = deepcopy(config)
    result.update(mode=mode, prompt_version=prompt_version(mode))
    result.pop("tools", None)
    if mode != "authored-validator":
        result.pop("validator", None)
    if mode in ("rules-tools", "authored-validator"):
        settings = config.get("tools", {})
        result["tools"] = {"version": "authored-tools-v1" if mode == "authored-validator" else "rules-tools-v1",
                           "calls": settings.get("calls", 4), "depth": settings.get("depth", 4),
                           "minimum_calls": 1, "output_budget": "per-turn"}
    return result


def _identity(value):
    return {key: item for key, item in value.items() if key != "validator_artifact_id"}


def _same_artifact(config, frozen, loader):
    current = loader(config)
    if current.artifact_id != frozen.artifact_id or current.source != frozen.source:
        raise EvaluationFailure("artifact_mismatch", "Frozen artifact identity or source changed during evaluation")
    return current


def _check_findings(case, execution):
    if execution.get("status") != "ok":
        return {"passed": False, "reason": execution.get("reason", "execution_error"),
                "missing_facts": None, "findings": None}
    try:
        findings = verify_result(case["request"], execution.get("findings"))
    except ValidatorContractError as exc:
        return {"passed": False, "reason": exc.reason, "path": exc.path,
                "message": str(exc), "missing_facts": None, "findings": None}
    facts = [{key: item for key, item in fact.items() if key != "id"} for fact in findings["facts"]]
    missing = [fact for fact in case["required_facts"] if fact not in facts]
    return {"passed": not missing, "reason": "passed" if not missing else "missing_required_findings",
            "missing_facts": missing, "findings": findings}


def markdown(report):
    lines = ["# Frozen authored-validator comparison", "", f"Status: {report['status']} | Reason: {report['reason']}", "",
             f"Artifact: {report['artifact_id']}", "",
             f"Held-out checks: {report['heldout']['passed']}/{report['heldout']['tested']} passed; "
             f"{report['heldout']['scheduled']} scheduled. Status: {report['heldout']['status']}.", "",
             "| Condition | Status | Reason | W/D/L | Scored / scheduled | Legal rate | Median turn seconds |",
             "|---|---|---|---|---|---|---|"]
    for row in report["conditions"]:
        benchmark = row.get("report", {})
        results = benchmark.get("results", {})
        outcome = "/".join(str(results.get(key, "n/a")) for key in ("wins", "draws", "losses"))
        lines.append(f"| {row['name']} | {row['status']} | {row['reason']} | {outcome} | "
                     f"{benchmark.get('scored_games', 'n/a')} / {benchmark.get('scheduled_games', 'n/a')} | "
                     f"{benchmark.get('legal_move_rate', 'n/a')} | {benchmark.get('llm_latency_seconds', {}).get('median', 'n/a')} |")
    lines += ["", "All conditions share model, thinking, inference budgets, engine settings, seeds, colors, and starting-position schedule.",
              "Rules-tool and authored-validator conditions share the tool-action budget. Tool visibility and the required first action differ.",
              "Held-out failures never trigger source repair, refreezing, generation, or fallback assistance. Unrun conditions stay visible.",
              "Artifact setup costs are recorded once; held-out execution, per-run preflight, and game execution are separate evaluation costs.",
              "Worker timings overlap with turn latency. Costs with missing measurements remain unavailable.",
              "Changed final choices are observational. Small samples, differing trajectories, and uncontrolled Stockfish skill randomness limit strength claims.",
              "No engine analysis is performed by this command. Optional move-quality analysis is a separate, post-game command."]
    if report.get("error"):
        lines += ["", report["error"]]
    lines += ["", *[f"- {warning}" for warning in report["warnings"]]]
    return "\n".join(lines) + "\n"


def run_experiment(config, output, pairs=1, *, heldout=None, positions=None,
                   backend_factory=DockerSandbox, artifact_loader=configured_artifact,
                   batch_runner=run_batch, report_builder=build_report):
    """Run a predeclared, fail-fast experiment; dependencies are injectable for tests."""
    if (config.get("mode") != "authored-validator"
            or config.get("validator", {}).get("enabled") is not True):
        raise ValueError("Authored-validator experiments require explicit enablement")
    if type(pairs) is not int or pairs <= 0:
        raise ValueError("pairs must be a positive integer")
    frozen = artifact_loader(config)
    artifact_manifest = frozen.manifest
    suite = deepcopy(heldout) if heldout is not None else load_heldout_suite()
    suite_body = {key: value for key, value in suite.items() if key != "sha256"}
    validate_heldout_suite(suite_body)
    # Hash exactly the public, predeclared case data even for an injected suite.
    suite["content_sha256"] = hashlib.sha256(_canonical(suite_body)).hexdigest()
    if positions is not None:
        validate_positions(positions)
        if any(position["split"] != "validation" for position in positions):
            raise ValueError("Comparison starting fixtures must be validation positions")
    jobs = schedule("predeclared", pairs, config["llm"]["seed"], positions)
    controls = [{key: value for key, value in job.items() if key != "run_id"} for job in jobs]
    conditions = [{"name": name, "config": condition_config(config, name)} for name in CONDITIONS]
    artifact = {"artifact_id": frozen.artifact_id, "source_sha256": hashlib.sha256(frozen.source).hexdigest(),
                "source_text": frozen.source.decode("utf-8"), "manifest": artifact_manifest,
                "setup_costs": deepcopy(artifact_manifest["setup_costs"])}
    recorder = Recorder(Path(output) / "validator-experiments" / new_run_id())
    plan = {"schema_version": 1, "kind": "frozen-validator-comparison", "created_at": datetime.now(timezone.utc).isoformat(),
            "validator_artifact": artifact, "heldout": suite, "conditions": conditions,
            "schedule": controls, "pairs": pairs, "positions": positions,
            "analysis": {"enabled": False, "policy": "separate post-game analysis only"},
            "policy": {"inference_budgets": "matched", "on_execution_failure": "stop and preserve unrun rows",
                       "on_accuracy_failure": "finish held-out checks, then stop before games",
                       "source_repair": False, "automatic_retry": False}}
    report = {"schema_version": 1, "kind": "frozen-validator-comparison", "directory": str(recorder.directory),
              "artifact_id": frozen.artifact_id, "status": "running", "reason": "heldout_pending", "warnings": [],
              "heldout": {"status": "pending", "scheduled": len(suite["cases"]), "tested": 0, "passed": 0,
                          "cases": [{"id": case["id"], "status": "not_run", "reason": "pending"} for case in suite["cases"]]},
              "conditions": [{"name": item["name"], "status": "not_run", "reason": "pending"} for item in conditions],
              "comparisons": []}
    recorder.write("plan.json", plan)
    heldout_events, game_metrics = [], []

    def checkpoint():
        metrics = validator_metrics_from_records({"validator_artifact": artifact}, heldout_events)
        report["costs"] = {"artifact_setup": {"artifact_id": frozen.artifact_id, "setup_costs": artifact["setup_costs"]},
                           "heldout_evaluation": {key: value for key, value in metrics.items() if key != "artifacts"},
                           "authored_game_evaluation": validator_cost_report([], game_metrics)["all_attempts"],
                           "condition_inference": [{"name": row["name"], "status": row["status"],
                               "recorded_token_usage": row.get("report", {}).get("recorded_token_usage"),
                               "turn_latency_seconds": row.get("report", {}).get("llm_latency_seconds")}
                               for row in report["conditions"]]}
        recorder.write("experiment.json", report)
        atomic_write(recorder.directory / "experiment.md", markdown(report))

    def event(kind, **fields):
        row = {"type": kind, **fields}
        heldout_events.append(row)
        recorder.event(kind, **fields)

    checkpoint()  # All conditions and held-out cases are visible before runtime access.
    try:
        backend = backend_factory(SandboxConfig(enabled=True, image=artifact_manifest["image"],
                                               docker_context=artifact_manifest["docker_context"]))
        start = time.monotonic()
        readiness = backend.prepare()
        if (not isinstance(readiness, dict) or readiness.get("status") not in ("ok", "error")
                or not isinstance(readiness.get("reason"), str) or not readiness["reason"].strip()):
            readiness = {"status": "error", "reason": "runtime_protocol_error", "message": "Invalid sandbox preparation report"}
        event("validator_preflight", report=readiness, elapsed_seconds=time.monotonic() - start)
        report["heldout"]["preflight"] = readiness
        if readiness.get("status") != "ok":
            raise EvaluationFailure(readiness.get("reason", "preflight_failed"), "Held-out preflight failed")
        report["heldout"]["status"] = "running"
        for index, (case, row) in enumerate(zip(suite["cases"], report["heldout"]["cases"]), 1):
            row.update(status="running", reason="artifact_check")
            checkpoint()
            current = _same_artifact(config, frozen, artifact_loader)
            request = deepcopy(case["request"])
            event("validator_requested", call=index, artifact_id=frozen.artifact_id,
                  candidate=request["candidate"], input=request)
            row["reason"] = "executing"
            checkpoint()
            interrupted = None
            try:
                execution = backend.run_prepared(current.source, deepcopy(request))
            except KeyboardInterrupt as exc:
                interrupted = exc
                execution = {"status": "error", "reason": "cancelled", "message": "Held-out execution interrupted"}
            except Exception as exc:
                execution = {"status": "error", "reason": getattr(exc, "reason", "runtime_error"), "message": str(exc)}
            if (not isinstance(execution, dict) or execution.get("status") not in ("ok", "error")
                    or not isinstance(execution.get("reason"), str) or not execution["reason"].strip()
                    or execution["status"] == "ok" and execution["reason"] != "completed"):
                execution = {"status": "error", "reason": "runtime_protocol_error", "message": "Invalid sandbox execution report"}
            if execution["status"] == "error" and execution["reason"] == "cancelled" and interrupted is None:
                # The production backend converts interruption into a measured
                # report after cleaning up the worker; preserve both semantics.
                interrupted = KeyboardInterrupt()
            checked = _check_findings(case, execution)
            if execution["status"] == "ok" and checked["reason"] not in ("passed", "missing_required_findings"):
                execution = deepcopy(execution)
                execution.update(status="error", reported_status="ok", reason=checked["reason"],
                                 rejected_findings=execution.pop("findings", None), verification=checked)
            event("validator_result", call=index, artifact_id=frozen.artifact_id,
                  candidate=request["candidate"], input=request, execution=execution)
            row.update(status="interrupted" if interrupted else "passed" if checked["passed"] else "failed", reason=checked["reason"],
                       execution=execution, verification=checked)
            report["heldout"]["tested"] += 1
            report["heldout"]["passed"] += int(checked["passed"])
            checkpoint()
            if interrupted is not None:
                raise interrupted
            if checked["reason"] not in ("passed", "missing_required_findings"):
                raise EvaluationFailure(checked["reason"], "Held-out execution or verification failed")
        if report["heldout"]["passed"] != report["heldout"]["scheduled"]:
            raise EvaluationFailure("heldout_accuracy_failure", "Held-out accuracy checks failed; artifact remains unchanged")
        report["heldout"]["status"] = "passed"
        expected_identity = None
        for settings, row in zip(conditions, report["conditions"]):
            _same_artifact(config, frozen, artifact_loader)
            row.update(status="running", reason="batch_running")
            checkpoint()
            progress = batch_runner(settings["config"], Path(output), pairs, positions)
            batch_directory = Path(output) / "batches" / progress["batch_id"]
            row.update(batch_id=progress["batch_id"], batch_directory=str(batch_directory),
                       status="completed" if progress["status"] == "completed" else "failed",
                       reason=progress.get("reason", progress["status"]))
            with batch_lock(batch_directory):
                benchmark = report_builder(batch_directory)
            row["report"] = benchmark
            recorder.write(settings["name"] + ".json", benchmark)
            if benchmark.get("validator_costs"):
                costs = benchmark["validator_costs"]
                game_metrics.append({**costs["all_attempts"], "artifacts": costs["artifacts"]})
            checkpoint()
            if progress["status"] != "completed":
                raise EvaluationFailure(row["reason"], f"Condition {settings['name']} failed")
            actual_controls = [{key: game[key] for key in ("index", "pair", "llm_color", "seed")}
                               | ({"position": game["position"]} if "position" in game else {})
                               for game in benchmark["games"]]
            if actual_controls != controls:
                row.update(status="failed", reason="schedule_mismatch")
                raise EvaluationFailure("schedule_mismatch", "Condition schedule differs from the predeclared plan")
            identities = [_identity(identity) for identity in benchmark["runtime_identities"]]
            if len(identities) != 1 or expected_identity is not None and identities[0] != expected_identity:
                row.update(status="failed", reason="environment_mismatch")
                raise EvaluationFailure("environment_mismatch", "Model/engine/runtime identity differs between conditions or is unavailable")
            expected_identity = identities[0]
        for index, left in enumerate(report["conditions"]):
            for right in report["conditions"][index + 1:]:
                inputs = []
                for item in (left, right):
                    value = deepcopy(item["report"])
                    value["kind"] = "games"
                    value["runtime_identities"] = [_identity(identity) for identity in value["runtime_identities"]]
                    inputs.append(value)
                comparison = compare(*inputs)
                name = left["name"] + "-vs-" + right["name"]
                recorder.write(name + ".json", comparison)
                atomic_write(recorder.directory / (name + ".md"), comparison_markdown(comparison))
                report["comparisons"].append(name)
        report.update(status="completed", reason="schedule_exhausted")
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        failure_reason = getattr(exc, "reason", "evaluation_failed")
        if not isinstance(failure_reason, str) or not failure_reason.strip():
            failure_reason = "evaluation_failed"
        report.update(status="interrupted" if interrupted else "failed",
                      reason="user_interrupt" if interrupted else failure_reason, error=str(exc))
        if report["heldout"]["status"] != "passed":
            report["heldout"]["status"] = "interrupted" if interrupted else "failed"
        for row in report["heldout"]["cases"] + report["conditions"]:
            if row["status"] == "not_run":
                row["reason"] = "experiment_stopped"
            elif row["status"] == "running":
                row.update(status="interrupted" if interrupted else "failed", reason=report["reason"])
    checkpoint()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_game_arguments(parser, include_color=False)
    parser.set_defaults(think=False, tokens=1024, context=16384)
    parser.add_argument("--pairs", type=positive, default=1)
    parser.add_argument("--positions", type=Path, help="optional game starting suite; validation split only")
    parser.add_argument("--heldout-suite", type=Path, help="optional separate functional validation suite")
    args = parser.parse_args(argv)
    if not args.enable_authored_validators:
        parser.error("Explicitly pass --enable-authored-validators to run this comparison")
    if args.validator_artifact is None:
        parser.error("--validator-artifact is required")
    if args.mode != "unassisted":
        parser.error("Comparison modes are fixed; omit --mode")
    if args.positions and args.fen != chess.STARTING_FEN:
        parser.error("--positions and a custom --fen cannot be combined")
    try:
        args.mode = "authored-validator"
        config = game_config(parser, args)
        suite = load_heldout_suite(args.heldout_suite)
        positions = load_suite(args.positions, "validation")["positions"] if args.positions else None
        report = run_experiment(config, args.output, args.pairs, heldout=suite, positions=positions)
        print(f"Frozen comparison {report['status']}: {Path(report['directory']) / 'experiment.md'}")
        return 0 if report["status"] == "completed" else 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"Frozen comparison error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
