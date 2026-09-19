from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import chess
import chess.engine

from chess_harness.analyze import analyze_decision, analyze_run, assess, replay_decisions, score_value, summarize
from chess_harness.compare import add_quality
from chess_harness.game import Recorder


class AnalysisTests(unittest.TestCase):
    def test_scores_use_movers_perspective_including_mate_zero(self):
        score = chess.engine.PovScore(chess.engine.Cp(120), chess.WHITE)
        self.assertEqual(score_value(score, chess.BLACK)["cp"], -120)
        for value, winning in ((chess.engine.Mate(0), False), (chess.engine.MateGiven, True)):
            result = score_value(chess.engine.PovScore(value, chess.BLACK), chess.BLACK)
            self.assertEqual(result["winning_mate"], winning)
            self.assertEqual(result["mate"], 0)
            self.assertIsNone(result["cp"])

    def test_cp_threshold_mates_and_search_noise_are_separate(self):
        cp = lambda x: {"cp": x, "mate": None, "winning_mate": None}
        won = {"cp": None, "mate": 2, "winning_mate": True}
        lost = {"cp": None, "mate": -2, "winning_mate": False}
        self.assertTrue(assess(cp(100), cp(-100), 200)["blunder"])
        self.assertFalse(assess(cp(100), cp(-99), 200)["blunder"])
        self.assertTrue(assess(won, cp(900), 200)["missed_mate"])
        self.assertTrue(assess(cp(-200), lost, 200)["allows_mate"])
        self.assertFalse(assess(lost, lost, 200)["allows_mate"])
        result = assess(cp(100), cp(120), 200)
        self.assertTrue(result["search_disagreement"])
        self.assertEqual(result["centipawn_loss"], 0)
        self.assertIsNone(assess(won, cp(900), 200)["centipawn_loss"])

    def test_root_restricted_analysis_and_full_history(self):
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "b8c6"):
            board.push_uci(move)
        chosen = chess.Move.from_uci("g1f3")
        engine = Mock()
        engine.analyse.side_effect = [
            {"pv": [chess.Move.from_uci("e2e4")], "score": chess.engine.PovScore(chess.engine.Cp(100), chess.WHITE)},
            {"pv": [chosen], "score": chess.engine.PovScore(chess.engine.Cp(-150), chess.WHITE)},
        ]
        result = analyze_decision(engine, board, chosen, 1000, 200)
        self.assertEqual(result["centipawn_loss"], 250)
        self.assertTrue(result["reversal"])
        self.assertEqual(engine.analyse.call_args.kwargs["root_moves"], [chosen])
        self.assertEqual(len(engine.analyse.call_args.args[0].move_stack), 4)
        self.assertIsNot(engine.analyse.call_args_list[0].kwargs["game"], engine.analyse.call_args_list[1].kwargs["game"])
        self.assertEqual(board.peek().uci(), "b8c6")

    def test_replay_rejects_corruption_and_excludes_fixture_moves(self):
        config = {"initial_fen": chess.STARTING_FEN, "initial_moves": ["e2e4"], "llm_color": "black"}
        board = chess.Board()
        board.push_uci("e2e4")
        records = [{"type": "move_requested", "fen": board.fen()}]
        board.push_uci("e7e5")
        records.append({"type": "move_applied", "ply": 1, "uci": "e7e5", "fen": board.fen()})
        decisions, count = replay_decisions({"config": config}, records)
        self.assertEqual((len(decisions), count), (1, 1))
        self.assertEqual(len(decisions[0][1].move_stack), 1)
        records[-1]["fen"] = chess.STARTING_FEN
        with self.assertRaisesRegex(ValueError, "FEN"):
            replay_decisions({"config": config}, records)

    def test_invalid_answers_are_not_silently_counted_as_good_moves(self):
        with tempfile.TemporaryDirectory() as root:
            rec = Recorder(Path(root) / "run")
            rec.write("manifest.json", {"config": {"initial_fen": chess.STARTING_FEN, "llm_color": "white"}})
            rec.event("move_requested", fen=chess.STARTING_FEN)
            rec.event("move_response", text="e2e5")
            engine = Mock()
            with self.assertRaisesRegex(ValueError, "not finished"):
                analyze_run(engine, rec.directory, 100, 200)
            rec.event("game_finished", status="forfeit", reason="illegal_move")
            before = {p.name: p.read_bytes() for p in rec.directory.iterdir()}
            source, rows = analyze_run(engine, rec.directory, 100, 200)
            self.assertEqual(source["excluded_unapplied"], 1)
            self.assertEqual(rows, [])
            self.assertIsNone(summarize(rows, [source])["mean_centipawn_loss"])
            engine.analyse.assert_not_called()
            self.assertEqual(before, {p.name: p.read_bytes() for p in rec.directory.iterdir()})

    def test_comparison_checks_quality_provenance_and_budgets(self):
        config = {"mode": "legal-moves", "prompt_version": "test", "llm": {"model": "test", "seed": 3}, "llm_color": "white"}
        result = {"kind": "games", "config": config, "games": [{"run_id": "test", "llm_color": "white", "seed": 3}]}
        analysis = {"kind": "move-quality", "complete": True, "sources": [{"run_id": "test", "config": config}],
                    "settings": {"nodes": 100}, "metrics": {"mean_centipawn_loss": 42}}
        comparison = {"metrics": {}, "warnings": []}
        add_quality(comparison, result, result, analysis, analysis)
        self.assertEqual(comparison["metrics"]["quality.mean_centipawn_loss"]["left"], 42)
        other = deepcopy(analysis)
        other["settings"]["nodes"] = 200
        with self.assertRaisesRegex(ValueError, "settings"):
            add_quality(comparison, result, result, analysis, other)
        other = deepcopy(analysis)
        other["sources"][0]["run_id"] = "wrong"
        with self.assertRaisesRegex(ValueError, "sources"):
            add_quality(comparison, result, result, analysis, other)
