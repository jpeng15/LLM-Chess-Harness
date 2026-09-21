"""Run sequential color-paired games against a chess engine."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

import chess

from .cli import add_game_arguments, game_config, positive
from .game import Recorder
from .runner import new_run_id, run_match
from .limits import LIMIT_POLICY_VERSION
from .players import prompt_version
from .storage import batch_lock, read_json, runtime_identity, saved_summary
from .suites import load_suite, position_config, validate_positions

FINISHED = {"completed", "forfeit", "truncated"}


def schedule(batch_id, pairs, seed, positions=None):
    """Pair the same seed and starting position across both LLM colors."""
    if pairs <= 0:
        raise ValueError("pairs must be positive")
    if positions is not None:
        validate_positions(positions)
    return [{"index": 2 * pair + offset + 1, "pair": pair + 1,
             "run_id": f"{batch_id}-{2 * pair + offset + 1:06d}",
             "llm_color": color, "seed": seed + pair,
             **({"position": positions[pair % len(positions)]} if positions is not None else {})}
            for pair in range(pairs)
            for offset, color in enumerate(("white", "black"))]


def run_batch(config, output, pairs, positions=None):
    """Persist the plan first, then checkpoint progress around each game."""
    batch_id = new_run_id()
    games = schedule(batch_id, pairs, config["llm"]["seed"], positions)
    recorder = Recorder(output / "batches" / batch_id)
    plan = {"schema_version": 1, "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": deepcopy(config),
            "scheduling": {"pairs": pairs, "execution": "sequential",
                           "colors": ["white", "black"],
                           "seed_policy": "base_seed_plus_zero_based_pair_index",
                           "on_infrastructure_failure": "stop"},
            "games": games}
    if positions is not None:
        plan["positions"] = deepcopy(positions)
    with batch_lock(recorder.directory):
        recorder.write("batch.json", plan)
        return execute(plan, new_progress(plan), recorder, output)


def new_progress(plan):
    return {"batch_id": plan["batch_id"], "status": "running", "reason": None,
            "games": [{"run_id": game["run_id"], "status": "pending", "attempts": []}
                      for game in plan["games"]]}


def load_batch(directory):
    """Validate identifiers before using any saved value as a filesystem path."""
    plan = read_json(directory / "batch.json")
    if directory.parent.name != "batches" or plan["batch_id"] != directory.name:
        raise ValueError("Expected the original runs/batches/<batch-id> directory")
    if plan["schema_version"] != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", plan["batch_id"]):
        raise ValueError("Unsupported batch format or invalid batch ID")
    expected = schedule(plan["batch_id"], plan["scheduling"]["pairs"], plan["config"]["llm"]["seed"], plan.get("positions"))
    if plan["games"] != expected:
        raise ValueError("Saved schedule does not match its pair/seed policy")
    path = directory / "progress.json"
    progress = read_json(path) if path.exists() else new_progress(plan)
    if progress["batch_id"] != plan["batch_id"] or [g["run_id"] for g in progress["games"]] != [g["run_id"] for g in expected]:
        raise ValueError("Progress does not match the saved schedule")
    return plan, progress


def reconcile(plan, progress, output):
    """Recover terminal files/events even when the batch checkpoint lagged behind."""
    for job, entry in zip(plan["games"], progress["games"]):
        base = job["run_id"]
        attempts = entry.setdefault("attempts", [])
        if not attempts and ((output / base).exists() or entry["status"] != "pending"):
            attempts.append({"run_id": base, "status": entry["status"]})
        # Recover reservations/folders after a missing or stale progress checkpoint.
        known = {a["run_id"] for a in attempts}
        candidates = [output / base, *sorted(output.glob(base + "-attempt-*"))]
        for candidate in candidates:
            if candidate.is_dir() and candidate.name not in known:
                attempts.append({"run_id": candidate.name, "status": "running"})
                known.add(candidate.name)
        for index, attempt in enumerate(attempts):
            expected = base if index == 0 else f"{base}-attempt-{index + 1:04d}"
            if attempt["run_id"] != expected:
                raise ValueError("Invalid or out-of-order attempt ID")
            directory = output / expected
            if directory.is_symlink() or directory.resolve().parent != output.resolve():
                raise ValueError("Attempt directory escapes the runs directory")
            manifest_path = directory / "manifest.json"
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                config = position_config(plan["config"], job["position"]) if "position" in job else deepcopy(plan["config"])
                config["llm_color"], config["llm"]["seed"] = job["llm_color"], job["seed"]
                if manifest["config"] != config or manifest.get("batch", {}).get("id") != plan["batch_id"]:
                    raise ValueError("Attempt manifest differs from the saved schedule")
            summary = saved_summary(directory)
            if summary is not None:
                if not manifest_path.exists():
                    raise ValueError("Finished attempt has no manifest")
                attempt.update(status=summary["status"], summary=summary)
            elif attempt["status"] in FINISHED:
                raise ValueError("Finished attempt is missing its terminal record")
            else:
                attempt.update(status="interrupted")
                attempt.pop("summary", None)
            if index < len(attempts) - 1 and attempt["status"] in FINISHED:
                raise ValueError("A finished game unexpectedly has a later attempt")
        entry.pop("summary", None)
        if attempts:
            latest = attempts[-1]
            entry.update(status=latest["status"], current_run_id=latest["run_id"])
            if "summary" in latest:
                entry["summary"] = latest["summary"]
        else:
            entry["status"] = "pending"


def resume_batch(directory):
    directory = directory.resolve()
    with batch_lock(directory):
        plan, progress = load_batch(directory)
        if plan["config"]["prompt_version"] != prompt_version(plan["config"]["mode"]) or plan["config"]["limit_policy"]["version"] != LIMIT_POLICY_VERSION:
            raise ValueError("Prompt or limit policy changed; start a new batch")
        output = directory.parent.parent
        reconcile(plan, progress, output)
        return execute(plan, progress, Recorder(directory, exist_ok=True), output)


def execute(plan, progress, recorder, output):
    games, config, batch_id = plan["games"], plan["config"], plan["batch_id"]
    identity_path = recorder.directory / "identity.json"
    identity = read_json(identity_path) if identity_path.exists() else None
    if identity is None:
        for entry in progress["games"]:
            for attempt in entry.get("attempts", []):
                path = output / attempt["run_id"] / "manifest.json"
                if path.exists():
                    identity = runtime_identity(read_json(path))
                    if identity is not None:
                        break
            if identity is not None:
                recorder.write("identity.json", identity)
                break
    progress.update(status="running", reason=None)
    progress.pop("error", None)

    def checkpoint():
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        recorder.write("progress.json", progress)

    checkpoint()
    print(f"Batch: {recorder.directory} ({len(games)} games)", flush=True)
    active = None
    try:
        if config.get("mode") == "authored-validator" or config.get("validator"):
            from .validator_player import configured_artifact
            configured_artifact(config)
        for game, entry in zip(games, progress["games"]):
            if entry["status"] in FINISHED:
                continue
            active = entry
            attempts = entry.setdefault("attempts", [])
            run_id = game["run_id"] if not attempts else f"{game['run_id']}-attempt-{len(attempts) + 1:04d}"
            attempt = {"run_id": run_id, "status": "running"}
            attempts.append(attempt)
            entry.update(status="running", current_run_id=run_id)
            entry.pop("summary", None)
            entry.pop("error", None)
            checkpoint()
            game_settings = position_config(config, game["position"]) if "position" in game else deepcopy(config)
            game_settings["llm_color"] = game["llm_color"]
            game_settings["llm"]["seed"] = game["seed"]
            print(f"Game {game['index']}/{len(games)}: LLM {game['llm_color']}, seed {game['seed']}", flush=True)
            options = {"expected_identity": identity} if identity is not None else {}
            summary = run_match(game_settings, output / run_id,
                                batch={"id": batch_id, "index": game["index"], "pair": game["pair"]}, **options)
            attempt.update(status=summary["status"], summary=summary)
            entry.update(status=summary["status"], summary=summary)
            active = None
            checkpoint()
            manifest_path = output / run_id / "manifest.json"
            if identity is None and manifest_path.exists():
                identity = runtime_identity(read_json(manifest_path))
                if identity is not None:
                    recorder.write("identity.json", identity)
            if summary["status"] in ("infrastructure_failure", "interrupted"):
                progress.update(status="interrupted" if summary["status"] == "interrupted" else "failed",
                                reason=summary["reason"])
                break
        else:
            progress.update(status="completed", reason="schedule_exhausted")
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "infrastructure_failure"
        reason = "user_interrupt" if status == "interrupted" else getattr(exc, "reason", "batch_runner_failure")
        progress.update(status="interrupted" if status == "interrupted" else "failed",
                        reason=reason, error=str(exc))
        if active is not None:
            active.update(status=status, error=str(exc))
            active["attempts"][-1].update(status=status, error=str(exc))
    checkpoint()
    finished = sum("summary" in entry for entry in progress["games"])
    print(f"Batch {progress['status']}: {finished}/{len(games)} game summaries saved. "
          f"Progress: {recorder.directory / 'progress.json'}", flush=True)
    return progress


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    add_game_arguments(parser, include_color=False)
    parser.add_argument("--pairs", type=positive, default=5,
                        help="number of White/Black pairs (default: 5, or 10 games)")
    parser.add_argument("--resume", type=Path, help="resume runs/batches/<batch-id> using its saved settings")
    parser.add_argument("--positions", type=Path, help="position suite for paired starting positions (cycled across pairs)")
    parser.add_argument("--split", choices=("development", "validation", "all"), default="validation")
    args = parser.parse_args(argv)
    try:
        if args.resume is not None:
            resume_parser = argparse.ArgumentParser(description="Resume using saved settings; overrides are not allowed.")
            resume_parser.add_argument("--resume", type=Path, required=True)
            resume_parser.parse_args(argv)
            progress = resume_batch(args.resume)
        else:
            config = game_config(parser, args)
            if args.positions and args.fen != chess.STARTING_FEN:
                parser.error("--positions and a custom --fen cannot be combined")
            positions = load_suite(args.positions, args.split)["positions"] if args.positions else None
            progress = run_batch(config, args.output, args.pairs, positions)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"Batch error: {exc}", file=sys.stderr)
        return 1
    return 0 if progress["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
