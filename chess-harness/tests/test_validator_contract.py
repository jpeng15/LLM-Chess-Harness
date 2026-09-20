from copy import deepcopy
import json
import unittest

import chess

from chess_harness.validator_contract import (
    CONTRACT_VERSION, RULES_API_VERSION, MAX_INPUT_BYTES, MAX_RESULT_BYTES,
    ValidatorContractError, parse_request, parse_result, request_schema,
    result_schema, validate_request, validate_result,
)


def request():
    return {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "position": {"fen": chess.STARTING_FEN},
            "history": {"initial_fen": chess.STARTING_FEN, "moves": []}, "candidate": "e2e4"}


def result():
    return {"contract_version": CONTRACT_VERSION,
            "facts": [{"id": "f1", "kind": "capture_available", "line": ["d1h5", "g6h5"],
                       "capturing_piece": "p", "captured_piece": "Q", "capture_square": "h5"}],
            "heuristics": [{"fact_ids": ["f1"], "interpretation": "Capture is possible; compensation is uncertain."}]}


def wire(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class ValidatorContractTests(unittest.TestCase):
    def assert_error(self, function, value, reason, path=None):
        with self.assertRaises(ValidatorContractError) as raised:
            function(value)
        self.assertEqual(raised.exception.reason, reason)
        if path is not None:
            self.assertEqual(raised.exception.path, path)

    def test_round_trip_and_detached_results(self):
        original_request, original_result = request(), result()
        self.assertEqual(parse_request(wire(original_request)), original_request)
        self.assertEqual(parse_result(wire(original_result)), original_result)
        detached = validate_request(original_request)
        detached["history"]["moves"].append("e2e4")
        self.assertEqual(original_request["history"]["moves"], [])
        detached = validate_result(original_result)
        detached["facts"][0]["line"][0] = "d2d4"
        self.assertEqual(original_result["facts"][0]["line"][0], "d1h5")

    def test_empty_findings_and_unverified_interpretations_are_valid_data(self):
        empty = {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}
        self.assertEqual(parse_result(wire(empty)), empty)
        empty["heuristics"] = [{"fact_ids": [], "interpretation": "My search found nothing; safety is unknown."}]
        self.assertEqual(validate_result(empty), empty)
        # Brackets, escapes and generated text are data, not executable content.
        empty["heuristics"][0]["interpretation"] = '"' + "[" * 100 + "\\" + "<script>not executed</script>"
        self.assertEqual(parse_result(wire(empty)), empty)

    def test_versions_exact_fields_and_types(self):
        for field in ("contract_version", "rules_api_version"):
            value = request()
            value[field] = "future-version"
            self.assert_error(validate_request, value, "unsupported_version", "$." + field)
        value = result()
        value["contract_version"] = "future-version"
        self.assert_error(validate_result, value, "unsupported_version", "$.contract_version")
        for field in ("fixture_id", "expected_moves", "engine_score", "source_path"):
            value = request()
            value[field] = "not allowed"
            self.assert_error(validate_request, value, "invalid_shape", "$")
        for bad in (None, [], True, "request"):
            self.assert_error(validate_request, bad, "invalid_shape", "$")
        value = result()
        value["verified"] = True
        self.assert_error(validate_result, value, "invalid_shape", "$")
        value = result()
        value["facts"][0]["description"] = "This move is bad"
        self.assert_error(validate_result, value, "invalid_shape", "$.facts[0]")

    def test_direct_validation_accepts_only_plain_data(self):
        class CustomText(str):
            def __deepcopy__(self, memo):
                raise AssertionError("Validation must not copy custom objects")

            def __eq__(self, other):
                raise AssertionError("Validation must not compare custom values")

            __hash__ = str.__hash__

        value = request()
        version = value.pop("contract_version")
        value[CustomText("contract_version")] = version
        self.assert_error(validate_request, value, "invalid_shape", "$")
        value = result()
        value["facts"][0]["kind"] = CustomText("capture_available")
        self.assert_error(validate_result, value, "invalid_shape", "$.facts[0].kind")

    def test_exact_uci_and_special_move_spelling(self):
        for move in (" e2e4", "e2e4 ", "e2e4\n", "E2E4", "e4", "0000", "e7e8Q", True, 1234):
            value = request()
            value["candidate"] = move
            self.assert_error(validate_request, value, "invalid_shape", "$.candidate")
        for move in ("e1g1", "e5d6", "a7a8n"):
            value = request()
            value["candidate"] = move
            self.assertEqual(validate_request(value)["candidate"], move)
        # Shape acceptance alone deliberately does not claim these are legal.

    def test_fact_vocabulary_and_witness_lengths(self):
        for kind, line in (("candidate_checkmate", ["f7h7"]),
                           ("reply_checkmate", ["g2g4", "d8h4"])):
            value = {"contract_version": CONTRACT_VERSION,
                     "facts": [{"id": "mate", "kind": kind, "line": line}], "heuristics": []}
            self.assertEqual(validate_result(value), value)
            value["facts"][0]["line"].append("a2a3")
            self.assert_error(validate_result, value, "invalid_shape", "$.facts[0].line")
        for kind in ("safe", "bad_move", "forced_capture", "no_reply_wins_material"):
            value = result()
            value["facts"][0]["kind"] = kind
            self.assert_error(validate_result, value, "invalid_shape", "$.facts[0].kind")
        value = result()
        value["facts"][0]["captured_piece"] = "k"
        self.assert_error(validate_result, value, "invalid_shape", "$.facts[0].captured_piece")

    def test_ids_and_heuristic_references(self):
        value = result()
        value["facts"].append(deepcopy(value["facts"][0]))
        self.assert_error(validate_result, value, "duplicate_fact_id", "$.facts[1].id")
        value = result()
        value["heuristics"][0]["fact_ids"] = ["missing"]
        self.assert_error(validate_result, value, "invalid_reference", "$.heuristics[0].fact_ids[0]")
        value["heuristics"][0]["fact_ids"] = ["f1", "f1"]
        self.assert_error(validate_result, value, "invalid_shape", "$.heuristics[0].fact_ids")
        value = result()
        value["facts"][0]["id"] = "f1\n"
        self.assert_error(validate_result, value, "invalid_shape", "$.facts[0].id")

    def test_strict_wire_json(self):
        for raw in (b'{', b'{} {}', b'{} trailing', b'\xff', b'{"n":NaN}',
                    b'{"n":Infinity}', b'{"n":-Infinity}', b'{"n":1e999}', b'{"n":' + b'1' * 5000 + b'}'):
            self.assert_error(parse_result, raw, "invalid_json", "$")
        self.assert_error(parse_result, '{"facts":[]}', "invalid_json", "$")
        for raw in (b'{"facts":[],"facts":[]}', b'{"outer":{"x":1,"x":2}}'):
            self.assert_error(parse_result, raw, "duplicate_json_key", "$")
        value = result()
        value["heuristics"][0]["interpretation"] = "\ud800"
        self.assert_error(validate_result, value, "invalid_shape", "$.heuristics[0].interpretation")
        self.assert_error(parse_result, json.dumps(value).encode(), "invalid_shape",
                          "$.heuristics[0].interpretation")

    def test_bounds_apply_before_parsing_and_to_direct_data(self):
        self.assert_error(parse_request, b" " * (MAX_INPUT_BYTES + 1), "input_limit", "$")
        self.assert_error(parse_result, b" " * (MAX_RESULT_BYTES + 1), "output_limit", "$")
        self.assert_error(parse_result, b"[" * 17 + b"]" * 17, "structure_limit", "$")
        value = request()
        value["history"]["moves"] = ["e2e4"] * 2049
        self.assert_error(validate_request, value, "invalid_shape", "$.history.moves")
        value = result()
        value["heuristics"][0]["interpretation"] = "x" * 513
        self.assert_error(validate_result, value, "invalid_shape", "$.heuristics[0].interpretation")
        value = result()
        value["facts"] = [dict(value["facts"][0], id=f"f{index}") for index in range(65)]
        self.assert_error(validate_result, value, "invalid_shape", "$.facts")
        value = {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": [
            {"fact_ids": [], "interpretation": "\U0001f600" * 512} for _ in range(32)]}
        # Character limits alone cannot enforce a UTF-8 byte budget.
        self.assert_error(validate_result, value, "output_limit", "$")

    def test_published_schemas_are_detached_and_describe_strict_objects(self):
        schema = request_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["rules_api_version"]["const"], RULES_API_VERSION)
        schema["properties"]["candidate"]["maxLength"] = 99
        self.assertEqual(request_schema()["properties"]["candidate"]["maxLength"], 5)
        schema = result_schema()
        choices = schema["properties"]["facts"]["items"]["oneOf"]
        self.assertEqual({item["properties"]["kind"]["const"] for item in choices},
                         {"candidate_checkmate", "reply_checkmate", "capture_available"})
        self.assertTrue(all(item["additionalProperties"] is False for item in choices))
