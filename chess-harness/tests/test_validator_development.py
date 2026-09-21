"""Development orchestration with injected data only: no generated source runs."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from chess_harness.validator_contract import CONTRACT_VERSION
from chess_harness.validator_development import (
    MAX_FEEDBACK_BYTES, MAX_GENERATION_RESPONSE_BYTES, OllamaValidatorGenerator, check_case, development_cases,
    development_costs, generate, generation_request, parse_source_response, suite_hash,
)
from chess_harness.validator_rules import RulesBoard
from chess_harness.validator_sandbox import SandboxFailure
from chess_harness.limits import PlayerFailure
from chess_harness.validator import main as validator_main


SOURCE = 'def validate(position, history, candidate):\r\n    raise RuntimeError("tests never execute this source")\r\n'
CONFIG = {"enabled": True, "image": "sha256:" + "a" * 64, "docker_context": "desktop-linux", "max_attempts": 3,
          "llm": {"model": "test", "url": "http://127.0.0.1:11434", "think": False, "tokens": 512,
                  "context": 16384, "seconds": 60, "seed": 12, "temperature": 0}}


def findings(case):
    return {"contract_version": CONTRACT_VERSION,
            "facts": [dict(fact, id=f"f{index}") for index, fact in enumerate(case["required_facts"])], "heuristics": []}


class FakeGenerator:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [SOURCE] * 3)
        self.requests = []
        self.preparations = 0
        self.identity = {"name": "test", "digest": "b" * 64, "context_length": 16384}

    def prepare(self):
        self.preparations += 1
        return {"model_identity": deepcopy(self.identity), "elapsed_seconds": 0.5}

    def generate(self, request):
        self.requests.append(deepcopy(request))
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, dict):
            return {"request": request, "raw": {"message": "rejected raw response"}, "elapsed_seconds": 1,
                    "prompt_tokens": 100, "output_tokens": 50, "error": value, "source": None}
        return {"request": request, "raw": {"message": {"content": json.dumps({"source": value})}},
                "elapsed_seconds": 1, "prompt_tokens": 100, "output_tokens": 50, "error": None, "source": value}


class FakeBackend:
    def __init__(self, handler=None, ready=True):
        self.calls = []
        self.preparations = 0
        self.handler = handler
        self.ready = ready

    def prepare(self):
        self.preparations += 1
        return {"status": "ok" if self.ready else "error", "reason": "ready" if self.ready else "runtime_unavailable"}

    def run_prepared(self, source, request):
        self.calls.append((source, deepcopy(request)))
        case = next(case for case in development_cases() if case["request"] == request)
        if self.handler:
            special = self.handler(case, len(self.calls))
            if special is not None:
                return special
        return {"status": "ok", "reason": "completed", "findings": findings(case), "cpu_seconds": 0.01,
                "execution_wall_seconds": 0.1, "worker_wall_seconds": 0.05, "cleanup_seconds": 0.02,
                "peak_memory_bytes": 1000000, "cleanup_confirmed": True}


class ValidatorDevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "development"

    def test_opt_in_gate_precedes_files_runtime_and_model_calls(self):
        backend, generator = FakeBackend(), FakeGenerator()
        for enabled in (False, None, 1, "true"):
            with patch("chess_harness.validator_development.runtime_source_hash", side_effect=AssertionError("no files")):
                with self.assertRaises(SandboxFailure) as raised:
                    generate(self.directory, {**CONFIG, "enabled": enabled}, backend=backend, generator=generator)
            self.assertEqual(raised.exception.reason, "disabled")
            self.assertFalse(self.directory.exists())
        self.assertEqual(backend.preparations, 0)
        self.assertEqual(generator.preparations, 0)
        self.assertEqual(generator.requests, [])

    def test_bad_attempt_budget_rejected_before_directory_and_existing_directory_not_reused(self):
        for attempts in (0, 4, True, 2.0):
            with self.assertRaises(ValueError):
                generate(self.directory, {**CONFIG, "max_attempts": attempts})
            self.assertFalse(self.directory.exists())
        self.directory.mkdir()
        sentinel = self.directory / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        backend = FakeBackend()
        with self.assertRaises(FileExistsError):
            generate(self.directory, CONFIG, backend=backend)
        self.assertEqual(backend.preparations, 0)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_failed_preflight_records_failure_without_any_model_request(self):
        backend, generator = FakeBackend(ready=False), FakeGenerator()
        report = generate(self.directory, CONFIG, backend=backend, generator=generator)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["reason"], "runtime_unavailable")
        self.assertEqual(generator.preparations, 0)
        self.assertEqual(generator.requests, [])
        self.assertEqual(report["attempts"], [])
        self.assertEqual(json.loads((self.directory / "development.json").read_text()), report)

    def test_success_records_exact_source_all_tests_model_identity_and_separate_costs(self):
        backend, generator = FakeBackend(), FakeGenerator()
        with patch("chess.engine.SimpleEngine.popen_uci", side_effect=AssertionError("no engine analysis")):
            report = generate(self.directory, CONFIG, backend=backend, generator=generator)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["selected_attempt"], 1)
        self.assertEqual(len(backend.calls), len(development_cases()))
        self.assertEqual(backend.preparations, 1)
        attempt = report["attempts"][0]
        self.assertEqual(attempt["source_sha256"], hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual((self.directory / "attempt-001" / "source.py").read_bytes(), SOURCE.encode())
        self.assertEqual(json.loads((self.directory / "attempt-001" / "attempt.json").read_text()), attempt)
        self.assertEqual(json.loads((self.directory / "development.json").read_text()), report)
        self.assertEqual(report["model_identity"]["digest"], "b" * 64)
        self.assertEqual(report["suite_sha256"], suite_hash())
        self.assertEqual(report["costs"]["generation"]["output_tokens"], 50)
        self.assertEqual(report["costs"]["development_execution"]["invocations"], len(development_cases()))
        self.assertEqual(report["costs"]["development_execution"]["peak_memory_bytes"], 1000000)
        self.assertTrue(all(test["passed"] for test in attempt["tests"]))

    def test_always_empty_fails_positive_cases_then_model_repair_can_pass(self):
        count = len(development_cases())
        def empty_first(case, index):
            if index <= count:
                return {"status": "ok", "reason": "completed", "findings": {
                    "contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}}
        backend, generator = FakeBackend(empty_first), FakeGenerator()
        report = generate(self.directory, CONFIG, backend=backend, generator=generator)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["selected_attempt"], 2)
        self.assertEqual([attempt["status"] for attempt in report["attempts"]], ["failed", "passed"])
        self.assertEqual(len(backend.calls), 2 * count)
        second_messages = generator.requests[1]["messages"]
        self.assertEqual(json.loads(second_messages[-2]["content"])["source"], SOURCE)
        feedback = second_messages[-1]["content"].split("\n", 1)[1]
        self.assertLessEqual(len(feedback.encode()), MAX_FEEDBACK_BYTES)
        self.assertIn("missing_required_finding", feedback)
        self.assertEqual(report["costs"]["generation"]["prompt_tokens"], 200)
        # Missing measurements in the first attempt remain unavailable, not zero.
        self.assertIsNone(report["costs"]["development_execution"]["cpu_seconds"])
        self.assertGreater(report["costs"]["development_execution"]["cpu_seconds_observed"], 0)

    def test_code_errors_exhaust_attempt_budget_without_fallback(self):
        backend = FakeBackend(lambda case, index: {"status": "error", "reason": "syntax_error", "stderr": "bad syntax"})
        generator = FakeGenerator()
        report = generate(self.directory, CONFIG, backend=backend, generator=generator)
        self.assertEqual(report["status"], "exhausted")
        self.assertEqual(len(generator.requests), 3)
        self.assertEqual(len(backend.calls), 3 * len(development_cases()))
        self.assertIsNone(report["selected_attempt"])
        self.assertEqual(report["costs"]["generation"]["calls"], 3)
        self.assertEqual(report["costs"]["development_execution"]["failures"], len(backend.calls))

    def test_infrastructure_failure_stops_and_keeps_generation_and_execution_records(self):
        backend = FakeBackend(lambda case, index: {"status": "error", "reason": "cleanup_failed", "cpu_seconds": 0.02})
        generator = FakeGenerator()
        report = generate(self.directory, CONFIG, backend=backend, generator=generator)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["reason"], "cleanup_failed")
        self.assertEqual(len(generator.requests), 1)
        self.assertEqual(len(report["attempts"][0]["tests"]), 1)
        self.assertEqual(report["costs"]["development_execution"]["cpu_seconds"], 0.02)

    def test_generation_and_execution_interruptions_are_durable(self):
        report = generate(self.directory, CONFIG, backend=FakeBackend(), generator=FakeGenerator([KeyboardInterrupt()]))
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["attempts"][0]["generation"]["error"]["reason"], "cancelled")
        self.assertIsNotNone(report["costs"]["generation"]["elapsed_seconds"])
        def interrupted(case, index):
            raise KeyboardInterrupt()
        report = generate(Path(self.temporary.name) / "second", CONFIG, backend=FakeBackend(interrupted), generator=FakeGenerator())
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["attempts"][0]["tests"][0]["execution"]["reason"], "cancelled")
        self.assertEqual(report["costs"]["development_execution"]["invocations"], 1)
        self.assertIsNone(report["costs"]["development_execution"]["cpu_seconds"])

    def test_malformed_generation_can_repair_but_missing_model_identity_cannot(self):
        generator = FakeGenerator([{"reason": "malformed_source_response", "message": "not JSON"}, SOURCE])
        report = generate(self.directory, CONFIG, backend=FakeBackend(), generator=generator)
        self.assertEqual(report["selected_attempt"], 2)
        self.assertIsNone(report["attempts"][0]["source_sha256"])
        self.assertFalse((self.directory / "attempt-001" / "source.py").exists())
        self.assertIsNotNone(report["attempts"][0]["generation"]["raw"])
        generator = FakeGenerator()
        generator.identity.pop("digest")
        backend = FakeBackend()
        report = generate(Path(self.temporary.name) / "missing-digest", CONFIG, backend=backend, generator=generator)
        self.assertEqual(report["error"]["reason"], "model_identity_unavailable")
        self.assertFalse(generator.requests)
        self.assertFalse(backend.calls)

    def test_development_cases_verify_and_are_detached_without_searching(self):
        cases = development_cases()
        original_hash = suite_hash()
        for case in cases:
            self.assertTrue(check_case(case, {"status": "ok", "findings": findings(case)})["passed"], case["id"])
        cases[0]["request"]["candidate"] = "wrong"
        self.assertEqual(suite_hash(), original_hash)
        for name in ("candidate_stalemate", "draw_history"):
            case = next(case for case in development_cases() if case["id"] == name)
            board = RulesBoard.from_request(case["request"])
            board.push(case["request"]["candidate"])
            self.assertIsNotNone(board.outcome())
            self.assertFalse(board.is_checkmate())
        case = next(case for case in development_cases() if case["id"] == "pinned_capture")
        bad = findings(case)
        bad["facts"].append({"id": "false", "kind": "capture_available", "line": ["a8b8", "e2a2"],
                             "capturing_piece": "R", "captured_piece": "q", "capture_square": "a2"})
        checked = check_case(case, {"status": "ok", "findings": bad})
        self.assertFalse(checked["passed"])
        self.assertEqual(checked["errors"][0]["reason"], "invalid_finding")
        self.assertIsNone(checked["findings"])

    def test_generation_prompt_has_only_contract_api_development_data_and_fixed_budgets(self):
        body = generation_request(CONFIG["llm"], development_cases())
        self.assertFalse(body["truncate"])
        self.assertFalse(body["shift"])
        self.assertEqual(body["options"]["num_predict"], CONFIG["llm"]["tokens"])
        self.assertEqual(body["options"]["num_ctx"], CONFIG["llm"]["context"])
        self.assertEqual(body["options"]["seed"], CONFIG["llm"]["seed"])
        self.assertEqual(body["format"]["required"], ["source"])
        self.assertNotIn("def validate", body["messages"][0]["content"])
        supplied = json.loads(body["messages"][1]["content"])
        self.assertEqual(supplied["development_cases"], development_cases())
        self.assertNotIn("engine_score", json.dumps(body))
        self.assertNotIn("held_out", supplied)

    def test_strict_source_envelope_and_source_size(self):
        self.assertEqual(parse_source_response(json.dumps({"source": SOURCE})), SOURCE)
        for content in ("```python\npass\n```", '{"source":"x","source":"y"}',
                        '{"source":"x","extra":1}', '{} {}', '{"source":NaN}',
                        '{"source":"\\ud800"}', "[" * 10 + "]" * 10):
            with self.subTest(content=content), self.assertRaises(PlayerFailure) as raised:
                parse_source_response(content)
            self.assertEqual(raised.exception.reason, "malformed_source_response")
        with self.assertRaises(PlayerFailure) as raised:
            parse_source_response(json.dumps({"source": "x" * (65536 + 1)}))
        self.assertEqual(raised.exception.reason, "source_limit")

    def test_ollama_generator_preserves_usage_raw_and_cutoff_before_accepting_source(self):
        generator = OllamaValidatorGenerator(CONFIG["llm"])
        identity = {"digest": "b" * 64, "context_length": CONFIG["llm"]["context"]}
        generator._prepared_identity = identity
        body = generation_request(CONFIG["llm"], development_cases())
        raw = {"done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 50,
               "message": {"content": json.dumps({"source": SOURCE})}}
        with patch.object(generator, "_post", AsyncMock(return_value=raw)), \
                patch.object(generator, "_loaded_identity", AsyncMock(return_value=identity)):
            record = generator.generate(body)
        self.assertEqual(record["source"], SOURCE)
        self.assertEqual(record["raw"], raw)
        self.assertEqual(record["prompt_tokens"], 100)
        raw["done_reason"], raw["eval_count"] = "length", CONFIG["llm"]["tokens"]
        with patch.object(generator, "_post", AsyncMock(return_value=raw)), \
                patch.object(generator, "_loaded_identity", AsyncMock(return_value=identity)):
            record = generator.generate(body)
        self.assertIsNone(record["source"])
        self.assertEqual(record["error"]["reason"], "output_limit")
        self.assertEqual(record["output_tokens"], CONFIG["llm"]["tokens"])
        raw["done_reason"], raw["eval_count"] = "stop", CONFIG["llm"]["tokens"] + 1
        with patch.object(generator, "_post", AsyncMock(return_value=raw)), \
                patch.object(generator, "_loaded_identity", AsyncMock(return_value=identity)):
            record = generator.generate(body)
        self.assertEqual(record["error"]["reason"], "output_limit")
        self.assertIsNone(record["source"])

    def test_real_generator_rechecks_digest_and_keeps_costs_when_identity_changes(self):
        generator = OllamaValidatorGenerator(CONFIG["llm"])
        identity = {"digest": "b" * 64, "context_length": CONFIG["llm"]["context"]}
        changed = {**identity, "digest": "c" * 64}
        generator._prepared_identity = deepcopy(identity)
        body = generation_request(CONFIG["llm"], development_cases())
        raw = {"done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 50,
               "message": {"content": json.dumps({"source": SOURCE})}}
        with patch.object(generator, "_post", AsyncMock(return_value=raw)) as post, \
                patch.object(generator, "_loaded_identity", AsyncMock(return_value=changed)):
            record = generator.generate(body)
            post.assert_not_awaited()
        self.assertEqual(record["error"]["reason"], "model_identity_mismatch")
        self.assertFalse(record["model_request_started"])
        self.assertEqual(record["output_tokens"], 0)  # No inference was requested.
        with patch.object(generator, "_post", AsyncMock(return_value=raw)), \
                patch.object(generator, "_loaded_identity", AsyncMock(side_effect=[identity, changed])):
            record = generator.generate(body)
        self.assertEqual(record["error"]["reason"], "model_identity_mismatch")
        self.assertIsNone(record["source"])
        self.assertEqual(record["output_tokens"], 50)
        self.assertEqual(record["raw"], raw)

    def test_identity_checks_share_whole_generation_deadline(self):
        generator = OllamaValidatorGenerator({**CONFIG["llm"], "seconds": 0.01})
        generator._prepared_identity = {"digest": "b" * 64}
        async def slow_identity():
            await asyncio.sleep(1)
        with patch.object(generator, "_loaded_identity", slow_identity), \
                patch.object(generator, "_post", AsyncMock()) as post:
            record = generator.generate(generation_request(generator.config, development_cases()))
        self.assertEqual(record["error"]["reason"], "generation_timeout")
        self.assertLess(record["elapsed_seconds"], 0.5)
        post.assert_not_awaited()

    def test_generation_http_response_is_bounded_before_json_and_recording(self):
        generator = OllamaValidatorGenerator(CONFIG["llm"])
        client_type = httpx.AsyncClient
        for body, reason in ((b"x" * (MAX_GENERATION_RESPONSE_BYTES + 1024), "generation_response_limit"),
                             (b'{"x":1,"x":2}', "invalid_server_response"),
                             (b'{"number":1e9999}', "invalid_server_response"),
                             (b'{"invalid_unicode":"\\ud800"}', "invalid_server_response"),
                             (b'{"error":"the input length exceeds the context length"}', "context_limit")):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
            with self.subTest(reason=reason), \
                    patch("chess_harness.validator_development.httpx.AsyncClient",
                          side_effect=lambda **kwargs: client_type(**kwargs, transport=transport)), \
                    self.assertRaises(PlayerFailure) as raised:
                asyncio.run(generator._post("/api/chat", {}, 1))
            self.assertEqual(raised.exception.reason, reason)
            if reason == "generation_response_limit":
                self.assertTrue(raised.exception.raw["truncated"])
                self.assertEqual(len(raised.exception.raw["body_prefix"]), MAX_GENERATION_RESPONSE_BYTES)

    def test_preparation_and_total_execution_costs_are_recorded_separately(self):
        backend, generator = FakeBackend(), FakeGenerator()
        report = generate(self.directory, CONFIG, backend=backend, generator=generator)
        self.assertIsNotNone(report["costs"]["preparation"]["sandbox_preflight_wall_seconds"])
        self.assertIsNotNone(report["costs"]["preparation"]["model_preparation_wall_seconds"])
        self.assertEqual(report["costs"]["preparation"]["model_preparation_wall_seconds"],
                         report["model_preparation_elapsed_seconds"])
        costs = development_costs([{"generation": {"model_request_started": False, "prompt_tokens": 0,
                                                   "output_tokens": 0, "elapsed_seconds": 0.1},
                                   "tests": [{"execution": {"status": "ok", "runtime_check_seconds": 0.2,
                                                             "total_wall_seconds": 1.0}}]}],
                                  preflight_seconds=2, model_preparation_seconds=3)
        self.assertEqual(costs["generation"]["calls"], 0)
        self.assertEqual(costs["generation"]["attempts"], 1)
        self.assertEqual(costs["development_execution"]["runtime_check_seconds"], 0.2)
        self.assertEqual(costs["development_execution"]["total_wall_seconds"], 1.0)
        self.assertEqual(costs["preparation"]["sandbox_preflight_wall_seconds"], 2)
        self.assertEqual(costs["preparation"]["model_preparation_wall_seconds"], 3)

    def test_costs_keep_missing_usage_unavailable_and_do_not_mix_generation_with_execution(self):
        costs = development_costs([{"generation": {"prompt_tokens": None, "output_tokens": 12, "elapsed_seconds": 2},
                                   "tests": [{"execution": {"status": "error", "reason": "timeout", "cpu_seconds": 1}}]}])
        self.assertIsNone(costs["generation"]["prompt_tokens"])
        self.assertEqual(costs["generation"]["prompt_tokens_unavailable"], 1)
        self.assertEqual(costs["generation"]["output_tokens"], 12)
        self.assertEqual(costs["development_execution"]["cpu_seconds"], 1)
        self.assertNotIn("output_tokens", costs["development_execution"])

    def test_generate_cli_requires_opt_in_before_output_or_development_work(self):
        output = Path(self.temporary.name) / "report.json"
        with patch("chess_harness.validator_development.generate") as mocked, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                validator_main(["generate", "--directory", str(self.directory), "--image", CONFIG["image"],
                                "--output", str(output)])
        self.assertEqual(raised.exception.code, 2)
        mocked.assert_not_called()
        self.assertFalse(output.exists())
        self.assertFalse(self.directory.exists())

    def test_generate_cli_forwards_explicit_budgets_and_uses_success_exit_status(self):
        output = io.StringIO()
        args = ["generate", "--enable-authored-validators", "--directory", str(self.directory),
                "--image", CONFIG["image"], "--model", "test", "--no-think", "--tokens", "700",
                "--context", "8192", "--generation-seconds", "90", "--seed", "31", "--max-attempts", "2"]
        with patch("chess_harness.validator_development.generate", return_value={"status": "passed"}) as mocked, redirect_stdout(output):
            code = validator_main(args)
        self.assertEqual(code, 0)
        directory, config = mocked.call_args.args
        self.assertEqual(directory, self.directory)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["max_attempts"], 2)
        self.assertEqual(config["llm"]["context"], 8192)
        self.assertEqual(config["llm"]["tokens"], 700)
        self.assertEqual(config["llm"]["seconds"], 90)
        self.assertEqual(config["llm"]["seed"], 31)
        self.assertFalse(config["llm"]["think"])
        self.assertEqual(json.loads(output.getvalue()), {"status": "passed"})
        with patch("chess_harness.validator_development.generate", return_value={"status": "exhausted"}), redirect_stdout(io.StringIO()):
            self.assertEqual(validator_main(args), 1)

    def test_generate_cli_rejects_invalid_budgets_without_calling_generator(self):
        base = ["generate", "--enable-authored-validators", "--directory", str(self.directory), "--image", CONFIG["image"]]
        for extra in (["--max-attempts", "4"], ["--tokens", "0"], ["--tokens", "8192", "--context", "8192"],
                      ["--generation-seconds", "0"]):
            with self.subTest(extra=extra), patch("chess_harness.validator_development.generate") as mocked, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    validator_main(base + extra)
                self.assertEqual(raised.exception.code, 2)
                mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
