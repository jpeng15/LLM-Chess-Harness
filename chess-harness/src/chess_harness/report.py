"""Summarize one saved batch without counting retry attempts as extra games."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys

import chess

from .batch import FINISHED, load_batch, reconcile
from .game import Recorder
from .storage import atomic_write, batch_lock, events, read_json, runtime_identity
from .validator_reporting import validator_cost_report, validator_metrics


def duration_stats(values):
    ordered = sorted(values)
    return {"samples": len(values), "total": sum(values),
            "mean": statistics.mean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "p95": ordered[math.ceil(0.95 * len(ordered)) - 1] if values else None,
            "min": ordered[0] if values else None, "max": ordered[-1] if values else None}


def turn_metrics(directory, llm_color):
    counts = Counter(requested=0, responses=0, applied=0, timeouts=0, failed=0)
    durations = []
    usage = Counter(prompt_tokens=0, output_tokens=0, responses_with_usage=0)
    is_llm = False
    detailed_calls = False

    def add_usage(raw):
        if isinstance(raw, dict):
            present = False
            for field, key in (("prompt_eval_count", "prompt_tokens"), ("eval_count", "output_tokens")):
                value = raw.get(field)
                if type(value) is int and value >= 0:
                    usage[key] += value
                    present = True
            usage["responses_with_usage"] += int(present)

    for event in events(directory):
        kind = event["type"]
        if kind == "move_requested":
            detailed_calls = False
            is_llm = chess.Board(event["fen"]).turn == (llm_color == "white")
            if is_llm:
                counts["requested"] += 1
        elif is_llm:
            if kind == "model_call_requested":
                detailed_calls = True
            elif kind == "model_call_response":
                detailed_calls = True
                add_usage(event.get("raw"))
            if kind in ("move_response", "move_timeout", "move_failed"):
                counts[{"move_response": "responses", "move_timeout": "timeouts", "move_failed": "failed"}[kind]] += 1
                seconds = event.get("elapsed_seconds")
                if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
                    durations.append(seconds)
                if not detailed_calls:
                    add_usage(event.get("raw"))
            elif kind == "move_applied":
                counts["applied"] += 1
    counts["unanswered"] = counts["requested"] - counts["responses"] - counts["timeouts"] - counts["failed"]
    return dict(counts), durations, dict(usage)


def build_report(directory):
    """Caller holds the batch lock; recovery is in-memory and does not resume play."""
    plan, progress = load_batch(directory)
    output = directory.parent.parent
    reconcile(plan, progress, output)
    statuses, reasons, attempt_statuses = Counter(), Counter(), Counter()
    results = Counter(wins=0, draws=0, losses=0)
    colors = {color: Counter(wins=0, draws=0, losses=0, scored=0) for color in ("white", "black")}
    counts = Counter(requested=0, responses=0, applied=0, timeouts=0, failed=0, unanswered=0)
    usage = Counter(prompt_tokens=0, output_tokens=0, responses_with_usage=0)
    durations, rows, identities = [], [], []
    validator_latest, validator_attempts, validator_cache = [], [], {}

    def validator_costs(run_id):
        if run_id not in validator_cache:
            validator_cache[run_id] = validator_metrics(output / run_id)
        return validator_cache[run_id]

    for job, entry in zip(plan["games"], progress["games"]):
        summary = entry.get("summary", {})
        status = entry["status"]
        reason = summary.get("reason", status)
        statuses[status] += 1
        reasons[reason] += 1
        attempts = entry.get("attempts", [])
        attempt_statuses.update(a["status"] for a in attempts)
        for attempt in attempts:
            costs = validator_costs(attempt["run_id"])
            if costs is not None:
                validator_attempts.append(costs)
        row = {"index": job["index"], "pair": job["pair"], "llm_color": job["llm_color"], "seed": job["seed"],
               "run_id": entry.get("current_run_id"), "attempt_count": len(attempts),
               "status": status, "reason": reason, "result": summary.get("result", "*"),
               "plies": summary.get("plies"), "llm_outcome": None}
        if "position" in job:
            row["position"] = job["position"]
        result = row["result"]
        if status in ("completed", "forfeit"):
            if result not in ("1-0", "0-1", "1/2-1/2"):
                raise ValueError(f"Scored game {job['index']} has invalid result {result!r}")
            win = "1-0" if job["llm_color"] == "white" else "0-1"
            outcome = "draws" if result == "1/2-1/2" else "wins" if result == win else "losses"
            row["llm_outcome"] = outcome
            results[outcome] += 1
            colors[job["llm_color"]][outcome] += 1
            colors[job["llm_color"]]["scored"] += 1
        elif result != "*":
            raise ValueError(f"Unscored game {job['index']} unexpectedly has result {result!r}")
        if row["run_id"] is not None:
            run_directory = output / row["run_id"]
            turn_counts, samples, token_counts = turn_metrics(run_directory, job["llm_color"])
            counts.update(turn_counts)
            usage.update(token_counts)
            durations.extend(samples)
            row["llm_turns"] = turn_counts
            row["llm_latency_seconds"] = duration_stats(samples)
            costs = validator_costs(row["run_id"])
            if costs is not None:
                row["validator_costs"] = costs
                validator_latest.append(costs)
            manifest_path = run_directory / "manifest.json"
            if manifest_path.exists():
                identity = runtime_identity(read_json(manifest_path))
                if identity is not None and identity not in identities:
                    identities.append(identity)
        rows.append(row)
    scored = sum(results.values())
    total = len(rows)
    finalized = sum(statuses[status] for status in FINISHED)
    warnings = []
    if len(identities) > 1:
        warnings.append("Multiple runtime identities are present; aggregate results are not a controlled comparison.")
    if not identities:
        warnings.append("No initialized model identity was available in the selected attempts.")
    if finalized != total:
        warnings.append("This batch has unfinished games; results cover only the saved attempts.")
    report = {"schema_version": 1, "batch_id": plan["batch_id"],
            "generated_at": datetime.now(timezone.utc).isoformat(), "config": plan["config"],
            "runtime_identities": identities, "warnings": warnings,
            "scheduled_games": total, "finalized_games": finalized, "scored_games": scored,
            "unscored_games": total - scored, "status_counts": dict(statuses), "reason_counts": dict(reasons),
            "results": dict(results), "results_by_color": {c: dict(v) for c, v in colors.items()},
            "score_rate": (results["wins"] + 0.5 * results["draws"]) / scored if scored else None,
            "attempts": {"total": sum(attempt_statuses.values()), "by_status": dict(attempt_statuses),
                         "superseded": sum(max(0, len(e.get("attempts", [])) - 1) for e in progress["games"])},
            "llm_turns": dict(counts), "legal_move_rate": counts["applied"] / counts["requested"] if counts["requested"] else None,
            "llm_latency_seconds": duration_stats(durations), "recorded_token_usage": dict(usage), "games": rows,
            "definitions": {"score_rate": "(wins + 0.5 * draws) / scored games; includes forfeits, excludes all unscored games",
                            "legal_move_rate": "applied LLM moves / requested LLM turns in the latest attempt of each scheduled game",
                            "latency": "recorded LLM response, timeout and failure durations in latest attempts; warm-up excluded; p95 is nearest-rank",
                            "attempts": "older attempts are retained and counted separately; never added to game scores or turn metrics",
                            "tokens": "sum of reported Ollama prompt_eval_count/eval_count, including thinking when reported; missing usage is not estimated"}}
    if validator_latest or validator_attempts:
        report["validator_costs"] = validator_cost_report(validator_latest, validator_attempts)
    return report


def markdown(report):
    def number(value):
        return "n/a" if value is None else f"{value:.3f}"
    score = report["score_rate"]
    lines = [f"# Batch report: {report['batch_id']}", "",
             f"Mode: {report['config']['mode']} | Prompt: {report['config']['prompt_version']}", "",
             f"Scheduled: {report['scheduled_games']} | Finalized: {report['finalized_games']} | Scored: {report['scored_games']} | Unscored: {report['unscored_games']}", "",
             f"LLM wins/draws/losses: {report['results']['wins']}/{report['results']['draws']}/{report['results']['losses']}", "",
             f"Score rate: {'n/a' if score is None else f'{score:.1%}'} (forfeits included; unscored games excluded).", "",
             "## Outcomes", "", "| Status | Games |", "|---|---:|"]
    lines += [f"| {key} | {value} |" for key, value in sorted(report["status_counts"].items())]
    lines += ["", "| Termination reason | Games |", "|---|---:|"]
    lines += [f"| {key} | {value} |" for key, value in sorted(report["reason_counts"].items())]
    lines += ["", "| LLM color | Wins | Draws | Losses | Scored |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {color} | {r['wins']} | {r['draws']} | {r['losses']} | {r['scored']} |" for color, r in report["results_by_color"].items()]
    turns, latency = report["llm_turns"], report["llm_latency_seconds"]
    lines += ["", "## LLM turns and latency", "",
              f"Requested: {turns['requested']} | Responses: {turns['responses']} | Applied: {turns['applied']} | Timeouts: {turns['timeouts']} | Failures: {turns['failed']} | Unanswered: {turns['unanswered']}", "",
              f"Legal move rate: {number(report['legal_move_rate'])} (applied / requested).", "",
              f"Latency in seconds ({latency['samples']} samples): mean {number(latency['mean'])}, median {number(latency['median'])}, p95 {number(latency['p95'])}.", "",
              f"Attempts: {report['attempts']['total']}, including {report['attempts']['superseded']} superseded attempts.", "",
              "## Games", "", "| Game | Color | Seed | Status | Reason | Result | Plies | Attempts |", "|---:|---|---:|---|---|---|---:|---:|"]
    lines += [f"| {g['index']} | {g['llm_color']} | {g['seed']} | {g['status']} | {g['reason']} | {g['result']} | {g['plies'] if g['plies'] is not None else 'n/a'} | {g['attempt_count']} |" for g in report["games"]]
    costs = report.get("validator_costs")
    if costs is not None:
        lines += ["", "## Authored-validator costs", "",
                  "Artifact setup is recorded once per artifact, separately from evaluation initialization and per-turn execution.", "",
                  "| Recorded evaluation costs | Latest attempts | All attempts, including superseded |", "|---|---:|---:|"]
        for field in ("requests", "results", "successes", "errors", "unanswered"):
            lines.append(f"| Validator {field} | {costs['latest_attempts'][field]} | {costs['all_attempts'][field]} |")

        def measured(row, key="total"):
            return f"{number(row[key])} ({row['samples']} measured, {row['missing']} unavailable)"

        for label, bucket, field, key in (
                ("Initialization wall seconds", "initialization", "elapsed_seconds", "total"),
                ("Model-call wall seconds", "model_inference", "elapsed_seconds", "total"),
                ("Model prompt tokens", "model_inference", "prompt_tokens", "total"),
                ("Model output tokens", "model_inference", "output_tokens", "total"),
                ("Validator total wall seconds", "execution", "total_wall_seconds", "total"),
                ("Validator execution wall seconds", "execution", "execution_wall_seconds", "total"),
                ("Validator CPU seconds", "execution", "cpu_seconds", "total"),
                ("Validator cleanup seconds", "execution", "cleanup_seconds", "total"),
                ("Validator peak memory bytes", "execution", "peak_memory_bytes", "max")):
            left = measured(costs["latest_attempts"][bucket][field], key)
            right = measured(costs["all_attempts"][bucket][field], key)
            lines.append(f"| {label} | {left} | {right} |")
        for artifact in costs["artifacts"]:
            lines += ["", f"### Artifact {artifact['artifact_id']}", "",
                      f"Source SHA-256: {artifact['source_sha256'] or 'unavailable'}", "",
                      "Generation, development, and freeze setup ledger (counted once):", "",
                      "```json", json.dumps(artifact["setup_costs"], indent=2, ensure_ascii=False), "```"]
        lines += ["", *[f"- {definition}" for definition in costs["definitions"].values()]]
        for warning in costs["all_attempts"]["warnings"]:
            lines.append(f"- {warning}")
    lines += ["", "## Metric definitions", ""] + [f"- {text}." for text in report["definitions"].values()]
    if report["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {text}" for text in report["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True, help="runs/batches/<batch-id>")
    parser.add_argument("--output", type=Path, help="report directory (default: the batch directory)")
    args = parser.parse_args(argv)
    try:
        directory = args.batch.resolve()
        with batch_lock(directory):
            report = build_report(directory)
            destination = args.output or directory
            recorder = Recorder(destination, exist_ok=True)
            recorder.write("report.json", report)
            path = destination / "report.md"
            atomic_write(path, markdown(report))
        print(markdown(report))
        print(f"Saved report.json and report.md in {destination}")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"Report error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
