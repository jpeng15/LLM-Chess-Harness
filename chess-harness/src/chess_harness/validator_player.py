"""Opt-in game adapter for a frozen, independently checked authored validator."""
import asyncio
import json
from pathlib import Path
import threading
import time

from .limits import PlayerFailure, cutoff_reason
from .players import ASSISTED_ADVICE, OllamaPlayer, Reply, observation, unique_json_object
from .rules import RulesSession, board_facts
from .tool_player import RulesToolPlayer
from .validator_artifacts import ArtifactError, load_artifact
from .validator_contract import CONTRACT_VERSION, RULES_API_VERSION, ValidatorContractError
from .validator_verify import verify_result


PROMPT = (
    "You play standard chess using rules-only tools and your frozen authored validator. "
    "No engine analysis is available. Return one JSON action matching the schema. "
    "Position 0 is the real board. simulate explores a legal move at a position ID and returns a new ID; "
    "validate checks a candidate legal move from position 0 using your frozen function. "
    "play commits a legal move from position 0. All simulations and validations share one call limit. "
    "You must validate at least one candidate before playing; reserve a call for this requirement. "
    "Validator facts have independently checked legal witnesses. They are incomplete observations, not "
    "an exhaustive safety guarantee. Heuristics are your function's unverified interpretations. "
    "A queen being capturable does not establish that a sacrifice is bad. Use judgment and legal replies. "
    "Attack maps include pinned pieces; use legal captures to check actual threats. "
    "You may play any root legal move, including a different candidate. Tools never alter the real board. "
    "No extra fields, markdown, or explanations. At the call limit, play."
) + ASSISTED_ADVICE


def configured_artifact(config):
    """Gate before reading artifact files, including on batch resume."""
    settings = config.get("validator")
    if config.get("mode") != "authored-validator":
        if settings:
            raise PlayerFailure("validator_disabled", "Validator settings require authored-validator mode")
        return None
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        raise PlayerFailure("validator_disabled", "Explicit authored-validator enablement is required")
    try:
        artifact = load_artifact(Path(settings["artifact"]))
        if artifact.artifact_id != settings["artifact_id"]:
            raise ArtifactError("artifact_mismatch", "Artifact differs from the saved configuration")
        return artifact
    except (ArtifactError, OSError, ValueError, KeyError, TypeError) as exc:
        raise PlayerFailure("validator_" + getattr(exc, "reason", "artifact_error"), str(exc)) from exc


def validation_request(board, candidate):
    return {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "position": {"fen": board.fen(en_passant="fen")},
            "history": {"initial_fen": board.root().fen(en_passant="fen"),
                        "moves": [move.uci() for move in board.move_stack]}, "candidate": candidate}


class AuthoredSession(RulesSession):
    def __init__(self, board, calls, depth):
        super().__init__(board, calls, depth)
        self.used = 0
        self.validated = False

    def schema(self):
        def action(kind, board, position=None):
            properties = {"action": {"type": "string", "enum": [kind]},
                          "move": {"type": "string", "enum": sorted(m.uci() for m in board.legal_moves)}}
            if position is not None:
                properties["position"] = {"type": "integer", "enum": [position]}
            return {"type": "object", "properties": properties, "required": list(properties),
                    "additionalProperties": False}
        choices = []
        if self.used < self.calls:
            choices.append(action("validate", self.positions[0]))
            if self.validated or self.used < self.calls - 1:
                choices.extend(action("simulate", board, index) for index, board in enumerate(self.positions)
                               if self.expandable(index))
        if self.validated:
            choices.append(action("play", self.positions[0]))
        return {"anyOf": choices}

    def validate(self, action):
        if type(action) is not dict:
            raise ValueError("Expected action object")
        # Membership against the current finite schema enforces root legality,
        # branch IDs, exact fields, call limits, and the required validation.
        for choice in self.schema()["anyOf"]:
            props = choice["properties"]
            if set(action) == set(props) and all(
                type(action[key]) is (int if spec["type"] == "integer" else str)
                and action[key] in spec["enum"] for key, spec in props.items()
            ):
                return
        raise ValueError("Action does not match the current legal schema")

    def simulate(self, action):
        result = super().simulate(action)
        self.used += 1
        return result


