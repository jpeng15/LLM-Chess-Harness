import json
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch

from chess_harness.validator_sandbox import DockerSandbox, SandboxConfig, _probe_request, policy_limits


CONFIG = SandboxConfig(True, "sha256:" + "a" * 64)
SOURCE = b'def validate(position, history, candidate): return {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}'
EMPTY = {"contract_version": "authored-validator-v1", "facts": [], "heuristics": []}
READY = {"status": "ok", "reason": "ready", "runtime": {"image": CONFIG.image, "daemon_id": "one"}}


class PreparedSandboxTests(unittest.TestCase):
    def prepared(self):
        backend = DockerSandbox(CONFIG)
        with patch.object(backend, "preflight", return_value=deepcopy(READY)):
            receipt = backend.prepare()
        receipt["runtime"] = {"tampered": True}
        return backend

    def test_in_memory_acceptance_required_and_returned_receipt_cannot_change_it(self):
        backend = DockerSandbox(CONFIG)
        with patch.object(backend, "_execute") as execute:
            self.assertEqual(backend.run_prepared(SOURCE, _probe_request())["reason"], "preflight_required")
            execute.assert_not_called()
        backend = self.prepared()
        with patch.object(backend, "_runtime", return_value={"image": CONFIG.image, "daemon_id": "one"}), \
                patch.object(backend, "_execute", return_value={"status": "ok", "reason": "completed", "stdout": json.dumps(EMPTY).encode()}):
            result = backend.run_prepared(SOURCE, _probe_request())
        self.assertEqual(result["findings"], EMPTY)
        self.assertIn("runtime_check_seconds", result)
        self.assertIn("total_wall_seconds", result)

    def test_runtime_change_invalidates_session_before_source_execution(self):
        backend = self.prepared()
        with patch.object(backend, "_runtime", return_value={"daemon_id": "changed"}), \
                patch.object(backend, "_execute") as execute:
            self.assertEqual(backend.run_prepared(SOURCE, _probe_request())["reason"], "environment_mismatch")
            self.assertEqual(backend.run_prepared(SOURCE, _probe_request())["reason"], "preflight_required")
            execute.assert_not_called()

    def test_cancelled_or_expired_turn_starts_no_container(self):
        for options, reason in (({"deadline": time.monotonic() - 1}, "timeout"),
                                ({"cancel_event": threading.Event()}, "cancelled")):
            if "cancel_event" in options:
                options["cancel_event"].set()
            backend = self.prepared()
            with patch.object(backend, "_runtime") as runtime, patch.object(backend, "_execute") as execute:
                self.assertEqual(backend.run_prepared(SOURCE, _probe_request(), **options)["reason"], reason)
                runtime.assert_not_called()
                execute.assert_not_called()

    def test_late_result_is_not_accepted_and_factual_verification_still_runs(self):
        backend = self.prepared()
        cancellation = threading.Event()
        def execute(*args):
            cancellation.set()
            return {"status": "ok", "reason": "completed", "stdout": json.dumps(EMPTY).encode(), "cleanup_confirmed": True}
        with patch.object(backend, "_runtime", return_value={"image": CONFIG.image, "daemon_id": "one"}), \
                patch.object(backend, "_execute", side_effect=execute):
            result = backend.run_prepared(SOURCE, _probe_request(), cancel_event=cancellation)
        self.assertEqual(result["reason"], "cancelled")
        self.assertNotIn("findings", result)
        backend = self.prepared()
        false = {**EMPTY, "facts": [{"id": "f1", "kind": "candidate_checkmate", "line": ["e2e4"]}]}
        with patch.object(backend, "_runtime", return_value={"image": CONFIG.image, "daemon_id": "one"}), \
                patch.object(backend, "_execute", return_value={"status": "ok", "reason": "completed", "stdout": json.dumps(false).encode()}):
            self.assertEqual(backend.run_prepared(SOURCE, _probe_request())["reason"], "invalid_finding")

    def test_cleanup_calls_ignore_cancel_but_other_runtime_calls_do_not(self):
        backend = DockerSandbox(CONFIG)
        backend._docker_path = "docker"
        event = threading.Event()
        event.set()
        backend._control.cancel_event = event
        backend._control.deadline = time.monotonic() - 1
        from chess_harness.validator_sandbox import CommandResult
        with patch("chess_harness.validator_sandbox._bounded_process", return_value=CommandResult(0)) as process:
            self.assertEqual(backend._call(["start", "container"]).reason, "cancelled")
            process.assert_not_called()
            self.assertTrue(backend._cleanup("chess-validator-owned"))
            self.assertNotIn("cancel_event", process.call_args.kwargs)

    def test_limits_are_detached_and_concurrency_is_explicit(self):
        first = policy_limits()
        first["memory_bytes"] = 1
        self.assertEqual(policy_limits()["memory_bytes"], 256 * 1024 * 1024)
        backend = self.prepared()
        backend._execution_lock.acquire()
        try:
            self.assertEqual(backend.run_prepared(SOURCE, _probe_request())["reason"], "sandbox_busy")
        finally:
            backend._execution_lock.release()
