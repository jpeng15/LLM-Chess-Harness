from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chess_harness.validator_artifacts import ArtifactError, freeze, load_artifact
from chess_harness.validator import main
from chess_harness.validator_contract import CONTRACT_VERSION
from chess_harness.validator_development import check_case, development_cases, generate, record_costs, suite_hash
from chess_harness.validator_sandbox import POLICY_VERSION, SandboxConfig, policy_limits, runtime_source_hash


IMAGE = "sha256:" + "a" * 64
SOURCE = b"def validate(position, history, candidate):\n raise RuntimeError('not executed in unit tests')\n"


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def readiness():
    return {"status": "ok", "reason": "ready", "policy_version": POLICY_VERSION,
            "runtime": {"image": IMAGE, "context": "desktop-linux", "source_sha256": runtime_source_hash()},
            "checks": []}


def execution(source, request):
    case = next(case for case in development_cases() if case["request"] == request)
    findings = {"contract_version": CONTRACT_VERSION,
                "facts": [{"id": f"f{index}", **deepcopy(fact)}
                          for index, fact in enumerate(case["required_facts"], 1)], "heuristics": []}
    return {"status": "ok", "reason": "completed", "cleanup_confirmed": True,
            "source_sha256": sha(source), "input_sha256": sha(encoded(request)),
            "image": IMAGE, "policy_version": POLICY_VERSION, "findings": findings,
            "cpu_seconds": 0.25, "worker_wall_seconds": 0.5, "execution_wall_seconds": 0.75,
            "total_wall_seconds": 1.0, "cleanup_seconds": 0.1, "peak_memory_bytes": 1024}


