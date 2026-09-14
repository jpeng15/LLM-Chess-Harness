import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from pathlib import Path
import platform
import uuid

import chess
import httpx

from .game import Recorder, run_game
from .players import EnginePlayer, OllamaPlayer, PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[2]


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description="Run one unassisted Ollama vs Stockfish game.")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--engine", type=Path, default=ROOT / "engines/stockfish-19/stockfish/stockfish-windows-x86-64-universal.exe")
    parser.add_argument("--llm-color", choices=["white", "black"], default="white")
    parser.add_argument("--think", action="store_true")
    parser.add_argument("--move-seconds", type=positive, default=60)
    parser.add_argument("--tokens", type=positive, default=64)
    parser.add_argument("--context", type=positive, default=4096)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--engine-nodes", type=positive, default=10000)
    parser.add_argument("--engine-skill", type=int, choices=range(21), default=0)
    parser.add_argument("--max-plies", type=positive, default=300)
    parser.add_argument("--fen", default=chess.STARTING_FEN)
    parser.add_argument("--output", type=Path, default=ROOT / "runs")
    args = parser.parse_args()
    try:
        board = chess.Board(args.fen)
    except ValueError as exc:
        parser.error(str(exc))
    if not board.is_valid():
        parser.error("FEN does not describe a valid standard chess position")
    if not args.engine.is_file():
        parser.error(f"Engine does not exist: {args.engine}")
    config = {
        "mode": "unassisted", "prompt_version": PROMPT_VERSION,
        "llm": {"model": args.model, "url": args.url, "think": args.think,
                "seconds": args.move_seconds, "tokens": args.tokens, "context": args.context,
                "temperature": args.temperature, "seed": args.seed},
        "engine": {"path": str(args.engine.resolve()), "seconds": 30, "hash_mb": 64,
                   "nodes": args.engine_nodes, "skill": args.engine_skill, "threads": 1},
        "llm_color": args.llm_color, "initial_fen": args.fen, "max_plies": args.max_plies,
        "draw_policy": "automatically claim available draws", "retries": 0,
    }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    recorder = Recorder(args.output / run_id)
    manifest = {"run_id": run_id, "config": config, "python": platform.python_version(),
                "packages": {name: version(name) for name in ("chess", "httpx", "llm-chess-harness")},
                "engine_sha256": hashlib.sha256(args.engine.read_bytes()).hexdigest()}
    recorder.write("manifest.json", manifest)
    print(f"Run: {recorder.directory}", flush=True)
    engine = None
    try:
        with httpx.Client(base_url=args.url, timeout=10, trust_env=False) as client:
            for key, path in (("ollama_version", "/api/version"), ("models", "/api/tags")):
                response = client.get(path)
                response.raise_for_status()
                manifest[key] = response.json()
        engine = EnginePlayer(config["engine"])
        manifest["engine_id"] = engine.engine.id
        recorder.write("manifest.json", manifest)
        llm = OllamaPlayer(config["llm"])
        print("Warming model (excluded from game timing)...", flush=True)
        recorder.event("warmup_completed", response=llm.warmup())
        color = args.llm_color == "white"
        summary = run_game(board, {color: llm, not color: engine}, color, args.max_plies, recorder)
    except (Exception, KeyboardInterrupt) as exc:
        summary = {"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "infrastructure_failure",
                   "result": "*", "reason": "initialization_failure", "error": str(exc)}
        recorder.event("initialization_failed", **summary)
        recorder.write("summary.json", summary)
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception as exc:
                recorder.event("cleanup_error", message=str(exc))
    print(f"{summary['status']}: {summary['reason']} ({summary['result']})")
    return 1 if summary["status"] in ("infrastructure_failure", "interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
