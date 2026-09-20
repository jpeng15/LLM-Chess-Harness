"""Run a fixed-budget Stage 2 comparison, with independent move-quality analysis."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

import chess

from . import analyze
from .benchmark import run_benchmark
from .cli import add_game_arguments, game_config, positive
from .compare import add_quality, compare, markdown as comparison_markdown
from .game import Recorder
from .players import prompt_version
from .runner import new_run_id
from .storage import atomic_write, read_json
from .suites import DEFAULT_SUITE, load_suite

CONDITIONS = {"unassisted": ("unassisted", False),
              "assisted": ("legal-moves", False), "assisted-thinking": ("legal-moves", True),
              "constrained": ("constrained-legal", False)}


def condition_config(config, name):
    result = deepcopy(config)
    mode, think = CONDITIONS[name]
    result.update(mode=mode, prompt_version=prompt_version(mode), max_plies=1)
    result["llm"]["think"] = think
    return result


def markdown(report):
    lines = ["# Stage 2 experiment", "", f"Status: {report['status']}", "",
             "| Condition | Legal | Expected hits | Median seconds | Output tokens | Mean CP loss | Blunders |",
             "|---|---|---|---:|---:|---:|---:|"]
    for entry in report["conditions"]:
        result, quality = entry["benchmark"], entry.get("quality", {})
        lines.append(f"| {entry['name']} | {result['legal']}/{result['tested']} | "
                     f"{result['expected_move_hits']}/{result['expected_move_cases']} | "
                     f"{result['latency_seconds']['median']} | {result['output_tokens']} | "
                     f"{quality.get('mean_centipawn_loss')} | {quality.get('blunder')} |")
    if report.get("error"):
        lines += ["", report["error"]]
    lines += ["", "All conditions use the same model, suite, seed and context/output/time limits; only assistance and thinking change.",
              "Actual token usage can differ. Thinking consumes the output budget; a cutoff is a failed answer, not an extra retry.",
              "Each position is sampled once per condition. Conditions run sequentially in listed order, so latency may include cache or system-load effects.",
              "Quality averages exclude invalid answers and mate scores; inspect legality, exclusions and mate counts in the detailed comparisons.",
              "This small deterministic sample is not a model ranking or Elo measurement. Development fixtures are for tuning; do not tune prompts on validation outcomes."]
    return "\n".join(lines) + "\n"


def run_experiment(config, output, suite, names, nodes):
    recorder = Recorder(output / "experiments" / new_run_id())
    recorder.write("plan.json", {"schema_version": 1, "suite": suite, "analysis_nodes": nodes,
                                "conditions": [{"name": name, "config": condition_config(config, name)} for name in names]})
    report = {"schema_version": 1, "status": "running", "conditions": [], "comparisons": []}
    recorder.write("experiment.json", report)
    results, analyses, identity = {}, {}, None
    try:
        for name in names:
            print(f"Experiment condition: {name}", flush=True)
            result = run_benchmark(condition_config(config, name), output, suite, expected_identity=identity)
            report["conditions"].append({"name": name, "benchmark": result})
            recorder.write("experiment.json", report)
            if not result["complete"]:
                raise ValueError(f"Condition {name} did not complete; later conditions were not run")
            if identity is None:
                identity = result["cases"][0]["runtime_identity"]
            analysis_path = recorder.directory / ("analysis-" + name)
            code = analyze.main(["--benchmark", str(output / "positions" / result["id"]),
                                 "--engine", config["engine"]["path"], "--nodes", str(nodes),
                                 "--output", str(analysis_path)])
            if code:
                raise ValueError(f"Analysis failed for {name}; partial outputs are preserved")
            results[name] = result
            analyses[name] = read_json(analysis_path / "analysis.json")
            report["conditions"][-1]["quality"] = analyses[name]["metrics"]
            recorder.write("experiment.json", report)
        # Both scientifically useful contrasts, plus others if the user selected a subset.
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                comparison = add_quality(compare(results[left], results[right]), results[left], results[right],
                                         analyses[left], analyses[right])
                filename = f"{left}-vs-{right}"
                recorder.write(filename + ".json", comparison)
                atomic_write(recorder.directory / (filename + ".md"), comparison_markdown(comparison))
                report["comparisons"].append(filename)
        report["status"] = "completed"
    except (OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
    recorder.write("experiment.json", report)
    atomic_write(recorder.directory / "experiment.md", markdown(report))
    print(f"Experiment {report['status']}: {recorder.directory / 'experiment.md'}", flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_game_arguments(parser, include_color=False)
    # Named conditions select thinking; keep this controlled experiment's budgets stable.
    parser.set_defaults(think=False, tokens=1024, context=4096, move_seconds=180)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--split", choices=("development", "validation", "all"), default="validation")
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS,
                        default=["unassisted", "assisted", "assisted-thinking"])
    parser.add_argument("--analysis-nodes", type=positive, default=100000)
    args = parser.parse_args(argv)
    if len(set(args.conditions)) != len(args.conditions) or len(args.conditions) < 2:
        parser.error("Select at least two distinct conditions")
    if args.mode != "unassisted" or args.think:
        parser.error("Experiment modes/thinking are selected by --conditions, not --mode or --think")
    if args.max_plies != 300 or args.fen != chess.STARTING_FEN:
        parser.error("Experiment positions and one-decision limit come from --suite")
    try:
        config = game_config(parser, args)
        report = run_experiment(config, args.output, load_suite(args.suite, args.split), args.conditions, args.analysis_nodes)
        return 0 if report["status"] == "completed" else 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"Experiment error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
