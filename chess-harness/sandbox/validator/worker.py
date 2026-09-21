"""Trusted bootstrap for the disposable Linux validator image.

Importing this module only defines helpers. It never executes validator source,
installs seccomp, or starts a worker. The entry point requires the immutable image
marker and enforced container limits before accepting any source. AST restrictions
describe the rules-only API; kernel/container/VM isolation is the security boundary.
"""
import ast
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys

try:
    import resource
except ImportError:  # Pure policy helpers remain testable on Windows.
    resource = None

# The launcher supplies a clean environment and disables cwd/user-site imports.
# Only the immutable image has this API path.
if Path(__file__).as_posix() == "/opt/validator/worker.py":
    sys.path.insert(0, "/opt/validator/api")

from chess_harness.validator_contract import (  # noqa: E402
    CONTRACT_VERSION, MAX_RESULT_BYTES, ValidatorContractError,
    validate_request, validate_result,
)
from chess_harness.validator_rules import RulesBoard  # noqa: E402


WORKER_VERSION = "authored-validator-worker-v1"
MARKER_PATH = "/opt/validator/.worker-image-v1"
MARKER_BYTES = b"llm-chess-validator-worker-v1\n"
MAX_SOURCE_BYTES = 64 * 1024
MAX_WIRE_BYTES = 400 * 1024
MAX_AST_NODES = 10000
MAX_WIRE_DEPTH = 20
MAX_STDERR_BYTES = 16 * 1024
MAX_MEMORY_BYTES = 256 * 1024 * 1024
EXIT_SYNTAX = 20
EXIT_POLICY = 21
EXIT_RUNTIME = 22
EXIT_RESULT = 23
EXIT_BOOTSTRAP = 24
EXIT_INPUT = 25

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float,
    "frozenset": frozenset, "int": int, "isinstance": isinstance, "iter": iter,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "next": next, "print": print, "range": range, "reversed": reversed,
    "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "RuntimeError": RuntimeError, "ArithmeticError": ArithmeticError,
    "IndexError": IndexError, "KeyError": KeyError, "StopIteration": StopIteration,
}
FORBIDDEN_NAMES = frozenset({
    "exec", "eval", "compile", "open", "input", "globals", "locals", "vars",
    "dir", "getattr", "setattr", "delattr", "hasattr", "type", "object", "super",
    "help", "breakpoint", "exit", "quit", "memoryview", "classmethod",
    "staticmethod", "property", "builtins", "importlib", "os", "sys", "ctypes",
    "socket", "subprocess", "resource",
})
FORBIDDEN_NODES = (
    ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef, ast.Await,
    ast.AsyncFor, ast.AsyncWith, ast.Global, ast.Nonlocal, ast.With, ast.AnnAssign,
    ast.Match,
)

# This is an allowlist, not an enumeration of known bad system calls. No open,
# networking, exec/spawn, ptrace/process_vm, kill, prlimit/setrlimit, seccomp,
# mount, io_uring, bpf, or namespace operation is allowed after bootstrap.
ALLOWED_SYSCALLS = (
    "read", "write", "close", "fstat", "lseek", "brk", "mmap", "mprotect",
    "munmap", "mremap", "madvise", "rt_sigaction", "rt_sigprocmask",
    "rt_sigreturn", "sigaltstack", "futex", "clock_gettime", "gettimeofday",
    "getrandom", "getpid", "gettid", "sched_yield", "restart_syscall",
    "exit", "exit_group",
)


class WorkerError(ValueError):
    def __init__(self, exit_code, message):
        super().__init__(message)
        self.exit_code = exit_code


def _fail(exit_code, message):
    raise WorkerError(exit_code, message)


def _source_bytes(source):
    if type(source) is not str:
        _fail(EXIT_INPUT, "source must be a string")
    try:
        encoded = source.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail(EXIT_INPUT, "source is not valid UTF-8")
    if not encoded or len(encoded) > MAX_SOURCE_BYTES:
        _fail(EXIT_INPUT, "source size is outside the contract limit")
    return encoded


