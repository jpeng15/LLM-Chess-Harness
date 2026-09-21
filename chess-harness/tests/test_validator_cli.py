import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import chess

from chess_harness.cli import add_game_arguments, game_config
from chess_harness.validator import MAX_SOURCE_BYTES, _read_bounded, main
from chess_harness.validator_contract import CONTRACT_VERSION, MAX_INPUT_BYTES, RULES_API_VERSION


IMAGE = "sha256:" + "a" * 64
ENABLED = ["--enable-authored-validators", "--image", IMAGE]


def request():
    return {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "position": {"fen": chess.STARTING_FEN},
            "history": {"initial_fen": chess.STARTING_FEN, "moves": []}, "candidate": "e2e4"}


class ValidatorCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source_path = self.directory / "validator.py"
        self.request_path = self.directory / "request.json"
        self.source = b"# Preserve exact source bytes.\r\ndef validate(position, history, candidate):\r\n    return {}\r\n"
        self.source_path.write_bytes(self.source)
        self.request = request()
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        self.backend_module = ModuleType("chess_harness.validator_sandbox")
        self.backend_module.SandboxConfig = Mock(name="SandboxConfig")
        self.backend_module.DockerSandbox = Mock(name="DockerSandbox")
        self.backend = self.backend_module.DockerSandbox.return_value
        self.backend.preflight.return_value = {"status": "ok", "evidence": {"checked": True}}
        self.backend.run.return_value = {"status": "ok", "findings": {"facts": [], "heuristics": []}}
        self.fake_module = patch.dict(sys.modules, {self.backend_module.__name__: self.backend_module})
        self.fake_module.start()
        self.addCleanup(self.fake_module.stop)

    def run_arguments(self, *extra):
        return ["run", *ENABLED, "--source", str(self.source_path),
                "--request", str(self.request_path), *extra]

    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assertParserError(self, argv):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(argv)
        self.assertEqual(caught.exception.code, 2)

    def test_disabled_commands_never_touch_files_or_instantiate_backend(self):
        cases = [
            ["preflight"],
            ["preflight", "--image", IMAGE, "--output", str(self.directory / "unused.json")],
            ["run", "--source", str(self.source_path), "--request", str(self.request_path)],
            ["run", "--image", IMAGE, "--source", str(self.source_path),
             "--request", str(self.request_path), "--output", str(self.directory / "unused.json")],
        ]
        with patch("pathlib.Path.open", side_effect=AssertionError("disabled file access")):
            for args in cases:
                with self.subTest(args=args):
                    self.assertParserError(args)
        self.backend_module.SandboxConfig.assert_not_called()
        self.backend_module.DockerSandbox.assert_not_called()

    def test_enabled_commands_require_pinned_image_and_run_inputs(self):
        cases = [
            ["preflight", "--enable-authored-validators"],
            ["preflight", "--enable-authored-validators", "--image", "validator:latest"],
            ["preflight", "--enable-authored-validators", "--image", "sha256:" + "a" * 63],
            ["run", *ENABLED],
            ["run", *ENABLED, "--source", str(self.source_path)],
        ]
        with patch("pathlib.Path.open", side_effect=AssertionError("invalid flag file access")):
            for args in cases:
                with self.subTest(args=args):
                    self.assertParserError(args)
        self.backend_module.DockerSandbox.assert_not_called()

    def test_preflight_reports_evidence_and_uses_explicit_context(self):
        code, stdout, stderr = self.invoke(["preflight", *ENABLED, "--docker-context", "isolated-vm"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), self.backend.preflight.return_value)
        self.backend_module.SandboxConfig.assert_called_once_with(
            enabled=True, image=IMAGE, docker_context="isolated-vm")
        self.backend.preflight.assert_called_once_with()
        self.backend.run.assert_not_called()

    def test_failed_preflight_is_reported_and_returns_nonzero(self):
        self.backend.preflight.return_value = {
            "status": "error", "reason": "isolation_unavailable", "evidence": {"vm": False}}
        code, stdout, stderr = self.invoke(["preflight", *ENABLED])
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(json.loads(stdout), self.backend.preflight.return_value)

    def test_run_passes_exact_source_bytes_and_parsed_request(self):
        code, stdout, stderr = self.invoke(self.run_arguments())
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), self.backend.run.return_value)
        self.backend_module.SandboxConfig.assert_called_once_with(
            enabled=True, image=IMAGE, docker_context="desktop-linux")
        self.backend.run.assert_called_once_with(self.source, self.request)

    def test_result_file_preserves_failure_evidence_and_partial_measurements(self):
        report = {"status": "error", "reason": "timeout", "stdout": "partial output",
                  "costs": {"wall_seconds": 2.0, "cpu_seconds": None},
                  "evidence": {"worker_killed": True}}
        self.backend.run.return_value = report
        output = self.directory / "execution.json"
        code, stdout, stderr = self.invoke(self.run_arguments("--output", str(output)))
        self.assertEqual((code, stdout, stderr), (1, "", ""))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)

    def test_existing_output_is_not_overwritten_and_prevents_execution(self):
        output = self.directory / "prior.json"
        output.write_text("keep this report", encoding="utf-8")
        code, stdout, stderr = self.invoke(self.run_arguments("--output", str(output)))
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["reason"], "report_output_error")
        self.assertEqual(output.read_text(encoding="utf-8"), "keep this report")
        self.backend_module.DockerSandbox.assert_not_called()

    def test_malformed_or_oversized_input_fails_before_backend_creation(self):
        cases = [
            (self.request_path, b"not JSON", None),
            (self.request_path, b"x" * (MAX_INPUT_BYTES + 1), "input_too_large"),
            (self.source_path, b"x" * (MAX_SOURCE_BYTES + 1), "source_too_large"),
        ]
        for path, content, reason in cases:
            with self.subTest(path=path, reason=reason):
                self.source_path.write_bytes(self.source)
                self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
                path.write_bytes(content)
                code, stdout, stderr = self.invoke(self.run_arguments())
                report = json.loads(stdout)
                self.assertEqual((code, stderr, report["status"]), (1, "", "error"))
                if reason is not None:
                    self.assertEqual(report["reason"], reason)
        self.backend_module.DockerSandbox.assert_not_called()

    def test_bounded_reader_requests_only_limit_plus_one_bytes(self):
        stream = Mock()
        stream.read.return_value = b"data"
        path = Mock()
        path.open.return_value.__enter__ = Mock(return_value=stream)
        path.open.return_value.__exit__ = Mock(return_value=False)
        self.assertEqual(_read_bounded(path, 8, "too_large", "$.source"), b"data")
        path.open.assert_called_once_with("rb")
        stream.read.assert_called_once_with(9)

    def test_input_io_and_backend_errors_are_explicit_without_fallback(self):
        self.source_path.unlink()
        code, stdout, stderr = self.invoke(self.run_arguments())
        self.assertEqual((code, stderr, json.loads(stdout)["reason"]), (1, "", "input_error"))
        self.backend_module.DockerSandbox.assert_not_called()
        self.source_path.write_bytes(self.source)
        self.backend.run.side_effect = OSError("Docker unavailable")
        code, stdout, stderr = self.invoke(self.run_arguments())
        self.assertEqual((code, stderr, json.loads(stdout)["reason"]), (1, "", "sandbox_error"))
        self.backend.run.assert_called_once_with(self.source, self.request)

    def test_game_assistance_still_requires_existing_mode_flags(self):
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        with patch("pathlib.Path.is_file", return_value=True):
            baseline = game_config(parser, parser.parse_args([]))
            self.assertEqual(baseline["mode"], "unassisted")
            self.assertNotIn("tools", baseline)
            for mode in ("legal-moves", "constrained-legal", "rules-tools"):
                with self.subTest(mode=mode):
                    config = game_config(parser, parser.parse_args(["--mode", mode]))
                    self.assertEqual(config["mode"], mode)
                    self.assertEqual("tools" in config, mode == "rules-tools")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            game_config(parser, parser.parse_args(["--mode", "authored-validator"]))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            game_config(parser, parser.parse_args(["--enable-authored-validators"]))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            game_config(parser, parser.parse_args(["--tool-calls", "1"]))


if __name__ == "__main__":
    unittest.main()
