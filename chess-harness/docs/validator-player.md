# Playing with a frozen authored validator

This optional Stage 3 mode adds Qwen's frozen checking function to the existing
rules-only board facts and simulations. The default remains `unassisted`.
`constrained-legal` and `rules-tools` retain their existing prompts and budgets.

Prerequisites: the [isolated runtime](validator-sandbox.md) must pass real
acceptance, and [development](validator-development.md) and
[freezing](validator-artifacts.md) must have produced an eligible artifact.
Docker installation is deferred on the current machine. Injected test results
exercise orchestration; they do not establish real sandbox acceptance or chess
strength. Never run generated Python directly on the host.

PowerShell (replace the artifact ID):

```powershell
.\.venv\Scripts\python.exe -m chess_harness --mode authored-validator --enable-authored-validators --validator-artifact "runs/validator-artifacts/<artifact-id>" --no-think --context 16384 --tokens 1024 --move-seconds 90 --tool-calls 4 --tool-depth 4 --max-plies 300
```

Linux/macOS (with an accepted Docker Desktop runtime and Stockfish path):

```sh
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --mode authored-validator --enable-authored-validators --validator-artifact "runs/validator-artifacts/<artifact-id>" --no-think --context 16384 --tokens 1024 --move-seconds 90 --tool-calls 4 --tool-depth 4 --max-plies 300
```

The same mode and flags work with `python -m chess_harness.batch --pairs 5`
and `python -m chess_harness.benchmark`. Resume retains the saved opt-in and
artifact identity; it revalidates the artifact before reusing that configuration.
Supplying validator flags to another mode is an error. A flag by itself never
activates assistance in a baseline.

## One turn

1. Qwen receives the current board, complete history, legal moves and rules facts.
2. A constrained JSON schema allows `simulate` at an explored position, or
   `validate` for a root legal candidate. Both consume the same tool-call budget.
3. Validation checks the artifact again, passes its verified source bytes and
   only the position/history/candidate to a fresh isolated worker, and independently
   replays the returned factual witnesses. No engine call is involved.
4. Qwen receives verified factual findings and separately labeled, unverified
   heuristic interpretations. Empty findings do not establish safety.
5. Qwen chooses a root legal `play`. It must validate at least once; the schema
   reserves the final tool call for validation if necessary. It can ultimately
   select a different move. The referee alone applies moves and declares outcomes.

All inference calls share one output-token budget and one turn deadline. The
worker also has its own smaller CPU, memory, wall-time and output limits. At the
turn deadline, cancellation requests worker teardown and waits for its bounded
cleanup before recording failure. Cleanup may finish after the decision deadline;
late findings are never passed back to Qwen.

A validator code error, timeout, invalid finding, changed artifact or unconfirmed
cleanup stops the game **unscored** and stops its batch. Events preserve the input,
execution status and available measurements. There is no retry, alternative
checker, auto-selected move or source repair during play. Ordinary invalid model
actions still follow the established referee failure policy.

## Records and viewing

`manifest.json` identifies the frozen artifact, source/hash, development provenance
and separate setup costs. The append-only event log contains every model call,
simulation, validator request/result and game-level sandbox preflight. The browser
viewer presents source and generated text as text, labels facts versus heuristics,
and keeps hypothetical moves off the real game board.

Batch reports separate artifact setup (once per artifact), per-run initialization,
per-turn inference, and validator execution. The latest-attempt view follows game
scoring; the all-attempt view includes failed and superseded attempts. Missing
measurements stay unavailable. Overlapping wall-time measurements are not added
to turn latency. A changed final choice is observational, not proof of improvement.

Rewriting functions during games remains excluded; a revised function needs new
development, a new artifact, and a separately declared evaluation.