class AuthoredValidatorPlayer(RulesToolPlayer):
    def __init__(self, config, artifact, backend, emit=None):
        # Preserve baseline implementation; this adapter owns its different policy.
        OllamaPlayer.__init__(self, config)
        settings = config.get("tools", {})
        if (config.get("mode") != "authored-validator" or config.get("validator", {}).get("enabled") is not True
                or settings.get("version") != "authored-tools-v1" or settings.get("minimum_calls") != 1
                or settings.get("output_budget") != "per-turn"
                or type(settings.get("calls")) is not int or not 1 <= settings["calls"] <= 16
                or type(settings.get("depth")) is not int or not 1 <= settings["depth"] <= 8
                or artifact.artifact_id != config["validator"].get("artifact_id")):
            raise ValueError("Unsupported authored-validator policy or missing explicit enablement")
        self.artifact, self.backend = artifact, backend
        self.emit = emit or (lambda *args, **kwargs: None)

    def session(self, board):
        return AuthoredSession(board, self.config["tools"]["calls"], self.config["tools"]["depth"])

    def initial_messages(self, board):
        return [{"role": "system", "content": PROMPT}, {"role": "user", "content":
                observation(board, "rules-tools") + "\nVerified board facts: " + json.dumps(board_facts(board)) +
                f"\nCombined tool call limit: {self.config['tools']['calls']}; "
                f"simulation depth: {self.config['tools']['depth']}. All output shares one turn budget."}]

    async def _validation(self, board, candidate, call, deadline):
        request = validation_request(board, candidate)
        self.emit("validator_requested", call=call, artifact_id=self.artifact.artifact_id,
                  candidate=candidate, input=request)
        cancel = threading.Event()
        task = None
        interrupted = False
        execution = None
        artifact_check_seconds = None
        try:
            # Check every provenance byte before execution; only verified bytes go
            # to the backend, so a path replacement cannot alter the worker input.
            check_started = time.monotonic()
            try:
                current = configured_artifact(self.config)
                if current.source != self.artifact.source:
                    raise PlayerFailure("validator_artifact_mismatch", "Frozen source changed")
            finally:
                artifact_check_seconds = time.monotonic() - check_started
            task = asyncio.create_task(asyncio.to_thread(self.backend.run_prepared, current.source, request,
                                                        deadline=deadline, cancel_event=cancel))
            try:
                execution = await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
                cancel.set()
                # Cleanup has its own bounded deadline. Never leave the container
                # running or lose its diagnostics when the turn deadline expires.
                execution = await asyncio.shield(task)
            if (type(execution) is not dict or execution.get("status") not in {"ok", "error"}
                    or type(execution.get("reason")) is not str or not execution["reason"]):
                raise ValueError("Sandbox returned a malformed execution report")
            if interrupted or time.monotonic() >= deadline:
                cleanup_failed = execution.get("reason") == "cleanup_failed" or execution.get("cleanup_confirmed") is False
                execution = {**execution, "status": "error", "turn_deadline_exceeded": True,
                             "reason": "cleanup_failed" if cleanup_failed else "timeout"}
                execution.pop("findings", None)
            elif execution.get("status") == "ok":
                execution["findings"] = verify_result(request, execution.get("findings"))
        except Exception as exc:
            reported_reason = getattr(exc, "reason", None)
            reason = reported_reason.removeprefix("validator_") if isinstance(reported_reason, str) and reported_reason else "execution_error"
            execution = {**(execution if isinstance(execution, dict) else {}),
                         "status": "error", "reason": reason, "message": str(exc)}
            if hasattr(exc, "path"):
                execution["path"] = exc.path
            execution.pop("findings", None)
        execution["artifact_check_seconds"] = artifact_check_seconds
        self.emit("validator_result", call=call, artifact_id=self.artifact.artifact_id,
                  candidate=candidate, input=request, execution=execution)
        if execution.get("status") != "ok":
            raise PlayerFailure("validator_" + execution.get("reason", "execution_error"),
                                execution.get("message", "Authored validator failed; no fallback is permitted"), raw=execution)
        return execution["findings"]

    async def _turn(self, board, start):
        session, request = self.session(board), self.request(board)
        remaining, deadline = self.config["tokens"], start + self.config["seconds"]
        async with asyncio.timeout_at(deadline):
            for call in range(1, session.calls + 2):
                request["format"], request["options"]["num_predict"] = session.schema(), remaining
                self.emit("model_call_requested", call=call, request=request)
                begin = time.monotonic()
                raw = await self._post("/api/chat", request, max(0.001, deadline - begin))
                self.emit("model_call_response", call=call, raw=raw, elapsed_seconds=time.monotonic() - begin)
                message = raw.get("message")
                if raw.get("done") is not True or not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise PlayerFailure("incomplete_response", "Incomplete authored-tool response", raw=raw)
                if raw.get("done_reason") not in ("stop", "length"):
                    raise PlayerFailure("unknown_stop_reason", "Unknown authored-tool stop reason", raw=raw)
                content = message["content"]
                failure = cutoff_reason(raw, remaining)
                if failure:
                    return Reply(content, time.monotonic() - start, raw, failure)
                count = raw.get("eval_count")
                if type(count) is not int or count < 0:
                    raise PlayerFailure("tool_usage_missing", "Total output budget requires eval_count", raw=raw)
                if count > remaining:
                    return Reply(content, time.monotonic() - start, raw, "output_limit")
                remaining -= count
                try:
                    action = json.loads(content, object_pairs_hook=unique_json_object)
                    session.validate(action)
                except (ValueError, RecursionError):
                    return Reply(content, time.monotonic() - start, raw, "invalid_tool_action")
                kind = action["action"]
                if kind == "play":
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    return Reply(action["move"], time.monotonic() - start, raw)
                if kind == "simulate":
                    result = session.simulate(action)
                    self.emit("simulation_result", call=call, action=action, result=result)
                    label = "Simulation result"
                else:
                    result = await self._validation(board, action["move"], call, deadline)
                    session.used += 1
                    session.validated = True
                    label = "Validator findings (facts verified; heuristics unverified)"
                if remaining == 0:
                    return Reply(content, time.monotonic() - start, raw, "output_limit")
                request["messages"].extend([{"role": "assistant", "content": content},
                    {"role": "user", "content": label + ": " + json.dumps(result) +
                     f"\nTool calls remaining: {session.calls - session.used}. Output tokens remaining: {remaining}. "
                     "Follow the current action schema; play selects a root legal move."}])
        raise RuntimeError("Authored-tool loop exceeded its action bound")