def decode_payload(raw):
    """Pure bounded input decoder; never evaluates or compiles source."""
    if type(raw) is not bytes or len(raw) > MAX_WIRE_BYTES:
        _fail(EXIT_INPUT, "worker input exceeds its byte limit or is not bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail(EXIT_INPUT, "worker input is not UTF-8")
    quoted = escaped = False
    depth = 0
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
            if depth > MAX_WIRE_DEPTH:
                _fail(EXIT_INPUT, "worker input nesting exceeds its limit")
        elif character in "]}":
            depth -= 1

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail(EXIT_INPUT, "duplicate input key")
            value[key] = item
        return value

    def reject_number(value):
        _fail(EXIT_INPUT, "non-finite input number")

    def finite_float(value):
        value = float(value)
        if not math.isfinite(value):
            reject_number(value)
        return value

    try:
        value = json.loads(text, object_pairs_hook=unique_object,
                           parse_constant=reject_number, parse_float=finite_float)
    except (ValueError, RecursionError) as error:
        if isinstance(error, WorkerError):
            raise
        _fail(EXIT_INPUT, "malformed worker JSON")
    if type(value) is not dict or set(value) != {"source", "request"}:
        _fail(EXIT_INPUT, "worker input requires only source and request")
    _source_bytes(value["source"])
    try:
        request = validate_request(value["request"])
        RulesBoard.from_request(request)
    except ValidatorContractError as error:
        _fail(EXIT_INPUT, "invalid request: " + error.reason + " at " + error.path)
    return value["source"], request


def prepare_source(source):
    """Check and compile source without executing it; this is not a sandbox."""
    _source_bytes(source)
    try:
        tree = ast.parse(source, filename="<authored-validator>", mode="exec")
    except (SyntaxError, ValueError, RecursionError):
        _fail(EXIT_SYNTAX, "validator source could not be parsed")
    nodes = 0
    for node in ast.walk(tree):
        nodes += 1
        if nodes > MAX_AST_NODES:
            _fail(EXIT_POLICY, "validator AST exceeds its node limit")
        if isinstance(node, FORBIDDEN_NODES):
            _fail(EXIT_POLICY, "unsupported source construct: " + type(node).__name__)
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id in FORBIDDEN_NAMES:
                _fail(EXIT_POLICY, "private or forbidden name")
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in {"RulesBoard", "CONTRACT_VERSION"}:
                _fail(EXIT_POLICY, "rules API bindings cannot be replaced")
        if isinstance(node, ast.arg) and (node.arg.startswith("_") or node.arg in FORBIDDEN_NAMES
                                         or node.arg in {"RulesBoard", "CONTRACT_VERSION"}):
            _fail(EXIT_POLICY, "private or reserved parameter name")
        if isinstance(node, ast.ExceptHandler) and node.name is not None and (
                node.name.startswith("_") or node.name in FORBIDDEN_NAMES
                or node.name in {"RulesBoard", "CONTRACT_VERSION"}):
            _fail(EXIT_POLICY, "private or reserved exception binding")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or not isinstance(node.ctx, ast.Load):
                _fail(EXIT_POLICY, "private access or attribute mutation is unsupported")
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            if node.args.defaults or any(value is not None for value in node.args.kw_defaults):
                _fail(EXIT_POLICY, "function default expressions are unsupported")
            if any(argument.annotation is not None for argument in (
                    *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else []))):
                _fail(EXIT_POLICY, "function annotations are unsupported")
        if isinstance(node, ast.FunctionDef):
            if (node.name.startswith("_") or node.name in FORBIDDEN_NAMES
                    or node.name in {"RulesBoard", "CONTRACT_VERSION"}
                    or node.decorator_list or node.returns is not None
                    or getattr(node, "type_params", ())):
                _fail(EXIT_POLICY, "function name, decorator or annotation is unsupported")
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef)
                or isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            _fail(EXIT_POLICY, "module may contain only function definitions and docstrings")
    validators = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "validate"]
    if len(validators) != 1:
        _fail(EXIT_POLICY, "source must define exactly one validate function")
    arguments = validators[0].args
    if (arguments.posonlyargs or arguments.kwonlyargs or arguments.vararg or arguments.kwarg
            or [argument.arg for argument in arguments.args] != ["position", "history", "candidate"]):
        _fail(EXIT_POLICY, "validate must accept exactly position, history, candidate")
    try:
        return compile(tree, "<authored-validator>", "exec", dont_inherit=True, optimize=0)
    except (SyntaxError, ValueError, RecursionError):
        _fail(EXIT_SYNTAX, "validator source could not be compiled")


