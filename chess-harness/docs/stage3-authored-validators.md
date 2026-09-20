# Optional Stage 3 phase: LLM-authored validation tools

Status: Step 1 is implemented; see the [public contract and API](validator-contract.md).
Steps 2–7 remain planned. Each step is a separate implementation and acceptance-test
phase. Existing `constrained-legal` and `rules-tools` modes remain comparison
baselines. Generated source execution and rewriting code during games are unavailable.

## Objective and authority

Qwen writes the checking logic before evaluation. The harness supplies a small,
documented rules-only board API, isolation, evidence verification, and budgets.
The generated function may branch over legal moves and replies. No Stockfish
evaluation, principal variation, engine-selected reply, or benchmark answer may
reach the generator, generated function, or playing model. Stockfish remains an
opponent and an optional separate post-game evaluator.

The real board and referee remain authoritative. The function returns findings,
not a replacement move or permission to mutate the actual game. Qwen selects
its final move through the existing legal-move schema and referee. No finding
automatically vetoes a candidate or selects an alternative.

## 1. Version the function and findings contract

Proposed entry point:

```python
def validate(position, history, candidate):
    # Logic authored by Qwen; no supplied chess-checking implementation.
    ...
```

`position` identifies the current FEN. `history` contains the starting FEN and
the complete ordered UCI move sequence up to this position. `candidate` is one
legal UCI move from the current position. Reconstruct history and require the
resulting FEN to match before invoking the function. FEN alone does not preserve
repetition history. Inputs contain no fixture IDs, answer annotations, analysis
scores, paths, or information from other games. The current evaluation position
is necessarily an allowed input; held-out answers and other fixtures are not.

Publish a versioned rules API for copying boards, enumerating legal moves,
applying moves, inspecting pieces/captures/check, and detecting outcomes with the
harness's draw policy. Qwen supplies iteration, branching, and checking logic.
No built-in tactical checker, engine wrapper, search score, or hidden candidate
ranking is supplied. Generated code may implement its own bounded search using
these primitives.

Return JSON with separate `facts` and `heuristics` arrays and a contract version.
Version 1 supports `capture_available` (candidate plus immediate legal opponent
capture), `candidate_checkmate` (candidate alone), and `reply_checkmate` (candidate
plus immediate mating reply). Each fact includes a witness beginning with the
candidate move; captures identify both pieces and the actual capture square.
Heuristics include a clearly labeled interpretation and references to facts.
Longer conditional lines need a separate explicit factual vocabulary; the rules
API itself permits deeper simulation.

For example, a legal line ending in a queen capture can substantiate
`capture_available`. It cannot substantiate `bad_move`: a sacrifice can be
correct. Empty findings mean nothing was reported, not that the move is safe.
Do not accept universal claims such as "no opponent move wins material" as
facts in version 1; proving search coverage is a separate extension.

Acceptance:

- Reconstruct ordinary and custom starts with full history; reject mismatches.
- Round-trip a schema-valid result and reject malformed, oversized, duplicate-key,
  non-finite, and extra-field output.
- Replay witness lines with trusted rules; reject illegal lines, wrong capture
  identities, false mates, and facts unrelated to the requested candidate.
- Preserve the distinction between a verified capture and a heuristic warning.
- Referee, baseline prompts, and baseline results remain unchanged.

## 2. Build and prove the isolated execution backend

Use a minimal, pinned Linux runtime inside a dedicated VM boundary. A hardened
disposable container inside that VM is the initial proposed execution backend.
On Windows/macOS this requires a verified VM-backed container runtime; on Linux
use the same VM boundary for equivalent isolation. Runtime installation and
capabilities must be checked explicitly. A Python subprocess or a restricted
`exec` is not an acceptable substitute.

The worker image contains the interpreter, the rules API, and pinned rules
dependencies only. It has no chess-engine binary, model service connection,
credentials, benchmark fixtures, gold answers, or repository mount. The host
sends source and one input through a bounded message/pipe and collects bounded
output. No Docker socket, host process namespace, GPU/device access, or host
filesystem sharing is exposed. Use a fresh worker and temporary storage for
each invocation so validators cannot retain state between positions or games.

Apply no networking, an unprivileged UID, dropped capabilities, read-only root
filesystem, bounded temporary storage, no-new-privileges, and a syscall policy
that blocks network operations and process execution/spawning after startup.
CPU and memory limits must be enforced by the runtime, not by generated code.
Apply a host wall-clock deadline and terminate the entire worker on expiry.
Bound stdout/stderr while streaming; do not collect unlimited output first.
The supervisor owns resource measurements and status classification. A worker
cannot declare its own successful execution, CPU use, or memory limit outcome.