class FakeBackend:
    """Fabricated execution evidence only; source is never compiled or run."""
    def __init__(self, change=None):
        self.config = SandboxConfig(enabled=True, image=IMAGE)
        self.preparations = 0
        self.calls = []
        self.change = change

    def prepare(self):
        self.preparations += 1
        return readiness()

    def run_prepared(self, source, request):
        self.calls.append((source, deepcopy(request)))
        result = execution(source, request)
        if self.change:
            self.change(result, len(self.calls))
        return result


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.development = self.root / "development"
        self.artifacts = self.root / "artifacts"

    def write_development(self, directory=None, source=SOURCE, failed_attempt=False):
        directory = directory or self.development
        directory.mkdir()
        attempts = []
        if failed_attempt:
            attempts.append({"index": 1, "status": "failed", "source_sha256": sha(b"bad source"),
                             "generation": {"raw": {"source": "bad source"}, "prompt_tokens": 3}, "tests": []})
            path = directory / "attempt-001"
            path.mkdir()
            (path / "source.py").write_bytes(b"bad source")
        index = len(attempts) + 1
        tests = []
        for case in development_cases():
            result = execution(source, case["request"])
            tests.append({"case_id": case["id"], "request": case["request"], "execution": result,
                          **check_case(case, result)})
        attempts.append({"index": index, "status": "passed", "source_sha256": sha(source),
                         "generation": {"request": {"model": "test"}, "raw": {"source": source.decode()},
                                        "prompt_tokens": 10, "output_tokens": 20}, "tests": tests})
        path = directory / f"attempt-{index:03d}"
        path.mkdir()
        (path / "source.py").write_bytes(source)
        report = {"schema_version": 1, "status": "passed", "config": {
            "enabled": True, "image": IMAGE, "docker_context": "desktop-linux", "max_attempts": 3,
            "llm": {"model": "test"}}, "suite_sha256": suite_hash(), "prompt_version": "validator-generation-v1",
            "model_identity": {"digest": "b" * 64}, "selected_attempt": index, "attempts": attempts,
            "costs": {"generation": {"calls": len(attempts), "prompt_tokens": 13, "output_tokens": 20}},
            "preflight": readiness(), "model_preparation": {"model_identity": {"digest": "b" * 64}},
            "sandbox_policy": policy_limits(), "runtime_source_sha256": runtime_source_hash()}
        self.save_development(report, directory)
        return report

    def save_development(self, report, directory=None):
        directory = directory or self.development
        report["costs"] = record_costs(report)
        for attempt in report["attempts"]:
            (directory / f"attempt-{attempt['index']:03d}" / "attempt.json").write_bytes(encoded(attempt))
        (directory / "development.json").write_bytes(encoded(report))

    def freeze_ok(self, backend=None, development=None):
        backend = backend or FakeBackend()
        report = freeze(development or self.development, self.artifacts, enabled=True, backend=backend)
        self.assertEqual(report["status"], "ok", report)
        return Path(report["directory"]), backend, report

    def resign_manifest(self, directory, change):
        manifest = json.loads((directory / "manifest.json").read_bytes())
        change(manifest)
        manifest["artifact_id"] = sha(encoded({k: v for k, v in manifest.items() if k != "artifact_id"}))
        (directory / "manifest.json").write_bytes(encoded(manifest))
        destination = directory.with_name(manifest["artifact_id"])
        directory.rename(destination)
        return destination

    def test_disabled_gate_precedes_file_access_and_runtime(self):
        backend = FakeBackend()
        with patch("pathlib.Path.open") as opened, patch("chess_harness.validator_artifacts.runtime_source_hash") as runtime:
            report = freeze(self.development, self.artifacts, backend=backend)
        self.assertEqual(report["reason"], "disabled")
        self.assertEqual(backend.preparations, 0)
        opened.assert_not_called()
        runtime.assert_not_called()
        self.assertFalse(self.artifacts.exists())

    def test_freeze_cli_requires_opt_in_and_paths_and_uses_bound_image(self):
        args = ["freeze", "--development", str(self.development), "--artifacts", str(self.artifacts)]
        with patch("chess_harness.validator_artifacts.freeze", return_value={"status": "ok"}) as frozen, \
                patch("pathlib.Path.open") as opened, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(args)
            with self.assertRaises(SystemExit):
                main(["freeze", "--enable-authored-validators"])
            opened.assert_not_called()
            frozen.assert_not_called()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([*args, "--enable-authored-validators"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"status": "ok"})
            frozen.assert_called_once_with(self.development, self.artifacts, enabled=True)

    def test_generate_output_can_be_frozen_without_schema_translation(self):
        class Generator:
            def prepare(self):
                return {"model_identity": {"digest": "b" * 64}}

            def generate(self, request):
                return {"source": SOURCE.decode(), "request": request,
                        "raw": {"message": {"content": json.dumps({"source": SOURCE.decode()})}},
                        "elapsed_seconds": 1, "prompt_tokens": 10, "output_tokens": 20, "error": None}

        config = {"enabled": True, "image": IMAGE, "docker_context": "desktop-linux", "max_attempts": 3,
                  "llm": {"model": "test", "url": "http://localhost:11434", "think": False,
                          "tokens": 512, "context": 16384, "seconds": 60, "seed": 0, "temperature": 0}}
        generated = generate(self.development, config, backend=FakeBackend(), generator=Generator())
        self.assertEqual(generated["status"], "passed")
        directory, _, _ = self.freeze_ok()
        self.assertEqual(load_artifact(directory).source, SOURCE)

    def test_freeze_preserves_all_provenance_repeats_and_separate_costs(self):
        development = self.write_development(failed_attempt=True)
        with patch("chess_harness.validator_sandbox.subprocess.Popen") as process, \
                patch("chess.engine.SimpleEngine.popen_uci") as engine:
            directory, backend, report = self.freeze_ok()
            artifact = load_artifact(directory)
        process.assert_not_called()
        engine.assert_not_called()
        self.assertEqual(backend.preparations, 1)
        self.assertEqual(len(backend.calls), len(development_cases()) * 2)
        self.assertTrue(all(source == SOURCE for source, _ in backend.calls))
        self.assertEqual(artifact.source, SOURCE)
        self.assertEqual(artifact.artifact_id, directory.name)
        self.assertEqual((directory / "development.json").read_bytes(), (self.development / "development.json").read_bytes())
        self.assertEqual((directory / "attempts/attempt-001/source.py").read_bytes(), b"bad source")
        self.assertEqual(report["setup_costs"]["generation_development"], development["costs"])
        self.assertEqual(report["setup_costs"]["freeze_validation"]["invocations"], 20)
        self.assertEqual(report["setup_costs"]["freeze_validation"]["cpu_seconds"], 5)
        metadata = artifact.manifest
        metadata["limits"]["memory_bytes"] = 0
        self.assertNotEqual(artifact.manifest["limits"]["memory_bytes"], 0)
        original_development = artifact.development
        self.assertEqual(original_development, development)
        original_development["attempts"].clear()
        self.assertTrue(artifact.development["attempts"])

    def test_unpassed_development_and_selected_attempt_fail_before_runtime(self):
        original = self.write_development()
        for status, attempt_status in (("exhausted", "passed"), ("passed", "failed")):
            report = deepcopy(original)
            report["status"] = status
            report["attempts"][0]["status"] = attempt_status
            self.save_development(report)
            backend = FakeBackend()
            result = freeze(self.development, self.artifacts, enabled=True, backend=backend)
            self.assertEqual(result["reason"], "development_failed")
            self.assertEqual(backend.preparations, 0)
        self.assertFalse(self.artifacts.exists())

    def test_failed_attempt_source_is_verified_too(self):
        self.write_development(failed_attempt=True)
        (self.development / "attempt-001/source.py").write_bytes(b"changed rejected proposal")
        backend = FakeBackend()
        report = freeze(self.development, self.artifacts, enabled=True, backend=backend)
        self.assertEqual(report["reason"], "provenance_mismatch")
        self.assertEqual(backend.preparations, 0)

    def test_development_costs_are_reconciled_before_freezing(self):
        report = self.write_development()
        report["costs"]["generation"]["output_tokens"] = 99999
        (self.development / "development.json").write_bytes(encoded(report))
        backend = FakeBackend()
        result = freeze(self.development, self.artifacts, enabled=True, backend=backend)
        self.assertEqual(result["reason"], "provenance_mismatch")
        self.assertEqual(backend.preparations, 0)

    def test_any_frozen_file_change_is_rejected(self):
        self.write_development(failed_attempt=True)
        directory, _, _ = self.freeze_ok()
        paths = ["validator.py", "manifest.json", "development.json", "acceptance.json",
                 "attempts/attempt-001/source.py", "attempts/attempt-001/attempt.json",
                 "attempts/attempt-002/source.py", "attempts/attempt-002/attempt.json"]
        for name in paths:
            path = directory / name
            original = path.read_bytes()
            with self.subTest(path=name):
                # Manifest whitespace is semantically irrelevant; alter its bound metadata.
                path.write_bytes(original.replace(b'"state":"frozen"', b'"state":"mutable"')
                                 if name == "manifest.json" else original + b"\n")
                with self.assertRaises(ArtifactError):
                    load_artifact(directory)
                path.write_bytes(original)
        load_artifact(directory)

    def test_manifest_cannot_redirect_provenance_reads(self):
        self.write_development()
        directory, _, _ = self.freeze_ok()
        directory = self.resign_manifest(directory, lambda m: m["provenance"].update({"../../secret": "c" * 64}))
        with self.assertRaises(ArtifactError) as caught:
            load_artifact(directory)
        self.assertEqual(caught.exception.reason, "invalid_artifact")

    def test_current_runtime_and_policy_are_required_even_with_valid_hashes(self):
        self.write_development()
        directory, _, _ = self.freeze_ok()
        with patch("chess_harness.validator_artifacts.runtime_source_hash", return_value="d" * 64):
            with self.assertRaises(ArtifactError) as caught:
                load_artifact(directory)
            self.assertEqual(caught.exception.reason, "environment_mismatch")
            backend = FakeBackend()
            self.assertEqual(freeze(self.development, self.artifacts, enabled=True, backend=backend)["reason"],
                             "environment_mismatch")
            self.assertEqual(backend.preparations, 0)
        directory = self.resign_manifest(directory, lambda m: m["limits"].update({"wall_seconds": 999}))
        with self.assertRaises(ArtifactError):
            load_artifact(directory)

    def test_different_fact_ids_are_nondeterministic_even_when_true(self):
        self.write_development()
        def change(result, count):
            result["findings"]["facts"][0]["id"] = "another_id"
        backend = FakeBackend(change)
        report = freeze(self.development, self.artifacts, enabled=True, backend=backend)
        self.assertEqual(report["reason"], "nondeterministic")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(report["acceptance"]["costs"]["invocations"], 1)
        self.assertFalse(self.artifacts.exists())

    def test_execution_error_stops_freeze_and_preserves_cost_evidence(self):
        self.write_development()
        backend = FakeBackend(lambda result, count: result.update(status="error", reason="timeout"))
        report = freeze(self.development, self.artifacts, enabled=True, backend=backend)
        self.assertEqual(report["reason"], "timeout")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(report["acceptance"]["costs"]["cpu_seconds"], 0.25)
        self.assertFalse(self.artifacts.exists())

    def test_identical_artifact_cannot_be_overwritten(self):
        self.write_development()
        with patch("chess_harness.validator_artifacts.time.monotonic", return_value=0):
            directory, _, first = self.freeze_ok()
            original = (directory / "manifest.json").read_bytes()
            result = freeze(self.development, self.artifacts, enabled=True, backend=FakeBackend())
        self.assertEqual(result["reason"], "artifact_exists")
        self.assertEqual((directory / "manifest.json").read_bytes(), original)
        self.assertEqual(load_artifact(directory).artifact_id, first["artifact_id"])
        self.assertEqual(list(self.artifacts.iterdir()), [directory])

    def test_new_source_creates_new_identity(self):
        self.write_development()
        first, _, _ = self.freeze_ok()
        second_development = self.root / "development-two"
        self.write_development(second_development, source=SOURCE + b"# revision\n")
        second, _, _ = self.freeze_ok(development=second_development)
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(load_artifact(first).source, SOURCE)
        self.assertEqual(load_artifact(second).source, SOURCE + b"# revision\n")

    def test_verified_bytes_are_used_even_if_original_path_changes(self):
        self.write_development()
        backend = FakeBackend(lambda result, count: (self.development / "attempt-001/source.py").write_bytes(b"changed"))
        directory, backend, _ = self.freeze_ok(backend)
        self.assertEqual(load_artifact(directory).source, SOURCE)
        self.assertTrue(all(source == SOURCE for source, _ in backend.calls))

    def test_extra_files_and_duplicate_json_are_rejected(self):
        self.write_development()
        directory, _, _ = self.freeze_ok()
        (directory / "unrecorded.txt").write_text("not bound to the identity")
        with self.assertRaises(ArtifactError):
            load_artifact(directory)
        (directory / "unrecorded.txt").unlink()
        manifest = directory / "manifest.json"
        original = manifest.read_bytes()
        manifest.write_bytes(b'{"schema_version":1,' + original[1:])
        with self.assertRaises(ArtifactError):
            load_artifact(directory)

    def test_symlink_source_is_rejected(self):
        self.write_development()
        path = self.development / "attempt-001/source.py"
        target = self.root / "source.py"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Creating Windows symbolic links requires privileges")
        report = freeze(self.development, self.artifacts, enabled=True, backend=FakeBackend())
        self.assertEqual(report["reason"], "unsafe_path")


if __name__ == "__main__":
    unittest.main()