def encode_result(value):
    """Validate before serializing, including all structural and output limits."""
    try:
        value = validate_result(value)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValidatorContractError, TypeError, ValueError, RecursionError):
        _fail(EXIT_RESULT, "validator returned an invalid result")
    if len(raw) > MAX_RESULT_BYTES:
        _fail(EXIT_RESULT, "validator result exceeded its byte limit")
    return raw


def _status_values():
    values = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return values


def _read_cgroup_limit(name):
    value = Path("/sys/fs/cgroup", name).read_text(encoding="ascii").strip()
    if not value.isdecimal():
        _fail(EXIT_BOOTSTRAP, "missing finite cgroup limit: " + name)
    return int(value)


def verify_environment():
    """Require the immutable worker image and observed Linux container limits."""
    if sys.platform != "linux" or resource is None or Path(__file__).as_posix() != "/opt/validator/worker.py":
        _fail(EXIT_BOOTSTRAP, "worker must run inside its isolated Linux image")
    marker = Path(MARKER_PATH)
    metadata = marker.lstat()
    if (marker.read_bytes() != MARKER_BYTES or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o444 or not stat.S_ISREG(metadata.st_mode)):
        _fail(EXIT_BOOTSTRAP, "immutable worker image marker did not match")
    root_readonly = bool(os.statvfs("/").f_flag & os.ST_RDONLY)
    status = _status_values()
    uid = os.geteuid()
    soft_cpu, hard_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    memory_max = _read_cgroup_limit("memory.max")
    swap_max = _read_cgroup_limit("memory.swap.max")
    pids_max = _read_cgroup_limit("pids.max")
    cpu_values = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
    if len(cpu_values) != 2 or not all(value.isdecimal() for value in cpu_values):
        _fail(EXIT_BOOTSTRAP, "finite CPU cgroup quota is required")
    cpu_quota, cpu_period = map(int, cpu_values)
    interfaces = [name for _, name in socket.if_nameindex()]
    if (uid == 0 or not root_readonly or int(status.get("CapEff", "1"), 16) != 0
            or status.get("NoNewPrivs") != "1" or status.get("Seccomp") != "2"
            or not 0 < soft_cpu <= hard_cpu <= 1
            or not 0 < memory_max <= MAX_MEMORY_BYTES or swap_max != 0
            or pids_max != 2 or not 0 < cpu_quota <= cpu_period
            or any(name != "lo" for name in interfaces)):
        _fail(EXIT_BOOTSTRAP, "required container limits or isolation are not active")
    return {"uid": uid, "root_readonly": root_readonly, "cpu_soft": soft_cpu,
            "cpu_hard": hard_cpu, "cgroup_memory_max": memory_max,
            "cgroup_swap_max": swap_max, "cgroup_pids_max": pids_max,
            "cpu_quota": cpu_quota, "cpu_period": cpu_period,
            "cgroup_cpu_max": [cpu_quota, cpu_period],
            "no_new_privs": 1, "outer_seccomp": 2, "effective_capabilities": 0,
            "network_interfaces": interfaces}