Provisional per-invocation limits, to calibrate on this machine before freeze:
1 CPU-second, 2 wall-clock seconds including startup/IPC, 256 MiB memory,
64 KiB source, 128 KiB input, 64 KiB result/stdout, 16 KiB stderr, and bounded
process count and scratch space. Record final numeric limits in the artifact;
no implicit relaxation during evaluation. A source/API allowlist is useful for
the rules-only contract but is not the security boundary.

Acceptance:

- Infinite loops, memory allocation floods, stdout/stderr floods, recursion,
  process spawning, and scratch-space exhaustion terminate within measured caps.
- Socket/DNS/localhost requests, file reads outside the worker, engine invocation,
  package installation, and access to fixtures, answers, secrets or runtime sockets
  fail without altering host state.
- Test forged success output followed by a crash, hang, or output flood; accept
  only complete valid output from a successful worker exit.
- Verify cleanup after normal exit, timeout, crash, cancellation, and VM/runtime
  interruption. Test cross-invocation state isolation.
- Preflight proves the required limits are active. Missing isolation, unsupported
  limits, or unavailable runtime stops the run; no host-execution fallback.

Docker exposes network, memory, process, read-only-filesystem and security
controls, but they must be configured and tested rather than assumed from the
presence of a container. See the official [run options](https://docs.docker.com/reference/cli/docker/container/run/)
and [security model](https://docs.docker.com/engine/security/).

## 3. Generate and develop the function

Add a separate preparation command, conceptually `validator generate`. Give
Qwen the contract, rules API documentation, resource limits and development-only
positions. Request actual function source from the model. Contract examples may
illustrate formats but must not supply a completed validation algorithm.

Use a fixed development budget (initial proposal: at most three generation or
repair attempts), with explicitly recorded model digest, seed, thinking setting,
prompt/output/time budgets, prompt version and raw responses. Run each proposal
only through the isolated backend. Return bounded compiler/runtime diagnostics
and development-test feedback to Qwen between attempts. Our acceptance tests and
factual witness verification stay outside the worker. No held-out results or
Stockfish analysis are used to repair code or choose an artifact.

Store every proposal, source hash, test input identifier, execution result,
diagnostic and generation/development cost, including rejected attempts. Stop
when the acceptance criteria pass or the fixed budget is exhausted. An exhausted
development run produces no eligible artifact; never substitute handwritten
checking logic and call it model-authored.

Acceptance:

- A real Qwen generation produces source that executes successfully on the
  development checks inside the verified sandbox.
- Cover candidate checkmate, a legal capture of a queen, a capturable sacrifice
  without declaring it objectively bad, pinned/illegal apparent captures,
  promotion, en passant, castling, and history-sensitive draws.
- Required positive cases prevent an always-empty function from passing.
- Demonstrate failed proposal logging, bounded repair, exhausted-budget handling,
  and correct attribution of all model calls to generation/development.
- Generator and worker cannot read held-out positions or annotations.

## 4. Freeze an immutable artifact before evaluation

Add a `validator freeze` step. Store the exact source bytes and SHA-256 together
with a manifest identifying the contract/API versions, generator provenance,
development-suite hash, all attempts, acceptance evidence, runtime image digest,
dependency versions, and final resource limits. Derive an artifact ID from the
source and complete manifest, not source alone.

Evaluation requires an explicit frozen artifact reference. Verify its complete
identity before the batch, on resume, and before passing source to workers.
Load the verified bytes directly to avoid a path changing after validation.
Record the artifact identity in every game manifest. Frozen artifacts cannot
be overwritten; revisions create new artifacts and a new evaluation run.
No generation endpoint is called from a game or batch-resume path.

Acceptance:

- Reject unfrozen artifacts, tampered source/manifests, changed runtime/API
  versions, and resume attempts with a different artifact or limits.
- Re-execution of development cases with the same artifact yields the same
  semantic findings; unexpected nondeterminism prevents freezing.
- A failing held-out run cannot repair or replace the artifact in that run.

## 5. Integrate an optional authored-validator player mode

Add a separate mode, provisionally `authored-validator`, reusing the rules-tool
action loop. It retains the same built-in rules facts and simulations as the
baseline, and adds a `validate(candidate)` action for legal moves from real
position 0. Qwen chooses which candidates to check. Version 1 does not execute
the validator against arbitrary hypothetical position IDs.

Use a shared total tool-action limit (initially four per turn), so validation
does not add an unreported extra allowance. Require at least one validation
call in this mode; the schema enforces this before a final `play` action.
All inference and execution time counts toward the same whole-turn deadline.
Model output shares the existing total turn token budget; worker output has its
own byte bound and consumes context when returned to Qwen. Do not automatically
raise inference budgets, discard prompts, or rank candidates.

The host validates result shape and replays factual witnesses with the referee's
rules library. A failed verification is an explicit invalid-finding error, not
a silently dropped warning. Verified facts and clearly labeled, unverified
heuristics are returned to Qwen. Only its final legal `play` changes the real
board. Log source/artifact identity, exact inputs, results, evidence verification,
worker measurements and model calls incrementally.

Acceptance:

- Qwen requests a candidate check, receives its own function's findings, and
  selects a final legal move. The real board/history is untouched by checks.
- Tests show no engine-analysis calls from generation, validation or selection.
- Test errors after earlier successful checks: syntax/import/runtime error,
  timeout, memory termination, output overflow, invalid result and false evidence.
- Each error has an explicit reason. Initial policy: stop the affected game
  unscored and stop the batch, preserving partial records. Do not switch to
  another mode, retry, choose a fallback move, or count it as checkmate.
- Report failed/interrupted/unscored runs and rates prominently; they cannot be
  excluded to make a validator look more successful. Unknown worker termination
  remains an unknown termination, not an inferred OOM or timeout.
- Integration tests confirm baseline behavior, resume identity checks, total
  budgets, cancellation and incremental log recovery.

## 6. Add reporting and viewer support

Separate cost buckets:

1. Generation/development: all generation/repair prompt and output tokens, model
   latency, development execution CPU/wall time, startup overhead, peak memory,
   and failed attempts. Charged once to the artifact.
2. Evaluation: per-turn model tokens/latency; validator invocation count,
   function CPU time, startup/IPC overhead and total execution wall time, peak
   memory, output bytes and typed failures. Report end-to-end turn latency too.

Missing measurements are unavailable, not zero. Report physical resource usage;
do not invent a dollar cost for local inference. Optional amortized setup cost
must state its denominator and must not replace either raw cost bucket.

Extend manifests/events and reports without double-counting worker time already
included in end-to-end turn latency. Show validator source/hash and development
record, per-candidate findings, witness lines, heuristics, execution status and
costs in the viewer. Render all generated text as text, never executable markup.

Acceptance:

- Synthetic known-cost traces verify separate setup and evaluation totals,
  including failures and resumed batches, without duplicate artifact charges.
- Viewer distinguishes verified factual findings, heuristic interpretations,
  rejected findings and execution errors. It never draws speculative positions
  on the real game board as if moves had been played.
- Old saved runs and baseline reports remain readable.

## 7. Run the first frozen comparison

Before running evaluation, fix the artifact, held-out suite/schedule, model,
thinking setting, seeds/colors, opponent strength, turn budgets, tool-action
limits, sandbox limits, and analysis settings. Preserve both existing baselines:
`constrained-legal`, `rules-tools`, and the new `authored-validator` condition.
Use matched inference budgets for the main comparison; distinguish any separate
practical-default comparison where budgets differ. Tool visibility differs by
condition and must be documented.

Evaluate function execution reliability and factual accuracy on held-out checks
without feeding those answers back into the artifact. Then measure game results,
legality, blunders, mate errors, move quality, execution failures and costs.
Stockfish post-game analysis starts only after play and remains outside every
player/generator/worker context. Record runtime identities and whether Qwen's
final choice changes after findings, without claiming that this alone proves
causation. Small samples and different game trajectories limit strength claims.

Acceptance:

- One frozen artifact completes the predeclared evaluation with full provenance,
  or produces an explicit partial/failure report; neither outcome triggers repair.
- Audit the information boundary and cost reconciliation from saved records.
- Publish results for every scheduled condition, including unsuccessful ones.
- No minimum chess-strength improvement is required for implementation acceptance;
  effectiveness is an experimental result, not a pass/fail test to tune against.

## Later extension, explicitly excluded

During-game source rewriting, self-repair, new dependencies, persistent validator
memory, and selection among multiple validators using held-out scores are not
part of this phase. Adaptive code changes would require their own mode, version
history, cost accounting, development/evaluation rules and comparison benchmark.
