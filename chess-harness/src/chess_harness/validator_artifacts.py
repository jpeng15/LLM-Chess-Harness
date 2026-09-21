"""Freeze and verify development-only validators without host code execution."""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .validator_contract import CONTRACT_VERSION, RULES_API_VERSION, ValidatorContractError
from .validator_sandbox import (
    DockerSandbox, MAX_SOURCE_BYTES, POLICY_VERSION, SandboxConfig, SandboxFailure,
    policy_limits, runtime_source_hash,
)


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ACCEPTANCE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_PROVENANCE_BYTES = 48 * 1024 * 1024
COMPARISON = "exact-canonical-findings-v1"
_HEX = re.compile(r"[0-9a-f]{64}")
_MANIFEST_FIELDS = {
    "schema_version", "kind", "state", "artifact_id", "contract_version", "rules_api_version",
    "policy_version", "source_sha256", "runtime_source_sha256", "image", "docker_context", "limits",
    "development_suite_sha256", "provenance", "setup_costs",
}


class ArtifactError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class FrozenValidator:
    artifact_id: str
    source: bytes
    _manifest: dict = field(repr=False)
    _development: dict | None = field(default=None, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_manifest", deepcopy(self._manifest))
        object.__setattr__(self, "_development", deepcopy(self._development))

    @property
    def manifest(self):
        """Return detached metadata; mutations cannot change the loaded identity."""
        return deepcopy(self._manifest)

    @property
    def development(self):
        """Return the already-verified development record, detached for viewers."""
        return deepcopy(self._development)


def _fail(reason, message):
    raise ArtifactError(reason, message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("invalid_artifact", "Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        _fail("invalid_artifact", "Nonfinite JSON number")

    def finite(value):
        number = float(value)
        return number if math.isfinite(number) else nonfinite(value)

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=nonfinite, parse_float=finite)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        _fail("invalid_artifact", "Invalid bounded UTF-8 JSON document")


def _plain_path(path, *, directory=False):
    """Reject links, junctions and nonregular inputs, including linked ancestors."""
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            _fail("unsafe_path", "Artifact paths cannot contain symbolic links or junctions")
    if directory and not path.is_dir():
        _fail("invalid_artifact", "Expected a development or artifact directory")
    return path


def _read(root, relative, limit):
    # Callers supply fixed names only; manifest paths never reach this function.
    path = _plain_path(root / relative)
    try:
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            _fail("unsafe_path", "Artifact inputs must be regular files")
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                _fail("unsafe_path", "Artifact inputs must be regular files")
            value = handle.read(limit + 1)
    except OSError as exc:
        _fail("artifact_io_error", f"Cannot read required file {relative}: {exc}")
    if len(value) > limit:
        _fail("artifact_limit", f"File {relative} exceeds its byte limit")
    return value


def _tree(root, names):
    """Reject unrecorded files without traversing symlinks or arbitrary paths."""
    expected = {"": set()}
    for name in names:
        parts = name.split("/")
        for index, part in enumerate(parts):
            parent = "/".join(parts[:index])
            expected.setdefault(parent, set()).add(part)
            if index < len(parts) - 1:
                expected.setdefault("/".join(parts[:index + 1]), set())
    for relative, children in expected.items():
        directory = _plain_path(root / relative, directory=True)
        seen = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in children:
                    _fail("invalid_artifact", "Directory contains unrecorded files or directories")
                _plain_path(Path(entry.path))
                seen.add(entry.name)
        if seen != children:
            _fail("invalid_artifact", "Directory is missing required provenance files")


def _runtime(report, image, context, source_hash):
    if type(report) is dict and report.get("status") == "error":
        reason = report.get("reason")
        _fail(reason if type(reason) is str else "invalid_preflight",
              "Sandbox preparation failed; no artifact is eligible")
    runtime = report.get("runtime", {}) if type(report) is dict else {}
    if (type(report) is not dict or report.get("status") != "ok"
            or report.get("policy_version") != POLICY_VERSION or type(runtime) is not dict
            or runtime.get("image") != image or runtime.get("context") != context
            or runtime.get("source_sha256") != source_hash):
        _fail("environment_mismatch", "Development or preparation runtime does not match the frozen policy")


def _execution(case, execution, source_hash):
    from .validator_development import check_case

    if (type(execution) is not dict or execution.get("status") != "ok"
            or execution.get("reason") != "completed" or execution.get("cleanup_confirmed") is not True):
        reason = execution.get("reason", "invalid_execution") if type(execution) is dict else "invalid_execution"
        _fail(reason, "A development validation did not complete successfully with confirmed cleanup")
    if (execution.get("source_sha256") != source_hash
            or execution.get("input_sha256") != _sha(_canonical(case["request"]))):
        _fail("provenance_mismatch", "Execution identity does not match the source and development input")
    checked = check_case(case, execution)
    if checked.get("passed") is not True:
        _fail("development_failed", "Frozen validation failed development acceptance")
    return checked["findings"]


def _development(root, *, frozen=False):
    from .validator_development import development_cases, record_costs, suite_hash

    files = {"development.json": _read(root, "development.json", MAX_JSON_BYTES)}
    development = _json(files["development.json"])
    if (type(development) is not dict or type(development.get("schema_version")) is not int
            or development["schema_version"] != 1 or development.get("status") != "passed"):
        _fail("development_failed", "Only completed, passing development runs can be frozen")
    config = development.get("config", {})
    if (type(config) is not dict or config.get("enabled") is not True
            or type(config.get("image")) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", config["image"]) is None
            or config.get("docker_context") != "desktop-linux"):
        _fail("invalid_artifact", "Development did not use an enabled, pinned supported sandbox")
    identity = development.get("model_identity")
    if (type(identity) is not dict or not isinstance(identity.get("digest"), str)
            or not identity["digest"]):
        _fail("provenance_mismatch", "Development must record its generation model digest")
    if (development.get("prompt_version") != "validator-generation-v1"
            or development.get("suite_sha256") != suite_hash()
            or development.get("sandbox_policy") != policy_limits()
            or development.get("runtime_source_sha256") != runtime_source_hash()):
        _fail("environment_mismatch", "Development suite, prompt, runtime or limits changed")
    _runtime(development.get("preflight"), config["image"], config["docker_context"],
             development["runtime_source_sha256"])
    attempts = development.get("attempts")
    selected = development.get("selected_attempt")
    if (type(attempts) is not list or not 1 <= len(attempts) <= 3
            or type(selected) is not int or not 1 <= selected <= len(attempts)
            or type(development.get("costs")) is not dict):
        _fail("invalid_artifact", "Invalid attempt selection or development cost record")
    sources = {}
    for index, attempt in enumerate(attempts, 1):
        if type(attempt) is not dict or attempt.get("index") != index or type(attempt.get("index")) is not int:
            _fail("invalid_artifact", "Development attempt numbering is invalid")
        prefix = ("attempts/" if frozen else "") + f"attempt-{index:03d}/"
        report_name = prefix + "attempt.json"
        files[report_name] = _read(root, report_name, MAX_JSON_BYTES)
        if _json(files[report_name]) != attempt:
            _fail("provenance_mismatch", "Attempt report differs from the development record")
        source_hash = attempt.get("source_sha256")
        if source_hash is not None:
            if not _hash(source_hash):
                _fail("invalid_artifact", "Invalid attempt source hash")
            source_name = prefix + "source.py"
            source = _read(root, source_name, MAX_SOURCE_BYTES)
            files[source_name] = source
            if _sha(source) != source_hash:
                _fail("provenance_mismatch", "An attempt source changed after development")
            sources[index] = source
    chosen = attempts[selected - 1]
    if development["costs"] != record_costs(development):
        _fail("provenance_mismatch", "Development costs do not reconcile with the recorded measurements")
    if chosen.get("status") != "passed" or selected not in sources or not sources[selected]:
        _fail("development_failed", "The selected attempt did not pass with preserved source")
    try:
        sources[selected].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("invalid_artifact", "Selected source is not UTF-8")
    cases = development_cases()
    tests = chosen.get("tests")
    if type(tests) is not list or len(tests) != len(cases):
        _fail("development_failed", "The selected attempt must pass every development case")
    expected = {}
    for case, test in zip(cases, tests):
        if (type(test) is not dict or test.get("case_id") != case["id"]
                or test.get("request") != case["request"] or test.get("passed") is not True):
            _fail("provenance_mismatch", "Development acceptance does not match the recorded suite")
        findings = _execution(case, test.get("execution"), chosen["source_sha256"])
        if test.get("findings") != findings or test.get("errors") != []:
            _fail("provenance_mismatch", "Stored development findings do not match independent verification")
        expected[case["id"]] = findings
    if sum(map(len, files.values())) > MAX_PROVENANCE_BYTES:
        _fail("artifact_limit", "Development provenance exceeds its total byte limit")
    if not frozen:
        _tree(root, files)
    return development, files, sources[selected], cases, expected


def _costs(tests, preflight_seconds):
    executions = [test["execution"] for test in tests]

    def measured(key, reducer=sum):
        values = [row.get(key) for row in executions]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
            return None
        return reducer(values) if values else 0

    return {"invocations": len(executions), "preflight_wall_seconds": preflight_seconds,
            "cpu_seconds": measured("cpu_seconds"), "worker_wall_seconds": measured("worker_wall_seconds"),
            "execution_wall_seconds": measured("execution_wall_seconds"),
            "total_wall_seconds": measured("total_wall_seconds"), "peak_memory_bytes": measured("peak_memory_bytes", max),
            "cleanup_seconds": measured("cleanup_seconds")}


def _identity(manifest):
    return _sha(_canonical({key: value for key, value in manifest.items() if key != "artifact_id"}))


def _validate_acceptance(acceptance, cases, expected, source_hash, manifest):
    if (type(acceptance) is not dict or acceptance.get("schema_version") != 1
            or acceptance.get("comparison") != COMPARISON or acceptance.get("repeats") != 2
            or type(acceptance.get("tests")) is not list
            or len(acceptance["tests"]) != len(cases) * 2):
        _fail("invalid_artifact", "Frozen acceptance evidence is incomplete")
    _runtime(acceptance.get("preflight"), manifest["image"], manifest["docker_context"],
             manifest["runtime_source_sha256"])
    for index, test in enumerate(acceptance["tests"]):
        case = cases[index % len(cases)]
        if (type(test) is not dict or type(test.get("repeat")) is not int
                or test["repeat"] != index // len(cases) + 1 or test.get("case_id") != case["id"]
                or test.get("request") != case["request"] or test.get("passed") is not True):
            _fail("invalid_artifact", "Frozen acceptance case order or identity is invalid")
        findings = _execution(case, test.get("execution"), source_hash)
        if (test.get("findings") != findings
                or _canonical(findings) != _canonical(expected[case["id"]])):
            _fail("nondeterministic", "Frozen findings differ from the accepted development result")
    if acceptance.get("costs") != _costs(acceptance["tests"], acceptance.get("preflight_wall_seconds")):
        _fail("provenance_mismatch", "Frozen validation costs do not match execution records")


def _load_artifact(directory):
    directory = _plain_path(directory, directory=True)
    manifest = _json(_read(directory, "manifest.json", MAX_MANIFEST_BYTES))
    if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
        _fail("invalid_artifact", "Incorrect frozen manifest fields")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != "frozen-authored-validator" or manifest["state"] != "frozen"
            or manifest["contract_version"] != CONTRACT_VERSION or manifest["rules_api_version"] != RULES_API_VERSION
            or manifest["policy_version"] != POLICY_VERSION or manifest["limits"] != policy_limits()
            or manifest["runtime_source_sha256"] != runtime_source_hash()):
        _fail("environment_mismatch", "Frozen contract, rules API, runtime or resource policy is incompatible")
    if (not _hash(manifest["artifact_id"]) or manifest["artifact_id"] != _identity(manifest)
            or directory.name != manifest["artifact_id"]):
        _fail("artifact_identity_mismatch", "Frozen artifact identity or directory name changed")
    development, files, source, cases, expected = _development(directory, frozen=True)
    if (manifest["image"] != development["config"]["image"]
            or manifest["docker_context"] != development["config"]["docker_context"]
            or manifest["development_suite_sha256"] != development["suite_sha256"]
            or manifest["source_sha256"] != _sha(source)):
        _fail("provenance_mismatch", "Manifest differs from its development provenance")
    frozen_source = _read(directory, "validator.py", MAX_SOURCE_BYTES)
    if frozen_source != source:
        _fail("provenance_mismatch", "Frozen validator source differs from its accepted attempt")
    files["acceptance.json"] = _read(directory, "acceptance.json", MAX_ACCEPTANCE_BYTES)
    if sum(map(len, files.values())) > MAX_PROVENANCE_BYTES:
        _fail("artifact_limit", "Frozen provenance exceeds its total byte limit")
    provenance = manifest["provenance"]
    if type(provenance) is not dict or set(provenance) != set(files):
        _fail("invalid_artifact", "Manifest contains missing or arbitrary provenance paths")
    if any(not _hash(provenance[name]) or provenance[name] != _sha(raw) for name, raw in files.items()):
        _fail("provenance_mismatch", "Frozen provenance bytes were modified")
    _tree(directory, {*files, "manifest.json", "validator.py"})
    acceptance = _json(files["acceptance.json"])
    _validate_acceptance(acceptance, cases, expected, manifest["source_sha256"], manifest)
    if manifest["setup_costs"] != {"generation_development": development["costs"],
                                   "freeze_validation": acceptance["costs"]}:
        _fail("provenance_mismatch", "Setup costs differ from their development and freeze records")
    return FrozenValidator(manifest["artifact_id"], frozen_source, manifest, development)


def load_artifact(directory):
    """Verify every frozen byte and current runtime identity; never run source."""
    try:
        return _load_artifact(directory)
    except ArtifactError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise ArtifactError("invalid_artifact", str(exc)) from exc


def freeze(development_dir, artifact_root, *, enabled=False, backend=None):
    """Rerun development evidence twice in the sandbox, then publish one artifact.

    Results must match canonical JSON exactly, including fact IDs and list order.
    Failed attempts return partial execution/cost evidence and publish no artifact.
    """
    if enabled is not True:
        return {"status": "error", "reason": "disabled", "message": "Authored validators require explicit opt-in"}
    acceptance = {"schema_version": 1, "comparison": COMPARISON, "repeats": 2,
                  "tests": [], "preflight": None, "preflight_wall_seconds": None}
    try:
        development_dir = _plain_path(development_dir, directory=True)
        development, provenance, source, cases, expected = _development(development_dir)
        config = development["config"]
        backend = backend if backend is not None else DockerSandbox(
            SandboxConfig(enabled=True, image=config["image"], docker_context=config["docker_context"]))
        if (backend.config.enabled is not True or backend.config.image != config["image"]
                or backend.config.docker_context != config["docker_context"]):
            _fail("environment_mismatch", "Freeze must use the development runtime configuration")
        started = time.monotonic()
        acceptance["preflight"] = backend.prepare()
        acceptance["preflight_wall_seconds"] = time.monotonic() - started
        _runtime(acceptance["preflight"], config["image"], config["docker_context"],
                 development["runtime_source_sha256"])
        for repeat in (1, 2):
            for case in cases:
                execution = backend.run_prepared(source, deepcopy(case["request"]))
                test = {"repeat": repeat, "case_id": case["id"], "request": deepcopy(case["request"]),
                        "execution": execution, "passed": False, "findings": None}
                acceptance["tests"].append(test)
                findings = _execution(case, execution, _sha(source))
                test["findings"] = findings
                if _canonical(findings) != _canonical(expected[case["id"]]):
                    _fail("nondeterministic", "Repeated findings differ from the accepted development result")
                test["passed"] = True
        if runtime_source_hash() != development["runtime_source_sha256"]:
            _fail("environment_mismatch", "Trusted runtime source changed while freezing")
        acceptance["costs"] = _costs(acceptance["tests"], acceptance["preflight_wall_seconds"])
        files = {(name if name == "development.json" else "attempts/" + name): raw
                 for name, raw in provenance.items()}
        files["acceptance.json"] = _canonical(acceptance)
        manifest = {"schema_version": 1, "kind": "frozen-authored-validator", "state": "frozen",
                    "contract_version": CONTRACT_VERSION, "rules_api_version": RULES_API_VERSION,
                    "policy_version": POLICY_VERSION, "source_sha256": _sha(source),
                    "runtime_source_sha256": development["runtime_source_sha256"],
                    "image": config["image"], "docker_context": config["docker_context"],
                    "limits": policy_limits(), "development_suite_sha256": development["suite_sha256"],
                    "provenance": {name: _sha(raw) for name, raw in files.items()},
                    "setup_costs": {"generation_development": development["costs"],
                                    "freeze_validation": acceptance["costs"]}}
        manifest["artifact_id"] = _identity(manifest)
        _validate_acceptance(acceptance, cases, expected, _sha(source), manifest)
        if (len(files["acceptance.json"]) > MAX_ACCEPTANCE_BYTES
                or sum(map(len, files.values())) > MAX_PROVENANCE_BYTES):
            _fail("artifact_limit", "Frozen provenance exceeds its byte limits")
        artifact_root = _plain_path(artifact_root)
        artifact_root.mkdir(parents=True, exist_ok=True)
        _plain_path(artifact_root, directory=True)
        destination = artifact_root / manifest["artifact_id"]
        if destination.exists() or destination.is_symlink():
            _fail("artifact_exists", "Frozen artifacts cannot be overwritten")
        with tempfile.TemporaryDirectory(prefix=".freeze-", dir=artifact_root) as temporary:
            staging = Path(temporary)
            for name, raw in {**files, "validator.py": source, "manifest.json": _canonical(manifest)}.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            # A published artifact is always a nonempty directory; rename cannot
            # replace another published artifact. Staging is private and complete.
            if destination.exists() or destination.is_symlink():
                _fail("artifact_exists", "Frozen artifacts cannot be overwritten")
            staging.rename(destination)
        return {"status": "ok", "reason": "frozen", "artifact_id": manifest["artifact_id"],
                "directory": str(destination), "source_sha256": manifest["source_sha256"],
                "setup_costs": deepcopy(manifest["setup_costs"])}
    except (ArtifactError, SandboxFailure, ValidatorContractError) as exc:
        reason, message = exc.reason, str(exc)
    except KeyboardInterrupt:
        reason, message = "cancelled", "Freeze interrupted; incomplete staging is discarded"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        reason, message = "artifact_error", str(exc)
    acceptance["costs"] = _costs(acceptance["tests"], acceptance["preflight_wall_seconds"])
    return {"status": "error", "reason": reason, "message": message, "acceptance": acceptance}
