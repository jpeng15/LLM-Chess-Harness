"""Versioned local chess fixtures; annotations never enter a player prompt."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import chess

DEFAULT_SUITE = Path(__file__).with_name("positions.json")


def initial_board(config):
    board = chess.Board(config["initial_fen"])
    if not board.is_valid():
        raise ValueError("Invalid starting position")
    for text in config.get("initial_moves", []):
        move = chess.Move.from_uci(text)
        if move not in board.legal_moves:
            raise ValueError(f"Illegal fixture history move: {text}")
        board.push(move)
    return board


def validate_positions(positions):
    if not isinstance(positions, list) or not positions:
        raise ValueError("Position suite must contain positions")
    ids = set()
    for position in positions:
        if not isinstance(position.get("id"), str) or not position["id"] or position["id"] in ids:
            raise ValueError("Position IDs must be nonempty and unique")
        ids.add(position["id"])
        if position.get("split") not in ("development", "validation"):
            raise ValueError("Position split must be development or validation")
        board = initial_board({"initial_fen": position["fen"], "initial_moves": position.get("moves", [])})
        if board.outcome(claim_draw=True):
            raise ValueError(f"Position {position['id']} is already terminal under the draw policy")
        expected = position.get("expected_moves", [])
        if not isinstance(expected, list) or len(expected) != len(set(expected)):
            raise ValueError("Expected moves must be a unique list")
        for move in expected:
            if chess.Move.from_uci(move) not in board.legal_moves:
                raise ValueError(f"Illegal expected move in {position['id']}")
    return positions


def load_suite(path=DEFAULT_SUITE, split="validation"):
    raw = path.read_bytes()
    suite = json.loads(raw)
    if suite.get("schema_version") != 1 or not isinstance(suite.get("name"), str):
        raise ValueError("Unsupported position suite")
    validate_positions(suite["positions"])
    positions = [p for p in suite["positions"] if split == "all" or p["split"] == split]
    validate_positions(positions)
    return {"name": suite["name"], "sha256": hashlib.sha256(raw).hexdigest(),
            "split": split, "positions": positions}


def position_config(config, position, *, probe=False):
    result = deepcopy(config)
    result.update(initial_fen=position["fen"], initial_moves=position.get("moves", []))
    if probe:
        result.update(llm_color="white" if initial_board(result).turn else "black", max_plies=1)
    return result
