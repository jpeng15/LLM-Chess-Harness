from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from chess_harness.validator_sandbox import (
    CommandResult, DockerSandbox, LABEL, MEMORY_BYTES, SOURCE_LABEL, SandboxConfig,
    SandboxFailure, _probe_request, runtime_source_hash,
)


IMAGE = "sha256:" + "a" * 64
CONFIG = SandboxConfig(enabled=True, image=IMAGE)
EMPTY = {"contract_version": "authored-validator-v1", "facts": [], "heuristics": []}
SOURCE = b'def validate(position, history, candidate):\n return {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}'


def encoded(value):
    return json.dumps(value).encode()


def container_policy():
    return {"Image": IMAGE, "Config": {"User": "65534:65534", "Labels": {LABEL: "sandbox-v1"}},
            "Mounts": [], "HostConfig": {
                "ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "none", "IpcMode": "none",
                "PidMode": "", "UTSMode": "", "CgroupnsMode": "private", "Memory": MEMORY_BYTES,
                "MemorySwap": MEMORY_BYTES, "NanoCpus": 1_000_000_000, "PidsLimit": 2,
                "CapDrop": ["ALL"], "CapAdd": None, "SecurityOpt": ["no-new-privileges:true"],
                "Binds": None, "Devices": [], "DeviceRequests": None, "PortBindings": {}, "VolumesFrom": None,
                "LogConfig": {"Type": "none"}, "RestartPolicy": {"Name": "no"},
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=1048576,mode=1777"},
                "Ulimits": [{"Name": name, "Soft": value, "Hard": value} for name, value in
                            (("cpu", 1), ("nofile", 64), ("fsize", 1048576), ("core", 0))]}}


def envelope(output=EMPTY, reason="completed", exit_code=0, signal=None):
    return {"launcher_version": "validator-launcher-v1", "reason": reason, "exit_code": exit_code,
            "signal": signal, "stdout_hex": encoded(output).hex(), "stderr_hex": "",
            "stdout_bytes": len(encoded(output)), "stderr_bytes": 0,
            "cpu_seconds": 0.04, "peak_memory_bytes": 20 * 1024 * 1024, "worker_wall_seconds": 0.08}


class FakeDocker(DockerSandbox):
    """Fake native CLI responses only; never executes source or a real container."""
    def __init__(self, *, worker=None, state=None, attachment=None, cleanup=True):
        super().__init__(CONFIG)
        self.commands = []
        self.worker = envelope() if worker is None else worker
        self.state = {"Running": False, "OOMKilled": False, "ExitCode": 0} if state is None else state
        self.attachment = attachment
        self.cleanup = cleanup
        self.policy = container_policy()
        self.inspections = 0

    def _call(self, args, **kwargs):
        self.commands.append((args, kwargs))
        if args[0] == "create":
            return CommandResult(0, b"container-id")
        if args[0] == "inspect":
            self.inspections += 1
            return CommandResult(0, encoded([self.policy if self.inspections == 1 else {"State": self.state}]))
        if args[0] == "start":
            if isinstance(self.attachment, BaseException):
                raise self.attachment
            return self.attachment or CommandResult(0, encoded(self.worker))
        if args[0] == "rm":
            return CommandResult(0 if self.cleanup else 1)
        if args[:2] == ["container", "ls"]:
            return CommandResult(0, b"still-running" if not self.cleanup else b"")
        raise AssertionError(args)


