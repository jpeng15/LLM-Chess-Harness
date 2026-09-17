"""Run one unassisted Ollama vs Stockfish game."""
import argparse

from .cli import add_game_arguments, game_config
from .runner import new_run_id, run_match


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_game_arguments(parser)
    args = parser.parse_args(argv)
    config = game_config(parser, args)
    summary = run_match(config, args.output / new_run_id())
    return 1 if summary["status"] in ("infrastructure_failure", "interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
