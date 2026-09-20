"""Shared game options for the single-game and batch commands."""
import argparse
from pathlib import Path

import chess

from .limits import LIMIT_POLICY_VERSION
from .players import PROMPT_VERSIONS, prompt_version

ROOT = Path(__file__).resolve().parents[2]


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def add_game_arguments(parser, *, include_color=True):
    parser.add_argument("--mode", choices=PROMPT_VERSIONS, default="unassisted",
                        help="prompt assistance (default: unassisted)")
    parser.add_argument("--model", default="qwen3.6:35b-a3b")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--engine", type=Path, default=ROOT / "engines/stockfish-19/stockfish/stockfish-windows-x86-64-universal.exe")
    if include_color:
        parser.add_argument("--llm-color", choices=["white", "black"], default="white")
    parser.add_argument("--think", action=argparse.BooleanOptionalAction, default=True,
                        help="enable model thinking (default: enabled; disable with --no-think)")
    parser.add_argument("--move-seconds", type=positive, default=180)
    parser.add_argument("--tokens", type=positive, default=4096)
    parser.add_argument("--context", type=positive,
                        help="context tokens (default: 16384 for rules-tools, 8192 otherwise)")
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--engine-nodes", type=positive, default=10000)
    parser.add_argument("--engine-skill", type=int, choices=range(21), default=0)
    parser.add_argument("--max-plies", type=positive, default=300)
    parser.add_argument("--tool-calls", type=int, choices=range(1, 17),
                        help="rules-tools simulations per turn (default: 4)")
    parser.add_argument("--tool-depth", type=int, choices=range(1, 9),
                        help="rules-tools maximum branch depth in plies (default: 4)")
    parser.add_argument("--fen", default=chess.STARTING_FEN)
    parser.add_argument("--output", type=Path, default=ROOT / "runs")


def game_config(parser, args):
    if args.mode != "rules-tools" and (args.tool_calls is not None or args.tool_depth is not None):
        parser.error("--tool-calls and --tool-depth require --mode rules-tools")
    context = args.context if args.context is not None else (16384 if args.mode == "rules-tools" else 8192)
    if args.tokens >= context:
        parser.error("--tokens must be smaller than --context, leaving room for the input prompt")
    try:
        board = chess.Board(args.fen)
    except ValueError as exc:
        parser.error(str(exc))
    if not board.is_valid():
        parser.error("FEN does not describe a valid standard chess position")
    if not args.engine.is_file():
        parser.error(f"Engine does not exist: {args.engine}")
    config = {
        "mode": args.mode, "prompt_version": prompt_version(args.mode),
        "limit_policy": {"version": LIMIT_POLICY_VERSION, "truncate": False, "shift": False,
                         "output_limit": "forfeit", "context_limit": "truncated",
                         "ambiguous_generation_limit": "truncated"},
        "llm": {"model": args.model, "url": args.url, "think": args.think,
                "seconds": args.move_seconds, "tokens": args.tokens, "context": context,
                "temperature": args.temperature, "seed": args.seed},
        "engine": {"path": str(args.engine.resolve()), "seconds": 30, "hash_mb": 64,
                   "nodes": args.engine_nodes, "skill": args.engine_skill, "threads": 1},
        "llm_color": getattr(args, "llm_color", "white"), "initial_fen": args.fen, "max_plies": args.max_plies,
        "draw_policy": "automatically claim available draws", "retries": 0,
    }
    if args.mode == "rules-tools":
        config["tools"] = {"version": "rules-tools-v1", "calls": args.tool_calls or 4,
                           "depth": args.tool_depth or 4, "minimum_calls": 1, "output_budget": "per-turn"}
    return config
