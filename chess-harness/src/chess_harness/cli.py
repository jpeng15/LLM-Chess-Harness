"""Shared game options for the single-game and batch commands."""
import argparse
from pathlib import Path

import chess

from .limits import LIMIT_POLICY_VERSION
from .players import PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[2]


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def add_game_arguments(parser, *, include_color=True):
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--engine", type=Path, default=ROOT / "engines/stockfish-19/stockfish/stockfish-windows-x86-64-universal.exe")
    if include_color:
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


def game_config(parser, args):
    if args.tokens >= args.context:
        parser.error("--tokens must be smaller than --context, leaving room for the input prompt")
    try:
        board = chess.Board(args.fen)
    except ValueError as exc:
        parser.error(str(exc))
    if not board.is_valid():
        parser.error("FEN does not describe a valid standard chess position")
    if not args.engine.is_file():
        parser.error(f"Engine does not exist: {args.engine}")
    return {
        "mode": "unassisted", "prompt_version": PROMPT_VERSION,
        "limit_policy": {"version": LIMIT_POLICY_VERSION, "truncate": False, "shift": False,
                         "output_limit": "forfeit", "context_limit": "truncated",
                         "ambiguous_generation_limit": "truncated"},
        "llm": {"model": args.model, "url": args.url, "think": args.think,
                "seconds": args.move_seconds, "tokens": args.tokens, "context": args.context,
                "temperature": args.temperature, "seed": args.seed},
        "engine": {"path": str(args.engine.resolve()), "seconds": 30, "hash_mb": 64,
                   "nodes": args.engine_nodes, "skill": args.engine_skill, "threads": 1},
        "llm_color": getattr(args, "llm_color", "white"), "initial_fen": args.fen, "max_plies": args.max_plies,
        "draw_policy": "automatically claim available draws", "retries": 0,
    }
