"""Compare saved position benchmarks or game batches without running a model."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

from .game import Recorder
from .report import build_report
from .storage import atomic_write, batch_lock, read_json
from .suites import position_config


def flatten(value, prefix=""):
    if not isinstance(value, dict):
        return {prefix: value}
    result = {}
    for key, item in value.items():
        result.update(flatten(item, f"{prefix}.{key}" if prefix else key))
    return result


def load_result(directory):
    if (directory / "batch.json").exists():
        with batch_lock(directory):
            return {"kind": "games", **build_report(directory)}
    report = read_json(directory / "report.json")
    if report.get("kind") != "positions" or report.get("schema_version") != 1:
        raise ValueError("Expected a game batch or a position benchmark directory")
    return report


def compare(left, right):
    if left["kind"] != right["kind"]:
        raise ValueError("Compare two position benchmarks or two game batches")
    configs = [flatten(r["config"]) for r in (left, right)]
    differences = {k: {"left": configs[0].get(k), "right": configs[1].get(k)}
                   for k in sorted(configs[0].keys() | configs[1].keys())
                   if configs[0].get(k) != configs[1].get(k)}
    warnings, paired = [], []
    if left["kind"] == "positions":
        if not left["complete"] or not right["complete"]:
            raise ValueError("Position comparisons require complete benchmarks")
        if left["suite"] != right["suite"]:
            raise ValueError("Position suite contents, digest and split must match")
        if [r["id"] for r in left["cases"]] != [r["id"] for r in right["cases"]]:
            raise ValueError("Position IDs/order differ")
        for a, b in zip(left["cases"], right["cases"]):
            paired.append({"id": a["id"], "left_move": a["move"], "right_move": b["move"],
                           "left_legal": a["legal"], "right_legal": b["legal"],
                           "changed": a["move"] != b["move"]})
        metrics = ("tested", "legal_rate", "expected_move_hits", "expected_move_cases", "prompt_tokens", "output_tokens")
        identities = [[r["runtime_identity"] for r in result["cases"]] for result in (left, right)]
        labels = [r["id"] for r in (left, right)]
        latency = [r["latency_seconds"] for r in (left, right)]
    else:
        metrics = ("scheduled_games", "finalized_games", "scored_games", "score_rate", "legal_move_rate", "results")
        identities = [r["runtime_identities"] for r in (left, right)]
        labels = [r["batch_id"] for r in (left, right)]
        latency = [r["llm_latency_seconds"] for r in (left, right)]
        controls = lambda r: [(g["llm_color"], g["seed"], g.get("position")) for g in r["games"]]
        if controls(left) != controls(right):
            warnings.append("Colors, seeds, starting fixtures or game counts differ.")
        if any(r["finalized_games"] != r["scheduled_games"] for r in (left, right)):
            warnings.append("At least one batch is unfinished.")
        warnings.extend(left["warnings"] + right["warnings"])
        warnings.append("Stockfish skill-mode randomness is not controlled by the LLM seed; trajectories can differ.")
    if differences:
        warnings.append("Settings differ; review the full differences before attributing changes to one factor.")
    if identities[0] != identities[1]:
        warnings.append("Runtime/model identities differ; inspect the saved identities.")
    return {"schema_version": 1, "kind": left["kind"], "left": labels[0], "right": labels[1],
            "metrics": {k: {"left": left[k], "right": right[k]} for k in metrics},
            "latency_seconds": {"left": latency[0], "right": latency[1]},
            "settings_differences": differences, "runtime_identities": {"left": identities[0], "right": identities[1]},
            "paired_positions": paired, "warnings": list(dict.fromkeys(warnings))}


def markdown(report):
    lines = ["# Benchmark comparison", "", f"Left: {report['left']} | Right: {report['right']}", "",
             "| Metric | Left | Right |", "|---|---|---|"]
    for key, values in report["metrics"].items():
        lines.append(f"| {key} | {values['left']} | {values['right']} |")
    for key in ("mean", "median", "p95"):
        lines.append(f"| latency {key} (s) | {report['latency_seconds']['left'][key]} | {report['latency_seconds']['right'][key]} |")
    lines += ["", "## Settings differences", "", "| Setting | Left | Right |", "|---|---|---|"]
    for key, values in report["settings_differences"].items():
        lines.append(f"| {key} | {values['left']} | {values['right']} |")
    if report["paired_positions"]:
        lines += ["", "## Position choices", "", "| Position | Left | Right | Changed |", "|---|---|---|---|"]
        for row in report["paired_positions"]:
            lines.append(f"| {row['id']} | {row['left_move']} | {row['right_move']} | {row['changed']} |")
    lines += ["", "## Interpretation", "", "No Elo estimate or statistical significance is inferred from these counts."]
    lines += [f"- {warning}" for warning in report["warnings"]]
    return "\n".join(lines) + "\n"


def add_quality(comparison, left, right, left_analysis, right_analysis):
    for result, analysis in ((left, left_analysis), (right, right_analysis)):
        if analysis.get("kind") != "move-quality" or not analysis.get("complete"):
            raise ValueError("Expected complete move-quality analyses")
        rows = result["cases"] if result["kind"] == "positions" else result["games"]
        if [r["run_id"] for r in rows] != [r["run_id"] for r in analysis["sources"]]:
            raise ValueError("Analysis sources do not match the compared runs")
        for row, source in zip(rows, analysis["sources"]):
            if result["kind"] == "positions":
                expected = position_config(result["config"], row["position"], probe=True)
            else:
                expected = position_config(result["config"], row["position"]) if "position" in row else deepcopy(result["config"])
                expected["llm_color"] = row["llm_color"]
                expected["llm"]["seed"] = row["seed"]
            if source["config"] != expected:
                raise ValueError("Analysis configuration differs from benchmark")
    if left_analysis["settings"] != right_analysis["settings"]:
        raise ValueError("Use identical engine/analysis settings for a quality comparison")
    comparison["analysis_settings"] = left_analysis["settings"]
    for key in left_analysis["metrics"]:
        comparison["metrics"]["quality." + key] = {
            "left": left_analysis["metrics"][key], "right": right_analysis["metrics"][key]}
    comparison["warnings"].append("Move quality is a finite-search estimate; mate scores are excluded from centipawn averages.")
    return comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new comparison directory")
    parser.add_argument("--left-analysis", type=Path, help="optional analysis directory for the left runs")
    parser.add_argument("--right-analysis", type=Path, help="optional analysis directory for the right runs")
    args = parser.parse_args(argv)
    if bool(args.left_analysis) != bool(args.right_analysis):
        parser.error("Provide both --left-analysis and --right-analysis")
    try:
        left, right = load_result(args.left.resolve()), load_result(args.right.resolve())
        result = compare(left, right)
        if args.left_analysis:
            add_quality(result, left, right, read_json(args.left_analysis / "analysis.json"),
                        read_json(args.right_analysis / "analysis.json"))
        recorder = Recorder(args.output)
        recorder.write("comparison.json", result)
        atomic_write(args.output / "comparison.md", markdown(result))
        print(markdown(result))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Comparison error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
