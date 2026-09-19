"""Probe one LLM move per fixed position, using the normal referee and limits."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

import chess

from .cli import add_game_arguments, game_config
from .game import Recorder
from .report import duration_stats, turn_metrics
from .runner import new_run_id, run_match
from .storage import atomic_write, events, read_json, runtime_identity
from .suites import DEFAULT_SUITE, load_suite, position_config


def summarize(plan, rows):
    durations = [r["elapsed_seconds"] for r in rows if r["elapsed_seconds"] is not None]
    scored = [r for r in rows if r["expected_moves"]]
    return {"kind": "positions", "schema_version": 1, "id": plan["id"],
            "config": plan["config"], "suite": plan["suite"],
            "complete": len(rows) == len(plan["suite"]["positions"]) and
                        all(r["status"] not in ("infrastructure_failure", "interrupted") for r in rows),
            "scheduled": len(plan["suite"]["positions"]), "tested": len(rows),
            "legal": sum(r["legal"] for r in rows),
            "legal_rate": sum(r["legal"] for r in rows) / len(rows) if rows else None,
            "expected_move_cases": len(scored), "expected_move_hits": sum(r["expected_hit"] for r in scored),
            "latency_seconds": duration_stats(durations),
            "prompt_tokens": sum(r["usage"].get("prompt_tokens", 0) for r in rows),
            "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in rows),
            "cases": rows}


def markdown(report):
    lines = [f"# Position benchmark: {report['id']}", "",
             f"Suite: {report['suite']['name']} / {report['suite']['split']} | Complete: {report['complete']}", "",
             f"Legal: {report['legal']}/{report['tested']} | Expected-move hits: {report['expected_move_hits']}/{report['expected_move_cases']}", "",
             "| Position | Move | Legal | Outcome | Seconds |", "|---|---|---|---|---:|"]
    for row in report["cases"]:
        elapsed = row["elapsed_seconds"]
        seconds = f"{elapsed:.3f}" if elapsed is not None else "n/a"
        lines.append(f"| {row['id']} | {row['move'] or '—'} | {row['legal']} | {row['reason']} | {seconds} |")
    lines += ["", "One decision per position. A legal nonterminal move ends at max_plies=1; this is not a game loss or draw.",
              "Expected-move accuracy covers annotated fixtures only. Raw responses, history, runtime identity and token usage are saved.",
              "Missing token usage is not estimated. Warmup is excluded from move latency. Engine analysis is never sent to the model."]
    return "\n".join(lines) + "\n"


def run_benchmark(config, output, suite, *, expected_identity=None):
    benchmark_id = new_run_id()
    recorder = Recorder(output / "positions" / benchmark_id)
    plan = {"schema_version": 1, "id": benchmark_id, "config": deepcopy(config), "suite": suite}
    recorder.write("plan.json", plan)
    rows, identity = [], expected_identity
    report = summarize(plan, rows)
    recorder.write("report.json", report)
    for index, position in enumerate(suite["positions"], 1):
        settings = position_config(config, position, probe=True)
        directory = output / f"{benchmark_id}-position-{index:04d}"
        summary = run_match(settings, directory, expected_identity=identity)
        manifest = read_json(directory / "manifest.json")
        actual_identity = runtime_identity(manifest)
        if identity is None:
            identity = actual_identity
        records = events(directory)
        applied = next((e for e in records if e["type"] == "move_applied"), None)
        counts, durations, usage = turn_metrics(directory, settings["llm_color"])
        move = applied["uci"] if applied else None
        expected = position.get("expected_moves", [])
        rows.append({"id": position["id"], "run_id": directory.name,
                     "position": position, "runtime_identity": actual_identity,
                     "status": summary["status"], "reason": summary["reason"], "move": move,
                     "legal": counts["applied"] == 1,
                     "expected_moves": expected, "expected_hit": bool(expected and move in expected),
                     "elapsed_seconds": durations[0] if durations else None, "usage": usage})
        report = summarize(plan, rows)
        recorder.write("report.json", report)
        atomic_write(recorder.directory / "report.md", markdown(report))
        if summary["status"] in ("infrastructure_failure", "interrupted"):
            break
    print(f"Position report: {recorder.directory / 'report.md'}", flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_game_arguments(parser, include_color=False)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--split", choices=("development", "validation", "all"), default="validation")
    args = parser.parse_args(argv)
    if args.fen != chess.STARTING_FEN:
        parser.error("Use --suite to set probe positions; --fen is for game runs")
    try:
        config = game_config(parser, args)
        # A probe always measures exactly one LLM decision, with the suite's history.
        config["max_plies"] = 1
        suite = load_suite(args.suite, args.split)
        report = run_benchmark(config, args.output, suite)
        return 0 if report["complete"] else 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
