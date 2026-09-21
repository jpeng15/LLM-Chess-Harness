"""Pure worker contract/policy tests; no source execution or sandbox startup."""
import ast
import importlib.util
import json
from pathlib import Path
from types import CodeType
import unittest
from unittest.mock import patch

import chess

from chess_harness.validator_contract import CONTRACT_VERSION, RULES_API_VERSION


WORKER_PATH = Path(__file__).resolve().parents[1] / "sandbox" / "validator" / "worker.py"
SPEC = importlib.util.spec_from_file_location("validator_worker_policy_only", WORKER_PATH)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)  # Imports trusted helper definitions only.

SOURCE = '''def validate(position, history, candidate):
    board = RulesBoard.from_input(position, history)
    preview = board.copy()
    preview.push(candidate)
    facts = []
    for reply in preview.legal_moves():
        capture = preview.capture(reply)
        if capture:
            facts.append({"id": "f" + str(len(facts)), "kind": "capture_available",
                          "line": [candidate, reply], **capture})
    return {"contract_version": CONTRACT_VERSION, "facts": facts, "heuristics": []}
'''


def request():
    return {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "position": {"fen": chess.STARTING_FEN},
            "history": {"initial_fen": chess.STARTING_FEN, "moves": []}, "candidate": "e2e4"}


def payload(source=SOURCE):
    return json.dumps({"source": source, "request": request()}, ensure_ascii=False).encode("utf-8")


