# Stage 1 acceptance and baseline

Stage 1 is complete for the local unassisted workflow: single games, paired
batches, interruption recovery, saved artifacts, aggregate reports, and browser
viewing. The acceptance checks below ran on Windows on September 17, 2026 (the
baseline run ID uses September 18 UTC). Linux/macOS instructions and lock code
are provided, but those operating systems have not been exercised here.

## Incremental implementation

- `122d1d4`: sequential color-balanced batch runner.
- `5eb99ac`: resume with separate attempt folders, exclusive OS locking, recovery
  from terminal events, and runtime identity checks.
- `a54524f`: aggregate outcomes, move reliability, latency and usage reports.
- This validation phase adds a forced-process-termination recovery test and
  preserves the measured baseline and its PGNs.

## Baseline configuration

Command, from `chess-harness` using its virtual-environment Python:

```text
python -m chess_harness.batch --pairs 2 --seed 1000
python -m chess_harness.report --batch runs/batches/20260918T015406Z-13a3ed30
```

The second command references this particular run; use the batch directory
printed by the first command when repeating the experiment. On Windows use
`.\.venv\Scripts\python.exe`; on Linux/macOS use `./.venv/bin/python` and pass the
local Stockfish executable with `--engine` when creating the batch.

| Setting | Observed value |
|---|---|
| GPU | NVIDIA GeForce RTX 5070, 12,227 MiB reported memory |
| NVIDIA driver | 591.44 |
| Model | `qwen3.5:9b` |
| Ollama / Python | 0.34.1 / 3.12.14 |
| chess / httpx | 1.11.2 / 0.28.1 |
| Prompt / limit policy | `unassisted-v2` / `strict-limits-v1` |
| Model budget | 4,096 context, 64 generation tokens, 60 seconds per turn |
| Sampling | Temperature 0; thinking disabled; no move retries |
| Engine | Stockfish 19, skill 0, 10,000 nodes, one thread, 64 MiB hash |
| Schedule | Two pairs, standard initial position, seeds 1000 and 1001 |
| Game cap | 300 plies; none of these games reached the cap |

Full model/engine digests, runtime identity, metrics and raw-artifact hashes are
in [the portable JSON record](baselines/stage1-2026-09-17.json). Only the absolute
engine path was replaced for portability. The original manifests and event logs
remain under the ignored local `runs/` directory. The four games are also saved
in [a PGN collection](baselines/stage1-2026-09-17.pgn).

## Results

| Game | LLM color | Seed | Applied plies | Rejected answer | Result |
|---:|---|---:|---:|---|---|
| 1 | White | 1000 | 2 | `e4c5` | LLM forfeits, 0-1 |
| 2 | Black | 1000 | 3 | `e7e5` | LLM forfeits, 1-0 |
| 3 | White | 1001 | 2 | `e4d5` | LLM forfeits, 0-1 |
| 4 | Black | 1001 | 3 | `e7e6` | LLM forfeits, 1-0 |

All four answers were complete, syntactically valid UCI strings but illegal in
their positions. Game 1 tried a pawn move across two files; game 3 attempted a
diagonal pawn move to an empty square. In games 2 and 4, the pawn had already
left e7. Each model made one legal move before its second turn caused a forfeit.

- LLM wins/draws/losses: **0/0/4**; score rate **0%**, including forfeits.
- Requested LLM turns: **8**; applied legal moves: **4**; legal-move rate **50%**.
- No truncations, timeouts, incomplete games, or infrastructure failures.
- Recorded LLM latency: **0.223 s mean**, **0.204 s median**, **0.328 s p95**,
  across eight response samples, excluding warm-up and engine turns.
- Reported token usage: **3,406 prompt tokens**, **40 generation tokens**.

This is a small workflow and legality baseline. It does not establish an Elo,
a reliable ranking, or a statistically stable win rate. Seed recording does not
guarantee bitwise reproducibility; Stockfish is not seeded by the Ollama seed,
and temperature-zero games need not provide diverse independent samples.

## Acceptance checks

**Automated:** 50 Python tests and two JavaScript controller tests passed. Tests
cover referee outcomes, strict inference limits, scheduling, color/seed pairing,
attempt isolation, checkpoints, report denominators, latency filtering, viewer
routes, and replay behavior. Recovery tests include an actual child process
being forcibly terminated while holding the batch lock with a partial game on
disk, followed by successful resume and preservation of the original attempt.
A separate process is also verified to be unable to acquire an active lock.

**Live Ollama 0.34.1:** a one-token generation returned partial text `e` and was
classified as `output_limit`. An oversized prompt was rejected as `context_limit`.
Requesting a 512-token context was detected as `context_size_mismatch` after the
server loaded its larger minimum. The normal 4,096-token context was restored.
These checks supplement the earlier Ollama 0.34.0 validation; future backend
versions still require validation.

**Baseline artifact audit:** every applied move was independently replayed and
checked for legality, SAN, and resulting FEN. Each PGN replay matched the event
log and final summary; results and ply counts agreed. The rejected final answers
were independently checked as illegal on the unchanged final boards. The live
viewer HTTP endpoints listed all four games, returned matching final positions
and failure states, and served valid board SVGs. Full visual UI coverage remains
documented in [the earlier browser validation](viewer-validation.md).

**Resume smoke check:** resuming the earlier completed two-game smoke batch
skipped both truncated games. It produced no new game attempts. Finishing a
batch and then resuming it does not spend another inference budget.

## Boundaries and next stage

The supported Stage 1 backend is Ollama plus a local UCI engine. Benchmarks are
sequential; there is no distributed runner or automatic parameter sweep. Reports
are explicit saved snapshots generated after a batch stops. Locks assume a local
filesystem. Warm-up, model installation, and hardware performance are not included
in move-latency metrics. Earlier attempts are available for inspection and counted
separately, rather than added as extra scored games.

Stage 2 can now add legal-move assistance as a separately recorded mode, then
compare it against this unassisted baseline using the same scheduling, referee,
budget, persistence, reporting and viewer infrastructure.
