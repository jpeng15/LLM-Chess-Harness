from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import chess
import chess.engine

from chess_harness.validator_contract import (
    CONTRACT_VERSION,
    RULES_API_VERSION,
    ValidatorContractError,
)
from chess_harness.validator_rules import RulesBoard
from chess_harness.validator_verify import verify_result, verify_result_bytes


MATE_FEN = "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"


def request(candidate, *, initial_fen=chess.STARTING_FEN, moves=()):
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


def finding(kind, line, *, fact_id="f1", **fields):
    return {"id": fact_id, "kind": kind, "line": list(line), **fields}


def result(*facts, heuristics=()):
    return {"contract_version": CONTRACT_VERSION, "facts": list(facts),
            "heuristics": list(heuristics)}


def capture(line, capturing_piece, captured_piece, capture_square, **kwargs):
    return finding("capture_available", line, capturing_piece=capturing_piece,
                   captured_piece=captured_piece, capture_square=capture_square,
                   **kwargs)


class ValidatorVerifyTests(unittest.TestCase):
    def assertInvalidFinding(self, req, findings, path):
        with self.assertRaises(ValidatorContractError) as caught:
            verify_result(req, findings)
        self.assertEqual(caught.exception.reason, "invalid_finding")
        self.assertEqual(caught.exception.path, path)

    def test_candidate_and_opponent_reply_checkmates_are_verified(self):
        cases = [
            (request("f7h7", initial_fen=MATE_FEN),
             finding("candidate_checkmate", ["f7h7"])),
            (request("g2g4", moves=["f2f3", "e7e5"]),
             finding("reply_checkmate", ["g2g4", "d8h4"])),
        ]
        for req, fact in cases:
            with self.subTest(kind=fact["kind"]):
                expected = result(fact)
                self.assertEqual(verify_result(req, expected), expected)

    def test_check_stalemate_and_ordinary_move_are_not_checkmate(self):
        for candidate in ("f7f6", "f7e6", "f7e7"):
            with self.subTest(candidate=candidate):
                self.assertInvalidFinding(
                    request(candidate, initial_fen=MATE_FEN),
                    result(finding("candidate_checkmate", [candidate])),
                    "$.facts[0].kind")
        self.assertInvalidFinding(
            request("g2g4", moves=["f2f3", "e7e5"]),
            result(finding("reply_checkmate", ["g2g4", "d7d6"])),
            "$.facts[0].kind")

    def test_capture_fact_does_not_assign_a_move_quality_judgment(self):
        req = request("d1h5", moves=["e2e4", "g7g6"])
        facts = result(capture(["d1h5", "g6h5"], "p", "Q", "h5"), heuristics=[{
            "fact_ids": ["f1"],
            "interpretation": "The queen sacrifice may have compensation; assess the continuation.",
        }])
        with patch("chess.engine.SimpleEngine.popen_uci") as engine:
            verified = verify_result(req, facts)
        engine.assert_not_called()
        self.assertEqual(verified, facts)
        self.assertEqual(set(verified), {"contract_version", "facts", "heuristics"})
        self.assertEqual(verify_result(req, result()), result())

    def test_pinned_capture_and_capture_ignoring_check_are_rejected(self):
        cases = [
            ("k3r3/8/8/8/8/8/p3R3/4K3 b - - 0 1", "a8a7", "e2a2", "a2"),
            ("k7/8/8/8/8/8/1p3r2/1R2K3 b - - 0 1", "f2e2", "b1b2", "b2"),
        ]
        for fen, candidate, reply, square in cases:
            with self.subTest(reply=reply):
                self.assertInvalidFinding(
                    request(candidate, initial_fen=fen),
                    result(capture([candidate, reply], "R", "p", square)),
                    "$.facts[0].line[1]")

    def test_en_passant_records_captured_pawn_square_for_both_colors(self):
        cases = [
            ("7k/3p4/8/4P3/8/8/8/7K b - - 0 1", "d7d5", "e5d6", "P", "p", "d5", "d6"),
            ("7k/8/8/8/4p3/8/3P4/7K w - - 0 1", "d2d4", "e4d3", "p", "P", "d4", "d3"),
        ]
        for fen, candidate, reply, attacker, victim, square, destination in cases:
            with self.subTest(reply=reply):
                req = request(candidate, initial_fen=fen)
                facts = result(capture([candidate, reply], attacker, victim, square))
                self.assertEqual(verify_result(req, facts), facts)
                facts["facts"][0]["capture_square"] = destination
                self.assertInvalidFinding(req, facts, "$.facts[0].capture_square")

    def test_capturing_promotion_identifies_the_pawn_before_promotion(self):
        req = request("h8h7", initial_fen="1r5k/P7/8/8/8/8/8/7K b - - 0 1")
        facts = result(capture(["h8h7", "a7b8q"], "P", "r", "b8"))
        self.assertEqual(verify_result(req, facts), facts)
        facts["facts"][0]["capturing_piece"] = "Q"
        self.assertInvalidFinding(req, facts, "$.facts[0].capturing_piece")

    def test_each_capture_detail_and_actual_capture_are_required(self):
        req = request("d1h5", moves=["e2e4", "g7g6"])
        base = result(capture(["d1h5", "g6h5"], "p", "Q", "h5"))
        for field, wrong in (("capturing_piece", "P"), ("captured_piece", "R"),
                             ("capture_square", "g6")):
            with self.subTest(field=field):
                facts = deepcopy(base)
                facts["facts"][0][field] = wrong
                self.assertInvalidFinding(req, facts, f"$.facts[0].{field}")
        self.assertInvalidFinding(req, result(capture(["d1h5", "g6g5"], "p", "Q", "g5")),
                                  "$.facts[0].line[1]")

    def test_wrong_candidate_is_not_accepted_even_when_its_claim_is_true(self):
        self.assertInvalidFinding(
            request("f7h7", initial_fen=MATE_FEN),
            result(finding("candidate_checkmate", ["f7f8"])),
            "$.facts[0].line[0]")

    def test_one_false_fact_rejects_the_complete_result(self):
        req = request("d1h5", moves=["e2e4", "g7g6"])
        facts = result(
            capture(["d1h5", "g6h5"], "p", "Q", "h5"),
            finding("candidate_checkmate", ["d1h5"], fact_id="f2"),
        )
        self.assertInvalidFinding(req, facts, "$.facts[1].kind")

    def test_terminal_candidate_stops_witness_even_if_a_reply_is_board_legal(self):
        history = ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6"]
        req = request("f3g1", moves=history)
        self.assertInvalidFinding(
            req, result(finding("reply_checkmate", ["f3g1", "f6g8"])),
            "$.facts[0].line[1]")
        # A quiet candidate can also cross the referee's fifty-move claim threshold.
        req = request("a1a2", initial_fen="7k/8/8/8/8/8/8/R6K w - - 98 51")
        self.assertInvalidFinding(
            req, result(finding("reply_checkmate", ["a1a2", "h8g8"])),
            "$.facts[0].line[1]")

    def test_invalid_request_errors_propagate_without_becoming_finding_errors(self):
        terminal = request("f6g8", moves=[
            "g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1",
        ])
        mismatch = request("e2e4")
        mismatch["position"]["fen"] = chess.STARTING_FEN.replace(" 0 1", " 1 1")
        for req in (terminal, mismatch):
            with self.subTest(req=req):
                with self.assertRaises(ValidatorContractError) as direct:
                    RulesBoard.from_request(req)
                with self.assertRaises(ValidatorContractError) as verified:
                    verify_result(req, result())
                self.assertEqual(verified.exception.reason, direct.exception.reason)
                self.assertEqual(verified.exception.path, direct.exception.path)
                self.assertNotEqual(verified.exception.reason, "invalid_finding")

    def test_inputs_and_independent_fact_branches_are_not_mutated(self):
        req = request("d1h5", moves=["e2e4", "g7g6"])
        facts = result(
            capture(["d1h5", "g6h5"], "p", "Q", "h5"),
            capture(["d1h5", "g6h5"], "p", "Q", "h5", fact_id="f2"),
            heuristics=[{"fact_ids": ["f1", "f2"], "interpretation": "Two reports of one possibility."}],
        )
        before = deepcopy((req, facts))
        verified = verify_result(req, facts)
        self.assertEqual((req, facts), before)
        verified["facts"][0]["line"].append("h1h2")
        verified["heuristics"][0]["fact_ids"].append("f3")
        self.assertEqual((req, facts), before)

    def test_byte_entrypoint_uses_strict_contract_parser(self):
        req = request("f7h7", initial_fen=MATE_FEN)
        facts = result(finding("candidate_checkmate", ["f7h7"]))
        self.assertEqual(verify_result_bytes(req, json.dumps(facts).encode("utf-8")), facts)
        malformed = [
            json.dumps(facts).encode("utf-8") + b" trailing",
            b'{"contract_version":"authored-validator-v1","facts":[],"facts":[],"heuristics":[]}',
        ]
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ValidatorContractError):
                verify_result_bytes(req, raw)
        with self.assertRaises(ValidatorContractError):
            verify_result(req, {**facts, "verified": True})


if __name__ == "__main__":
    unittest.main()