class SandboxTests(unittest.TestCase):
    def test_disabled_backend_never_discovers_or_starts_a_process(self):
        backend = DockerSandbox()
        with patch("chess_harness.validator_sandbox.shutil.which") as which, \
                patch("chess_harness.validator_sandbox.subprocess.Popen") as process:
            self.assertEqual(backend.preflight()["reason"], "disabled")
            self.assertEqual(backend.run(SOURCE, _probe_request())["reason"], "disabled")
            with self.assertRaises(SandboxFailure):
                backend._call(["info"])
            which.assert_not_called()
            process.assert_not_called()

    def test_unpinned_image_other_context_and_missing_runtime_fail_closed(self):
        for config, reason in ((SandboxConfig(True, "validator:latest"), "invalid_image"),
                               (SandboxConfig(True, IMAGE, "default"), "unsupported_runtime")):
            with patch("chess_harness.validator_sandbox.shutil.which") as which:
                self.assertEqual(DockerSandbox(config).preflight()["reason"], reason)
                which.assert_not_called()
        with patch("chess_harness.validator_sandbox.shutil.which", return_value=None), \
                patch("chess_harness.validator_sandbox.subprocess.Popen") as process:
            self.assertEqual(DockerSandbox(CONFIG).preflight()["reason"], "runtime_unavailable")
            process.assert_not_called()

    def test_vm_and_image_provenance_are_checked(self):
        context = [{"Endpoints": {"docker": {"Host": "npipe:////./pipe/dockerDesktopLinuxEngine"}}}]
        info = {"OSType": "linux", "OperatingSystem": "Docker Desktop", "CgroupVersion": "2",
                "MemoryLimit": True, "SwapLimit": True, "PidsLimit": True, "CpuCfsQuota": True,
                "SecurityOptions": ["name=seccomp,profile=builtin"], "ID": "test-daemon"}
        image = [{"Id": IMAGE, "Os": "linux", "Config": {
            "Labels": {LABEL: "sandbox-v1", SOURCE_LABEL: runtime_source_hash(),
                       "org.llm-chess.contract": "authored-validator-v1", "org.llm-chess.rules-api": "rules-board-v1"},
            "Entrypoint": ["/opt/validator/launcher"], "Cmd": None, "Volumes": None}}]
        backend = DockerSandbox(CONFIG)
        with patch.object(backend, "_read_json", side_effect=[context, info, image]), \
                patch.object(backend, "_call", return_value=CommandResult(0)):
            self.assertEqual(backend._runtime()["image"], IMAGE)
        with patch.object(backend, "_read_json", side_effect=[context, info, image]), \
                patch.object(backend, "_call", return_value=CommandResult(0, b"chess-validator-leftover")), \
                self.assertRaises(SandboxFailure) as caught:
            backend._runtime()
        self.assertEqual(caught.exception.reason, "cleanup_required")
        for target, field, value in (("info", "OperatingSystem", "Ubuntu"),
                                     ("info", "MemoryLimit", False), ("info", "CgroupVersion", "1")):
            modified = deepcopy(info)
            modified[field] = value
            with self.subTest(field=field), patch.object(backend, "_read_json", side_effect=[context, modified, image]), \
                    self.assertRaises(SandboxFailure):
                backend._runtime()
        image[0]["Config"]["Labels"][SOURCE_LABEL] = "stale-build"
        with patch.object(backend, "_read_json", side_effect=[context, info, image]), self.assertRaises(SandboxFailure):
            backend._runtime()
        context[0]["Endpoints"]["docker"]["Host"] = "tcp://127.0.0.1:2375"
        with patch.object(backend, "_read_json", return_value=context), self.assertRaises(SandboxFailure):
            backend._runtime()

    def test_launch_has_no_mounts_engine_network_devices_or_host_namespaces(self):
        backend = FakeDocker()
        report = backend._execute(b"only-source-and-request")
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["cleanup_confirmed"])
        create = backend.commands[0][0]
        for switch, value in (("--network", "none"), ("--memory", str(MEMORY_BYTES)),
                              ("--pids-limit", "2"), ("--cap-drop", "ALL"), ("--log-driver", "none")):
            self.assertEqual(create[create.index(switch) + 1], value)
        for forbidden in ("--mount", "--volume", "--gpus", "--device", "--privileged", "--pid", "--env-file"):
            self.assertNotIn(forbidden, create)
        start = next(kwargs for args, kwargs in backend.commands if args[0] == "start")
        self.assertEqual(start["data"], b"only-source-and-request")
        self.assertEqual(report["cpu_seconds"], 0.04)
        self.assertEqual(report["peak_memory_bytes"], 20 * 1024 * 1024)

    def test_effective_container_policy_is_checked_before_source_is_sent(self):
        for key, value in (("ReadonlyRootfs", False), ("Memory", 0), ("PidsLimit", 0),
                           ("NetworkMode", "host"), ("Binds", ["/secret:/secret"]),
                           ("SecurityOpt", ["seccomp=unconfined"]), ("CapAdd", ["SYS_ADMIN"])):
            backend = FakeDocker()
            backend.policy["HostConfig"][key] = value
            with self.subTest(key=key):
                report = backend._execute(b"source")
                self.assertEqual(report["reason"], "isolation_failed")
                self.assertNotIn("start", [args[0] for args, _ in backend.commands])
                self.assertTrue(report["cleanup_confirmed"])

    def test_valid_looking_output_is_rejected_after_crash_hang_or_flood(self):
        for reason, code, signal in (("worker_error", 22, None), ("timeout", None, 9),
                                    ("output_limit", None, 9), ("stderr_limit", None, 9),
                                    ("process_terminated", None, 9)):
            with self.subTest(reason=reason):
                backend = FakeDocker(worker=envelope(reason=reason, exit_code=code, signal=signal))
                report = backend._execute(b"source")
                self.assertEqual(report["status"], "error")
                self.assertNotIn("stdout", report)
                self.assertEqual(bytes.fromhex(report["stdout_hex"]), encoded(EMPTY))
                self.assertTrue(report["cleanup_confirmed"])

    def test_client_timeout_and_cancellation_remove_daemon_owned_container(self):
        for response, reason in ((CommandResult(-1, reason="timeout"), "timeout"),
                                 (KeyboardInterrupt(), "cancelled")):
            backend = FakeDocker(attachment=response)
            report = backend._execute(b"source")
            self.assertEqual(report["reason"], reason)
            self.assertTrue(report["cleanup_confirmed"])
            self.assertEqual(backend.commands[-1][0][0:2], ["rm", "--force"])

    def test_unknown_kill_is_not_inferred_to_be_out_of_memory(self):
        backend = FakeDocker(state={"Running": False, "OOMKilled": False, "ExitCode": 137})
        self.assertEqual(backend._execute()["reason"], "process_terminated")
        backend = FakeDocker(state={"Running": False, "OOMKilled": True, "ExitCode": 137})
        report = backend._execute()
        self.assertEqual(report["reason"], "memory_limit")
        self.assertIsNone(report["cpu_seconds"])

    def test_cleanup_failure_overrides_success_or_failure(self):
        for response in (None, CommandResult(-1, reason="timeout")):
            backend = FakeDocker(attachment=response, cleanup=False)
            report = backend._execute()
            self.assertEqual(report["reason"], "cleanup_failed")
            self.assertEqual(report["status"], "error")
            self.assertNotIn("stdout", report)
            self.assertFalse(report["cleanup_confirmed"])
            with self.assertRaises(SandboxFailure) as caught:
                backend._execute()
            self.assertEqual(caught.exception.reason, "cleanup_required")

    def test_launcher_protocol_and_measurements_are_not_taken_from_child_output(self):
        for field, value in (("cpu_seconds", -1), ("cpu_seconds", float("inf")),
                             ("peak_memory_bytes", True), ("stdout_hex", "invalid"),
                             ("launcher_version", "fake"), ("exit_code", False), ("exit_code", 22)):
            worker = envelope()
            worker[field] = value
            with self.subTest(field=field):
                self.assertEqual(FakeDocker(worker=worker)._execute()["reason"], "runtime_protocol_error")

    def test_run_requires_preflight_and_independently_checks_factual_output(self):
        backend = DockerSandbox(CONFIG)
        with patch.object(backend, "preflight", return_value={"status": "error", "reason": "preflight_failed"}), \
                patch.object(backend, "_execute") as execute:
            report = backend.run(SOURCE, _probe_request())
            self.assertEqual(report["reason"], "preflight_failed")
            self.assertIn("source_sha256", report)
            execute.assert_not_called()
        false = {**EMPTY, "facts": [{"id": "mate", "kind": "candidate_checkmate", "line": ["e2e4"]}]}
        for findings, expected in ((EMPTY, "completed"), (false, "invalid_finding")):
            execution = {"status": "ok", "reason": "completed", "stdout": encoded(findings)}
            with patch.object(backend, "preflight", return_value={"status": "ok", "reason": "ready"}), \
                    patch.object(backend, "_execute", return_value=execution):
                report = backend.run(SOURCE, _probe_request())
                self.assertEqual(report["reason"], expected)
                self.assertNotIn("stdout", report)
                self.assertIn("preflight_seconds", report)
                self.assertEqual("findings" in report, findings == EMPTY)

    def test_each_invocation_gets_a_distinct_disposable_container(self):
        first, second = FakeDocker(), FakeDocker()
        self.assertNotEqual(first._execute()["container"], second._execute()["container"])

    def test_full_preflight_compiles_stress_sources_and_records_each_acceptance_case(self):
        # Load trusted pure helpers only; no worker entry point or source runs.
        path = Path(__file__).resolve().parents[1] / "sandbox/validator/worker.py"
        spec = importlib.util.spec_from_file_location("preflight_policy_check", path)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        backend = DockerSandbox(CONFIG)
        calls = []
        def execute(payload=None, command=()):
            calls.append(command)
            report = {"status": "ok", "reason": "completed", "cleanup_confirmed": True}
            if command == ("--probe",):
                report["stdout"] = encoded({"protocol_version": "authored-validator-worker-v1", "marker_valid": True,
                    "seccomp": {"loaded": True, "denied": dict.fromkeys(("open", "network", "fork", "exec", "prlimit", "kill"), True)}})
            elif command:
                reason = {"stderr_flood": "stderr_limit", "forged_crash": "code_error", "forged_hang": "timeout",
                          "recursion": "code_error"}.get(command[1], "completed")
                report.update(reason=reason, status="ok" if reason == "completed" else "error")
            else:
                source, request = worker.decode_payload(payload)
                worker.prepare_source(source)  # compile only; catches policy-ineligible stress probes
                if "while True" not in source:
                    report["stdout"] = encoded(EMPTY)
                else:
                    reason = "output_limit" if "print(" in source else "memory_limit" if "append(" in source else "process_terminated"
                    report.update(status="error", reason=reason)
            return report
        with patch.object(backend, "_runtime", return_value={"image": IMAGE}), \
                patch.object(backend, "_execute", side_effect=execute):
            report = backend.preflight()
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual([item["case"] for item in report["checks"]], [
            "kernel_policy", "stderr_flood", "forged_crash", "forged_hang", "recursion", "scratch",
            "state_writer", "state_reader", "valid_function", "cpu_loop", "memory_flood", "stdout_flood"])
        json.dumps(report, allow_nan=False)  # Public evidence must not leak internal bytes.
        with patch.object(backend, "_runtime", return_value={}), patch.object(backend, "_execute", return_value={
                "status": "error", "reason": "isolation_failed", "cleanup_confirmed": True}) as execute:
            self.assertEqual(backend.preflight()["reason"], "preflight_failed")
            execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
