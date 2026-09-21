"""Read-only authored-validator costs, with explicit missing measurements.

Game scoring still uses the latest attempt. Actual spending can additionally be
summed across every retained attempt, without charging artifact setup per game.
"""

from collections import Counter
from copy import deepcopy
import math

from .storage import events, read_json


EXECUTION_FIELDS = (
    "cpu_seconds", "worker_wall_seconds", "execution_wall_seconds", "total_wall_seconds",
    "cleanup_seconds", "runtime_check_seconds", "artifact_check_seconds", "stdout_bytes", "stderr_bytes",
    "peak_memory_bytes",
)


def measurement(values, *, maximum=False):
    known = [value for value in values
             if type(value) in (int, float) and math.isfinite(value) and value >= 0]
    key = "max" if maximum else "total"
    value = max(known) if maximum and known else sum(known) if known else None
    return {"samples": len(known), "missing": len(values) - len(known), key: value}


def _artifact(manifest, records):
    declared = manifest.get("validator_artifact")
    identifiers = {record["artifact_id"] for record in records
                   if record["type"] in ("validator_requested", "validator_result") and record.get("artifact_id")}
    if declared is not None:
        if not isinstance(declared, dict) or not isinstance(declared.get("artifact_id"), str):
            raise ValueError("Invalid validator artifact metadata")
        if identifiers - {declared["artifact_id"]}:
            raise ValueError("Validator events differ from the manifest artifact identity")
        frozen = declared.get("manifest", {})
        return [{"artifact_id": declared["artifact_id"],
                 "source_sha256": declared.get("source_sha256", frozen.get("source_sha256")),
                 "setup_costs": deepcopy(declared.get("setup_costs", frozen.get("setup_costs")))}]
    return [{"artifact_id": identity, "source_sha256": None, "setup_costs": None}
            for identity in sorted(identifiers)]


def validator_metrics_from_records(manifest, records):
    """Summarize one already-read snapshot without changing files or game state."""
    if not manifest.get("validator_artifact") and not any(
            record["type"] in ("validator_requested", "validator_result", "validator_preflight")
            for record in records):
        return None
    requests, executions, initialization, inference = [], [], [], []
    pending, model_pending = Counter(), Counter()
    model_requests = 0
    failures = Counter()
    warnings = []
    turn, last_candidate = 0, None
    eligible, changed = 0, 0
    for record in records:
        kind = record["type"]
        if kind == "move_requested":
            turn += 1
            last_candidate = None
        elif kind == "validator_requested":
            key = (turn, record.get("call"))
            pending[key] += 1
            requests.append(record)
            if pending[key] > 1:
                warnings.append("Duplicate validator request identifiers are present.")
        elif kind == "validator_result":
            key = (turn, record.get("call"))
            if pending[key]:
                pending[key] -= 1
            else:
                warnings.append("A validator result has no matching request.")
            execution = record.get("execution", {})
            if not isinstance(execution, dict):
                raise ValueError("Validator execution record is not an object")
            executions.append(execution)
            if execution.get("status") == "ok":
                last_candidate = record.get("candidate")
            else:
                failures[execution.get("reason", "unknown")] += 1
        elif kind == "validator_preflight":
            initialization.append(record)
        elif kind == "model_call_requested":
            model_requests += 1
            model_pending[(turn, record.get("call"))] += 1
        elif kind == "model_call_response":
            key = (turn, record.get("call"))
            if model_pending[key]:
                model_pending[key] -= 1
            inference.append(record)
        elif kind == "move_applied" and last_candidate is not None:
            eligible += 1
            changed += int(record.get("uci") != last_candidate)
            last_candidate = None
    unanswered, model_unanswered = sum(pending.values()), sum(model_pending.values())
    return {
        "schema_version": 1, "runs": 1, "artifacts": _artifact(manifest, records),
        "requests": len(requests), "results": len(executions),
        "successes": sum(row.get("status") == "ok" for row in executions),
        "errors": sum(failures.values()), "unanswered": unanswered,
        "failure_reasons": dict(failures),
        "execution": {field: measurement([row.get(field) for row in executions] + [None] * unanswered,
                                           maximum=field == "peak_memory_bytes")
                      for field in EXECUTION_FIELDS},
        "initialization": {
            "invocations": len(initialization),
            "errors": sum(row.get("report", {}).get("status") != "ok" for row in initialization),
            "elapsed_seconds": measurement([row.get("elapsed_seconds") for row in initialization]),
        },
        "model_inference": {
            "requests": model_requests, "responses": len(inference), "unanswered": model_unanswered,
            "elapsed_seconds": measurement([row.get("elapsed_seconds") for row in inference] + [None] * model_unanswered),
            "prompt_tokens": measurement([row.get("raw", {}).get("prompt_eval_count")
                                           if isinstance(row.get("raw"), dict) else None for row in inference] + [None] * model_unanswered),
            "output_tokens": measurement([row.get("raw", {}).get("eval_count")
                                           if isinstance(row.get("raw"), dict) else None for row in inference] + [None] * model_unanswered),
        },
        "choice_observations": {"eligible_turns": eligible, "different_from_last_validated_candidate": changed},
        "warnings": list(dict.fromkeys(warnings)),
    }


