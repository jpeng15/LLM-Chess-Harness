# Optional authored-validator execution backend

Status: Step 2 code is implemented; **real Docker image/kernel acceptance is
pending**. Installing Docker was deferred. Passing mocked lifecycle tests and
compiling the native supervisor do not establish that a machine's isolation is
working. Every enabled execution requires the real preflight suite to pass first.

## Explicit opt-in

Normal game commands still default to `--mode unassisted`. Assistance is selected
explicitly and recorded in the game configuration:

| Assistance | Selection |
| --- | --- |
| Legal-move list in prompt | `--mode legal-moves` |
| JSON-schema-constrained legal move | `--mode constrained-legal` |
| Board facts and model-directed simulations | `--mode rules-tools` |
| Authored-validator preparation/execution checks | Separate `validator` command with `--enable-authored-validators` |

`--tool-calls` and `--tool-depth` still require `--mode rules-tools`. The new
backend is disabled by default, including its Python API (`SandboxConfig.enabled
= False`). Without explicit opt-in, validator commands fail before reading input
files, importing the backend, or discovering/starting Docker. No environment
variable or saved game silently enables this feature.

This step does not add an authored-validator game mode, generate code, freeze
artifacts, or modify model prompts. Those are later phases. The standalone `run`
command is a preparation/acceptance interface and cannot feed results into a game.

## Supported runtime and trust boundary

The first backend supports only a local Docker Desktop Linux VM, selected via
the `desktop-linux` context. It verifies the local endpoint, Linux daemon,
Docker Desktop identity, cgroup v2 and resource-limit capabilities. Native Docker
on a bare Linux host, arbitrary remote Docker contexts, plain WSL Python, and
local subprocess execution are not fallback backends. Other dedicated VM
providers require separately implemented and tested support.

The operator supplies an immutable local image ID (`sha256:` plus 64 lowercase
hex digits). Tags such as `latest` are rejected. The image must have the expected
entry point, no automatic volumes, version labels, and a fingerprint of the
trusted runtime sources. The image and Docker daemon are trusted infrastructure;
labels identify a locally reviewed build, not a cryptographic attestation against
a malicious operator or compromised daemon.

The Dockerfile requires a digest-pinned official Python 3.12 slim base. It builds
a native supervisor, installs libseccomp, and verifies the locked chess source
archive's SHA-256 before copying only its rules module. `chess.engine`, the
harness player, model client, fixtures, runs, credentials, and Stockfish are not
copied into the worker image. A Dockerfile-specific ignore file limits the build
context to the listed runtime inputs. The final image ID also identifies the
resolved OS packages; rebuilding may produce a different ID and requires fresh
preflight rather than pretending to be the same runtime.

Each invocation creates a new disposable container. The host supplies source
and one request through stdin; no workspace, Docker socket, host directory,
device, GPU, port, credential, or model endpoint is mounted or passed through.

## Enforced limits and execution order

| Control | Initial policy |
| --- | --- |
| Network | No network; kernel socket syscalls denied after bootstrap |
| Identity | UID/GID 65534; all capabilities dropped; no-new-privileges |
| Filesystem | Read-only root, 1 MiB temporary filesystem; all file opens denied after bootstrap |
| Process count | Two: trusted supervisor and one Python worker; further spawn/exec denied |
| CPU | One CPU quota; one CPU-second hard limit per process |
| Memory | 256 MiB container limit, no swap |
| Invocation deadline | 2 seconds including create/start and policy inspection |
| Child stdout / stderr | 64 KiB / 16 KiB, captured while streaming |
| Source / request | 64 KiB / 128 KiB |
| Logging | Docker logging disabled; only bounded invocation reports |

These initial limits are deliberately fixed. There is no automatic relaxation
on failure. Runtime-control commands have separate bounded deadlines; cleanup
may take up to two five-second control attempts. This overhead is measured
separately from the invocation deadline. If Docker becomes unavailable, the
host cannot assert that a container has been removed: it reports `cleanup_failed`
and accepts no findings. A process stuck in the kernel or an unavailable daemon
is an explicit infrastructure failure, not a promise of instantaneous removal.

The host inspects the effective container configuration before sending source.
Inside it, the trusted Python bootstrap checks the immutable image marker,
read-only filesystem, cgroups, CPU limits, UID, capabilities, network interfaces,
and existing seccomp/no-new-privileges state. It loads the required rules and
serialization code, then installs an additional native-kernel **default-deny**
syscall filter before executing any validator source. File opens, networking,
fork/clone/exec, limit changes, ptrace/process-memory access, and signals to other
processes are excluded. Source is never executed by the host's parser/verifier.

The source-policy layer allows function definitions and docstrings at module
level. The entry point is exactly `validate(position, history, candidate)`.
`RulesBoard`, `CONTRACT_VERSION`, and a small set of ordinary builtins are
provided directly. Imports, private attribute access, reflection, decorators,
function defaults/annotations, classes, asynchronous code, and global/nonlocal
mutation are excluded. Helpers, loops, comprehensions, local state, recursion,
and rules-based branching are available. AST size is capped at 10,000 nodes.
This policy describes the supported language/API; it is not a substitute for
kernel/container/VM isolation. No checking algorithm is inserted as a fallback.

The native supervisor is a different process from generated code. It closes
extra inherited descriptors, starts Python with a clean fixed environment and
hash seed, drains bounded child pipes, kills the process group on limits, and
uses `wait4` for CPU time, peak RSS, and exit evidence. The child cannot replace
those measurements with fields in its JSON result. Successful child exit alone
is insufficient: the stream must complete without overflow or timeout, and the
host must independently verify every factual witness using the Step 1 contract.

