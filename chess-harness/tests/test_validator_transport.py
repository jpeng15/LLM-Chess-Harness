"""Exercise pipe transport with fixed Python fixtures, never validator source.

These tests do not start Docker, import the worker, or claim that VM isolation
has passed. They check the host subprocess transport independently.
"""

import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from chess_harness.validator_sandbox import _bounded_process


def python_command(program):
    return [sys.executable, "-I", "-c", program]


class ValidatorTransportTests(unittest.TestCase):
    def test_cancellation_reaps_native_client_before_return(self):
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        try:
            started = time.monotonic()
            result = _bounded_process(python_command("import time; time.sleep(5)"),
                                      seconds=3, cancel_event=cancel)
            self.assertEqual(result.reason, "cancelled")
            self.assertIsNotNone(result.exit_code)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            timer.cancel()
            timer.join()

    def test_both_output_pipes_drain_while_input_is_waiting(self):
        # Both output bursts exceed ordinary pipe capacity, and the child reads
        # stdin only afterwards. A serial write-then-read transport deadlocks.
        program = (
            "import sys; "
            "sys.stderr.buffer.write(b'e' * 131072); sys.stderr.buffer.flush(); "
            "sys.stdout.buffer.write(b'o' * 131072); sys.stdout.buffer.flush(); "
            "data = sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(str(len(data)).encode('ascii')); sys.stdout.buffer.flush()"
        )
        result = _bounded_process(python_command(program), data=b"input" * 65536,
                                  seconds=3, stdout_limit=140000, stderr_limit=131072)
        self.assertIsNone(result.reason)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"o" * 131072 + b"327680")
        self.assertEqual(result.stderr, b"e" * 131072)

    def test_independent_output_limits_retain_only_the_bounded_prefix(self):
        for stream, expected in (("stdout", "output_limit"), ("stderr", "stderr_limit")):
            with self.subTest(stream=stream):
                program = (
                    "import sys, time; "
                    f"sys.{stream}.buffer.write(b'x' * 262144); sys.{stream}.buffer.flush(); "
                    "time.sleep(5)"
                )
                started = time.monotonic()
                result = _bounded_process(python_command(program), seconds=2,
                                          stdout_limit=127, stderr_limit=113)
                self.assertEqual(result.reason, expected)
                self.assertLessEqual(len(result.stdout), 127)
                self.assertLessEqual(len(result.stderr), 113)
                self.assertEqual(getattr(result, stream), b"x" * (127 if stream == "stdout" else 113))
                self.assertIsNotNone(result.exit_code)
                self.assertLess(time.monotonic() - started, 3)

    def test_exact_limits_are_accepted_and_empty_input_reaches_eof(self):
        program = (
            "import sys; "
            "assert sys.stdin.buffer.read() == b''; "
            "sys.stdout.buffer.write(b'o' * 127); "
            "sys.stderr.buffer.write(b'e' * 113)"
        )
        result = _bounded_process(python_command(program), data=b"", seconds=2,
                                  stdout_limit=127, stderr_limit=113)
        self.assertIsNone(result.reason)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"o" * 127)
        self.assertEqual(result.stderr, b"e" * 113)

    def test_timeout_reaps_child_and_closes_pipes_even_when_stdin_is_blocked(self):
        program = (
            "import sys, time; "
            "sys.stdout.buffer.write(b'started'); sys.stdout.buffer.flush(); "
            "time.sleep(5)"
        )
        native_popen = subprocess.Popen
        children = []

        def launch(*args, **kwargs):
            child = native_popen(*args, **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with patch("chess_harness.validator_sandbox.subprocess.Popen", side_effect=launch):
            result = _bounded_process(python_command(program), data=b"x" * 524288,
                                      seconds=0.5, stdout_limit=128, stderr_limit=128)
        self.assertEqual(result.reason, "timeout")
        self.assertNotEqual(result.exit_code, 0)
        # A loaded machine may exhaust the deadline before interpreter startup.
        self.assertIn(result.stdout, (b"", b"started"))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)

    def test_docker_environment_overrides_are_removed_without_exposing_values(self):
        overrides = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_API_VERSION",
                     "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
        program = (
            "import os, sys; "
            f"assert all(key not in os.environ for key in {overrides!r}); "
            "assert os.environ.get('CHESS_TRANSPORT_TEST_SENTINEL') == 'present'; "
            "sys.stdout.buffer.write(b'clean')"
        )
        environment = {key: "test-override" for key in overrides}
        environment["CHESS_TRANSPORT_TEST_SENTINEL"] = "present"
        with patch.dict(os.environ, environment):
            result = _bounded_process(python_command(program), seconds=2)
            self.assertTrue(all(os.environ[key] == "test-override" for key in overrides))
        self.assertIsNone(result.reason)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"clean")
        self.assertEqual(result.stderr, b"")

    def test_child_failure_keeps_partial_output_and_nonzero_exit_code(self):
        program = (
            "import sys; "
            "sys.stdout.buffer.write(b'partial'); sys.stdout.buffer.flush(); "
            "sys.stderr.buffer.write(b'failure'); sys.stderr.buffer.flush(); "
            "sys.exit(7)"
        )
        result = _bounded_process(python_command(program), seconds=2)
        self.assertIsNone(result.reason)
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout, b"partial")
        self.assertEqual(result.stderr, b"failure")


if __name__ == "__main__":
    unittest.main()
