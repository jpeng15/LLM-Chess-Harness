"""Run sequential color-paired games against a chess engine."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone

from .cli import add_game_arguments, game_config, positive
from .game import Recorder
from .runner import new_run_id, run_match


def schedule(batch_id, pairs, seed):
    """Pair the same seed and starting position across both LLM colors."""
    if pairs <= 0:
        raise ValueError("pairs must be positive")
    return [{"index": 2 * pair + offset + 1, "pair": pair + 1,
             "run_id": f"{batch_id}-{2 * pair + offset + 1:06d}",
             "llm_color": color, "seed": seed + pair}
            for pair in range(pairs)
            for offset, color in enumerate(("white", "black"))]


def run_batch(config, output, pairs):
    """Persist the plan first, then checkpoint progress around each game."""
    batch_id = new_run_id()
    games = schedule(batch_id, pairs, config["llm"]["seed"])
    recorder = Recorder(output / "batches" / batch_id)
    plan = {"schema_version": 1, "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": deepcopy(config),
            "scheduling": {"pairs": pairs, "execution": "sequential",
                           "colors": ["white", "black"],
                           "seed_policy": "base_seed_plus_zero_based_pair_index",
                           "on_infrastructure_failure": "stop"},
            "games": games}
    recorder.write("batch.json", plan)
    progress = {"batch_id": batch_id, "status": "running", "reason": None,
                "games": [{"run_id": game["run_id"], "status": "pending"} for game in games]}

    def checkpoint():
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        recorder.write("progress.json", progress)

    checkpoint()
    print(f"Batch: {recorder.directory} ({len(games)} games)", flush=True)
    active = None
    try:
        for game, entry in zip(games, progress["games"]):
            active = entry
            entry["status"] = "running"
            checkpoint()
            game_settings = deepcopy(config)
            game_settings["llm_color"] = game["llm_color"]
            game_settings["llm"]["seed"] = game["seed"]
            print(f"Game {game['index']}/{len(games)}: LLM {game['llm_color']}, seed {game['seed']}", flush=True)
            summary = run_match(game_settings, output / game["run_id"],
                                batch={"id": batch_id, "index": game["index"], "pair": game["pair"]})
            entry.update(status=summary["status"], summary=summary)
            active = None
            checkpoint()
            if summary["status"] in ("infrastructure_failure", "interrupted"):
                progress.update(status="interrupted" if summary["status"] == "interrupted" else "failed",
                                reason=summary["reason"])
                break
        else:
            progress.update(status="completed", reason="schedule_exhausted")
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "infrastructure_failure"
        reason = "user_interrupt" if status == "interrupted" else "batch_runner_failure"
        progress.update(status="interrupted" if status == "interrupted" else "failed",
                        reason=reason, error=str(exc))
        if active is not None:
            active.update(status=status, error=str(exc))
    checkpoint()
    finished = sum("summary" in entry for entry in progress["games"])
    print(f"Batch {progress['status']}: {finished}/{len(games)} game summaries saved. "
          f"Progress: {recorder.directory / 'progress.json'}", flush=True)
    return progress


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_game_arguments(parser, include_color=False)
    parser.add_argument("--pairs", type=positive, default=5,
                        help="number of White/Black pairs (default: 5, or 10 games)")
    args = parser.parse_args(argv)
    config = game_config(parser, args)
    progress = run_batch(config, args.output, args.pairs)
    return 0 if progress["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
