"""Bounded model-authored validator development, separate from evaluation.

This module never executes generated Python. Each source is sent only to a
prepared sandbox. Fixtures and expected findings below are development data;
neither held-out positions nor engine analysis enter generation or repair.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

import chess
import httpx

from .limits import PlayerFailure, context_error, cutoff_reason, require_supported_ollama
from .players import OllamaPlayer, unique_json_object
from .storage import atomic_write
from .validator_contract import CONTRACT_VERSION, RULES_API_VERSION, ValidatorContractError, result_schema
from .validator_rules import RulesBoard
from .validator_sandbox import (
    DockerSandbox, MAX_SOURCE_BYTES, SandboxConfig, SandboxFailure,
    policy_limits, runtime_source_hash,
)
from .validator_verify import verify_result


PROMPT_VERSION = "validator-generation-v1"
MAX_FEEDBACK_BYTES = 8 * 1024
MAX_GENERATION_CONTENT_BYTES = 400 * 1024
MAX_GENERATION_RESPONSE_BYTES = 512 * 1024
REPAIRABLE_GENERATION = {"malformed_source_response", "source_limit", "output_limit", "generation_limit"}
REPAIRABLE_EXECUTION = {
    "syntax_error", "source_policy_error", "code_error", "invalid_result", "invalid_finding",
    "invalid_shape", "duplicate_fact_id", "invalid_reference", "invalid_json", "duplicate_json_key",
    "structure_limit", "output_limit", "stderr_limit", "timeout", "memory_limit", "process_terminated",
}

GENERATION_SYSTEM = (
    "Author a general Python chess-checking function validate(position, history, candidate). "
    "You supply all checking logic using only the rules API. Do not hardcode fixture positions, "
    "IDs or expected answers. No engine, model, filesystem, network, external library, or benchmark "
    "answer access is available. Return only a JSON object with the single string field source, "
    "containing complete Python source, without markdown. Module scope permits only function "
    "definitions and docstrings. Define exactly one validate with precisely these three argument "
    "names. Helpers are allowed. No imports, classes, decorators, annotations, defaults, async, "
    "global/nonlocal, context managers, match statements, private names/attributes, reflection, "
    "attribute assignment, exec/eval/open or replacement of API bindings. "
    "Available globals are RulesBoard and CONTRACT_VERSION. Small builtins include abs, all, any, "
    "bool, dict, enumerate, filter, float, frozenset, int, isinstance, iter, len, list, map, max, min, "
    "next, print, range, reversed, round, set, sorted, str, sum, tuple, zip and ordinary exception "
    "types. Avoid printing because stdout must contain only the returned result. "
    "RulesBoard.from_input(position, history) reconstructs an independent board with complete "
    "history. position is {fen}; history is {initial_fen,moves}, with UCI moves. "
    "RulesBoard methods: copy() retains history; fen(); side_to_move() returns white/black; "
    "legal_moves() returns sorted legal UCI strings or [] after the game ends; push(uci) applies "
    "a legal move; piece_at(square) returns a case-sensitive piece symbol or None; capture(uci) "
    "returns None or {capturing_piece,captured_piece,capture_square} BEFORE the move, identifying "
    "the actual captured-pawn square for en passant and the pawn before promotion; "
    "is_castling(uci); is_en_passant(uci); promotion(uci) returns the promoted piece symbol or "
    "None; is_check(); is_checkmate(); outcome() returns None or {result,reason}. Invalid moves "
    "and attempts to continue terminal positions raise ValueError. Outcome policy automatically "
    "claims available draws, including a draw claimable by the next move. "
    "Return the supplied result contract with facts and heuristics. Each fact is independently "
    "verified by replay; one false fact rejects the whole result. Initial facts concern only "
    "candidate checkmate, immediate opponent reply checkmate, or an immediate legal opponent "
    "capture after the candidate. Distinguish a true capture possibility from its strategic "
    "interpretation: a sacrifice may be good. Empty facts mean nothing reported, never safety. "
    "Complete the development criteria within the fixed resource limits."
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _request(candidate, *, fen=chess.STARTING_FEN, moves=()):
    board = chess.Board(fen)
    for move in moves:
        board.push_uci(move)
    value = {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
             "position": {"fen": board.fen()}, "history": {"initial_fen": fen, "moves": list(moves)},
             "candidate": candidate}
    RulesBoard.from_request(value)
    return value


def _capture(candidate, reply, attacker, victim, square):
    return {"kind": "capture_available", "line": [candidate, reply],
            "capturing_piece": attacker, "captured_piece": victim, "capture_square": square}


def development_cases():
    """Fresh copies of the versioned development-only acceptance cases."""
    return [
        {"id": "candidate_mate", "purpose": "Distinguish checkmate from check and stalemate.",
         "request": _request("f7h7", fen="7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"),
         "required_facts": [{"kind": "candidate_checkmate", "line": ["f7h7"]}]},
        {"id": "opponent_reply_mate", "purpose": "Find an immediate legal mating reply.",
         "request": _request("g2g4", moves=["f2f3", "e7e5"]),
         "required_facts": [{"kind": "reply_checkmate", "line": ["g2g4", "d8h4"]}]},
        {"id": "queen_capture", "purpose": "Report a queen capture as a fact, not a move verdict.",
         "request": _request("d1h5", moves=["e2e4", "g7g6"]),
         "required_facts": [_capture("d1h5", "g6h5", "p", "Q", "h5")]},
        {"id": "queen_offer", "purpose": "A queen offer can have compensation; facts do not certify a bad move.",
         "request": _request("f3e5", moves=["e2e4", "e7e5", "g1f3", "d7d6", "f1c4", "c8g4", "b1c3", "g7g6"]),
         "required_facts": [_capture("f3e5", "g4d1", "b", "Q", "d1")]},
        {"id": "pinned_capture", "purpose": "The e2 rook cannot capture a2 while pinned, but can capture its e8 pinner.",
         "request": _request("a8b8", fen="k3r3/8/8/8/8/8/q3R3/4K3 b - - 0 1"),
         "required_facts": [_capture("a8b8", "e2e8", "R", "r", "e8")]},
        {"id": "en_passant", "purpose": "Use the captured pawn's square rather than the landing square.",
         "request": _request("e2e4", fen="7k/8/8/8/3p4/8/4P3/7K w - - 0 1"),
         "required_facts": [_capture("e2e4", "d4e3", "p", "P", "e4")]},
        {"id": "capturing_promotion", "purpose": "Identify the capturing pawn before it promotes.",
         "request": _request("h8g8", fen="1r5k/P7/8/8/8/8/8/7K b - - 0 1"),
         "required_facts": [_capture("h8g8", "a7b8q", "P", "r", "b8")]},
        {"id": "castling", "purpose": "Simulate legal castling and inspect the resulting opponent captures.",
         "request": _request("e1g1", fen="r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"),
         "required_facts": [_capture("e1g1", "a8a1", "r", "R", "a1")]},
        {"id": "draw_history", "purpose": "Do not simulate an opponent reply after the history makes a draw claimable.",
         "request": _request("f3g1", moves=["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6"]),
         "required_facts": []},
        {"id": "candidate_stalemate", "purpose": "Do not mislabel a stalemate as checkmate.",
         "request": _request("f7e6", fen="7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"),
         "required_facts": []},
    ]


def suite_hash():
    return hashlib.sha256(_canonical(development_cases()).encode("utf-8")).hexdigest()


def check_case(case, execution):
    """Verify ALL facts and require specified positive findings, without code execution."""
    errors = []
    if type(execution) is not dict or execution.get("status") != "ok":
        value = execution if isinstance(execution, dict) else {}
        return {"passed": False, "findings": None, "errors": [{
            "reason": value.get("reason", "invalid_execution"),
            "message": value.get("message", "Validator execution did not complete successfully")}]}
    try:
        findings = verify_result(case["request"], execution.get("findings"))
    except ValidatorContractError as error:
        return {"passed": False, "findings": None, "errors": [
            {"reason": error.reason, "path": error.path, "message": str(error)}]}
    for required in case["required_facts"]:
        if not any(all(fact.get(key) == value for key, value in required.items()) for fact in findings["facts"]):
            errors.append({"reason": "missing_required_finding", "message": "Required development fact was not reported",
                           "required": deepcopy(required)})
    return {"passed": not errors, "errors": errors, "findings": findings}


def generation_request(llm, cases, feedback=None, previous_source=None):
    user = {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "result_schema": result_schema(), "limits": policy_limits(), "development_cases": cases}
    messages = [{"role": "system", "content": GENERATION_SYSTEM},
                {"role": "user", "content": _canonical(user)}]
    if previous_source is not None:
        messages.append({"role": "assistant", "content": _canonical({"source": previous_source})})
    if feedback is not None:
        messages.append({"role": "user", "content": "Repair the validator using these development diagnostics:\n" + feedback})
    return {"model": llm["model"], "stream": False, "think": llm["think"], "truncate": False, "shift": False,
            "keep_alive": "30m", "options": {"num_ctx": llm["context"], "num_predict": llm["tokens"],
                "temperature": llm["temperature"], "seed": llm["seed"]}, "messages": messages,
            "format": {"type": "object", "properties": {"source": {"type": "string", "minLength": 1,
                "maxLength": MAX_SOURCE_BYTES}}, "required": ["source"], "additionalProperties": False}}


def parse_source_response(content):
    """Parse only a bounded exact JSON source envelope; never extract/repair source."""
    try:
        if type(content) is not str or len(content.encode("utf-8")) > MAX_GENERATION_CONTENT_BYTES:
            raise ValueError
        # Valid envelopes are only one object/string deep; stop hostile nesting
        # before json.loads while ignoring brackets inside the source string.
        depth = 0
        quoted = escaped = False
        for char in content:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 4:
                    raise ValueError
            elif char in "]}":
                depth -= 1
        value = json.loads(content, object_pairs_hook=unique_json_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        if type(value) is not dict or set(value) != {"source"} or type(value["source"]) is not str:
            raise ValueError
        source = value["source"].encode("utf-8", errors="strict")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PlayerFailure("malformed_source_response", "Expected exactly one UTF-8 JSON string field named source") from None
    if not source or len(source) > MAX_SOURCE_BYTES:
        raise PlayerFailure("source_limit", "Generated source is empty or exceeds 64 KiB")
    return value["source"]


def _usage(raw, key):
    value = raw.get(key) if type(raw) is dict else None
    return value if type(value) is int and value >= 0 else None


class OllamaValidatorGenerator(OllamaPlayer):
    """Uses the existing strict Ollama HTTP/context helpers for source generation."""

    def __init__(self, llm):
        super().__init__({**llm, "mode": "unassisted"})
        self._prepared_identity = None

    async def _http(self, method, path, body, seconds):
        """Bound all model-service response bytes before JSON parsing or storage."""
        async with asyncio.timeout(seconds):
            async with httpx.AsyncClient(base_url=self.config["url"], timeout=None, trust_env=False) as client:
                async with client.stream(method, path, json=body) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        remaining = MAX_GENERATION_RESPONSE_BYTES - len(raw)
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            raise PlayerFailure("generation_response_limit", "Ollama response exceeded its byte limit",
                                                raw={"http_status": response.status_code, "truncated": True,
                                                     "body_prefix": raw.decode("utf-8", errors="replace")})
                    def nonfinite(value):
                        raise ValueError("Nonfinite JSON")
                    def finite(value):
                        number = float(value)
                        return number if math.isfinite(number) else nonfinite(value)
                    try:
                        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_object,
                                          parse_constant=nonfinite, parse_float=finite)
                        # Reject lone surrogates anywhere in server metadata so
                        # saving a failure record cannot itself fail UTF-8 encoding.
                        _canonical(data).encode("utf-8", errors="strict")
                    except (ValueError, UnicodeError, RecursionError):
                        raise PlayerFailure("invalid_server_response", "Ollama returned invalid bounded JSON",
                                            raw={"http_status": response.status_code,
                                                 "body_prefix": raw.decode("utf-8", errors="replace")}) from None
                    if type(data) is not dict:
                        raise PlayerFailure("invalid_server_response", "Ollama returned a non-object response", raw=data)
                    if response.is_error or data.get("error"):
                        reason = "context_limit" if context_error(data) else "ollama_http_error"
                        raise PlayerFailure(reason, f"Ollama request failed (HTTP {response.status_code})", raw=data)
                    return data

    async def _post(self, path, body, seconds):
        return await self._http("POST", path, body, seconds)

    async def _loaded_identity(self):
        data = await self._http("GET", "/api/ps", None, 10)
        canonical = self.name if ":" in self.name.rsplit("/", 1)[-1] else self.name + ":latest"
        models = data.get("models")
        loaded = next((model for model in models if type(model) is dict
                       and (model.get("name") in (self.name, canonical)
                            or model.get("model") in (self.name, canonical))), None) if type(models) is list else None
        if (loaded is None or type(loaded.get("context_length")) is not int
                or loaded["context_length"] != self.config["context"]):
            raise PlayerFailure("context_verification_failed", "Loaded generation model context does not match", raw=data)
        if type(loaded.get("digest")) is not str or not loaded["digest"]:
            raise PlayerFailure("model_identity_unavailable", "Ollama did not report the generation model digest", raw=data)
        return loaded

    def _same_model(self, identity):
        if identity["digest"] != self._prepared_identity["digest"]:
            raise PlayerFailure("model_identity_mismatch", "Generation model changed after preparation", raw=identity)

    def prepare(self):
        self._prepared_identity = None
        started = time.monotonic()
        version = asyncio.run(self._http("GET", "/api/version", None, 10))
        require_supported_ollama(version.get("version"))
        warmup = self.warmup()
        identity = asyncio.run(self._loaded_identity())
        self._prepared_identity = deepcopy(identity)
        return {"model_identity": identity, "ollama_version": version, "warmup": warmup,
                "elapsed_seconds": time.monotonic() - started, "warmup_seconds_limit": 180}

    def generate(self, request):
        start = time.monotonic()
        record = {"request": deepcopy(request), "raw": None, "elapsed_seconds": None,
                  "prompt_tokens": None, "output_tokens": None, "error": None, "source": None,
                  "model_request_started": False, "model_identity_before": None, "model_identity_after": None}

        async def checked_request():
            async with asyncio.timeout(self.config["seconds"]):
                record["model_identity_before"] = await self._loaded_identity()
                self._same_model(record["model_identity_before"])
                record["model_request_started"] = True
                data = await self._post("/api/chat", request, self.config["seconds"])
                record.update(raw=data, prompt_tokens=_usage(data, "prompt_eval_count"), output_tokens=_usage(data, "eval_count"))
                record["model_identity_after"] = await self._loaded_identity()
                self._same_model(record["model_identity_after"])
                return data

        try:
            if self._prepared_identity is None:
                raise PlayerFailure("model_identity_unavailable", "Prepare the generator before requesting source")
            data = asyncio.run(checked_request())
            message = data.get("message")
            if data.get("done") is not True or type(message) is not dict or type(message.get("content")) is not str:
                raise PlayerFailure("incomplete_response", "Incomplete Ollama source response", raw=data)
            if data.get("done_reason") not in {"stop", "length"}:
                raise PlayerFailure("unknown_stop_reason", "Unrecognized Ollama completion reason", raw=data)
            failure = cutoff_reason(data, self.config["tokens"])
            if record["output_tokens"] is not None and record["output_tokens"] > self.config["tokens"]:
                failure = "output_limit"
            if failure:
                raise PlayerFailure(failure, "Source generation ended at a generation limit", raw=data)
            record["source"] = parse_source_response(message["content"])
        except PlayerFailure as error:
            record["error"] = {"reason": error.reason, "message": str(error)}
            if record["raw"] is None:
                record["raw"] = error.raw
        except TimeoutError:
            record["error"] = {"reason": "generation_timeout", "message": "Source generation exceeded its wall-clock budget"}
        except httpx.TransportError as error:
            record["error"] = {"reason": "ollama_transport_error", "message": str(error)}
        except KeyboardInterrupt:
            record["error"] = {"reason": "cancelled", "message": "Source generation interrupted"}
        finally:
            if not record["model_request_started"]:
                record.update(prompt_tokens=0, output_tokens=0)
            record["elapsed_seconds"] = time.monotonic() - start
        return record


def _measurement_sum(records, key):
    values = [record.get(key) for record in records]
    valid = [value for value in values if type(value) in (int, float) and math.isfinite(value) and value >= 0]
    return {"total": sum(valid) if len(valid) == len(values) else None,
            "observed": sum(valid), "unavailable": len(values) - len(valid)}


def _execution_costs(executions):
    development = {"invocations": len(executions), "failures": sum(item.get("status") != "ok" for item in executions)}
    for key in ("cpu_seconds", "execution_wall_seconds", "worker_wall_seconds", "cleanup_seconds",
                "runtime_check_seconds", "total_wall_seconds"):
        value = _measurement_sum(executions, key)
        development.update({key: value["total"], key + "_observed": value["observed"], key + "_unavailable": value["unavailable"]})
    peaks = [item.get("peak_memory_bytes") for item in executions]
    known_peaks = [value for value in peaks if type(value) is int and value >= 0]
    development["peak_memory_bytes"] = max(known_peaks, default=0) if len(peaks) == len(known_peaks) else None
    development["peak_memory_bytes_observed"] = max(known_peaks, default=0)
    development["peak_memory_bytes_unavailable"] = len(peaks) - len(known_peaks)
    return development


def development_costs(attempts, *, preflight=None, preflight_seconds=None,
                      model_preparation=None, model_preparation_seconds=None):
    """Separate model calls, executions and preparation; missing is not zero."""
    generations = [attempt["generation"] for attempt in attempts]
    executions = [test["execution"] for attempt in attempts for test in attempt["tests"]]
    generation = {"attempts": len(generations),
                  "calls": sum(row.get("model_request_started", True) is True for row in generations)}
    for key in ("prompt_tokens", "output_tokens", "elapsed_seconds"):
        value = _measurement_sum(generations, key)
        generation.update({key: value["total"], key + "_observed": value["observed"], key + "_unavailable": value["unavailable"]})
    checks = preflight.get("checks", []) if type(preflight) is dict else []
    warmup = model_preparation.get("warmup") if type(model_preparation) is dict else None
    preparation = {"sandbox_preflight_wall_seconds": preflight_seconds,
                   "model_preparation_wall_seconds": model_preparation_seconds,
                   "sandbox_preflight_execution": _execution_costs(checks) if type(preflight) is dict else None,
                   "warmup_prompt_tokens": _usage(warmup, "prompt_eval_count"),
                   "warmup_output_tokens": _usage(warmup, "eval_count")}
    return {"generation": generation, "development_execution": _execution_costs(executions),
            "preparation": preparation}


def record_costs(report):
    """Reconcile saved development costs without consulting any runtime."""
    return development_costs(report["attempts"], preflight=report.get("preflight"),
                             preflight_seconds=report.get("preflight_elapsed_seconds"),
                             model_preparation=report.get("model_preparation"),
                             model_preparation_seconds=report.get("model_preparation_elapsed_seconds"))


def _validate_config(config):
    if type(config) is not dict or config.get("enabled") is not True:
        raise SandboxFailure("disabled", "Authored-validator development requires explicit opt-in")
    value = deepcopy(config)
    attempts = value.setdefault("max_attempts", 3)
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("max_attempts must be an integer from 1 to 3")
    llm = value.get("llm")
    if type(llm) is not dict:
        raise ValueError("llm configuration is required")
    for key in ("model", "url"):
        if type(llm.get(key)) is not str or not llm[key]:
            raise ValueError("llm " + key + " is required")
    for key in ("tokens", "context"):
        if type(llm.get(key)) is not int or llm[key] <= 0:
            raise ValueError("llm " + key + " must be a positive integer")
    if llm["tokens"] >= llm["context"]:
        raise ValueError("llm tokens must be smaller than context")
    if type(llm.get("seed")) is not int or type(llm.get("think")) is not bool:
        raise ValueError("llm seed must be an integer and think must be a bool")
    for key in ("seconds", "temperature"):
        number = llm.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0 or key == "seconds" and number == 0:
            raise ValueError("invalid llm " + key)
    _canonical(value)  # Do not create records for non-JSON settings.
    return value


def _feedback(attempt):
    value = {"generation_error": attempt["generation"].get("error"), "tests": [
        {"case_id": test["case_id"], "errors": test["errors"],
         "execution_reason": test["execution"].get("reason"),
         "diagnostics": test["execution"].get("stderr", "")[:1024]}
        for test in attempt["tests"] if not test["passed"]]}
    return _canonical(value).encode("utf-8")[:MAX_FEEDBACK_BYTES].decode("utf-8", errors="ignore")


def generate(directory, config, *, backend=None, generator=None):
    """Create a development record with at most three model-authored proposals.

    Injectable collaborators support tests without source execution. Generator
    methods are prepare()->metadata and generate(request)->generation record.
    Backend methods are prepare()->readiness and run_prepared(bytes,request).
    """
    config = _validate_config(config)  # Must precede every filesystem/runtime/model action.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    cases = development_cases()
    report = {"schema_version": 1, "status": "failed", "config": config,
              "suite_sha256": suite_hash(), "prompt_version": PROMPT_VERSION,
              "runtime_source_sha256": runtime_source_hash(), "sandbox_policy": policy_limits(),
              "model_identity": None, "model_preparation": None, "selected_attempt": None,
              "attempts": [], "preflight": None, "costs": development_costs([]),
              "preflight_elapsed_seconds": None, "model_preparation_elapsed_seconds": None,
              "created_at": datetime.now(timezone.utc).isoformat(), "error": None}

    def persist():
        report["costs"] = record_costs(report)
        for attempt in report["attempts"]:
            atomic_write(directory / f"attempt-{attempt['index']:03d}" / "attempt.json", _canonical(attempt) + "\n")
        atomic_write(directory / "development.json", _canonical(report) + "\n")

    persist()
    current_attempt = None
    generating = False
    generation_started = None
    try:
        backend = backend if backend is not None else DockerSandbox(SandboxConfig(
            enabled=True, image=config.get("image", ""), docker_context=config.get("docker_context", "desktop-linux")))
        preparation_started = time.monotonic()
        try:
            report["preflight"] = backend.prepare()
        finally:
            report["preflight_elapsed_seconds"] = time.monotonic() - preparation_started
        persist()
        if report["preflight"].get("status") != "ok":
            report["error"] = {"reason": report["preflight"].get("reason", "preflight_failed"),
                               "message": "Sandbox readiness failed; no model request was made"}
            return report
        generator = generator if generator is not None else OllamaValidatorGenerator(config["llm"])
        preparation_started = time.monotonic()
        try:
            report["model_preparation"] = generator.prepare()
        finally:
            report["model_preparation_elapsed_seconds"] = time.monotonic() - preparation_started
        identity = report["model_preparation"].get("model_identity")
        if type(identity) is not dict or type(identity.get("digest")) is not str or not identity["digest"]:
            raise PlayerFailure("model_identity_unavailable", "Generation requires a recorded model digest")
        report["model_identity"] = deepcopy(identity)
        persist()
        previous_source = feedback = None
        for index in range(1, config["max_attempts"] + 1):
            attempt_dir = directory / f"attempt-{index:03d}"
            attempt_dir.mkdir()
            body = generation_request(config["llm"], cases, feedback, previous_source)
            current_attempt = {"index": index, "status": "failed", "source_sha256": None,
                               "generation": {"request": body, "raw": None, "elapsed_seconds": None,
                                              "prompt_tokens": None, "output_tokens": None, "error": None},
                               "tests": []}
            report["attempts"].append(current_attempt)
            persist()
            generating = True
            generation_started = time.monotonic()
            generated = generator.generate(deepcopy(body))
            generating = False
            current_attempt["generation"] = {key: value for key, value in generated.items() if key != "source"}
            current_attempt["generation"]["request"] = body
            source = generated.get("source")
            generation_error = generated.get("error")
            if generation_error is None:
                try:
                    # Validate injected results too, without parsing source as Python.
                    source = parse_source_response(_canonical({"source": source}))
                except PlayerFailure as error:
                    generation_error = {"reason": error.reason, "message": str(error)}
                    current_attempt["generation"]["error"] = generation_error
            if generation_error is None:
                source_bytes = source.encode("utf-8")
                with (attempt_dir / "source.py").open("xb") as file:
                    file.write(source_bytes)
                current_attempt["source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
                previous_source = source
            persist()
            if generation_error is not None:
                if generation_error.get("reason") not in REPAIRABLE_GENERATION:
                    if generation_error.get("reason") == "cancelled":
                        report["status"] = "interrupted"
                    report["error"] = generation_error
                    return report
                feedback = _feedback(current_attempt)
                continue
            for case in cases:
                test = {"case_id": case["id"], "request": deepcopy(case["request"]),
                        "execution": {"status": "error", "reason": "invocation_incomplete"},
                        "passed": False, "errors": [{"reason": "invocation_incomplete", "message": "No completed invocation record"}],
                        "findings": None}
                current_attempt["tests"].append(test)
                persist()
                execution_started = time.monotonic()
                try:
                    execution = backend.run_prepared(source_bytes, deepcopy(case["request"]))
                except (Exception, KeyboardInterrupt) as error:
                    execution = {"status": "error", "reason": "cancelled" if isinstance(error, KeyboardInterrupt)
                                 else getattr(error, "reason", "runtime_error"), "message": str(error),
                                 "execution_wall_seconds": time.monotonic() - execution_started}
                checked = check_case(case, execution)
                test.update(execution=execution, **checked)
                persist()
                if execution.get("status") != "ok" and execution.get("reason") not in REPAIRABLE_EXECUTION:
                    report["status"] = "interrupted" if execution.get("reason") == "cancelled" else "failed"
                    report["error"] = {"reason": execution.get("reason", "runtime_error"),
                                       "message": "Development stopped after a sandbox infrastructure failure"}
                    return report
            if all(test["passed"] for test in current_attempt["tests"]):
                current_attempt["status"] = report["status"] = "passed"
                report["selected_attempt"] = index
                return report
            feedback = _feedback(current_attempt)
        report["status"] = "exhausted"
        report["error"] = {"reason": "attempt_budget_exhausted", "message": "No proposal passed the development acceptance cases"}
    except (Exception, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        reason = "cancelled" if interrupted else getattr(error, "reason", "development_failure")
        report["status"] = "interrupted" if interrupted else "failed"
        report["error"] = {"reason": reason, "message": str(error)}
        if current_attempt is not None and generating:
            current_attempt["generation"]["error"] = report["error"]
            current_attempt["generation"]["elapsed_seconds"] = time.monotonic() - generation_started
            if isinstance(error, PlayerFailure):
                current_attempt["generation"]["raw"] = error.raw
                if error.elapsed_seconds is not None:
                    current_attempt["generation"]["elapsed_seconds"] = error.elapsed_seconds
    finally:
        persist()
    return report
