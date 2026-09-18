"""Authoritative referee with append-only events and replayable PGN."""
from datetime import datetime, timezone
import json
import re
import time

import chess
import chess.pgn

from .limits import PlayerFailure


class Recorder:
    def __init__(self, directory, *, exist_ok=False, mode="unassisted"):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=exist_ok)
        self.sequence = 0
        self.mode = mode

    def write(self, name, value):
        path = self.directory / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def event(self, kind, **fields):
        self.sequence += 1
        event = {"sequence": self.sequence, "time": datetime.now(timezone.utc).isoformat(),
                 "type": kind, **fields}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(event) + "\n")

    def pgn(self, board, players, result="*", reason="in_progress"):
        game = chess.pgn.Game.from_board(board)
        event = "Legal-move-assisted LLM vs engine" if self.mode == "legal-moves" else "Unassisted LLM vs engine"
        game.headers.update({"Event": event, "White": players[chess.WHITE].name,
                             "Black": players[chess.BLACK].name, "Result": result,
                             "Termination": reason, "Date": datetime.now().strftime("%Y.%m.%d")})
        path = self.directory / "game.pgn"
        temporary = path.with_suffix(".pgn.tmp")
        temporary.write_text(str(game) + "\n", encoding="utf-8")
        temporary.replace(path)


def run_game(board, players, llm_color, max_plies, recorder):
    start = time.monotonic()
    plies = 0
    result, status, reason = "*", "in_progress", "in_progress"
    recorder.pgn(board, players)
    try:
        while True:
            outcome = board.outcome(claim_draw=True)
            if outcome:
                result, status, reason = outcome.result(), "completed", outcome.termination.name.lower()
                break
            if plies >= max_plies:
                status, reason = "truncated", "max_plies"
                break
            color = board.turn
            player = players[color]
            request = player.request(board.copy()) if hasattr(player, "request") else None
            recorder.event("move_requested", ply=plies + 1, fen=board.fen(),
                           player=player.name, request=request)
            print(f"{board.fullmove_number}{'.' if color else '...'} {player.name} thinking...", flush=True)
            move_start = time.monotonic()
            try:
                reply = player.choose(board.copy())
            except TimeoutError:
                if color != llm_color:
                    raise
                result = "0-1" if color else "1-0"
                status, reason = "forfeit", "timeout"
                recorder.event("move_timeout", player=player.name, elapsed_seconds=time.monotonic() - move_start)
                break
            except PlayerFailure as exc:
                reason = exc.reason
                status = "truncated" if color == llm_color and reason == "context_limit" else "infrastructure_failure"
                recorder.event("move_failed", player=player.name, reason=reason, message=str(exc),
                               elapsed_seconds=exc.elapsed_seconds, raw=exc.raw)
                break
            recorder.event("move_response", text=reply.text, elapsed_seconds=reply.elapsed,
                           raw=reply.raw, failure_reason=reply.failure_reason)
            if reply.failure_reason:
                reason = reply.failure_reason
                if color != llm_color:
                    status = "infrastructure_failure"
                elif reason == "output_limit":
                    status, result = "forfeit", "0-1" if color else "1-0"
                else:
                    status = "truncated"
                break
            text = reply.text.strip()
            move = chess.Move.from_uci(text) if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", text) and text[:2] != text[2:4] else None
            if move is None or move not in board.legal_moves:
                if color != llm_color:
                    raise RuntimeError(f"Engine returned invalid move: {text!r}")
                result = "0-1" if color else "1-0"
                status, reason = "forfeit", "malformed_response" if move is None else "illegal_move"
                break
            san = board.san(move)
            board.push(move)
            plies += 1
            recorder.event("move_applied", ply=plies, uci=text, san=san, fen=board.fen())
            recorder.pgn(board, players)
            print(f"  {san} ({text})  {reply.elapsed:.2f}s", flush=True)
    except KeyboardInterrupt:
        status, reason = "interrupted", "user_interrupt"
    except Exception as exc:
        status, reason = "infrastructure_failure", type(exc).__name__
        recorder.event("error", error_type=type(exc).__name__, message=str(exc))
    summary = {"status": status, "result": result, "reason": reason, "plies": plies,
               "final_fen": board.fen(), "elapsed_seconds": time.monotonic() - start}
    recorder.event("game_finished", **summary)
    recorder.pgn(board, players, result, reason)
    recorder.write("summary.json", summary)
    return summary
