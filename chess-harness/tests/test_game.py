import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.pgn

from chess_harness.game import Recorder, run_game
from chess_harness.players import OllamaPlayer, Reply


class Scripted:
    name = "scripted"

    def __init__(self, moves):
        self.moves = iter(moves)

    def choose(self, board):
        value = next(self.moves)
        if isinstance(value, Exception):
            raise value
        return Reply(value, 0, {})


class GameTests(unittest.TestCase):
    def run_case(self, white, black=(), fen=chess.STARTING_FEN, limit=10):
        with tempfile.TemporaryDirectory() as temp:
            recorder = Recorder(Path(temp) / "run")
            with redirect_stdout(io.StringIO()):
                summary = run_game(chess.Board(fen), {True: Scripted(white), False: Scripted(black)},
                                   True, limit, recorder)
            game = chess.pgn.read_game(io.StringIO((recorder.directory / "game.pgn").read_text()))
            self.assertEqual(game.end().board().fen(), summary["final_fen"])
            self.assertEqual(game.headers["Result"], summary["result"])
            events = [json.loads(line) for line in (recorder.directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[-1]["type"], "game_finished")
            return summary

    def test_illegal_and_malformed_forfeits(self):
        for move, reason in (("e2e5", "illegal_move"), ("I choose e2e4", "malformed_response"),
                             ("0000", "malformed_response"), ("Nf3", "malformed_response")):
            with self.subTest(move=move):
                result = self.run_case([move])
                self.assertEqual((result["status"], result["result"], result["reason"]),
                                 ("forfeit", "0-1", reason))

    def test_timeout_vs_service_failure(self):
        self.assertEqual(self.run_case([TimeoutError()])["reason"], "timeout")
        result = self.run_case([RuntimeError("server unavailable")])
        self.assertEqual((result["status"], result["result"]), ("infrastructure_failure", "*"))

    def test_limit_is_not_draw(self):
        result = self.run_case(["e2e4"], ["e7e5"], limit=2)
        self.assertEqual((result["status"], result["result"], result["plies"]), ("truncated", "*", 2))

    def test_checkmate_and_pgn_replay(self):
        result = self.run_case(["f2f3", "g2g4"], ["e7e5", "d8h4"])
        self.assertEqual((result["reason"], result["result"]), ("checkmate", "0-1"))

    def test_promotion(self):
        result = self.run_case(["a7a8q"], fen="7k/P7/8/8/8/8/8/7K w - - 0 1", limit=1)
        self.assertEqual(chess.Board(result["final_fen"]).piece_at(chess.A8), chess.Piece(chess.QUEEN, True))

    def test_terminal_draw(self):
        result = self.run_case([], fen="7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        self.assertEqual((result["reason"], result["result"]), ("stalemate", "1/2-1/2"))

    def test_http_deadline(self):
        async def slow_post(*args, **kwargs):
            await asyncio.sleep(1)
        player = OllamaPlayer({"model": "fake", "url": "http://localhost:11434"})
        with patch("httpx.AsyncClient.post", slow_post):
            with self.assertRaises(TimeoutError):
                asyncio.run(player._post("/api/chat", {}, 0.01))


if __name__ == "__main__":
    unittest.main()
