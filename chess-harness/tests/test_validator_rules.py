from copy import deepcopy
import unittest

import chess

from chess_harness.validator_contract import CONTRACT_VERSION, RULES_API_VERSION, ValidatorContractError
from chess_harness.validator_rules import RulesBoard


def request(moves=(), initial_fen=chess.STARTING_FEN, candidate="e2e4"):
    board = chess.Board(initial_fen)
    for move in moves:
        board.push_uci(move)
    return {
        "contract_version": CONTRACT_VERSION,
        "rules_api_version": RULES_API_VERSION,
        "position": {"fen": board.fen()},
        "history": {"initial_fen": initial_fen, "moves": list(moves)},
        "candidate": candidate,
    }


def from_fen(fen):
    return RulesBoard.from_input({"fen": fen}, {"initial_fen": fen, "moves": []})


class ValidatorRulesTests(unittest.TestCase):
    def assert_error(self, reason, path, function, *args):
        with self.assertRaises(ValidatorContractError) as caught:
            function(*args)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.path, path)

    def test_reconstruction_detaches_input_and_branches(self):
        value = request(["e2e4", "c7c5"], candidate="g1f3")
        before = deepcopy(value)
        board = RulesBoard.from_request(value)
        self.assertEqual(board.fen(), value["position"]["fen"])
        self.assertEqual(board.side_to_move(), "white")
        self.assertEqual(board.legal_moves(), sorted(board.legal_moves()))
        branch = board.copy()
        branch.push("g1f3")
        self.assertEqual(branch.piece_at("f3"), "N")
        self.assertEqual(branch.side_to_move(), "black")
        self.assertEqual(board.piece_at("g1"), "N")
        self.assertIsNone(board.piece_at("f3"))
        self.assertEqual(value, before)
        value["history"]["moves"].append("a1a8")
        value["position"]["fen"] = "garbage"
        self.assertEqual(board.fen(), before["position"]["fen"])
        moves = board.legal_moves()
        moves.clear()
        self.assertTrue(board.legal_moves())

    def test_history_reconstruction_checks_legality_position_and_counters(self):
        bad = request()
        bad["history"]["moves"] = ["e2e5"]
        self.assert_error("illegal_move", "$.history.moves[0]", RulesBoard.from_request, bad)
        bad = request(["e2e4"], candidate="e7e5")
        bad["position"]["fen"] = chess.STARTING_FEN
        self.assert_error("position_mismatch", "$.position.fen", RulesBoard.from_request, bad)
        for index in (4, 5):
            bad = request()
            fields = bad["position"]["fen"].split()
            fields[index] = str(int(fields[index]) + 1)
            bad["position"]["fen"] = " ".join(fields)
            self.assert_error("position_mismatch", "$.position.fen", RulesBoard.from_request, bad)
        self.assert_error("illegal_move", "$.candidate", RulesBoard.from_request, request(candidate="e2e5"))

    def test_full_valid_standard_fen_required(self):
        for fen in ("not a fen", "8/8/8/8/8/8/8/8 w - - 0 1",
                    "8/8/8/8/8/8/4k3/4K3 w - - 0 1",
                    "8/8/8/8/8/8/8/4K3 w - - 0 1",
                    chess.STARTING_FEN.rsplit(" ", 2)[0],
                    chess.STARTING_FEN.rsplit(" ", 1)[0] + " 0",
                    chess.STARTING_FEN.replace(" 0 1", " +0 1"),
                    chess.STARTING_FEN.replace(" 0 1", " 0 \u0661"),
                    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w HAha - 0 1"):
            with self.subTest(fen=fen):
                self.assert_error("invalid_fen", "$.history.initial_fen", from_fen, fen)
        value = request()
        value["position"]["fen"] = "bad"
        self.assert_error("invalid_fen", "$.position.fen", RulesBoard.from_request, value)

    def test_canonical_fen_normalization_keeps_history_and_ep_behavior(self):
        value = request(["e2e4"], candidate="e7e5")
        fields = value["position"]["fen"].split()
        fields[3] = "e3"  # Standard FEN target exists although Black cannot capture.
        value["position"]["fen"] = " ".join(fields)
        board = RulesBoard.from_request(value)
        self.assertEqual(board.fen().split()[3], "-")
        self.assertEqual(board.side_to_move(), "black")

    def test_all_move_inspections_require_canonical_legal_move_without_mutation(self):
        board = from_fen(chess.STARTING_FEN)
        before = board.fen()
        for method in (board.push, board.capture, board.is_castling, board.is_en_passant, board.promotion):
            for move, reason in ((" e2e4", "invalid_move"), ("E2E4", "invalid_move"),
                                 ("0000", "invalid_move"), (None, "invalid_move"),
                                 ("e2e5", "illegal_move")):
                with self.subTest(method=method.__name__, move=move):
                    self.assert_error(reason, "$.move", method, move)
                    self.assertEqual(board.fen(), before)
        for square in ("A1", "a9", None, 0):
            self.assert_error("invalid_square", "$.square", board.piece_at, square)
        self.assertIsNone(board.capture("e2e4"))
        self.assertIsNone(board.promotion("e2e4"))
        self.assertFalse(board.is_castling("e2e4"))
        self.assertFalse(board.is_en_passant("e2e4"))

    def test_pinned_capture_and_capture_that_exposes_king_are_not_legal(self):
        board = from_fen("k3r3/8/8/8/8/8/p3R3/4K3 w - - 0 1")
        self.assertNotIn("e2a2", board.legal_moves())
        self.assert_error("illegal_move", "$.move", board.capture, "e2a2")
        ep = from_fen("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
        self.assert_error("illegal_move", "$.move", ep.capture, "e5d6")

    def test_en_passant_captures_report_actual_square_for_both_colors(self):
        cases = [
            ("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", "e5d6", "P", "p", "d5", "d6"),
            ("7k/8/8/8/3Pp3/8/8/7K b - d3 0 2", "e4d3", "p", "P", "d4", "d3"),
        ]
        for fen, move, attacker, victim, square, target in cases:
            with self.subTest(move=move):
                board = from_fen(fen)
                self.assertTrue(board.is_en_passant(move))
                self.assertEqual(board.capture(move), {"capturing_piece": attacker, "captured_piece": victim,
                                                       "capture_square": square})
                board.push(move)
                self.assertIsNone(board.piece_at(square))
                self.assertEqual(board.piece_at(target), attacker)

    def test_capturing_promotion_identifies_pawn_before_move_for_both_colors(self):
        cases = [
            ("1r5k/P7/8/8/8/8/8/7K w - - 0 1", "a7b8q", "P", "r", "b8", "Q"),
            ("7k/8/8/8/8/8/p7/1R5K b - - 0 1", "a2b1q", "p", "R", "b1", "q"),
        ]
        for fen, move, attacker, victim, target, promotion in cases:
            with self.subTest(move=move):
                board = from_fen(fen)
                self.assertEqual(board.promotion(move), promotion)
                self.assertEqual(board.capture(move), {"capturing_piece": attacker, "captured_piece": victim,
                                                       "capture_square": target})
                board.push(move)
                self.assertEqual(board.piece_at(target), promotion)

    def test_castling_moves_both_pieces_and_does_not_capture(self):
        board = from_fen("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertTrue(board.is_castling("e1g1"))
        self.assertIsNone(board.capture("e1g1"))
        board.push("e1g1")
        self.assertEqual((board.piece_at("g1"), board.piece_at("f1")), ("K", "R"))
        self.assertIsNone(board.piece_at("h1"))

    def test_copy_preserves_repetition_and_referee_claim_policy(self):
        moves = ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6"]
        original = RulesBoard.from_request(request(moves, candidate="f3g1"))
        branch = original.copy()
        branch.push("f3g1")
        self.assertEqual(branch.outcome(), {"result": "1/2-1/2", "reason": "threefold_repetition"})
        # The exact same FEN without its earlier moves cannot establish repetition.
        without_history = from_fen(branch.fen())
        self.assertEqual(without_history.fen(), branch.fen())
        self.assertIsNone(without_history.outcome())
        self.assertIsNone(original.outcome())
        self.assertEqual(branch.legal_moves(), [])
        self.assert_error("terminal_position", "$.move", branch.push, "f6g8")
        terminal = request(moves + ["f3g1"], candidate="f6g8")
        inspected = RulesBoard.from_input(terminal["position"], terminal["history"])
        self.assertEqual(inspected.outcome(), branch.outcome())
        self.assert_error("terminal_position", "$.candidate", RulesBoard.from_request, terminal)
        continued = request(moves + ["f3g1", "f6g8"], candidate="e2e4")
        self.assert_error("terminal_position", "$.history.moves[7]", RulesBoard.from_request, continued)

    def test_check_mate_stalemate_and_draws(self):
        checked = from_fen("4r2k/8/8/8/8/8/8/4K3 w - - 0 1")
        self.assertTrue(checked.is_check())
        self.assertFalse(checked.is_checkmate())
        self.assertIsNone(checked.outcome())
        mate = from_fen("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        mate.push("f7h7")
        self.assertTrue(mate.is_checkmate())
        self.assertEqual(mate.outcome(), {"result": "1-0", "reason": "checkmate"})
        for method in (mate.push, mate.capture, mate.is_castling, mate.is_en_passant, mate.promotion):
            self.assert_error("terminal_position", "$.move", method, "h8g8")
        for fen, reason in (("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", "stalemate"),
                            ("7k/8/8/8/8/8/8/7K w - - 0 1", "insufficient_material"),
                            ("7k/8/8/8/8/8/8/R6K w - - 99 51", "fifty_moves")):
            with self.subTest(reason=reason):
                board = from_fen(fen)
                self.assertFalse(board.is_checkmate())
                self.assertEqual(board.outcome(), {"result": "1/2-1/2", "reason": reason})
                self.assertEqual(board.legal_moves(), [])


if __name__ == "__main__":
    unittest.main()
