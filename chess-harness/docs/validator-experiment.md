# Frozen authored-validator comparison

This is an explicit evaluation command. It never generates, repairs, or freezes
source. It requires an already frozen artifact and a working, verified isolated
runtime. All assistance remains opt-in; ordinary game commands retain their
existing defaults. An unavailable sandbox produces a failure report, not a local
execution fallback.

From the project directory, using the environment's Python:

```text
python -m chess_harness.validator_experiment --enable-authored-validators --validator-artifact runs/validator-artifacts/ARTIFACT_ID --output runs --pairs 1 --context 16384 --tokens 1024 --move-seconds 180 --no-think
```

On Windows, use `.venv\Scripts\python.exe`; on Linux/macOS, use
`.venv/bin/python` and add `--engine "$STOCKFISH"` with your executable path.
The current sandbox backend requires its supported Docker
Desktop Linux VM configuration and the pinned image recorded in the artifact.
Preparing an artifact and runtime are separate prerequisites; this command does
not install Docker, build an image, pull a model, or start a benchmark implicitly.

## What is predeclared

Before touching the runtime, the command saves `plan.json` and an initial
`experiment.json` under `runs/validator-experiments/<id>`. The plan records:

- The frozen artifact ID, exact source, source hash, manifest, sandbox limits,
  image identity, and setup-cost ledger.
- The full functional held-out suite, required factual witnesses, and digests.
- Three ordered conditions: `constrained-legal`, `rules-tools`, and
  `authored-validator`.
- Identical model, thinking, context, output-token and whole-turn time budgets,
  Stockfish opponent settings, maximum plies, seeds, colors, and starting positions.
- A shared tool-action count and simulation depth for the two tool modes. The
  constrained baseline exposes no tools. The rules baseline requires a simulation;
  the authored mode requires a validator invocation. These are intended differences.
- Explicit failure policies and the exclusion of engine analysis from this command.

The command defaults to one White/Black pair per condition, 16,384 context tokens,
1,024 output tokens per turn, 180 seconds per turn, and thinking disabled. Set the
common game flags to change all three conditions together. `--tool-calls` and
`--tool-depth` change the two tool conditions together. Do not pass `--mode`:
this command fixes the three modes. No inference budget is raised between conditions.

`--positions path/to/suite.json` optionally selects the existing position-suite
format's validation split for color-paired starting positions. Otherwise the
shared initial position is used. Development starting fixtures are rejected.

## Functional held-out checks

The built-in evaluation suite is separate from development fixtures. It covers
both mate claim kinds, a black queen capture, white en passant, black capturing
promotion, pinned-rook legality, black queenside castling, repetition history, and
stalemate. All expected witnesses are themselves independently checked with the
rules library before evaluation.

The worker receives only the frozen source and the Step 1 request: current
position, full relevant history, and candidate. Case IDs, expected findings, other
cases, and benchmark answers remain in trusted host records. The host verifies
every returned fact and then checks required positive findings. Fact IDs and
heuristic wording need not match an answer key. Heuristics remain unverified;
there is no requirement to declare a capturable sacrifice bad.

Use `--heldout-suite path/to/checks.json` for a separately maintained evaluation
suite. Its format is:

```json
{
  "schema_version": 1,
  "name": "my-heldout-suite-v1",
  "split": "validation",
  "cases": [
    {
      "id": "case-identifier",
      "request": {"contract_version": "authored-validator-v1", "rules_api_version": "rules-board-v1", "position": {"fen": "CURRENT_FEN"}, "history": {"initial_fen": "START_FEN", "moves": []}, "candidate": "LEGAL_UCI"},
      "required_facts": [{"kind": "candidate_checkmate", "line": ["LEGAL_UCI"]}]
    }
  ]
}
```

Replace the placeholders with a legal, true witness. Required facts omit their
report-local `id`. Empty requirements do not assert that a move is safe: the host
still verifies every returned fact. Suites are bounded to 128 cases and 2 MiB.

Execution errors and false factual evidence stop evaluation immediately. Missing
required findings are recorded across the remaining functional cases; any such
accuracy failure prevents game batches from starting. Neither outcome triggers
repair or refreezing. All remaining cases and conditions remain visible as
`not_run`. This gate measures checking functionality, not chess strength.

## Games, costs, and interpretation

Once all functional checks pass, three independent saved game batches run in the
predeclared order. Artifact bytes are reverified before each case and condition;
the authored player additionally verifies them before each invocation. Model,
engine, and runtime identities must match across conditions. The intentional
authored artifact identity field is excluded from that common-runtime comparison.

Reports retain every scheduled condition, partial batch report, typed failure,
and unrun row. A stopped batch stops the comparison. There is no automatic retry
or resume command here; existing batch records remain available for inspection.

`experiment.json` keeps artifact generation/development/freeze costs once,
functional evaluation costs separately, each condition's recorded inference
usage and turn latency, and authored game execution costs across all retained
attempts. Unknown measurements remain unavailable. Worker and subprocess wall
times overlap with turn latency and must not be added to it. Local dollar costs
are not invented. Full source, requests, execution results, witness checks, and
partial diagnostics remain in the saved plan, events, artifact, and game records.

The completed experiment writes all three pairwise comparisons. Its text report
shows wins/draws/losses, scored/scheduled counts, legality, and latency, including
failed and unrun conditions. No Elo or strength improvement threshold is claimed.
Condition order, small samples, different trajectories, and Stockfish skill-mode
randomness limit causal interpretation. A final move differing from the last
validated candidate is an observation, not proof that validation caused it.

Move quality, blunders, and mate-error estimates can be computed afterwards with
the existing `python -m chess_harness.analyze --batch ... --output ...` command and
compared with `chess_harness.compare --left-analysis ... --right-analysis ...`.
Those separate post-game results never enter source development, the validator,
or a player's decision context. Do not use held-out outcomes to tune this artifact.