def install_seccomp():
    """Install native-kernel default-deny filtering before executing source."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    context = library.seccomp_init(0x00050000 | errno.EPERM)  # SCMP_ACT_ERRNO(EPERM)
    if not context:
        _fail(EXIT_BOOTSTRAP, "seccomp context creation failed")
    try:
        for name in ALLOWED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and library.seccomp_rule_add(context, 0x7FFF0000, number, 0) != 0:
                _fail(EXIT_BOOTSTRAP, "seccomp allowlist rule failed")
        if library.seccomp_load(context) != 0:
            _fail(EXIT_BOOTSTRAP, "seccomp kernel filter installation failed")
    finally:
        library.seccomp_release(context)


def probe_denials():
    """Trusted acceptance probes, run only after guard and kernel filtering."""
    operations = {
        "open": lambda: os.open("/etc/passwd", os.O_RDONLY),
        "network": lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        "fork": os.fork,
        "exec": lambda: os.execve("/blocked-validator-probe", ["blocked-validator-probe"], {}),
        "prlimit": lambda: resource.prlimit(0, resource.RLIMIT_CPU, (1, 1)),
        "kill": lambda: os.kill(os.getpid(), 0),
    }
    denied = {}
    for name, operation in operations.items():
        try:
            operation()
        except OSError as error:
            denied[name] = error.errno == errno.EPERM
        else:
            denied[name] = False
        if not denied[name]:
            _fail(EXIT_BOOTSTRAP, "kernel denial probe failed: " + name)
    return denied


def _probe_case(name):
    # These operations are trusted acceptance fixtures, never generated source.
    valid = b'{"contract_version":"authored-validator-v1","facts":[],"heuristics":[]}'
    if name == "stderr_flood":
        while True:
            os.write(2, b"x" * 4096)
    elif name == "forged_crash":
        os.write(1, valid)
        os._exit(EXIT_RUNTIME)
    elif name == "forged_hang":
        os.write(1, valid)
        while True:
            pass
    elif name == "recursion":
        def recurse():
            return recurse()
        recurse()
    elif name in {"scratch", "state_writer", "state_reader"}:
        try:
            flags = os.O_RDONLY if name == "state_reader" else os.O_WRONLY | os.O_CREAT
            os.open("/tmp/validator-write-probe", flags, 0o600)
        except OSError as error:
            if error.errno != errno.EPERM:
                _fail(EXIT_BOOTSTRAP, "scratch probe was not denied by kernel policy")
        else:
            _fail(EXIT_BOOTSTRAP, "scratch probe unexpectedly opened a file")
        os.write(1, json.dumps({"probe": name, "file_access_denied": True}).encode("utf-8"))
    else:
        _fail(EXIT_INPUT, "unknown trusted probe case")


def _diagnostic(message):
    raw = message.encode("utf-8", errors="replace")[:MAX_STDERR_BYTES - 1] + b"\n"
    try:
        os.write(2, raw)
    except OSError:
        pass


def main(argv=None):
    """Container-only entry point; never use as a host execution fallback."""
    argv = sys.argv[1:] if argv is None else argv
    execution_started = False
    try:
        runtime = verify_environment()
        if argv == ["--probe"] or len(argv) == 2 and argv[0] == "--probe-case":
            install_seccomp()
            if argv == ["--probe"]:
                output = {"protocol_version": WORKER_VERSION, "marker_valid": True,
                          "runtime": runtime, "seccomp": {"loaded": True, "denied": probe_denials()}}
                os.write(1, json.dumps(output, separators=(",", ":")).encode("utf-8"))
            else:
                execution_started = True
                _probe_case(argv[1])
            return 0
        if argv:
            _fail(EXIT_INPUT, "unsupported worker arguments")
        source, request = decode_payload(sys.stdin.buffer.read(MAX_WIRE_BYTES + 1))
        code = prepare_source(source)
        # Preload serialization/rules paths while filesystem opens are available.
        encode_result({"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []})
        install_seccomp()
        namespace = {"__builtins__": dict(SAFE_BUILTINS), "RulesBoard": RulesBoard,
                     "CONTRACT_VERSION": CONTRACT_VERSION}
        execution_started = True
        exec(code, namespace, namespace)
        result = namespace["validate"](request["position"], request["history"], request["candidate"])
        raw = encode_result(result)
        sys.stdout.flush()  # Generated print output remains visible and invalidates the JSON.
        os.write(1, raw)
        return 0
    except WorkerError as error:
        _diagnostic(str(error))
        return error.exit_code
    except BaseException as error:
        # No traceback formatting or source access after the filesystem is sealed.
        _diagnostic("worker failure: " + type(error).__name__)
        return EXIT_RUNTIME if execution_started else EXIT_BOOTSTRAP


if __name__ == "__main__":
    raise SystemExit(main())
