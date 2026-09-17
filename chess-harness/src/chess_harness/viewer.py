"""Read-only local browser viewer. Run separately from the game process."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import chess
import chess.svg

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).with_name("web")


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def run_directory(root, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Invalid run ID")
    directory = (root / run_id).resolve()
    if directory.parent != root.resolve() or not directory.is_dir():
        raise FileNotFoundError("Run not found")
    return directory


def snapshot(directory):
    manifest = read_json(directory / "manifest.json")
    if manifest is None:
        raise FileNotFoundError("Run manifest not found")
    config = manifest["config"]
    events = []
    try:
        # Ignore a final incomplete append; the next poll will receive it.
        lines = (directory / "events.jsonl").read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    for line in lines:
        if line.endswith(b"\n"):
            events.append(json.loads(line))
    positions = [{"fen": config["initial_fen"], "san": "Start", "uci": None}]
    responses = []
    pending = None
    summary = None
    for event in events:
        kind = event["type"]
        if kind == "move_requested":
            pending = event
        elif kind in ("move_response", "move_failed"):
            raw = event.get("raw")
            message = raw.get("message", {}) if isinstance(raw, dict) else {}
            message = message if isinstance(message, dict) else {}
            responses.append({"player": pending["player"] if pending else "Unknown",
                              "ply": pending["ply"] if pending else len(positions),
                              "text": event.get("text", message.get("content", "")),
                              "seconds": event.get("elapsed_seconds"),
                              "thinking": message.get("thinking", ""),
                              "failure_reason": event.get("failure_reason") or event.get("reason"),
                              "error": event.get("message") if kind == "move_failed" else None,
                              "applied": False})
            pending = None
        elif kind == "move_applied":
            positions.append({"fen": event["fen"], "san": event["san"], "uci": event["uci"]})
            if responses:
                responses[-1]["applied"] = True
        elif kind in ("game_finished", "initialization_failed"):
            summary = event
            pending = None
    # Derive terminal state from this same event snapshot so that a newly written
    # summary cannot race ahead of the moves shown on the board.
    return {"id": directory.name, "config": config,
            "engine_name": manifest.get("engine_id", {}).get("name", "Stockfish"),
            "positions": positions, "responses": responses, "pending": pending,
            "summary": summary, "last_event": events[-1]["time"] if events else None,
            "sequence": events[-1]["sequence"] if events else 0}


def make_handler(root):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, content, content_type="application/json", status=200):
            if not isinstance(content, bytes):
                content = json.dumps(content).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            url = urlsplit(self.path)
            try:
                if url.path in ("/", "/app.js", "/style.css"):
                    name, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                                  "/style.css": ("style.css", "text/css; charset=utf-8")}[url.path]
                    self.send((STATIC / name).read_bytes(), mime)
                elif url.path == "/api/runs":
                    runs = []
                    for directory in sorted(root.iterdir(), reverse=True) if root.exists() else []:
                        if directory.is_dir() and not directory.is_symlink():
                            try:
                                manifest = read_json(directory / "manifest.json")
                            except (ValueError, OSError):
                                continue
                            if manifest:
                                runs.append({"id": directory.name, "model": manifest["config"]["llm"]["model"]})
                    self.send(runs)
                elif url.path.startswith("/api/runs/"):
                    self.send(snapshot(run_directory(root, url.path.removeprefix("/api/runs/"))))
                elif url.path == "/api/board":
                    query = parse_qs(url.query)
                    board = chess.Board(query.get("fen", [chess.STARTING_FEN])[0])
                    last = query.get("last", [""])[0]
                    move = chess.Move.from_uci(last) if last else None
                    svg = chess.svg.board(board, lastmove=move, check=board.king(board.turn) if board.is_check() else None,
                                          orientation=query.get("flip", ["0"])[0] != "1", size=640)
                    self.send(svg.encode(), "image/svg+xml")
                else:
                    self.send({"error": "Not found"}, status=404)
            except (FileNotFoundError, ValueError, KeyError) as exc:
                self.send({"error": str(exc)}, status=404 if isinstance(exc, FileNotFoundError) else 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send({"error": "Run files temporarily unavailable; retrying may help"}, status=503)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=ROOT / "runs")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.runs.resolve()))
    print(f"Chess viewer: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Reading: {args.runs.resolve()} (Ctrl+C stops viewer only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