def validator_metrics(directory):
    """Read one saved attempt; old runs without validator activity return None."""
    try:
        manifest = read_json(directory / "manifest.json")
    except FileNotFoundError:
        manifest = {}
    return validator_metrics_from_records(manifest, events(directory))


def _merge_measurements(rows, *, maximum=False):
    key = "max" if maximum else "total"
    known = [row[key] for row in rows if row[key] is not None]
    return {"samples": sum(row["samples"] for row in rows),
            "missing": sum(row["missing"] for row in rows),
            key: max(known) if maximum and known else sum(known) if known else None}


def _deduplicate_artifacts(rows):
    artifacts = {}
    for row in rows:
        for artifact in row["artifacts"]:
            identifier = artifact["artifact_id"]
            if identifier not in artifacts:
                artifacts[identifier] = deepcopy(artifact)
                continue
            existing = artifacts[identifier]
            for field in ("source_sha256", "setup_costs"):
                value = artifact[field]
                if existing[field] is not None and value is not None and existing[field] != value:
                    raise ValueError(f"Contradicting {field} records for validator artifact {identifier}")
                if existing[field] is None:
                    existing[field] = deepcopy(value)
    return [artifacts[key] for key in sorted(artifacts)]


def aggregate_validator_metrics(rows):
    """Combine attempts; keep each immutable artifact's setup ledger once."""
    rows = [row for row in rows if row is not None]
    reasons = Counter()
    for row in rows:
        reasons.update(row["failure_reasons"])
    return {
        "schema_version": 1, "artifacts": _deduplicate_artifacts(rows),
        **{field: sum(row[field] for row in rows)
           for field in ("runs", "requests", "results", "successes", "errors", "unanswered")},
        "failure_reasons": dict(reasons),
        "execution": {field: _merge_measurements([row["execution"][field] for row in rows],
                                                 maximum=field == "peak_memory_bytes")
                      for field in EXECUTION_FIELDS},
        "initialization": {
            **{field: sum(row["initialization"][field] for row in rows) for field in ("invocations", "errors")},
            "elapsed_seconds": _merge_measurements([row["initialization"]["elapsed_seconds"] for row in rows]),
        },
        "model_inference": {
            **{field: sum(row["model_inference"][field] for row in rows)
               for field in ("requests", "responses", "unanswered")},
            **{field: _merge_measurements([row["model_inference"][field] for row in rows])
               for field in ("elapsed_seconds", "prompt_tokens", "output_tokens")},
        },
        "choice_observations": {
            field: sum(row["choice_observations"][field] for row in rows)
            for field in ("eligible_turns", "different_from_last_validated_candidate")},
        "warnings": list(dict.fromkeys(warning for row in rows for warning in row["warnings"])),
    }


def validator_cost_report(latest, all_attempts):
    latest = aggregate_validator_metrics(latest)
    actual = aggregate_validator_metrics(all_attempts)
    # Both views share one setup ledger. Neither duplicates setup per game.
    artifacts = _deduplicate_artifacts([latest, actual])
    latest.pop("artifacts")
    actual.pop("artifacts")
    return {"schema_version": 1, "artifacts": artifacts,
            "latest_attempts": latest, "all_attempts": actual,
            "definitions": {
                "setup": "Artifact generation, development, and freeze costs are recorded once per artifact ID; missing setup costs are unavailable.",
                "initialization": "Validator preflight is evaluation initialization overhead charged for every actual run, separately from artifact setup and turn execution.",
                "overlap": "Worker, execution, cleanup, runtime-check and total wall times overlap; do not add them together or add them to end-to-end LLM turn latency. Model-call tokens are a breakdown of recorded usage, not extra tokens.",
                "attempts": "Latest attempts match game scoring; all attempts include superseded and failed runs and describe actual recorded spending.",
                "missing": "Totals sum available measurements only; samples and missing counts identify coverage, including requested calls with no result. No samples means unavailable, not zero; peak memory is a maximum, not a sum.",
                "choices": "A final move differing from the last validated candidate is an observation, not evidence the validator caused the change.",
            }}
