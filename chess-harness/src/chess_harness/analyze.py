"""Independent post-game engine analysis; never sends evaluations to an LLM."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys

import chess
import chess.engine

from .cli import positive
from .compare import load_result
from .game import Recorder
from .storage import atomic_write
from .suites import initial_board


def score_value(score, color):
    value = score.pov(color)
    mate = value.mate()
    return {"cp": value.score(), "mate": mate,
            "winning_mate": value > chess.engine.Cp(0) if mate is not None else None}


def assess(best, chosen, threshold):
    finite = best["cp"] is not None and chosen["cp"] is not None
    difference = best["cp"] - chosen["cp"] if finite else None
    missed = best["winning_mate"] is True and chosen["winning_mate"] is not True
    allowed = chosen["winning_mate"] is False and best["winning_mate"] is not False
    loss = max(0, difference) if finite else None
    return {"centipawn_loss": loss, "search_disagreement": finite and difference < 0,
            "missed_mate": missed, "allows_mate": allowed,
            "blunder": missed or allowed or (loss is not None and loss >= threshold)}


def replay_decisions(manifest, records):
    """Validate saved move/FEN pairs and retain full history for engine analysis."""
    board = initial_board(manifest["config"])
    llm = manifest["config"]["llm_color"] == "white"
    decisions, requests, ply = [], 0, 0
    for event in records:
        if event["type"] == "move_requested":
            if event["fen"] != board.fen():
                raise ValueError("Saved request FEN disagrees with replay")
            requests += int(board.turn == llm)
        if event["type"] != "move_applied":
            continue
        move = chess.Move.from_uci(event["uci"])
        if move not in board.legal_moves:
            raise ValueError("Saved game contains an illegal applied move")
        ply += 1
        if event["ply"] != ply:
            raise ValueError("Saved applied ply sequence is inconsistent")
        if board.turn == llm:
            decisions.append((ply, board.copy(), move))
        board.push(move)
        if board.fen() != event["fen"]:
            raise ValueError("Saved applied FEN disagrees with replay")
    return decisions, requests


def analyze_decision(engine, board, move, nodes, threshold):
    limit = chess.engine.Limit(nodes=nodes)
    # A new game object clears engine search state between independent probes.
    best = engine.analyse(board, limit, game=object())
    best_move = best["pv"][0]
    chosen = best if move == best_move else engine.analyse(board, limit, root_moves=[move], game=object())
    best_score = score_value(best["score"], board.turn)
    chosen_score = score_value(chosen["score"], board.turn)
    previous = board.move_stack[-2] if len(board.move_stack) >= 2 else None
    reversal = previous is not None and (move.from_square, move.to_square) == (previous.to_square, previous.from_square)
    return {"fen": board.fen(), "move": move.uci(), "san": board.san(move),
            "best_move": best_move.uci(), "best_score": best_score, "chosen_score": chosen_score,
            "best_pv": [m.uci() for m in best.get("pv", [])],
            "chosen_pv": [m.uci() for m in chosen.get("pv", [])],
            "best_nodes": best.get("nodes"), "chosen_nodes": chosen.get("nodes"),
            "best_depth": best.get("depth"), "chosen_depth": chosen.get("depth"),
            "reversal": reversal, "in_check": board.is_check(),
            **assess(best_score, chosen_score, threshold)}


def analyze_run(engine, directory, nodes, threshold):
    manifest_bytes = (directory / "manifest.json").read_bytes()
    event_bytes = (directory / "events.jsonl").read_bytes()
    manifest = json.loads(manifest_bytes)
    records = [json.loads(line) for line in event_bytes.splitlines(keepends=True) if line.endswith(b"\n")]
    if not any(r["type"] in ("game_finished", "initialization_failed") for r in records):
        raise ValueError(f"Run {directory.name} is not finished; analyze saved runs after play stops")
    decisions, requests = replay_decisions(manifest, records)
    rows = [{"run_id": directory.name, "ply": ply,
             **analyze_decision(engine, board, move, nodes, threshold)} for ply, board, move in decisions]
    source = {"run_id": directory.name, "config": manifest["config"],
              "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "events_sha256": hashlib.sha256(event_bytes).hexdigest(),
              "requested": requests, "analyzed": len(rows), "excluded_unapplied": requests - len(rows)}
    return source, rows


def summarize(rows, sources):
    cp = [row["centipawn_loss"] for row in rows if row["centipawn_loss"] is not None]
    return {"analyzed_moves": len(rows), "finite_cp_samples": len(cp),
            "mean_centipawn_loss": statistics.mean(cp) if cp else None,
            "median_centipawn_loss": statistics.median(cp) if cp else None,
            "mate_score_cases": len(rows) - len(cp),
            **{key: sum(row[key] for row in rows) for key in
               ("blunder", "missed_mate", "allows_mate", "search_disagreement", "reversal")},
            "excluded_unapplied": sum(s["excluded_unapplied"] for s in sources)}


def display_score(value):
    if value["mate"] is not None:
        return f"{'+' if value['winning_mate'] else '-'}M{abs(value['mate'])}"
    return str(value["cp"])


def markdown(report):
    lines = ["# Post-game move quality", "", f"Analysis settings: {report['settings']}", "",
             "Scores are from the moving LLM's perspective. Mate scores are kept separate from centipawns.", "",
             "| Metric | Value |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in report["metrics"].items()]
    lines += ["", "| Run / ply | Move | Engine best | Best score | Chosen score | CP loss | Flags |",
              "|---|---|---|---:|---:|---:|---|"]
    for row in report["moves"]:
        flags = ", ".join(k for k in ("blunder", "missed_mate", "allows_mate", "reversal", "search_disagreement") if row[k])
        lines.append(f"| {row['run_id']} / {row['ply']} | {row['san']} | {row['best_move']} | "
                     f"{display_score(row['best_score'])} | {display_score(row['chosen_score'])} | {row['centipawn_loss']} | {flags} |")
    lines += ["", "## Interpretation", "",
              "Blunder means finite centipawn loss at or above the configured threshold, losing an engine-detected forced mate, or allowing one when the best line avoids it.",
              "These are finite-search estimates. A missed mate means this search found mate in the best line but not the chosen line; it is not a mathematical proof that no mate remains.",
              "Negative raw losses are clamped to zero and counted as search disagreements. Mate scores never enter average centipawn loss.",
              "Reversals count moves that undo the previous own move, including fixture history; defensive reversals can be correct.",
              "Unapplied/invalid answers are excluded from move-quality averages and counted separately. No Elo rating is inferred.",
              "Analysis uses the full saved history, a fresh engine search state per query, and the configured node budget per search. It never calls the model or rewrites game artifacts."]
    return "\n".join(lines) + "\n"


def selected_runs(args):
    if args.run:
        return [args.run.resolve()]
    directory = (args.batch or args.benchmark).resolve()
    report = load_result(directory)
    if args.benchmark:
        if report["kind"] != "positions" or not report["complete"]:
            raise ValueError("Expected a complete position benchmark")
        names = [row["run_id"] for row in report["cases"]]
    else:
        if report["kind"] != "games" or report["finalized_games"] != report["scheduled_games"]:
            raise ValueError("Expected a finalized game batch")
        names = [row["run_id"] for row in report["games"]]
    root = directory.parent.parent
    paths = [(root / name).resolve() for name in names]
    if any(path.parent != root or not path.is_dir() for path in paths):
        raise ValueError("Run path escapes the saved runs directory")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", type=Path)
    source.add_argument("--batch", type=Path)
    source.add_argument("--benchmark", type=Path)
    parser.add_argument("--engine", type=Path, help="override the engine path saved in the first run")
    parser.add_argument("--nodes", type=positive, default=100000)
    parser.add_argument("--hash-mb", type=positive, default=64)
    parser.add_argument("--blunder-cp", type=positive, default=200)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    args = parser.parse_args(argv)
    try:
        directories = selected_runs(args)
        first = json.loads((directories[0] / "manifest.json").read_bytes())
        path = (args.engine or Path(first["config"]["engine"]["path"])).resolve()
        settings = {"nodes": args.nodes, "hash_mb": args.hash_mb, "blunder_cp": args.blunder_cp,
                    "threads": 1, "skill": 20, "limit_strength": False,
                    "engine_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "analysis_version": "move-quality-v1"}
        recorder = Recorder(args.output)
        report = {"schema_version": 1, "kind": "move-quality", "complete": False,
                  "settings": settings, "sources": [], "moves": [], "metrics": {}}
        recorder.write("analysis.json", report)
        with chess.engine.SimpleEngine.popen_uci(str(path), timeout=30) as engine:
            engine.configure({"Threads": 1, "Hash": args.hash_mb, "Skill Level": 20, "UCI_LimitStrength": False})
            report["engine_id"] = engine.id
            for directory in directories:
                print(f"Analyzing {directory.name}...", flush=True)
                source, rows = analyze_run(engine, directory, args.nodes, args.blunder_cp)
                report["sources"].append(source)
                report["moves"].extend(rows)
                report["metrics"] = summarize(report["moves"], report["sources"])
                recorder.write("analysis.json", report)
        report["complete"] = True
        recorder.write("analysis.json", report)
        atomic_write(args.output / "analysis.md", markdown(report))
        print(f"Analysis: {args.output / 'analysis.md'}", flush=True)
        return 0
    except (OSError, ValueError, KeyError, chess.engine.EngineError, TimeoutError) as exc:
        print(f"Analysis error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
