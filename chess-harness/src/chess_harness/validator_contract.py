"""Plain-data contract for authored validators; never loads or executes source.

Schemas describe shape. Chess semantics and factual evidence are checked by
validator_rules and validator_verify, independently of generated code.
"""
from copy import deepcopy
import json
import math
import re


CONTRACT_VERSION = "authored-validator-v1"
RULES_API_VERSION = "rules-board-v1"
DRAW_POLICY = "claim_draw=True"
MAX_INPUT_BYTES = 128 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_HISTORY_MOVES = 2048
MAX_FACTS = 64
MAX_HEURISTICS = 32
MAX_INTERPRETATION_CHARS = 512
MAX_JSON_DEPTH = 16


class ValidatorContractError(ValueError):
    """A stable machine-readable reason and JSON field path, plus diagnostics."""

    def __init__(self, reason, path, message):
        super().__init__(message)
        self.reason = reason
        self.path = path


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _array(items, maximum, minimum=0, unique=False):
    schema = {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}
    if unique:
        schema["uniqueItems"] = True
    return schema


def _text(maximum, pattern=None):
    schema = {"type": "string", "minLength": 1, "maxLength": maximum}
    if pattern:
        # Unlike $, this end assertion also rejects a final newline in JSON Schema.
        schema["pattern"] = "^" + pattern + r"(?![\s\S])"
    return schema


_MOVE = _text(5, "[a-h][1-8][a-h][1-8][qrbn]?")
_ID = _text(32, "[A-Za-z][A-Za-z0-9_-]{0,31}")
_POSITION = _object({"fen": _text(256)})
_HISTORY = _object({"initial_fen": _text(256), "moves": _array(_MOVE, MAX_HISTORY_MOVES)})
_VERSION = {"type": "string", "const": CONTRACT_VERSION}
_REQUEST = _object({
    "contract_version": _VERSION,
    "rules_api_version": {"type": "string", "const": RULES_API_VERSION},
    "position": _POSITION, "history": _HISTORY, "candidate": _MOVE,
})


def _fact(kind, plies, **fields):
    return _object({"id": _ID, "kind": {"type": "string", "const": kind},
                    "line": _array(_MOVE, plies, plies), **fields})


_RESULT = _object({
    "contract_version": _VERSION,
    "facts": _array({"oneOf": [
        _fact("candidate_checkmate", 1),
        _fact("reply_checkmate", 2),
        _fact("capture_available", 2,
              capturing_piece=_text(1, "[PNBRQKpnbrqk]"),
              captured_piece=_text(1, "[PNBRQpnbrq]"),
              capture_square=_text(2, "[a-h][1-8]")),
    ]}, MAX_FACTS),
    "heuristics": _array(_object({
        "fact_ids": _array(_ID, MAX_FACTS, unique=True),
        "interpretation": _text(MAX_INTERPRETATION_CHARS),
    }), MAX_HEURISTICS),
})


def request_schema():
    """Return a detached JSON Schema for the request's structural contract."""
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **deepcopy(_REQUEST)}


def result_schema():
    """Return a detached schema; references and factual truth need further checks."""
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **deepcopy(_RESULT)}


def _fail(reason, path, message):
    raise ValidatorContractError(reason, path, message)


def _shape(value, schema, path):
    """Validate the small schema vocabulary above from a single source of truth."""
    if "oneOf" in schema:
        if type(value) is not dict:
            _fail("invalid_shape", path, "Expected a fact object")
        if type(value.get("kind")) is not str:
            _fail("invalid_shape", path + ".kind", "Expected a fact kind string")
        for choice in schema["oneOf"]:
            if value.get("kind") == choice["properties"]["kind"]["const"]:
                return _shape(value, choice, path)
        _fail("invalid_shape", path + ".kind", "Unknown fact kind")
    kind = schema["type"]
    expected = {"object": dict, "array": list, "string": str}[kind]
    if type(value) is not expected:
        _fail("invalid_shape", path, f"Expected {kind}")
    if "const" in schema and value != schema["const"]:
        reason = "unsupported_version" if path.endswith("_version") else "invalid_shape"
        _fail(reason, path, "Unsupported constant value")
    if kind == "object":
        properties = schema["properties"]
        if (len(value) != len(properties) or any(type(key) is not str for key in value)
                or set(value) != set(properties)):
            _fail("invalid_shape", path, "Incorrect fields; missing or extra keys")
        for key, child in properties.items():
            _shape(value[key], child, path + "." + key)
    elif kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            _fail("invalid_shape", path, "Array length outside contract bounds")
        for index, item in enumerate(value):
            _shape(item, schema["items"], f"{path}[{index}]")
        if schema.get("uniqueItems") and len(set(value)) != len(value):
            _fail("invalid_shape", path, "Array items must be unique")
    else:
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 512):
            _fail("invalid_shape", path, "String length outside contract bounds")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            _fail("invalid_shape", path, "String does not match the contract pattern")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _fail("invalid_shape", path, "String contains an unpaired Unicode surrogate")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _bounded(value, limit, reason):
    if len(_bytes(value)) > limit:
        _fail(reason, "$", f"Encoded document exceeds {limit} bytes")


def validate_context(position, history):
    """Validate and detach context shape only; does not establish chess validity."""
    _shape(position, _POSITION, "$.position")
    _shape(history, _HISTORY, "$.history")
    _bounded({"position": position, "history": history}, MAX_INPUT_BYTES, "input_limit")
    return deepcopy(position), deepcopy(history)


def validate_request(value):
    """Validate/detach request shape; RulesBoard.from_request checks chess semantics."""
    _shape(value, _REQUEST, "$")
    _bounded(value, MAX_INPUT_BYTES, "input_limit")
    return deepcopy(value)


def validate_result(value):
    """Validate/detach result shape and references; does not verify factual truth."""
    _shape(value, _RESULT, "$")
    _bounded(value, MAX_RESULT_BYTES, "output_limit")
    ids = set()
    for index, fact in enumerate(value["facts"]):
        if fact["id"] in ids:
            _fail("duplicate_fact_id", f"$.facts[{index}].id", "Fact IDs must be unique")
        ids.add(fact["id"])
    for index, heuristic in enumerate(value["heuristics"]):
        for reference, fact_id in enumerate(heuristic["fact_ids"]):
            if fact_id not in ids:
                _fail("invalid_reference", f"$.heuristics[{index}].fact_ids[{reference}]",
                      "Heuristic refers to an unknown fact ID")
    return deepcopy(value)


def _parse(raw, limit, reason):
    if type(raw) is not bytes:
        _fail("invalid_json", "$", "Expected UTF-8 JSON bytes")
    if len(raw) > limit:
        _fail(reason, "$", f"Document exceeds {limit} bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("invalid_json", "$", "Invalid UTF-8")
    # Bound nesting before json.loads, including otherwise schema-invalid data.
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _fail("structure_limit", "$", "JSON nesting exceeds contract limit")
        elif character in "]}":
            depth -= 1

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("duplicate_json_key", "$", "Duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value):
        _fail("invalid_json", "$", "Non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject_constant(value)
        return number

    try:
        return json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=reject_constant, parse_float=finite_float)
    except ValidatorContractError:
        raise
    except (ValueError, RecursionError):
        _fail("invalid_json", "$", "Malformed JSON document")


def parse_request(raw):
    """Decode a bounded UTF-8 request; chess validation is a separate step."""
    return validate_request(_parse(raw, MAX_INPUT_BYTES, "input_limit"))


def parse_result(raw):
    """Decode a bounded result; witness verification is a separate step."""
    return validate_result(_parse(raw, MAX_RESULT_BYTES, "output_limit"))
