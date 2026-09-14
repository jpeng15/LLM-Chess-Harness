import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import chess
import httpx

from chess_harness.game import Recorder
from chess_harness.viewer import make_handler, run_directory, snapshot


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recorder = Recorder(self.root / "test-run")
        self.recorder.write("manifest.json", {"config": {"initial_fen": chess.STARTING_FEN,
            "llm": {"model": "test"}, "llm_color": "white"}})

    def event(self, kind, **fields):
        self.recorder.event(kind, **fields)

    def test_empty_initialization(self):
        value = snapshot(self.recorder.directory)
        self.assertEqual(value["positions"][0]["fen"], chess.STARTING_FEN)
        self.assertEqual(value["sequence"], 0)
        self.assertIsNone(value["summary"])

    def test_pending_move_then_completion(self):
        self.event("move_requested", player="test", ply=1, fen=chess.STARTING_FEN)
        self.assertEqual(snapshot(self.recorder.directory)["pending"]["player"], "test")
        self.event("move_response", text="e2e4", elapsed_seconds=0.5, raw={})
        board = chess.Board()
        board.push_uci("e2e4")
        self.event("move_applied", uci="e2e4", san="e4", fen=board.fen())
        self.event("game_finished", status="truncated", result="*", reason="max_plies")
        value = snapshot(self.recorder.directory)
        self.assertEqual(value["positions"][-1]["fen"], board.fen())
        self.assertTrue(value["responses"][0]["applied"])
        self.assertIsNone(value["pending"])
        self.assertEqual(value["summary"]["status"], "truncated")

    def test_rejected_response_does_not_change_board(self):
        self.event("move_requested", player="test", ply=1)
        self.event("move_response", text="<script>alert(1)</script>", elapsed_seconds=0.1, raw={})
        self.event("game_finished", status="forfeit", reason="malformed_response", result="0-1")
        value = snapshot(self.recorder.directory)
        self.assertEqual(len(value["positions"]), 1)
        self.assertFalse(value["responses"][0]["applied"])
        self.assertEqual(value["responses"][0]["text"], "<script>alert(1)</script>")

    def test_partial_append_recovers(self):
        self.event("warmup_completed")
        path = self.recorder.directory / "events.jsonl"
        with path.open("ab") as output:
            output.write(b'{"sequence":2,"type":"move_requested",')
        self.assertEqual(snapshot(self.recorder.directory)["sequence"], 1)
        with path.open("ab") as output:
            output.write(b'"time":"2026-09-14T00:00:00Z","player":"test","ply":1}\n')
        self.assertEqual(snapshot(self.recorder.directory)["pending"]["ply"], 1)

    def test_initialization_failure(self):
        self.event("initialization_failed", status="infrastructure_failure", error="server unavailable")
        self.assertEqual(snapshot(self.recorder.directory)["summary"]["error"], "server unavailable")

    def test_path_traversal_rejected(self):
        for name in ("../test-run", "..", "C:\\secrets", "test-run/other"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                run_directory(self.root, name)

    def test_http_routes_and_incremental_reads(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
                for route in ("/", "/app.js", "/style.css", "/api/board"):
                    self.assertEqual(client.get(route).status_code, 200)
                self.assertEqual(client.get("/api/runs").json()[0]["id"], "test-run")
                self.assertEqual(client.get("/api/runs/test-run").json()["sequence"], 0)
                self.event("move_requested", player="test", ply=1)
                self.assertEqual(client.get("/api/runs/test-run").json()["pending"]["player"], "test")
                self.assertEqual(client.get("/api/board", params={"fen": "invalid"}).status_code, 400)
                self.assertEqual(client.get("/api/runs/missing").status_code, 404)
                self.assertEqual(client.get("/README.md").status_code, 404)
                svg = client.get("/api/board", params={"fen": chess.STARTING_FEN, "flip": "1"})
                self.assertIn("image/svg+xml", svg.headers["content-type"])
                self.assertIn("<svg", svg.text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
