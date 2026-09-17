"""Player adapters; the LLM receives no legal-move list or corrective feedback."""
import asyncio
from dataclasses import dataclass
import time

import chess
import chess.engine
import httpx

from .limits import PlayerFailure, context_error, cutoff_reason

PROMPT_VERSION = "unassisted-v2"
SYSTEM_PROMPT = (
    "You play standard chess. Choose a move for the side to move in the supplied position.\n"
    "Output exactly one move in UCI coordinate notation: the two-character origin square "
    "followed by the two-character destination square, all lowercase. "
    "For a promotion, append one lowercase piece letter: q, r, b, or n.\n"
    "Notation examples only (not suggested moves for the supplied position):\n"
    "- Ordinary move: g1f3\n"
    "- White kingside castling: e1g1; White queenside castling: e1c1\n"
    "- Black kingside castling: e8g8; Black queenside castling: e8c8\n"
    "- Promotion: a7a8q\n"
    "The history uses SAN for readability, but your answer must use UCI. "
    "Do not output SAN such as Nf3, e4, O-O, or a8=Q. "
    "Do not include piece names, capture/check symbols, move numbers, quotes, "
    "markdown, explanations, or alternative moves. Return only the UCI move."
)


def observation(board: chess.Board) -> str:
    replay = board.root()
    history = []
    for move in board.move_stack:
        prefix = f"{replay.fullmove_number}." if replay.turn else f"{replay.fullmove_number}..."
        history.append(f"{prefix} {replay.san(move)}")
        replay.push(move)
    rows = str(board).splitlines()
    diagram = "\n".join(f"{8-i} {row}" for i, row in enumerate(rows))
    return (
        f"Side to move: {'White' if board.turn else 'Black'}\nFEN: {board.fen()}\n"
        f"Board (uppercase White, lowercase Black, dot empty):\n{diagram}\n  a b c d e f g h\n"
        f"Move history: {' '.join(history) or 'none'}\n"
        "Choose your move. Reply with only the origin and destination squares "
        "in lowercase UCI notation (4 characters, or 5 for promotion). Do not use SAN."
    )


@dataclass
class Reply:
    text: str
    elapsed: float
    raw: dict
    failure_reason: str | None = None


class OllamaPlayer:
    def __init__(self, config):
        self.config = config
        self.name = config["model"]

    def request(self, board):
        return {
            "model": self.name, "stream": False, "think": self.config["think"],
            "truncate": False, "shift": False,
            "keep_alive": "30m",
            "options": {"num_ctx": self.config["context"], "num_predict": self.config["tokens"],
                        "temperature": self.config["temperature"], "seed": self.config["seed"]},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": observation(board)}],
        }

    async def _post(self, path, body, seconds):
        # Covers connection, response generation, and body receipt with one deadline.
        async with asyncio.timeout(seconds):
            async with httpx.AsyncClient(base_url=self.config["url"], timeout=None, trust_env=False) as client:
                response = await client.post(path, json=body)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise PlayerFailure("invalid_server_response", "Ollama returned non-JSON data.",
                                        raw={"http_status": response.status_code, "body": response.text}) from exc
                if response.is_error or (isinstance(data, dict) and data.get("error")):
                    reason = "context_limit" if context_error(data) else "ollama_http_error"
                    raise PlayerFailure(reason, f"Ollama request failed (HTTP {response.status_code}): {data}",
                                        raw={"http_status": response.status_code, "body": data})
                if not isinstance(data, dict):
                    raise PlayerFailure("invalid_server_response", "Ollama returned a non-object JSON response.", raw=data)
                return data

    def warmup(self):
        return asyncio.run(self._post("/api/generate", {
            "model": self.name, "prompt": "", "stream": False, "keep_alive": "30m",
            "truncate": False, "shift": False,
            "options": {"num_ctx": self.config["context"]},
        }, 180))

    def choose(self, board):
        start = time.monotonic()
        try:
            data = asyncio.run(self._post("/api/chat", self.request(board), self.config["seconds"]))
        except PlayerFailure as exc:
            exc.elapsed_seconds = time.monotonic() - start
            raise
        except httpx.TransportError as exc:
            raise PlayerFailure("ollama_transport_error", str(exc), elapsed_seconds=time.monotonic() - start) from exc
        elapsed = time.monotonic() - start
        message = data.get("message")
        if data.get("done") is not True or not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise PlayerFailure("incomplete_response", "Incomplete or unexpected Ollama response.", raw=data, elapsed_seconds=elapsed)
        if data.get("done_reason") not in ("stop", "length"):
            raise PlayerFailure("unknown_stop_reason", "Ollama did not report a recognized completion reason.", raw=data, elapsed_seconds=elapsed)
        return Reply(message["content"], elapsed, data, cutoff_reason(data, self.config["tokens"]))

    def verify_loaded_context(self):
        with httpx.Client(base_url=self.config["url"], timeout=10, trust_env=False) as client:
            response = client.get("/api/ps")
            response.raise_for_status()
            data = response.json()
        canonical = self.name if ":" in self.name.rsplit("/", 1)[-1] else self.name + ":latest"
        models = data.get("models", []) if isinstance(data, dict) else []
        loaded = next((model for model in models if model.get("name") in (self.name, canonical)
                       or model.get("model") in (self.name, canonical)), None)
        if loaded is None or type(loaded.get("context_length")) is not int:
            raise PlayerFailure("context_verification_failed", "Ollama did not report the loaded model's context size.", raw=data)
        if loaded["context_length"] != self.config["context"]:
            raise PlayerFailure("context_size_mismatch",
                                f"Requested {self.config['context']} context tokens, but Ollama loaded {loaded['context_length']}. "
                                "Choose a supported --context value and start a new run.", raw=loaded)
        return loaded


class EnginePlayer:
    def __init__(self, config):
        self.engine = chess.engine.SimpleEngine.popen_uci(config["path"], timeout=config["seconds"])
        try:
            self.engine.configure({"Threads": 1, "Hash": config["hash_mb"], "Skill Level": config["skill"]})
        except BaseException:
            self.engine.close()
            raise
        self.name = self.engine.id.get("name", "UCI engine")
        self.nodes = config["nodes"]

    def choose(self, board):
        start = time.monotonic()
        result = self.engine.play(board, chess.engine.Limit(nodes=self.nodes), ponder=False)
        return Reply(result.move.uci() if result.move else "", time.monotonic() - start, {})

    def close(self):
        try:
            self.engine.quit()
        finally:
            self.engine.close()