class ValidatorWorkerPolicyTests(unittest.TestCase):
    def assert_code(self, code, function, *args):
        with self.assertRaises(worker.WorkerError) as caught:
            function(*args)
        self.assertEqual(caught.exception.exit_code, code)

    def test_policy_compiles_rules_logic_without_running_it_or_installing_filter(self):
        with patch("builtins.exec", side_effect=AssertionError("source must not execute")), \
                patch("builtins.eval", side_effect=AssertionError("source must not evaluate")), \
                patch.object(worker, "install_seccomp", side_effect=AssertionError("no host seccomp")), \
                patch.object(worker, "verify_environment", side_effect=AssertionError("no worker startup")):
            self.assertIsInstance(worker.prepare_source(SOURCE), CodeType)
            code = worker.prepare_source('def validate(position, history, candidate):\n    raise RuntimeError("not executed")\n')
            self.assertIsInstance(code, CodeType)
            source, decoded = worker.decode_payload(payload())
            self.assertEqual(source, SOURCE)
            self.assertEqual(decoded, request())

    def test_private_reflection_import_process_and_file_access_are_rejected(self):
        statements = [
            "import os", "from os import open", "return board._board",
            "return RulesBoard.__dict__", "return globals()", "return locals()",
            "return vars(RulesBoard)", "return getattr(RulesBoard, 'copy')",
            "return type(RulesBoard)", "return open('/etc/passwd')",
            "return eval('1 + 1')", "exec('pass')", "return compile('x', 'x', 'exec')",
            "return __import__('socket')", "return os.fork()", "return subprocess.run([])",
            "RulesBoard.copy = 1", "RulesBoard = 1", "CONTRACT_VERSION = 'changed'",
            "board.x = 1", "global position", "nonlocal candidate",
            "with candidate:\n        pass", "class Checker:\n        pass",
            "x: int = 1", "match candidate:\n        case 'e2e4': pass",
            "try:\n        pass\n    except Exception as RulesBoard:\n        pass",
        ]
        for statement in statements:
            with self.subTest(statement=statement):
                self.assert_code(worker.EXIT_POLICY, worker.prepare_source,
                                 "def validate(position, history, candidate):\n    " + statement + "\n")

    def test_function_signatures_decorators_defaults_and_top_level_execution_rejected(self):
        sources = [
            "def other(position, history, candidate): pass",
            "def validate(position, history): pass",
            "def validate(position, history, candidate, extra): pass",
            "def validate(position, history, candidate, /): pass",
            "def validate(position, history, *, candidate): pass",
            "def validate(position, history, candidate=None): pass",
            "def validate(position, history, candidate: str): pass",
            "def validate(position, history, candidate) -> dict: pass",
            "@print\ndef validate(position, history, candidate): pass",
            "async def validate(position, history, candidate): pass",
            "def validate(position, history, candidate): pass\ndef validate(position, history, candidate): pass",
            SOURCE + "\nprint('module top level')",
            SOURCE + "\nwhile True: pass",
            SOURCE + "\ndef helper(x=print('default')): pass",
            SOURCE + "\ndef helper(_secret): pass",
            SOURCE + "\ndef _private(): pass",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assert_code(worker.EXIT_POLICY, worker.prepare_source, source)

    def test_safe_helpers_comprehensions_local_state_and_documentation_are_allowed(self):
        source = '''"""Rules-only validator. Words such as import and open are data here."""
def identity(value):
    return value

def validate(position, history, candidate):
    values = [identity(value) for value in range(4)]
    seen = set(values)
    result = {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}
    result["heuristics"] = []
    try:
        values.remove(3)
    except ValueError as error:
        print(str(error))
    return result
'''
        self.assertIsInstance(worker.prepare_source(source), CodeType)

    def test_source_size_syntax_and_ast_limits_are_explicit(self):
        self.assert_code(worker.EXIT_INPUT, worker.prepare_source, "")
        self.assert_code(worker.EXIT_INPUT, worker.prepare_source, "x" * (worker.MAX_SOURCE_BYTES + 1))
        self.assert_code(worker.EXIT_INPUT, worker.prepare_source, "\ud800")
        self.assert_code(worker.EXIT_INPUT, worker.prepare_source, 123)
        self.assert_code(worker.EXIT_SYNTAX, worker.prepare_source, "def validate(")
        self.assert_code(worker.EXIT_SYNTAX, worker.prepare_source,
                         "def validate(position, history, candidate):\n    return '\x00'\n")
        source = "def validate(position, history, candidate):\n" + "    x = 1\n" * 3000
        self.assertLess(len(source), worker.MAX_SOURCE_BYTES)
        self.assert_code(worker.EXIT_POLICY, worker.prepare_source, source)

    def test_wire_limit_duplicate_keys_nonfinite_and_nesting_are_rejected(self):
        for raw in (b"", b"{", b"{} {}", b"\xff", b'{"n":NaN}', b'{"n":Infinity}',
                    b'{"n":1e999}', b'{"n":' + b"1" * 5000 + b'}',
                    b'{"source":"x","source":"y","request":{}}',
                    b'{"source":"x","request":{"x":1,"x":2}}',
                    b"[" * (worker.MAX_WIRE_DEPTH + 1) + b"]" * (worker.MAX_WIRE_DEPTH + 1),
                    b" " * (worker.MAX_WIRE_BYTES + 1), "{}"):
            with self.subTest(raw=repr(raw)[:80]):
                self.assert_code(worker.EXIT_INPUT, worker.decode_payload, raw)
        value = {"source": SOURCE, "request": request(), "answer": "not permitted"}
        self.assert_code(worker.EXIT_INPUT, worker.decode_payload, json.dumps(value).encode())
        value = {"source": SOURCE, "request": request()}
        value["request"]["candidate"] = "e2e5"
        self.assert_code(worker.EXIT_INPUT, worker.decode_payload, json.dumps(value).encode())

    def test_source_string_brackets_are_not_json_nesting(self):
        source = SOURCE + '\n"' + "[" * 100 + '"\n'
        decoded_source, _ = worker.decode_payload(payload(source))
        self.assertEqual(decoded_source, source)

    def test_result_encoding_enforces_contract_and_byte_budget(self):
        result = {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}
        self.assertEqual(json.loads(worker.encode_result(result)), result)
        for invalid in (None, {**result, "status": "success"},
                        {**result, "heuristics": [{"fact_ids": [], "interpretation": "x" * 513}]},
                        {**result, "heuristics": [{"fact_ids": [], "interpretation": "😀" * 512}] * 32}):
            self.assert_code(worker.EXIT_RESULT, worker.encode_result, invalid)

    def test_seccomp_allowlist_excludes_all_secondary_capabilities(self):
        banned = {"open", "openat", "openat2", "creat", "socket", "connect", "socketpair",
                  "fork", "vfork", "clone", "clone3", "execve", "execveat", "kill", "tgkill",
                  "ptrace", "process_vm_readv", "process_vm_writev", "prlimit64", "setrlimit",
                  "mount", "unshare", "setns", "bpf", "io_uring_setup", "seccomp", "prctl"}
        self.assertFalse(banned.intersection(worker.ALLOWED_SYSCALLS))
        self.assertTrue({"read", "write", "exit", "exit_group"}.issubset(worker.ALLOWED_SYSCALLS))
        self.assertFalse(set(worker.SAFE_BUILTINS).intersection(worker.FORBIDDEN_NAMES))
        # This tests configuration, not actual kernel enforcement; Docker probes
        # must prove enforcement before any generated function is accepted.

    def test_main_installs_filter_before_only_source_execution_site(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [node for node in ast.walk(main) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        executions = [node for node in calls if node.func.id == "exec"]
        self.assertEqual(len(executions), 1)
        self.assertTrue(any(node.func.id == "verify_environment" and node.lineno < executions[0].lineno for node in calls))
        self.assertTrue(any(node.func.id == "install_seccomp" and node.lineno < executions[0].lineno for node in calls))
        self.assertNotIn("chess.engine", WORKER_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