## Build and check when Docker is available

These commands are manual opt-in preparation, not setup performed by the
harness. Obtain a verified digest for the official Python 3.12 slim image and
replace the placeholder before building. Run from the project directory.

PowerShell:

```powershell
$validatorBaseImage = 'python:3.12-slim@sha256:<verified-base-digest>'
$validatorRuntimeHash = .\.venv\Scripts\python.exe -c "from chess_harness.validator_sandbox import runtime_source_hash; print(runtime_source_hash())"
docker --context desktop-linux build -f sandbox/validator/Dockerfile --build-arg "BASE_IMAGE=$validatorBaseImage" --build-arg "RUNTIME_SOURCE_SHA256=$validatorRuntimeHash" -t chess-validator:development .
$validatorImage = docker --context desktop-linux image inspect --format '{{.Id}}' chess-validator:development
.\.venv\Scripts\python.exe -m chess_harness.validator preflight --enable-authored-validators --image $validatorImage --output validator-preflight.json
```

Linux/macOS with Docker Desktop:

```bash
validator_base_image='python:3.12-slim@sha256:<verified-base-digest>'
validator_runtime_hash=$(./.venv/bin/python -c 'from chess_harness.validator_sandbox import runtime_source_hash; print(runtime_source_hash())')
docker --context desktop-linux build -f sandbox/validator/Dockerfile --build-arg "BASE_IMAGE=$validator_base_image" --build-arg "RUNTIME_SOURCE_SHA256=$validator_runtime_hash" -t chess-validator:development .
validator_image=$(docker --context desktop-linux image inspect --format '{{.Id}}' chess-validator:development)
./.venv/bin/python -m chess_harness.validator preflight --enable-authored-validators --image "$validator_image" --output validator-preflight.json
```

The runtime-source hash normalizes checkout line endings. It identifies the
Dockerfile, build-context allowlist, worker, launcher, dependency lock and rules
API files. The local image ID separately identifies the built bytes. No build
or runtime image is automatically downloaded by an evaluation invocation.

With a passing preflight, check a function and a
[version 1 request](validator-contract.md):

```text
python -m chess_harness.validator run --enable-authored-validators --image sha256:<image-id> --source validator.py --request position.json --output invocation.json
```

`run` repeats preflight; an old JSON report never authorizes execution. This
initial preparation interface trades startup overhead for explicit checks and
records that overhead separately. `--output` creates a new file exclusively;
existing reports, source files, and requests are never overwritten. Without it,
the report goes to stdout. Disabled/malformed CLI options exit 2, explicit
runtime or verification failures exit 1, and successful checks exit 0.

Development and game integrations can instead call `DockerSandbox.prepare()`
once for an in-memory session, followed by `run_prepared(source, request)`.
Preparation still runs the complete real acceptance suite. Every invocation
rechecks the daemon/image identity and effective container configuration; a
saved report cannot create a prepared session. Runtime changes invalidate the
session. This avoids charging repeated stress tests as per-turn validator work.
Callers can supply an absolute turn deadline and a cancellation event. Cancelled
work kills the Docker client and removes the daemon-owned container before the
invocation returns; cleanup uses its own bounded budget. Findings arriving after
the turn deadline or cancellation are rejected.

## Preflight and result evidence

Preflight checks the effective runtime, then exercises file/socket/process/exec
and resource-limit-change denials, stderr/stdout floods, output followed by a
crash or hang, recursion failure, scratch/state-access denial, normal execution,
CPU looping, and memory pressure. Every case gets a fresh container and must
confirm cleanup. Prior managed containers block a new preflight, allowing a
user to inspect and clean up an interrupted run rather than silently leaving it
running. Concurrent authored-validator invocations are not supported yet.

Reports distinguish disabled/unavailable runtime, unsupported isolation,
preflight failure, source syntax/policy errors, code errors, timeout, output
limits, confirmed OOM, malformed findings, false facts, cancellation, and failed
cleanup. An unknown signal/termination remains unknown; exit 137 alone is not
treated as proof of OOM. A cleanup failure blocks further work on that backend
instance, and later instances check for remaining managed containers.

Execution reports preserve source/input hashes, image/policy identity, bounded
raw stdout/stderr, captured and observed byte counts, child exit/signal, factual
verification outcomes, and cleanup evidence. CPU time and peak RSS measure the
worker process including interpreter startup. `worker_wall_seconds` is its
observed lifetime; `execution_wall_seconds` includes container/host operations.
Their difference is not presented as exact function-only startup overhead.
Preflight and cleanup time are separate fields. Missing measurements are null,
not zero; code-generation/model costs are absent because this step makes no
model calls. Later reporting must avoid adding overlapping wall-time totals.

## Validation status

The normal test suite covers opt-in controls, source and protocol parsing,
effective-policy checks, mocked lifecycle failures, independent factual
verification, and bounded native-CLI transport using fixed test subprocesses.
It never executes a validator function outside the container. The C launcher
also has a strict compile-only check. Real Docker build, syscall acceptance,
memory/CPU enforcement, and full runtime-interruption testing must be completed
on the supported backend before Step 2 is considered accepted for experiments.

Docker's documented [container options](https://docs.docker.com/reference/cli/docker/container/run/),
[resource controls](https://docs.docker.com/engine/containers/resource_constraints/),
and [seccomp model](https://docs.docker.com/engine/security/seccomp/) explain the
underlying mechanisms. Configuring these options alone is not evidence that
the required limits work; that is the purpose of the measured preflight.
