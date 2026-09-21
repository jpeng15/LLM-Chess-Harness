# Developing an LLM-authored validator

Step 3 asks the configured Ollama model to write checking logic, tests that source
inside the [isolated backend](validator-sandbox.md), and permits at most three
generation/repair attempts. It never imports generated Python into the harness.
Passing development is preparation for freezing an artifact, not evidence of
better chess play or permission to edit functions during a game.

## Run generation

Generation requires an explicitly enabled backend and its immutable local image
ID. The image must match the trusted runtime source and pass the actual Docker
acceptance suite. No model request is made when sandbox preparation fails. Docker
installation remains deferred on this machine, so real model-generated execution
has not been accepted here.

From the project root on Windows:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.validator generate --enable-authored-validators --image sha256:REPLACE_WITH_64_HEX_IMAGE_ID --directory runs/validators/development/qwen-validator-001 --model qwen3.6:35b-a3b --no-think --context 16384 --tokens 4096 --generation-seconds 180 --seed 0 --max-attempts 3
```

On Linux or macOS, use `.venv/bin/python` in place of the Windows interpreter
path. The currently supported isolated backend is the verified Docker Desktop
Linux VM described in the sandbox document; a native Linux Docker daemon does
not qualify as the required VM boundary.

The development directory must be new. The command never overwrites an existing
run and never creates a fallback local executor. `--output new-report.json` can
also save the final report instead of printing it. Exit status is zero only when
a proposal passes every development case.

## Generation and development contract

The prompt supplies the [public rules API and result contract](validator-contract.md),
fixed budgets, and only the development cases with their required findings. It
contains no implementation example, engine analysis, held-out fixtures, or
benchmark answers. The model returns one JSON object with a `source` string.
Source must be nonempty UTF-8 and no larger than 64 KiB. No code extraction from
Markdown, handwritten replacement, or silent repair is performed by the host.

Ollama generation uses JSON-schema output, the requested thinking setting,
temperature and seed, explicit context/output budgets, and `truncate=false` /
`shift=false`. The harness verifies the loaded context and records the model
digest during preparation, then checks that digest before and after each request.
These checks share the generation deadline. A changed or unverifiable model
identity prevents source acceptance while preserving any returned token usage.
All model-service responses are capped at 512 KiB while receiving them; an
oversized response retains a bounded prefix and an explicit failure.
Recognized output truncation, malformed source
responses, code errors and failed development checks can receive another attempt
within the original limit. Context or service errors and sandbox infrastructure
failures stop the run. Each subsequent prompt carries the preceding source and
at most 8 KiB of development diagnostics.

Ten development cases cover candidate mate, opponent reply mate, a capturable
queen, a queen offer with possible compensation, a pinned capture, en passant,
capturing promotion, castling, repetition history and stalemate. Positive cases
require particular verified facts, preventing an always-empty function from
passing. Every returned fact is independently replayed, including additional
facts beyond the required ones. One false fact rejects the entire result.
Heuristic interpretations remain unverified and cannot certify that a sacrifice
is good or bad. These cases check basic correctness, not generalization.

## Saved records and costs

The new directory contains:

```text
development.json
attempt-001/
  attempt.json
  source.py
attempt-002/             # only if another attempt was made
  attempt.json
  source.py
```

`source.py` contains the exact returned UTF-8 bytes and has a recorded SHA-256.
When generation fails before producing an acceptable source envelope, there is
no source file and its hash is null. The raw response and failure remain recorded.
Each attempt is saved before another repair is requested; each invocation is
recorded separately, including failures and interruptions.

`development.json` contains the complete configuration, prompt and suite
versions/hash, model identity, sandbox preflight evidence, trusted runtime source
hash, fixed sandbox policy, full attempt records and selected passing attempt.
The same complete per-attempt record is saved in `attempt.json`.

Costs have separate `generation`, `development_execution`, and `preparation`
buckets. The first records generation attempts, actual inference calls,
input/output tokens and elapsed time. The second records invocation count,
failures, CPU, worker/execution/total wall time, runtime checks, cleanup time and
peak memory. Preparation records measured sandbox preflight and model preparation
wall times, preflight execution costs and available warmup token counts. These
wall-time measurements overlap; they must not be added together as independent
charges. Missing measurements remain null, with observed totals and missing
counts alongside them. Raw warmup/preparation metadata is retained too.
No dollar cost is inferred for local inference. These are setup costs; later
evaluation must report per-turn execution separately.

Final states are `passed`, `exhausted`, `failed`, or `interrupted`. A passing
development directory is not itself a frozen evaluation artifact.

## Acceptance evidence

The unit tests use injected generator and sandbox responses. They check opt-in
gating, strict source parsing, positive case requirements, independent factual
verification, bounded repair, durable failure records, cost separation and CLI
arguments without executing generated code. These tests do not establish that
Docker limits or kernel isolation work on a machine. Actual sandbox acceptance
and a real generated proposal must pass before the workflow is operational there.
