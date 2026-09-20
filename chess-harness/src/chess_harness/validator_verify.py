"""Verify supplied chess witnesses without discovering or ranking candidate moves.

This is trusted host code. It does not execute a generated validator and it does
not treat free-form heuristic interpretations as verified chess facts.
"""

from .validator_contract import (
    ValidatorContractError,
    parse_result,
    validate_request,
    validate_result,
)
from .validator_rules import RulesBoard


def _invalid(path, message):
    raise ValidatorContractError("invalid_finding", path, message)


def _push(board, move, path):
    try:
        board.push(move)
    except ValueError as exc:
        _invalid(path, f"Witness move cannot be played: {exc}")


def _capture(board, move, path):
    try:
        return board.capture(move)
    except ValueError as exc:
        _invalid(path, f"Witness move cannot be played: {exc}")


def verify_result(request, result):
    """Return a detached result only if every structured fact is true.

    The input must describe a nonterminal position reconstructed from its full
    history and a legal candidate. Every fact is replayed independently from that
    position. Only the reported witness is checked: this function never searches
    for additional captures or replies, and never repairs a rejected result.

    Request and schema errors retain their contract reasons. False or unplayable
    factual witnesses raise ``invalid_finding`` with the offending field path.
    A valid empty facts list makes no claim about the candidate's safety.
    """
    request = validate_request(request)
    root = RulesBoard.from_request(request)
    result = validate_result(result)

    for index, fact in enumerate(result["facts"]):
        path = f"$.facts[{index}]"
        line = fact["line"]
        if line[0] != request["candidate"]:
            _invalid(f"{path}.line[0]", "Witness must begin with the requested candidate")

        board = root.copy()
        _push(board, line[0], f"{path}.line[0]")

        if fact["kind"] == "capture_available":
            capture = _capture(board, line[1], f"{path}.line[1]")
            _push(board, line[1], f"{path}.line[1]")
            if capture is None:
                _invalid(f"{path}.line[1]", "The supplied opponent reply is not a capture")
            for field in ("capturing_piece", "captured_piece", "capture_square"):
                if fact[field] != capture[field]:
                    _invalid(f"{path}.{field}", "Capture detail does not match the replayed move")
        else:
            # Structural validation restricts this branch to the two mate kinds
            # and fixes their witness lengths to one and two plies respectively.
            if fact["kind"] == "reply_checkmate":
                _push(board, line[1], f"{path}.line[1]")
            if not board.is_checkmate():
                _invalid(f"{path}.kind", "The supplied witness does not end in checkmate")

    return result


def verify_result_bytes(request, raw):
    """Strictly parse a JSON result, then verify every factual witness."""
    return verify_result(request, parse_result(raw))
