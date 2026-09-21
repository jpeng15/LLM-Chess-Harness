"""Player adapters with explicit unassisted and legal-move prompt modes."""
import asyncio
from dataclasses import dataclass
import json
import time

import chess
import chess.engine
import httpx

from .limits import PlayerFailure, context_error, cutoff_reason

PROMPT_VERSION = "unassisted-v2"
PROMPT_VERSIONS = {"unassisted": PROMPT_VERSION, "legal-moves": "legal-moves-v2",
                   "constrained-legal": "constrained-legal-v1", "rules-tools": "rules-tools-v2",
                   "authored-validator": "authored-validator-v1"}


def prompt_version(mode):
    if mode not in PROMPT_VERSIONS:
        raise ValueError(f"Unsupported assistance mode: {mode!r}")
    return PROMPT_VERSIONS[mode]


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


ASSISTED_ADVICE = (
    "\nPlay for a win while keeping your king and pieces safe. Before choosing, consider "
    "the opponent's threats and likely reply. When there is no urgent tactic, develop "
    "inactive knights and bishops, improve king safety, and coordinate your pieces. "
    "Use the history to avoid pointless back-and-forth moves, but repeat if it is the "
    "best defense or secures a draw. The first legal move is not necessarily the best move."
)


ASSISTED_SYSTEM_PROMPT = SYSTEM_PROMPT + (
    "\nThe position includes every legal move in sorted UCI order, without rankings. "
    "Choose exactly one of the listed moves."
) + ASSISTED_ADVICE

CONSTRAINED_SYSTEM_PROMPT = (
    "You play standard chess. Choose a move for the side to move in the supplied position.\n"
    "Return exactly one JSON object with a single key, move, whose value is one of the "
    "listed legal moves in lowercase UCI coordinate notation. For promotion, include "
    "the final piece letter. The history uses SAN, but the move value must use UCI. "
    "Follow the supplied JSON schema. Do not include extra keys, markdown, or explanations. "
    "The list contains every legal move in sorted order, without rankings."
) + ASSISTED_ADVICE


def legal_move_schema(board):
    moves = sorted(move.uci() for move in board.legal_moves)
    if not moves:
        raise ValueError("Cannot request a constrained move from a position with no legal moves")
    return {"type": "object", "properties": {"move": {"type": "string", "enum": moves}},
            "required": ["move"], "additionalProperties": False}


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def constrained_move(content, board):
    """Validate the entire response; never extract a move from malformed JSON."""
    try:
        value = json.loads(content, object_pairs_hook=unique_json_object)
        if not isinstance(value, dict) or set(value) != {"move"} or not isinstance(value["move"], str):
            raise ValueError("Expected exactly one string field named move")
    except (ValueError, RecursionError):
        return content, "malformed_response"
    move = value["move"]
    # Exact enum membership also rejects whitespace, SAN and invalid coordinates.
    return move, None if move in legal_move_schema(board)["properties"]["move"]["enum"] else "illegal_move"


def observation(board: chess.Board, mode="unassisted") -> str:
    prompt_version(mode)
    replay = board.root()
    history = []
    for move in board.move_stack:
        prefix = f"{replay.fullmove_number}." if replay.turn else f"{replay.fullmove_number}..."
        history.append(f"{prefix} {replay.san(move)}")
        replay.push(move)
    rows = str(board).splitlines()
    diagram = "\n".join(f"{8-i} {row}" for i, row in enumerate(rows))
    instruction = ("Choose your move. Reply with only the origin and destination squares "
                   "in lowercase UCI notation (4 characters, or 5 for promotion). Do not use SAN.")
    if mode == "constrained-legal":
        instruction = 'Choose your move. Return only a JSON object with the single field "move" containing a listed UCI move.'
    elif mode == "rules-tools":
        instruction = 'This is real position 0. Return a JSON simulate or play action matching the supplied schema.'
    text = (
        f"Side to move: {'White' if board.turn else 'Black'}\nFEN: {board.fen()}\n"
        f"Board (uppercase White, lowercase Black, dot empty):\n{diagram}\n  a b c d e f g h\n"
        f"Move history: {' '.join(history) or 'none'}\n"
        f"{instruction}"
    )
    if mode in ("legal-moves", "constrained-legal", "rules-tools"):
        moves = sorted(move.uci() for move in board.legal_moves)
        text += (f"\nLegal moves in UCI notation (sorted, not ranked; {len(moves)} moves):\n"
                 + " ".join(moves)
                 + ("\nThese are the legal moves at real position 0."
                    if mode == "rules-tools" else
                    "\nChoose exactly one move from this list as the JSON move value."
                    if mode == "constrained-legal" else
                    "\nChoose exactly one move from this list. Return only that UCI move."))
    return text


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
        self.mode = config.get("mode", "unassisted")
        prompt_version(self.mode)

    def request(self, board):
        system = SYSTEM_PROMPT
        if self.mode == "legal-moves":
            system = ASSISTED_SYSTEM_PROMPT
        elif self.mode == "constrained-legal":
            system = CONSTRAINED_SYSTEM_PROMPT
        request = {
            "model": self.name, "stream": False, "think": self.config["think"],
            "truncate": False, "shift": False,
            "keep_alive": "30m",
            "options": {"num_ctx": self.config["context"], "num_predict": self.config["tokens"],
                        "temperature": self.config["temperature"], "seed": self.config["seed"]},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": observation(board, self.mode)}],
        }

        if self.mode == "constrained-legal":
            request["format"] = legal_move_schema(board)
            request["messages"][1]["content"] += "\nJSON schema: " + json.dumps(request["format"])
        return request

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
        text, failure = message["content"], cutoff_reason(data, self.config["tokens"])
        if self.mode == "constrained-legal" and failure is None:
            text, failure = constrained_move(text, board)
        return Reply(text, elapsed, data, failure)

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
