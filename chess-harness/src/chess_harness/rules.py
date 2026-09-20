"""Branching chess simulation using rules only; no engine or evaluation scores."""
import chess


def capture_details(board, move):
    square = move.to_square
    if board.is_en_passant(move):
        square += -8 if board.turn else 8
    piece = board.piece_at(square)
    return {"piece": piece.symbol(), "square": chess.square_name(square)} if piece else None


def board_facts(board):
    """Exact rule-derived facts, with no score or safety/strength judgment."""
    legal = sorted(board.legal_moves, key=lambda move: move.uci())
    colors = (("white", chess.WHITE), ("black", chess.BLACK))
    squares = sorted(board.piece_map())
    return {
        "pieces": {chess.square_name(square): board.piece_at(square).symbol() for square in squares},
        "piece_counts": {
            name: {chess.piece_name(piece): len(board.pieces(piece, color)) for piece in chess.PIECE_TYPES}
            for name, color in colors
        },
        "checkers": [chess.square_name(square) for square in board.checkers()],
        "attacks_on_occupied_squares": {
            chess.square_name(square): {
                name: [chess.square_name(source) for source in board.attackers(color, square)]
                for name, color in colors
            } for square in squares
        },
        "attack_map_note": "Attack maps include pinned pieces and are not legal-capture lists or safety evaluations. Legal captures/checks below are for the side to move only.",
        "pinned_pieces": {
            name: [chess.square_name(square) for square in squares
                   if board.color_at(square) == color and board.is_pinned(color, square)]
            for name, color in colors
        },
        "legal_captures": [{"move": move.uci(), "captured": capture_details(board, move)}
                           for move in legal if board.is_capture(move)],
        "legal_checks": [move.uci() for move in legal if board.gives_check(move)],
        "legal_castles": [move.uci() for move in legal if board.is_castling(move)],
        "legal_en_passant": [move.uci() for move in legal if board.is_en_passant(move)],
    }


class RulesSession:
    def __init__(self, board, calls=4, depth=4):
        self.positions = [board.copy(stack=True)]
        self.depths = [0]
        self.calls = calls
        self.max_depth = depth

    def inspect(self, position):
        board = self.positions[position]
        outcome = board.outcome(claim_draw=True)
        return {
            "position": position, "depth": self.depths[position], "fen": board.fen(),
            "board": str(board), "board_legend": "Rows 8 to 1; columns a to h. Uppercase White, lowercase Black.",
            "side_to_move": "white" if board.turn else "black", "check": board.is_check(),
            "legal_moves": sorted(move.uci() for move in board.legal_moves),
            "outcome": {"result": outcome.result(), "reason": outcome.termination.name.lower()} if outcome else None,
            **board_facts(board),
        }

    def expandable(self, position):
        return (len(self.positions) - 1 < self.calls and self.depths[position] < self.max_depth
                and self.positions[position].outcome(claim_draw=True) is None)

    def schema(self):
        def action(properties):
            return {"type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False}
        moves = lambda board: {"type": "string", "enum": sorted(m.uci() for m in board.legal_moves)}
        play = action({"action": {"type": "string", "enum": ["play"]},
                       "move": moves(self.positions[0])})
        choices = []
        for position, board in enumerate(self.positions):
            if self.expandable(position):
                choices.append(action({"action": {"type": "string", "enum": ["simulate"]},
                                       "position": {"type": "integer", "enum": [position]},
                                       "move": moves(board)}))
        # This mode requires one model-selected preview before committing.
        return {"anyOf": [*choices, play] if len(self.positions) > 1 else choices}

    def validate(self, action):
        if not isinstance(action, dict):
            raise ValueError("Expected an action object")
        kind = action.get("action")
        if kind not in ("play", "simulate"):
            raise ValueError("Unknown action")
        if kind == "play" and len(self.positions) == 1:
            raise ValueError("Simulate at least one move before playing")
        expected = {"action", "move"} if kind == "play" else {"action", "position", "move"}
        if set(action) != expected:
            raise ValueError("Incorrect action fields")
        position = 0 if kind == "play" else action["position"]
        if type(position) is not int or not 0 <= position < len(self.positions):
            raise ValueError("Unknown position")
        if kind == "simulate" and not self.expandable(position):
            raise ValueError("Position is terminal or simulation budget exhausted")
        move = action["move"]
        if not isinstance(move, str) or move not in {m.uci() for m in self.positions[position].legal_moves}:
            raise ValueError("Move must exactly match a legal UCI move at this position")

    def simulate(self, action):
        self.validate(action)
        if action["action"] != "simulate":
            raise ValueError("Expected simulate action")
        parent = action["position"]
        board = self.positions[parent].copy(stack=True)
        move = chess.Move.from_uci(action["move"])
        capture = capture_details(board, move)
        san = board.san(move)
        board.push(move)
        self.positions.append(board)
        self.depths.append(self.depths[parent] + 1)
        return {"parent": parent, "move": move.uci(), "san": san, "capture": capture,
                **self.inspect(len(self.positions) - 1)}
