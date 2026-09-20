"""Versioned rules primitives for authored validators, without engine analysis.

This facade is not a sandbox. Only a later isolated execution backend may run
untrusted validator source. Its boards are reconstructed from input, never from
the live referee, and use the referee's automatic claimable-draw policy.
"""
import re

import chess

from .validator_contract import ValidatorContractError, validate_context, validate_request


def _error(reason, path, message):
    raise ValidatorContractError(reason, path, message)


def _parse_fen(fen, path):
    fields = fen.split()
    # python-chess accepts abbreviated/extended FEN; this API promises full,
    # standard-chess FEN, including both counters.
    if (len(fields) != 6
            or re.fullmatch(r"[prnbqkPRNBQK1-8/]+", fields[0]) is None
            or re.fullmatch(r"(?:-|K?Q?k?q?)", fields[2]) is None
            or re.fullmatch(r"[0-9]+", fields[4]) is None
            or re.fullmatch(r"[0-9]+", fields[5]) is None):
        _error("invalid_fen", path, "Expected a full standard-chess FEN with six fields")
    try:
        board = chess.Board(fen, chess960=False)
    except ValueError:
        _error("invalid_fen", path, "FEN could not be parsed")
    if not board.is_valid() or int(fields[5]) < 1:
        _error("invalid_fen", path, "FEN must describe a valid standard-chess position")
    return board


def _legal_move(board, uci, path):
    if not isinstance(uci, str) or re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci) is None:
        _error("invalid_move", path, "Expected an exact lowercase UCI move")
    if board.outcome(claim_draw=True) is not None:
        _error("terminal_position", path, "Cannot move after the game has ended")
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        _error("invalid_move", path, "Expected an exact lowercase UCI move")
    if move not in board.legal_moves:
        _error("illegal_move", path, "Move is not legal in this position")
    return move


class RulesBoard:
    """Independent chess position with complete supplied history.

    Construct with :meth:`from_input` or :meth:`from_request`. Position FENs
    are compared after python-chess canonicalization, including both counters;
    irrelevant en-passant targets normalize to ``-``. Accessors return detached
    values, and :meth:`copy` preserves repetition history.
    """

    def __init__(self, position, history):
        position, history = validate_context(position, history)
        board = _parse_fen(history["initial_fen"], "$.history.initial_fen")
        current = _parse_fen(position["fen"], "$.position.fen")
        for index, uci in enumerate(history["moves"]):
            board.push(_legal_move(board, uci, f"$.history.moves[{index}]"))
        if board.fen() != current.fen():
            _error("position_mismatch", "$.position.fen", "Position does not match the supplied history and counters")
        self._board = board

    @classmethod
    def from_input(cls, position, history):
        """Reconstruct position/history, allowing a terminal position to be inspected."""
        return cls(position, history)

    @classmethod
    def from_request(cls, request):
        """Validate the complete contract and require a playable candidate."""
        request = validate_request(request)
        board = cls.from_input(request["position"], request["history"])
        _legal_move(board._board, request["candidate"], "$.candidate")
        return board

    def copy(self):
        """Return an isolated branch retaining all repetition history."""
        result = object.__new__(type(self))
        result._board = self._board.copy(stack=True)
        return result

    def fen(self):
        return self._board.fen()

    def side_to_move(self):
        return "white" if self._board.turn else "black"

    def legal_moves(self):
        """Return no continuations once the referee's draw/outcome policy ends play."""
        if self.outcome() is not None:
            return []
        return sorted(move.uci() for move in self._board.legal_moves)

    def push(self, uci):
        self._board.push(_legal_move(self._board, uci, "$.move"))

    def piece_at(self, square):
        if not isinstance(square, str) or re.fullmatch(r"[a-h][1-8]", square) is None:
            _error("invalid_square", "$.square", "Expected a lowercase algebraic square")
        piece = self._board.piece_at(chess.parse_square(square))
        return piece.symbol() if piece else None

    def capture(self, uci):
        """Describe a legal capture before moving; en passant uses the pawn's square."""
        move = _legal_move(self._board, uci, "$.move")
        if not self._board.is_capture(move):
            return None
        square = move.to_square
        if self._board.is_en_passant(move):
            square += -8 if self._board.turn else 8
        return {
            "capturing_piece": self._board.piece_at(move.from_square).symbol(),
            "captured_piece": self._board.piece_at(square).symbol(),
            "capture_square": chess.square_name(square),
        }

    def is_castling(self, uci):
        return self._board.is_castling(_legal_move(self._board, uci, "$.move"))

    def is_en_passant(self, uci):
        return self._board.is_en_passant(_legal_move(self._board, uci, "$.move"))

    def promotion(self, uci):
        move = _legal_move(self._board, uci, "$.move")
        return chess.Piece(move.promotion, self._board.turn).symbol() if move.promotion else None

    def is_check(self):
        return self._board.is_check()

    def is_checkmate(self):
        return self._board.is_checkmate()

    def outcome(self):
        result = self._board.outcome(claim_draw=True)
        return {"result": result.result(), "reason": result.termination.name.lower()} if result else None
