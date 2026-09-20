import argparse
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import chess

from chess_harness.cli import add_game_arguments, game_config
from chess_harness.game import Recorder, run_game
from chess_harness.limits import PlayerFailure
from chess_harness.report import turn_metrics
from chess_harness.rules import RulesSession, board_facts
from chess_harness.tool_player import RulesToolPlayer
from chess_harness.viewer import snapshot


CONFIG = dict(model="test", url="http://localhost:11434", mode="rules-tools", think=False,
              tokens=512, context=8192, seconds=10, seed=0, temperature=0,
              tools={"version": "rules-tools-v1", "calls": 4, "depth": 4, "minimum_calls": 1, "output_budget": "per-turn"})


def simulate(position, move):
    return dict(action="simulate", position=position, move=move)


def play(move):
    return dict(action="play", move=move)


def response(action, count=20, reason="stop"):
    return {"done": True, "done_reason": reason, "eval_count": count, "prompt_eval_count": 100,
            "message": {"content": json.dumps(action)}}


class RulesTests(unittest.TestCase):
    def test_rich_facts_distinguish_attacks_from_legal_captures(self):
        # The e2 rook is pinned to its king, but still attacks the black pawn on a2.
        board = chess.Board("k3r3/8/8/8/8/8/p3R3/4K3 w - - 0 1")
        self.assertTrue(board.is_valid())
        before = board.fen()
        facts = board_facts(board)
        self.assertEqual(facts["pieces"]["e2"], "R")
        self.assertIn("e2", facts["pinned_pieces"]["white"])
        self.assertIn("e2", facts["attacks_on_occupied_squares"]["a2"]["white"])
        self.assertNotIn("e2a2", [row["move"] for row in facts["legal_captures"]])
        self.assertEqual(board.fen(), before)
        ep = chess.Board("7k/8/8/3pP3/8/8/8/7K w - d6 0 2")
        facts = board_facts(ep)
        self.assertEqual(facts["legal_en_passant"], ["e5d6"])
        self.assertIn({"move": "e5d6", "captured": {"piece": "p", "square": "d5"}}, facts["legal_captures"])
        checked = chess.Board("4r2k/8/8/8/8/8/8/4K3 w - - 0 1")
        self.assertEqual(board_facts(checked)["checkers"], ["e8"])
        mate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        self.assertIn("f7h7", board_facts(mate)["legal_checks"])

    def test_branches_are_isolated_and_legal_replies_refresh(self):
        board = chess.Board()
        session = RulesSession(board)
        first = session.simulate(simulate(0, "e2e4"))
        reply = session.simulate(simulate(1, "e7e5"))
        alternate = session.simulate(simulate(0, "d2d4"))
        self.assertEqual(board.fen(), chess.STARTING_FEN)
        self.assertEqual(session.positions[0].fen(), chess.STARTING_FEN)
        self.assertEqual([first["depth"], reply["depth"], alternate["depth"]], [1, 2, 1])
        self.assertEqual(first["side_to_move"], "black")
        self.assertIn("e7e5", first["legal_moves"])
        self.assertNotIn("e2e4", first["legal_moves"])
        self.assertEqual(session.positions[3].piece_at(chess.E2).symbol(), "P")
        self.assertIsNone(first["capture"])
        with self.assertRaises(ValueError):
            session.validate(play("e7e5"))
        session.validate(play("g1f3"))  # Final choice need not be a simulated candidate.

    def test_special_moves_and_capture_facts(self):
        cases = [
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", None, chess.F1, "R"),
            ("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", "e5d6", {"piece": "p", "square": "d5"}, chess.D6, "P"),
            ("7k/P7/8/8/8/8/8/7K w - - 0 1", "a7a8n", None, chess.A8, "N"),
        ]
        for fen, move, capture, square, piece in cases:
            with self.subTest(move=move):
                session = RulesSession(chess.Board(fen))
                result = session.simulate(simulate(0, move))
                self.assertEqual(result["capture"], capture)
                self.assertEqual(session.positions[1].piece_at(square).symbol(), piece)
                self.assertEqual(result["legal_moves"], sorted(m.uci() for m in session.positions[1].legal_moves))
        session = RulesSession(chess.Board("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 2"))
        with self.assertRaises(ValueError):
            session.simulate(simulate(0, "e5d6"))

    def test_terminal_and_history_based_draws_stop_branch_expansion(self):
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
            board.push_uci(move)
        session = RulesSession(board, calls=8, depth=8)
        for index, move in enumerate(("g1f3", "g8f6", "f3g1")):
            result = session.simulate(simulate(index, move))
        self.assertEqual(result["outcome"]["reason"], "threefold_repetition")
        self.assertFalse(session.expandable(3))
        self.assertEqual(len(board.move_stack), 4)
        session = RulesSession(chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"))
        result = session.simulate(simulate(0, "f7h7"))
        self.assertEqual(result["outcome"], {"result": "1-0", "reason": "checkmate"})
        self.assertEqual(result["legal_moves"], [])

    def test_action_validation_and_limits(self):
        session = RulesSession(chess.Board(), calls=2, depth=1)
        invalid = [None, [], {}, {**play("e2e4"), "extra": 1}, simulate(True, "e2e4"),
                   play("e2e4"),
                   simulate(-1, "e2e4"), simulate(8, "e2e4"), simulate(0, " e2e4 "),
                   simulate(0, "e1g1"), {"action": "evaluate", "move": "e2e4"}]
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                session.validate(action)
        session.simulate(simulate(0, "e2e4"))
        with self.assertRaises(ValueError):
            session.simulate(simulate(1, "e7e5"))
        session.simulate(simulate(0, "d2d4"))
        self.assertEqual(len(session.schema()["anyOf"]), 1)
        with self.assertRaises(ValueError):
            session.simulate(simulate(0, "g1f3"))


class ToolPlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recorder = Recorder(Path(self.temp.name), exist_ok=True, mode="rules-tools")
        self.recorder.write("manifest.json", {"config": {"mode": "rules-tools", "llm": CONFIG,
                              "llm_color": "white", "initial_fen": chess.STARTING_FEN}})

    def run_turn(self, post, config=None):
        player = RulesToolPlayer(config or CONFIG, emit=self.recorder.event)
        with patch.object(player, "_post", post), patch("chess.engine.SimpleEngine.popen_uci") as engine, redirect_stdout(io.StringIO()):
            summary = run_game(chess.Board(), {True: player, False: player}, True, 1, self.recorder)
        engine.assert_not_called()
        return summary

    def test_model_directs_exploration_then_plays_root_move_and_usage_is_not_duplicated(self):
        outputs = iter([simulate(0, "e2e4"), simulate(1, "e7e5"), play("d2d4")])
        requests = []
        async def post(path, request, seconds):
            requests.append(deepcopy(request))
            return response(next(outputs))
        summary = self.run_turn(post)
        self.assertEqual(summary["plies"], 1)
        self.assertEqual(chess.Board(summary["final_fen"]).piece_at(chess.D4).symbol(), "P")
        self.assertEqual([r["options"]["num_predict"] for r in requests], [512, 492, 472])
        self.assertIn('"side_to_move": "black"', requests[1]["messages"][-1]["content"])
        self.assertIn('"position": 2', requests[2]["messages"][-1]["content"])
        self.assertIn('"attacks_on_occupied_squares"', requests[0]["messages"][1]["content"])
        self.assertIn('"legal_captures"', requests[1]["messages"][-1]["content"])
        counts, durations, usage = turn_metrics(self.recorder.directory, "white")
        self.assertEqual((counts["requested"], counts["responses"], counts["applied"]), (1, 1, 1))
        self.assertEqual(len(durations), 1)
        self.assertEqual(usage, {"prompt_tokens": 300, "output_tokens": 60, "responses_with_usage": 3})
        view = snapshot(self.recorder.directory)
        self.assertEqual(len(view["positions"]), 2)
        self.assertEqual(len([e for e in view["tool_activity"] if e["type"] == "simulation_result"]), 2)
        self.assertEqual(view["responses"][0]["text"], "d2d4")

    def test_exhausting_simulations_forces_play_schema(self):
        config = {**CONFIG, "tools": {**CONFIG["tools"], "calls": 1}}
        requests = []
        async def post(path, request, seconds):
            requests.append(deepcopy(request))
            return response(simulate(0, "e2e4") if len(requests) == 1 else play("e2e4"))
        self.run_turn(post, config)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(requests[-1]["format"]["anyOf"]), 1)
        self.assertEqual(requests[-1]["format"]["anyOf"][0]["properties"]["action"]["enum"], ["play"])

    def test_invalid_action_forfeits_without_retry(self):
        post = AsyncMock(return_value=response(simulate(0, "e2e5")))
        summary = self.run_turn(post)
        self.assertEqual((summary["status"], summary["reason"], summary["plies"]), ("forfeit", "invalid_tool_action", 0))
        post.assert_awaited_once()

    def test_cutoff_precedes_valid_action_and_total_tokens_are_bounded(self):
        post = AsyncMock(return_value=response(play("e2e4"), count=512, reason="length"))
        summary = self.run_turn(post)
        self.assertEqual(summary["reason"], "output_limit")
        self.assertEqual(summary["plies"], 0)
        # A completed simulation can also exhaust the shared turn budget.
        player = RulesToolPlayer(CONFIG)
        with patch.object(player, "_post", new_callable=AsyncMock, return_value=response(simulate(0, "e2e4"), count=512)) as call:
            reply = player.choose(chess.Board())
        call.assert_awaited_once()
        self.assertEqual(reply.failure_reason, "output_limit")

    def test_shared_deadline_and_partial_usage_survive_timeout(self):
        count = 0
        async def post(*args):
            nonlocal count
            count += 1
            if count == 1:
                return response(simulate(0, "e2e4"))
            await asyncio.sleep(1)
        summary = self.run_turn(post, {**CONFIG, "seconds": 0.03})
        self.assertEqual((summary["status"], summary["reason"]), ("forfeit", "timeout"))
        counts, _, usage = turn_metrics(self.recorder.directory, "white")
        self.assertEqual(counts["timeouts"], 1)
        self.assertEqual(usage["output_tokens"], 20)

    def test_missing_usage_fails_closed(self):
        raw = response(play("e2e4"))
        del raw["eval_count"]
        summary = self.run_turn(AsyncMock(return_value=raw))
        self.assertEqual(summary["reason"], "tool_usage_missing")
        self.assertEqual(summary["status"], "infrastructure_failure")

    def test_context_error_after_simulation_preserves_partial_usage(self):
        post = AsyncMock(side_effect=[response(simulate(0, "e2e4")),
                                     PlayerFailure("context_limit", "Prompt too long")])
        summary = self.run_turn(post)
        self.assertEqual((summary["status"], summary["reason"]), ("truncated", "context_limit"))
        counts, _, usage = turn_metrics(self.recorder.directory, "white")
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(usage["output_tokens"], 20)

    def test_cli_records_limits_and_rejects_them_in_other_modes(self):
        engine = Path(self.temp.name) / "engine"
        engine.write_bytes(b"test")
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        config = game_config(parser, parser.parse_args(["--engine", str(engine), "--mode", "rules-tools", "--no-think", "--tool-calls", "2", "--tool-depth", "3"]))
        self.assertEqual(config["tools"]["calls"], 2)
        self.assertEqual(config["tools"]["depth"], 3)
        self.assertEqual(config["tools"]["minimum_calls"], 1)
        self.assertEqual(config["prompt_version"], "rules-tools-v2")
        self.assertEqual(config["llm"]["context"], 16384)
        override = game_config(parser, parser.parse_args(["--engine", str(engine), "--mode", "rules-tools", "--context", "8192"]))
        self.assertEqual(override["llm"]["context"], 8192)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            game_config(parser, parser.parse_args(["--engine", str(engine), "--tool-calls", "2"]))

    def test_invalid_tool_policy_cannot_silently_change_saved_runs(self):
        for overrides in ({"calls": 0}, {"calls": True}, {"depth": 9}, {"minimum_calls": 0},
                          {"output_budget": "per-call"}, {"version": "unknown"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                RulesToolPlayer({**CONFIG, "tools": {**CONFIG["tools"], **overrides}})
