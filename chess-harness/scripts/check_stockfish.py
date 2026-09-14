"""Verify the installed engine's UCI connection and move generation."""

import argparse
from pathlib import Path

import chess
import chess.engine


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    default_engine = (
        project_root / "engines" / "stockfish-19" / "stockfish"
        / "stockfish-windows-x86-64-universal.exe"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=default_engine)
    args = parser.parse_args()
    if not args.engine.is_file():
        parser.error(f"Engine not found: {args.engine}")

    board = chess.Board()
    with chess.engine.SimpleEngine.popen_uci(str(args.engine.resolve()), timeout=15) as engine:
        engine.configure({"Threads": 1, "Hash": 64})
        engine.ping()
        result = engine.play(board, chess.engine.Limit(nodes=10_000))
        if result.move is None or result.move not in board.legal_moves:
            raise RuntimeError(f"Engine returned an invalid move: {result.move}")
        print(f"Engine: {engine.id.get('name', 'unknown')}")
        print(f"Legal opening move: {result.move.uci()} ({board.san(result.move)})")
        for name in ("Skill Level", "UCI_LimitStrength", "UCI_Elo"):
            if name in engine.options:
                option = engine.options[name]
                print(f"{name}: default={option.default}, min={option.min}, max={option.max}")
    print("PASS: UCI connection, move generation, legality, and shutdown.")


if __name__ == "__main__":
    main()
