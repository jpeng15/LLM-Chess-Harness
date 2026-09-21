"""Opt-in Docker Desktop VM backend. Never executes validator source on the host.

The pinned launcher measures its isolated child; the host owns deadlines,
container lifecycle, runtime checks and independent verification of findings.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from .validator_contract import CONTRACT_VERSION, RULES_API_VERSION, ValidatorContractError, validate_request
from .validator_rules import RulesBoard
from .validator_verify import verify_result_bytes


POLICY_VERSION = "validator-sandbox-v1"
MAX_SOURCE_BYTES = 64 * 1024
MEMORY_BYTES = 256 * 1024 * 1024
WALL_SECONDS = 2.0
CONTROL_SECONDS = 10.0
CLEANUP_SECONDS = 5.0
LAUNCHER_BYTES = 200 * 1024
LABEL = "org.llm-chess.validator-runtime"
SOURCE_LABEL = "org.llm-chess.validator-source-sha256"
ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILES = (
    "sandbox/validator/Dockerfile", "sandbox/validator/Dockerfile.dockerignore",
    "sandbox/validator/requirements.txt", "sandbox/validator/worker.py",
    "sandbox/validator/launcher.c", "src/chess_harness/validator_contract.py",
    "src/chess_harness/validator_rules.py",
)


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool = False
    image: str = ""
    docker_context: str = "desktop-linux"


class SandboxFailure(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


@dataclass
class CommandResult:
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    reason: str | None = None


def policy_limits():
    """The complete fixed resource policy recorded in frozen artifacts."""
    return {"cpu_seconds": 1, "cpu_quota": 1, "memory_bytes": MEMORY_BYTES,
            "swap_bytes": 0, "wall_seconds": WALL_SECONDS, "source_bytes": MAX_SOURCE_BYTES,
            "input_bytes": 128 * 1024, "stdout_bytes": 64 * 1024, "stderr_bytes": 16 * 1024,
            "pids": 2, "scratch_bytes": 1024 * 1024, "control_seconds": CONTROL_SECONDS,
            "cleanup_seconds_per_attempt": CLEANUP_SECONDS, "cleanup_attempts": 2}


def runtime_source_hash():
    """Hash the exact trusted runtime inputs, normalizing checkout line endings."""
    digest = hashlib.sha256()
    for name in RUNTIME_FILES:
        contents = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + contents + b"\0")
    return digest.hexdigest()


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate runtime JSON key")
            value[key] = item
        return value
    def nonfinite(value):
        raise ValueError("Nonfinite runtime JSON")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def _bounded_process(args, *, data=None, seconds=CONTROL_SECONDS,
                     stdout_limit=LAUNCHER_BYTES, stderr_limit=16 * 1024, cancel_event=None):
    """Drain both pipes with caps while a native Docker CLI runs, without a shell."""
    deadline = time.monotonic() + max(0, seconds)
    env = dict(os.environ)
    # Explicit context selection must not be redirected by inherited overrides.
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_API_VERSION", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    try:
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        raise SandboxFailure("runtime_unavailable", "Cannot start Docker CLI") from exc
    outputs = [bytearray(), bytearray()]
    overflow = threading.Event()
    overflow_reason = []

    def drain(pipe, index, limit):
        try:
            while chunk := pipe.read(4096):
                remaining = limit - len(outputs[index])
                outputs[index].extend(chunk[:max(0, remaining)])
                if len(chunk) > remaining:
                    if not overflow_reason:
                        overflow_reason.append("output_limit" if index == 0 else "stderr_limit")
                    overflow.set()
        except (OSError, ValueError):
            pass

    def write_input():
        try:
            if data:
                process.stdin.write(data)
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    threads = [threading.Thread(target=drain, args=(process.stdout, 0, stdout_limit), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, 1, stderr_limit), daemon=True),
               threading.Thread(target=write_input, daemon=True)]
    for thread in threads:
        thread.start()
    reason = None
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
                break
            if overflow.is_set():
                reason = overflow_reason[0]
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            try:
                process.wait(timeout=min(0.02, max(0.001, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
    if overflow.is_set():
        reason = overflow_reason[0]
    return CommandResult(process.returncode, bytes(outputs[0]), bytes(outputs[1]), reason)


class DockerSandbox:
    def __init__(self, config=SandboxConfig()):
        self.config = config
        self._docker_path = None
        self._unclean_container = None
        self._prepared = None
        self._control = threading.local()
        self._execution_lock = threading.Lock()

    def _enabled(self):
        if self.config.enabled is not True:
            raise SandboxFailure("disabled", "Authored validators require --enable-authored-validators")
        if self._unclean_container:
            raise SandboxFailure("cleanup_required", f"Confirm removal of {self._unclean_container} before creating a new backend")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.config.image) is None:
            raise SandboxFailure("invalid_image", "Use an immutable local image ID: sha256:<64 lowercase hex digits>")
        if self.config.docker_context != "desktop-linux":
            raise SandboxFailure("unsupported_runtime", "Only the verified local Docker Desktop Linux VM context is supported")

    def _call(self, args, *, ignore_cancel=False, **kwargs):
        self._enabled()
        if not ignore_cancel:
            cancel = getattr(self._control, "cancel_event", None)
            deadline = getattr(self._control, "deadline", None)
            if cancel is not None and cancel.is_set():
                return CommandResult(None, reason="cancelled")
            if deadline is not None:
                kwargs["seconds"] = min(kwargs.get("seconds", CONTROL_SECONDS), deadline - time.monotonic())
                if kwargs["seconds"] <= 0:
                    return CommandResult(None, reason="timeout")
            kwargs["cancel_event"] = cancel
        if self._docker_path is None:
            self._docker_path = shutil.which("docker")
        if not self._docker_path:
            raise SandboxFailure("runtime_unavailable", "Docker CLI is unavailable; no host execution fallback")
        return _bounded_process([self._docker_path, "--context", self.config.docker_context, *args], **kwargs)

    def _read_json(self, args, seconds=CONTROL_SECONDS):
        response = self._call(args, seconds=seconds)
        if response.reason or response.exit_code != 0:
            raise SandboxFailure(response.reason or "runtime_unavailable", "Docker inspection failed or exceeded its control budget")
        try:
            return _json(response.stdout)
        except (ValueError, RecursionError):
            raise SandboxFailure("runtime_protocol_error", "Docker returned invalid inspection JSON") from None

    def _runtime(self):
        contexts = self._read_json(["context", "inspect", self.config.docker_context])
        try:
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
            local = (endpoint == "npipe:////./pipe/dockerDesktopLinuxEngine" or
                     endpoint.startswith("unix://") and
                     (endpoint.endswith("/.docker/desktop/docker.sock") or endpoint.endswith("/.docker/run/docker.sock")))
            if len(contexts) != 1 or not local:
                raise ValueError
            info = self._read_json(["info", "--format", "{{json .}}"])
            if (info["OSType"] != "linux" or "docker desktop" not in info["OperatingSystem"].lower()
                    or str(info["CgroupVersion"]) != "2"
                    or not all(info.get(key) is True for key in ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota"))
                    or not any("seccomp" in item for item in info.get("SecurityOptions", []))):
                raise ValueError
            image = self._read_json(["image", "inspect", self.config.image])[0]
            labels = image["Config"]["Labels"]
            if (image["Id"] != self.config.image or image["Os"] != "linux"
                    or labels.get(LABEL) != "sandbox-v1"
                    or labels.get("org.llm-chess.contract") != CONTRACT_VERSION
                    or labels.get("org.llm-chess.rules-api") != RULES_API_VERSION
                    or labels.get(SOURCE_LABEL) != runtime_source_hash()
                    or image["Config"].get("Entrypoint") != ["/opt/validator/launcher"]
                    or image["Config"].get("Cmd") or image["Config"].get("Volumes")):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            raise SandboxFailure("unsupported_runtime", "VM, kernel limits, or pinned runtime image did not match policy") from None
        pending = self._call(["container", "ls", "--all", "--filter", "label=" + LABEL + "=sandbox-v1", "--format", "{{.Names}}"])
        if pending.reason or pending.exit_code != 0:
            raise SandboxFailure("runtime_unavailable", "Cannot inspect prior validator containers")
        if pending.stdout.strip():
            raise SandboxFailure("cleanup_required", "Prior validator containers remain; confirm cleanup before starting another run")
        return {"context": self.config.docker_context, "image": self.config.image,
                "source_sha256": labels[SOURCE_LABEL], "daemon_id": info.get("ID"),
                "server_version": info.get("ServerVersion"), "kernel": info.get("KernelVersion")}

    def _create_args(self, name, command):
        return ["create", "--pull=never", "--name", name, "--label", LABEL + "=sandbox-v1",
                "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "2", "--memory", str(MEMORY_BYTES), "--memory-swap", str(MEMORY_BYTES),
                "--cpus", "1", "--ulimit", "cpu=1:1", "--ulimit", "nofile=64:64",
                "--ulimit", "fsize=1048576:1048576", "--ulimit", "core=0:0",
                "--ipc", "none", "--cgroupns", "private",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1048576,mode=1777",
                "--workdir", "/tmp", "--log-driver", "none", "--restart", "no",
                "--no-healthcheck", "--stop-timeout", "0", "--interactive", self.config.image, *command]

    def _inspect_policy(self, name, seconds):
        row = self._read_json(["inspect", name], seconds)[0]
        try:
            host, config = row["HostConfig"], row["Config"]
            limits = {item["Name"]: (item["Soft"], item["Hard"]) for item in host["Ulimits"]}
            if (row["Image"] != self.config.image or config["User"] != "65534:65534"
                    or config["Labels"].get(LABEL) != "sandbox-v1"
                    or not host["ReadonlyRootfs"] or host["Privileged"]
                    or host["NetworkMode"] != "none" or host["IpcMode"] != "none"
                    or host["PidMode"] or host.get("UTSMode") or host["CgroupnsMode"] != "private"
                    or host["Memory"] != MEMORY_BYTES or host["MemorySwap"] != MEMORY_BYTES
                    or host["NanoCpus"] != 1_000_000_000 or host["PidsLimit"] != 2
                    or host["CapDrop"] != ["ALL"] or host.get("CapAdd")
                    or host["SecurityOpt"] != ["no-new-privileges:true"]
                    or host.get("Binds") or host.get("Devices") or host.get("DeviceRequests")
                    or host.get("PortBindings") or host.get("VolumesFrom")
                    or host["LogConfig"]["Type"] != "none"
                    or host["RestartPolicy"]["Name"] != "no"
                    or limits.get("cpu") != (1, 1) or limits.get("nofile") != (64, 64)
                    or limits.get("fsize") != (1048576, 1048576) or limits.get("core") != (0, 0)
                    or host.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,nodev,size=1048576,mode=1777"}
                    or any(m.get("Type") != "tmpfs" or m.get("Destination") != "/tmp" for m in row.get("Mounts", []))):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise SandboxFailure("isolation_failed", "Created container does not match the isolation policy") from None

    def _cleanup(self, name):
        try:
            removed = self._call(["rm", "--force", name], seconds=CLEANUP_SECONDS, ignore_cancel=True)
            if removed.exit_code == 0 and not removed.reason:
                return True
            # A failed create may never have allocated a container. Prove absence.
            absent = self._call(["container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
                                seconds=CLEANUP_SECONDS, ignore_cancel=True)
            return absent.exit_code == 0 and not absent.reason and not absent.stdout.strip()
        except (SandboxFailure, OSError, subprocess.SubprocessError):
            return False

    def _execute(self, payload=None, command=()):
        self._enabled()
        name = "chess-validator-" + uuid.uuid4().hex
        started = time.monotonic()
        report = {"status": "error", "reason": "runtime_error", "cleanup_confirmed": False,
                  "container": name, "image": self.config.image, "policy_version": POLICY_VERSION,
                  "cpu_seconds": None, "peak_memory_bytes": None, "worker_wall_seconds": None}
        def remaining():
            seconds = WALL_SECONDS - (time.monotonic() - started)
            deadline = getattr(self._control, "deadline", None)
            cancel = getattr(self._control, "cancel_event", None)
            if cancel is not None and cancel.is_set():
                raise SandboxFailure("cancelled", "Invocation cancelled; cleanup is still required")
            if deadline is not None:
                seconds = min(seconds, deadline - time.monotonic())
            if seconds <= 0:
                raise SandboxFailure("timeout", "Whole invocation deadline exceeded")
            return seconds
        try:
            created = self._call(self._create_args(name, command), seconds=remaining())
            if created.reason or created.exit_code != 0:
                raise SandboxFailure(created.reason or "runtime_error", "Container creation failed")
            self._inspect_policy(name, remaining())
            response = self._call(["start", "--attach", "--interactive", name], data=payload, seconds=remaining())
            # A dead Docker client is not proof the workload is stopped. Cleanup
            # below always addresses the daemon-owned container by its unique name.
            if response.reason:
                raise SandboxFailure(response.reason, "Docker attachment exceeded its budget")
            remaining()
            state = self._read_json(["inspect", name], seconds=remaining())[0]["State"]
            if state.get("Running"):
                raise SandboxFailure("runtime_error", "Worker has not exited")
            if state.get("OOMKilled") is True:
                raise SandboxFailure("memory_limit", "Runtime confirmed an out-of-memory termination")
            if response.exit_code != 0 or state.get("ExitCode") != 0:
                raise SandboxFailure("process_terminated", "Launcher terminated without a successful report")
            try:
                envelope = _json(response.stdout)
                if envelope["launcher_version"] != "validator-launcher-v1":
                    raise ValueError
                output = bytes.fromhex(envelope["stdout_hex"])
                diagnostics = bytes.fromhex(envelope["stderr_hex"])
                if len(output) > 65536 or len(diagnostics) > 16384:
                    raise ValueError
                for key, retained in (("stdout_bytes", len(output)), ("stderr_bytes", len(diagnostics))):
                    if type(envelope[key]) is not int or envelope[key] < retained:
                        raise ValueError
                code, signal = envelope["exit_code"], envelope["signal"]
                if (code is not None and (type(code) is not int or not 0 <= code <= 255)
                        or signal is not None and (type(signal) is not int or not 1 <= signal <= 64)
                        or (code is None) == (signal is None)):
                    raise ValueError
                for key in ("cpu_seconds", "peak_memory_bytes", "worker_wall_seconds"):
                    number = envelope[key]
                    if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                        raise ValueError
                report.update({key: envelope[key] for key in ("cpu_seconds", "peak_memory_bytes", "worker_wall_seconds")})
                report.update(stdout_bytes=envelope["stdout_bytes"], stderr_bytes=envelope["stderr_bytes"],
                              stdout_captured_bytes=len(output), stderr_captured_bytes=len(diagnostics),
                              stdout_hex=envelope["stdout_hex"], stderr_hex=envelope["stderr_hex"],
                              stderr=diagnostics.decode("utf-8", errors="replace"),
                              worker_exit_code=envelope["exit_code"], worker_signal=envelope["signal"])
                reason = envelope["reason"]
                if reason not in {"completed", "timeout", "output_limit", "stderr_limit", "process_terminated", "worker_error"}:
                    raise ValueError
                if reason == "completed" and (code != 0 or signal is not None):
                    raise ValueError
            except (KeyError, TypeError, ValueError, RecursionError):
                raise SandboxFailure("runtime_protocol_error", "Invalid trusted launcher report") from None
            if reason != "completed" or envelope["exit_code"] != 0 or envelope["signal"] is not None:
                codes = {20: "syntax_error", 21: "source_policy_error", 22: "code_error",
                         23: "invalid_result", 24: "isolation_failed", 25: "invalid_input"}
                raise SandboxFailure(codes.get(envelope["exit_code"], reason) if reason == "worker_error" else reason,
                                     "Worker execution failed; no findings are accepted")
            remaining()
            report.update(status="ok", reason="completed", stdout=output)
        except SandboxFailure as exc:
            report.update(reason=exc.reason, message=str(exc))
        except KeyboardInterrupt:
            report.update(reason="cancelled", message="Invocation cancelled")
        except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError):
            report.update(reason="runtime_protocol_error", message="Runtime did not provide valid lifecycle evidence")
        finally:
            report["execution_wall_seconds"] = time.monotonic() - started
            cleanup_start = time.monotonic()
            report["cleanup_confirmed"] = self._cleanup(name)
            report["cleanup_seconds"] = time.monotonic() - cleanup_start
            if not report["cleanup_confirmed"]:
                self._unclean_container = name
                report.update(status="error", execution_reason=report["reason"], reason="cleanup_failed",
                              message="Could not confirm container removal; do not start another validator")
                report.pop("stdout", None)
        return report

    def preflight(self):
        """Measure isolation and adversarial behavior before permitting user source."""
        checks = []
        try:
            self._enabled()
            runtime = self._runtime()
            probe = self._execute(command=("--probe",))
            checks.append(self._public_probe("kernel_policy", probe))
            if probe["status"] != "ok":
                raise SandboxFailure("preflight_failed", "Kernel isolation probe failed")
            evidence = _json(probe["stdout"])
            if (evidence.get("protocol_version") != "authored-validator-worker-v1"
                    or evidence.get("marker_valid") is not True
                    or evidence.get("seccomp", {}).get("loaded") is not True
                    or set(evidence.get("seccomp", {}).get("denied", {})) != {"open", "network", "fork", "exec", "prlimit", "kill"}
                    or not all(value is True for value in evidence["seccomp"]["denied"].values())):
                raise SandboxFailure("preflight_failed", "Kernel did not deny required operations")
            checks[-1]["evidence"] = evidence
            # Controlled probes have no game inputs, fixtures, model or engine.
            probes = [("stderr_flood", {"stderr_limit"}), ("forged_crash", {"code_error"}),
                      ("forged_hang", {"timeout", "process_terminated"}),
                      ("recursion", {"code_error"}), ("scratch", {"completed"}),
                      ("state_writer", {"completed"}), ("state_reader", {"completed"})]
            for case, expected in probes:
                execution = self._execute(command=("--probe-case", case))
                checks.append(self._public_probe(case, execution))
                if execution["reason"] not in expected or not execution["cleanup_confirmed"]:
                    raise SandboxFailure("preflight_failed", f"Isolation acceptance failed: {case}")
            request = _probe_request()
            for case, source, expected in (
                ("valid_function", 'def validate(position, history, candidate):\n'
                 ' board = RulesBoard.from_input(position, history)\n preview = board.copy()\n preview.push(candidate)\n'
                 ' if board.piece_at("e2") != "P" or preview.piece_at("e4") != "P" or "c7c5" not in preview.legal_moves():\n'
                 '  raise RuntimeError("Rules API self-check failed")\n'
                 ' return {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}', {"completed"}),
                ("cpu_loop", "def validate(position, history, candidate):\n while True:\n  pass", {"timeout", "process_terminated"}),
                ("memory_flood", "def validate(position, history, candidate):\n values = []\n while True:\n  values.append('x' * 1048576)", {"memory_limit", "code_error", "process_terminated"}),
                ("stdout_flood", "def validate(position, history, candidate):\n while True:\n  print('x' * 4096)", {"output_limit"}),
            ):
                execution = self._execute(_payload(source, request))
                checks.append(self._public_probe(case, execution))
                if execution["reason"] not in expected or not execution["cleanup_confirmed"]:
                    raise SandboxFailure("preflight_failed", f"Isolation acceptance failed: {case}")
                if case == "valid_function":
                    verify_result_bytes(request, execution["stdout"])
                if case == "memory_flood" and execution["reason"] != "memory_limit":
                    if (execution.get("peak_memory_bytes") or 0) < MEMORY_BYTES * 0.75:
                        raise SandboxFailure("preflight_failed", "Memory probe did not demonstrate memory pressure")
            return {"status": "ok", "reason": "ready", "policy_version": POLICY_VERSION,
                    "runtime": runtime, "checks": checks}
        except (SandboxFailure, ValidatorContractError) as exc:
            return {"status": "error", "reason": exc.reason, "message": str(exc), "checks": checks,
                    "policy_version": POLICY_VERSION}
        except (ValueError, KeyError, TypeError, RecursionError):
            return {"status": "error", "reason": "preflight_failed", "message": "Invalid probe evidence",
                    "checks": checks, "policy_version": POLICY_VERSION}

    @staticmethod
    def _public_probe(case, execution):
        return {"case": case, **{key: value for key, value in execution.items() if key != "stdout"}}

    def prepare(self):
        """Run acceptance once for this in-memory session; never trust a saved receipt."""
        self._prepared = None
        report = self.preflight()
        if report["status"] == "ok":
            self._prepared = deepcopy(report)
        return report

    def run_prepared(self, source, request, *, deadline=None, cancel_event=None):
        """Execute with fresh runtime/config checks and cooperative cancellation.

        Game initialization performs expensive acceptance. Every call still checks
        daemon/image identity and effective container isolation. Infrastructure
        failures poison the session. Development may inspect ordinary code errors;
        the game player still stops on every error without retrying.
        """
        started = time.monotonic()
        locked = self._execution_lock.acquire(blocking=False)
        if not locked:
            return {"status": "error", "reason": "sandbox_busy", "message": "Concurrent invocations are unsupported"}
        self._control.deadline, self._control.cancel_event = deadline, cancel_event
        report = {}
        try:
            self._enabled()
            if self._prepared is None:
                raise SandboxFailure("preflight_required", "Prepare this sandbox before executing source")
            if type(source) is not bytes or not source or len(source) > MAX_SOURCE_BYTES:
                raise SandboxFailure("source_limit", "Source must be nonempty UTF-8 bytes within 64 KiB")
            try:
                source_text = source.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise SandboxFailure("invalid_source", "Source is not UTF-8") from None
            request = validate_request(request)
            RulesBoard.from_request(request)
            report.update(source_sha256=hashlib.sha256(source).hexdigest(),
                          input_sha256=hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            if cancel_event is not None and cancel_event.is_set():
                raise SandboxFailure("cancelled", "Invocation cancelled before startup")
            if deadline is not None and time.monotonic() >= deadline:
                raise SandboxFailure("timeout", "Turn deadline expired before startup")
            check_start = time.monotonic()
            if self._runtime() != self._prepared["runtime"]:
                raise SandboxFailure("environment_mismatch", "Prepared sandbox runtime changed")
            report["runtime_check_seconds"] = time.monotonic() - check_start
            report.update(self._execute(_payload(source_text, request)))
            raw = report.pop("stdout", None)
            if report["status"] == "ok":
                report["findings"] = verify_result_bytes(request, raw)
                if cancel_event is not None and cancel_event.is_set():
                    raise SandboxFailure("cancelled", "Invocation cancelled before findings were accepted")
                if deadline is not None and time.monotonic() >= deadline:
                    raise SandboxFailure("timeout", "Turn deadline expired before findings were accepted")
        except (SandboxFailure, ValidatorContractError) as exc:
            report.update(status="error", reason=exc.reason, message=str(exc))
            if hasattr(exc, "path"):
                report["path"] = exc.path
            report.pop("findings", None)
        except KeyboardInterrupt:
            report.update(status="error", reason="cancelled", message="Invocation cancelled")
        finally:
            if report.get("reason") in {"runtime_unavailable", "unsupported_runtime", "isolation_failed",
                                        "environment_mismatch", "runtime_protocol_error", "cleanup_failed",
                                        "cleanup_required", "cancelled"}:
                self._prepared = None
            report["total_wall_seconds"] = time.monotonic() - started
            self._control.deadline = self._control.cancel_event = None
            self._execution_lock.release()
        return report

    def run(self, source, request):
        """Requires explicit opt-in and a fresh successful preflight; never falls back."""
        try:
            self._enabled()
            if type(source) is not bytes or len(source) > MAX_SOURCE_BYTES:
                raise SandboxFailure("source_limit", "Source must be UTF-8 bytes of at most 64 KiB")
            try:
                source_text = source.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise SandboxFailure("invalid_source", "Source is not UTF-8") from None
            request = validate_request(request)
            RulesBoard.from_request(request)
        except (SandboxFailure, ValidatorContractError) as exc:
            return {"status": "error", "reason": exc.reason, "message": str(exc)}
        preparation_start = time.monotonic()
        identity = {"source_sha256": hashlib.sha256(source).hexdigest(),
                    "input_sha256": hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        readiness = self.preflight()
        preparation_seconds = time.monotonic() - preparation_start
        if readiness["status"] != "ok":
            return {**readiness, **identity, "preflight_seconds": preparation_seconds}
        report = self._execute(_payload(source_text, request))
        raw = report.pop("stdout", None)
        report.update(**identity, preflight=readiness, preflight_seconds=preparation_seconds)
        if report["status"] == "ok":
            try:
                report["findings"] = verify_result_bytes(request, raw)
            except ValidatorContractError as exc:
                report.update(status="error", reason=exc.reason, path=exc.path, message=str(exc))
        return report


def _payload(source, request):
    return json.dumps({"source": source, "request": request}, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _probe_request():
    import chess
    return {"contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
            "position": {"fen": chess.STARTING_FEN},
            "history": {"initial_fen": chess.STARTING_FEN, "moves": []}, "candidate": "e2e4"}
