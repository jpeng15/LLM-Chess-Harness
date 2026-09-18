# Assisted-mode comprehensive validation

Tested commit: `b8e0eab`, on Windows, September 17, 2026 local time
(September 18 UTC). No implementation changes were required by these checks.

## Coverage

- All **56 Python tests** passed, including special-move lists, unchanged Stage 1
  prompts, invalid-answer forfeits, assisted batch/resume compatibility,
  cross-process locking, forced-termination recovery, reports and viewer routes.
- All **3 JavaScript controller tests** passed, covering reconnect recovery,
  preservation of replay position and mode labels.
- Ran **8 normal-budget comparison games**: four unassisted and four assisted.
- Ran **7 additional live game/setup checks** and one direct oversized-prompt check.
- Independently replayed all eight comparison games, checked their saved prompts
  and outcomes, and resumed both finished batches without creating new attempts
  or changing any game artifact hashes.
- Checked the actual in-app browser: assisted mode label, saved-game selection,
  replay, flipped board, Live returning to the final board, response display,
  and checkmate status. The selected 61-ply game's final FEN matched the log.
  No captured JavaScript console errors were present. The existing tab needed
  a reload to pick up the previously changed static interface.
- Built the wheel offline and verified inclusion of the assisted prompt,
  batch/report/recovery modules and viewer assets. The first build encountered
  a sandbox restriction on pip's global cache; `--no-cache-dir` succeeded without
  changing dependencies or permissions.

## Matched comparison

Both modes used `qwen3.5:9b`, temperature 0, thinking disabled, a 4,096-token
context, 64-token generation budget, 60-second LLM deadline, no move retries and
a 300-ply cap. Stockfish used skill 0, 10,000 nodes, one thread and 64 MiB hash.
Each mode ran two color-balanced pairs using seeds 1000 and 1001. Full runtime
identities matched across modes, including model digest, engine hash, Ollama
0.34.1, Python 3.12.14 and dependency versions.

| Metric | Unassisted | Legal-move assisted |
|---|---:|---:|
| Games | 4 | 4 |
| LLM wins / draws / losses | 0 / 0 / 4 | 0 / 0 / 4 |
| Illegal-move forfeits | 4 | 1 |
| Games ending in checkmate | 0 | 3 |
| Applied / requested LLM moves | 4 / 8 | 68 / 69 |
| Legal-move rate | 50.0% | 98.6% |
| Mean game length, plies | 2.5 | 34.5 |
| Mean LLM latency, seconds | 0.186 | 0.401 |
| Median LLM latency, seconds | 0.172 | 0.328 |
| p95 LLM latency, seconds | 0.265 | 0.422 |
| Recorded prompt tokens, total | 3,406 | 48,488 |
| Recorded generation tokens, total | 40 | 345 |
| Timeouts / truncations / infrastructure failures | 0 / 0 / 0 | 0 / 0 / 0 |

Assisted game lengths were 2, 61, 46 and 29 plies. The one illegal assisted
answer was `e2e4` after `1. e4 e5`: e2 was already empty. The complete supplied
legal list did not contain that move. The response was normally completed and
the referee correctly recorded a forfeit without applying it or retrying.

Every recorded assisted list was independently compared to the complete sorted
legal-move set for that request's FEN. Applied moves, SAN, resulting FENs, ply
counts, PGN results and checkmates agreed with the logs and summaries. All
rejected moves were independently confirmed illegal.

The improvement is in legality and sustained play in this small sample; it did
not produce a win or draw. Prompt assistance does not guarantee selection from
the list. Four games per mode are insufficient for an Elo or reliable ranking.
Stockfish is not seeded by the Ollama seed. Longer assisted games have different
positions and longer histories, so the token and latency differences cannot be
attributed entirely to the legal-move list. The assisted mean also includes one
5.047-second response; its median was 0.328 seconds.

## Additional live checks

| Case | Observation | Expected handling |
|---|---|---|
| Castling position | `e1g1`, O-O | Legal move applied |
| Promotion position | `a7a8q`, a8=Q+ | Legal promotion applied |
| En-passant position | `e5d6`, exd6 | Legal capture applied |
| King in check | `e1d1`, Kd1 | Legal evasion applied |
| One-token generation budget | Partial output | `forfeit / output_limit`, no move applied |
| Requested context of 512 tokens | Server loaded 2,048 | `infrastructure_failure / context_size_mismatch` |
| Unavailable service at localhost port 1 | Connection refused | `infrastructure_failure / initialization_failure` |
| Oversized prompt | Explicit server rejection | Typed `context_limit` failure |

The four special-position games intentionally stopped after one ply. Their
truncations and the deliberately triggered failures above are validation cases,
not part of the scored comparison. The loaded model context was restored to
4,096 after the context-size test. The automated tests additionally cover all
underpromotions, both castling sides/colors and illegal pinned en passant.

## Reproduction and saved results

Run from `chess-harness`, using the virtual-environment Python for your platform:

```text
python -m unittest discover -s tests -v
node --test tests/test_viewer_ui.cjs
python -m chess_harness.batch --mode legal-moves --pairs 2 --seed 1000
python -m chess_harness.batch --mode unassisted --pairs 2 --seed 1000
python -m chess_harness.report --batch runs/batches/<batch-id>
python -m pip wheel . --no-deps --no-build-isolation --no-index --no-cache-dir --wheel-dir runs/validation-wheel
```

Use `--engine /absolute/path/to/stockfish` for a different installation, including
Linux/macOS. This validation run was on Windows only.

- Assisted batch: `20260918T040426Z-502fffd3`.
- Fresh unassisted batch: `20260918T040517Z-daa26f27`.
- [Portable comparison and edge-case record](baselines/stage2-comparison-2026-09-17.json).
- [Assisted games](baselines/stage2-assisted-2026-09-17.pgn).
- [Fresh unassisted games](baselines/stage2-unassisted-2026-09-17.pgn).

Full manifests, raw requests/responses and generated Markdown/JSON reports remain
in each batch's local `runs/` folders. The portable record replaces absolute
engine paths with a placeholder and retains the runtime digests and settings.
