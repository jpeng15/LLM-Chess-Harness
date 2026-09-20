"""Bounded model-directed rules tools over schema-constrained JSON actions."""
import asyncio
import json
import time

import httpx

from .limits import PlayerFailure, cutoff_reason
from .players import ASSISTED_ADVICE, OllamaPlayer, Reply, observation, unique_json_object
from .rules import RulesSession, board_facts


TOOL_PROMPT = (
    "You play standard chess using a rules-only simulation tool. You choose all moves and hypothetical replies. "
    "No engine advice or evaluations are available. Position 0 is the real position. "
    "Return exactly one JSON action matching the supplied schema. "
    'To inspect a hypothetical move return {"action":"simulate","position":0,"move":"e2e4"}, '
    "using a legal move at the named position. The tool returns a new position ID, board, legal replies, "
    "piece locations/counts, attack maps, legal captures/checks and any game outcome. "
    "Attack maps can include pinned pieces; consult legal captures and simulations rather than assuming "
    "a piece is safe from attack counts alone. You may continue from that ID to explore an opponent reply, "
    "or branch again from position 0. Simulations never change the real board. "
    "You must simulate at least one move before committing. Use further simulations to check a dangerous opponent reply "
    "or compare another candidate. "
    'To commit return {"action":"play","move":"e2e4"}, selecting a legal move from position 0, '
    "not a hypothetical position. Examples show format only. At the simulation limit you must play. "
    "Do not include extra fields, markdown or explanations."
) + ASSISTED_ADVICE


class RulesToolPlayer(OllamaPlayer):
    def __init__(self, config, emit=None):
        super().__init__(config)
        settings = config["tools"]
        if (settings.get("version") != "rules-tools-v1" or settings.get("minimum_calls") != 1
                or settings.get("output_budget") != "per-turn"
                or type(settings.get("calls")) is not int or not 1 <= settings["calls"] <= 16
                or type(settings.get("depth")) is not int or not 1 <= settings["depth"] <= 8):
            raise ValueError("Unsupported rules-tool policy or limits; start a new run with valid settings")
        self.emit = emit or (lambda *args, **kwargs: None)

    def session(self, board):
        settings = self.config["tools"]
        return RulesSession(board, calls=settings["calls"], depth=settings["depth"])

    def initial_messages(self, board):
        settings = self.config["tools"]
        return [{"role": "system", "content": TOOL_PROMPT},
                {"role": "user", "content": observation(board, "rules-tools") +
                 "\nVerified board facts: " + json.dumps(board_facts(board), separators=(",", ":")) +
                 f"\nSimulation limit: {settings['calls']} calls, depth {settings['depth']} plies. "
                 "All output shares one turn budget. All position IDs reset on the next real turn."}]

    def request(self, board):
        request = super().request(board)
        request["messages"] = self.initial_messages(board)
        request["format"] = self.session(board).schema()
        return request

    def choose(self, board):
        start = time.monotonic()
        try:
            return asyncio.run(self._turn(board, start))
        except PlayerFailure as exc:
            exc.elapsed_seconds = time.monotonic() - start
            raise
        except httpx.TransportError as exc:
            raise PlayerFailure("ollama_transport_error", str(exc), elapsed_seconds=time.monotonic() - start) from exc

    async def _turn(self, board, start):
        session = self.session(board)
        request = self.request(board)
        remaining = self.config["tokens"]
        # One deadline covers every inference call and simulation in this turn.
        seconds_left = self.config["seconds"] - (time.monotonic() - start)
        if seconds_left <= 0:
            raise TimeoutError
        async with asyncio.timeout(seconds_left):
            for call in range(1, session.calls + 2):
                request["format"] = session.schema()
                request["options"]["num_predict"] = remaining
                self.emit("model_call_requested", call=call, request=request)
                call_start = time.monotonic()
                raw = await self._post("/api/chat", request, self.config["seconds"])
                self.emit("model_call_response", call=call, raw=raw, elapsed_seconds=time.monotonic() - call_start)
                message = raw.get("message")
                if raw.get("done") is not True or not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise PlayerFailure("incomplete_response", "Incomplete tool action response", raw=raw)
                if raw.get("done_reason") not in ("stop", "length"):
                    raise PlayerFailure("unknown_stop_reason", "Unknown tool action stop reason", raw=raw)
                content = message["content"]
                failure = cutoff_reason(raw, remaining)
                if failure:
                    return Reply(content, time.monotonic() - start, raw, failure)
                count = raw.get("eval_count")
                if type(count) is not int or count < 0:
                    raise PlayerFailure("tool_usage_missing", "Cannot enforce total turn output budget without eval_count", raw=raw)
                if count > remaining:
                    return Reply(content, time.monotonic() - start, raw, "output_limit")
                remaining -= count
                try:
                    action = json.loads(content, object_pairs_hook=unique_json_object)
                    session.validate(action)
                except (ValueError, RecursionError):
                    return Reply(content, time.monotonic() - start, raw, "invalid_tool_action")
                if action["action"] == "play":
                    if time.monotonic() - start >= self.config["seconds"]:
                        raise TimeoutError
                    return Reply(action["move"], time.monotonic() - start, raw)
                result = session.simulate(action)
                self.emit("simulation_result", call=call, action=action, result=result)
                if remaining == 0:
                    return Reply(content, time.monotonic() - start, raw, "output_limit")
                # This is a JSON action protocol, not Ollama native tool calling.
                request["messages"].append({"role": "assistant", "content": content})
                request["messages"].append({"role": "user", "content": "Simulation result: " + json.dumps(result) +
                    f"\nSimulations remaining: {session.calls - len(session.positions) + 1}. "
                    f"Output tokens remaining for this turn: {remaining}. "
                    "Choose another simulation or play a legal move from position 0. Follow the current action schema."})
        raise RuntimeError("Tool loop exceeded its action bound")
