# LLM Chess Harness

Stage 1 runs unassisted LLMs against UCI chess engines, with saved games,
live viewing, paired batch benchmarks, interruption recovery and aggregate reports.
The local Stage 1 workflow is complete; see the [acceptance checks and recorded
baseline](docs/stage1-validation.md). Stage 2's legal-move prompt assistance is
available through `--mode legal-moves`; unassisted remains the default.
The setup below covers
Windows, Linux, and macOS; execution has so far been verified on Windows only.

## Environment

Requires Python 3.12 or newer with `pip` and `venv`. Run all commands from the
`chess-harness` directory inside your checkout. Install Python from
[python.org](https://www.python.org/downloads/) or your platform's package manager.
Check the version before creating the environment; some Linux distributions and
macOS installations provide an older default Python.

### Windows (PowerShell)

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
```

If your installation exposes `py` instead of `python`, use `py -3.12` for the
first two commands (or select another installed version >=3.12).

No activation or PowerShell execution-policy change is required. Use
`.\.venv\Scripts\python.exe` to run Python in this project.

### Linux and macOS (bash/zsh)

```bash
python3 --version
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
./.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

If `python3` is older than 3.12, use the executable for your newer installation
(for example, `python3.12`) for the first two commands. On Debian/Ubuntu, if venv
creation reports that `ensurepip` is unavailable, install the matching venv package
for your selected interpreter (for example, `python3.12-venv`). No environment
activation is required; use `./.venv/bin/python` for subsequent commands.

Create a new `.venv` on each machine; do not copy it between operating systems.
The original Windows environment uses Codex's bundled Python 3.12.14. New checkouts
can use their own Python installation and do not require Codex.

## Dependencies

- `chess`: chess rules, board state, PGN, and UCI engine communication.
- `httpx`: HTTP requests to the local Ollama server.

`pyproject.toml` declares supported dependency ranges; `requirements.lock.txt`
records the exact installed versions for this setup, including build tooling.

The model runs in the separate Ollama service. This environment does not need
PyTorch or a CUDA toolkit. Stockfish runs on the CPU.

## Ollama

Install Ollama using its official instructions for
[Windows](https://docs.ollama.com/windows),
[Linux](https://docs.ollama.com/linux), or
[macOS](https://docs.ollama.com/macos).
The harness requires stable Ollama **0.34.0 or newer** for its strict context
controls. Older, unrecognized and prerelease version strings stop initialization;
unknown request fields on older servers can otherwise be silently ignored.
Start the desktop app or your installed Ollama service. If neither is running,
run `ollama serve` in a separate terminal and leave it open.

With the server running, download the model from another terminal (all platforms):

```text
ollama pull qwen3.5:9b
ollama list
```

The harness connects to `http://localhost:11434` by default. Use `--url URL` for
another reachable Ollama server. Local inference speed and memory requirements
depend on your hardware; the RTX 5070 measurements from development do not apply
to every machine. Ollama's macOS documentation lists GPU support for Apple silicon
and CPU-only support for Intel Macs.

## Stockfish

Engine downloads are ignored by Git and must be installed separately on every new
checkout. Obtain a build for your operating system and CPU architecture from the
[official Stockfish download page](https://stockfishchess.org/download/).
For comparisons with the initial baseline, select the
[Stockfish 19 release](https://github.com/official-stockfish/Stockfish/releases/tag/sf_19).
Keep the distribution's license and accompanying files with the engine.

**Both the runner and check script currently default to a Windows `.exe` path.**
On Linux and macOS, pass `--engine` with an actual executable file path on every
invocation. A bare command name such as `--engine stockfish` is not resolved
through PATH by the current implementation.

### Windows

The development installation uses the Stockfish 19 Windows x86-64 universal
archive, extracted into `engines/stockfish-19/`, with the executable at
`engines/stockfish-19/stockfish/stockfish-windows-x86-64-universal.exe`.
Its verified archive digest and download URL are in `stockfish-install.json`;
that file documents the Windows artifact only.
On a new checkout, download and extract the same archive into that directory,
or pass `--engine "C:\path\to\stockfish.exe"` for your own installation.

Check the engine from this directory:

```powershell
.\.venv\Scripts\python.exe scripts\check_stockfish.py
```

### Linux

Download and extract the Linux build matching your CPU. Set `STOCKFISH` to the
extracted executable's absolute path; replace the placeholder below:

```bash
STOCKFISH="/absolute/path/to/stockfish-executable"
chmod +x "$STOCKFISH"
./.venv/bin/python scripts/check_stockfish.py --engine "$STOCKFISH"
```

A distribution-installed Stockfish also works. If it is on PATH, use
`STOCKFISH="$(command -v stockfish)"`; otherwise use its full path (some
distributions install it in `/usr/games/stockfish`). Package-manager versions
may differ from Stockfish 19; the check prints the engine identity.

### macOS

If you use [Homebrew](https://brew.sh/), install its
[Stockfish formula](https://formulae.brew.sh/formula/stockfish):

```bash
brew install stockfish
STOCKFISH="$(brew --prefix stockfish)/bin/stockfish"
./.venv/bin/python scripts/check_stockfish.py --engine "$STOCKFISH"
```

Alternatively, obtain a compatible macOS build from the official download page
and set `STOCKFISH` to its executable's absolute path as in the Linux example.
Homebrew may install a newer version as releases change. Use the same engine
version and options across benchmark comparisons.

The check uses one CPU thread, 64 MiB hash, and a 10,000-node search. It verifies
a legal opening move and prints the engine's supported strength controls.
Use `--engine PATH` to check another UCI executable. These are smoke-check settings;
the game runner exposes separate match strength and search budget options.

## Watch games in your browser

Start the viewer from the project directory and leave it running:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness.viewer
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness.viewer
```

Open [the local viewer](http://127.0.0.1:8765), then run a game in a second
terminal using the commands below. The viewer defaults to **Follow newest game**,
so it also picks up games started after you opened the page. Select a run from
the menu to inspect an older game.

- The board shows the last legal position, highlights the previous move and check,
  and can be flipped to view either side from the bottom.
- Status shows the active player and elapsed thinking time, or the final outcome.
- The move list, slider, previous/next buttons, and **Play replay** control playback.
  **Live** resumes following the selected game's latest position. Replay controls
  never pause or change the actual game.
- Responses show each player's exact returned text and elapsed time, newest first.
  Rejected responses are retained and marked as not applied to the board. Separate
  thinking output is expandable when the model returns it. Responses and game status
  always show the latest received events, even while you replay an earlier position.
- The viewer polls saved events about every 0.7 seconds and automatically retries
  failed connections. Fast games may finish between updates; all recorded moves
  remain available for replay. An old event is flagged as potentially stale rather
  than treated as proof that the runner is still alive.

This is a read-only viewer, served on `127.0.0.1` with no remote assets or new Python
dependencies. Closing the page or stopping the viewer leaves games running.
Ctrl+C in the viewer terminal stops only the viewer; Ctrl+C in the game terminal
interrupts the game. The viewer has no game-start or game-stop controls.

Use `--port 8766` if 8765 is occupied, or `--runs /path/to/runs` to read a different
output directory. If you run games with `--output`, point the viewer's `--runs`
at that same directory. The viewer needs the manifest and event log; a PGN alone
does not contain model responses and is not currently importable.

## Run a game

Start Ollama with `qwen3.5:9b` installed, then run on **Windows**:

```powershell
.\.venv\Scripts\python.exe -m chess_harness
```

On **Linux or macOS**, use the `STOCKFISH` variable set above in the same terminal:

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH"
```

If you open another terminal, set `STOCKFISH` again or supply the full executable
path directly. Quote paths so directory names containing spaces work correctly.

Defaults: LLM plays White, thinking disabled, 4,096-token context, 64 output
tokens, 60-second deadline per LLM turn, and no retries. Stockfish uses skill 0,
one thread, 64 MiB hash and 10,000 nodes per turn. Skill 0 is still a strong
opponent and is not a human Elo rating. Warm-up has a separate 180-second timeout.

For a short smoke test or a different configuration:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness --max-plies 8
.\.venv\Scripts\python.exe -m chess_harness --llm-color black --engine-skill 5
.\.venv\Scripts\python.exe -m chess_harness --think --tokens 2048 --move-seconds 120
.\.venv\Scripts\python.exe -m chess_harness --help
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --max-plies 8
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --llm-color black --engine-skill 5
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --think --tokens 2048 --move-seconds 120
./.venv/bin/python -m chess_harness --help
```

In the default unassisted mode, the referee supplies FEN, a text board, and SAN history, but no legal moves or
tools. Each turn is a fresh request. Only surrounding whitespace is ignored:
prose, invalid notation, and illegal moves cause a forfeit. LLM deadlines also
cause forfeits; engine or service failures are recorded as infrastructure failures.

The current prompt is `unassisted-v2`: it explains origin/destination notation,
provides fixed notation examples for ordinary moves, castling and promotion,
explicitly forbids SAN, and repeats the UCI requirement after the position.
Examples are independent of the position and are not legal-move suggestions.
The original `unassisted-v1` run remains in its own run directory; each manifest
records the prompt version and each move-request event stores the exact messages.
Strict validation, assistance level and retry policy are unchanged between versions.

An HTTP deadline cancels the client request; immediate server-side cancellation
depends on Ollama. No replacement request is submitted after a timeout.

Rules-based draws are automatically claimed, including claims available by making
a move. Games stop at 300 plies by default, recorded as truncated with result `*`.
`--fen` supports alternative starts, but repetition history before that FEN is unknown.
Context must accommodate the growing history and any thinking output. The harness
never shortens the history or automatically retries with a larger budget.

### Context and generation limits

Every move request sends `truncate: false` and `shift: false`, so the server must
retain the supplied prompt rather than discard context to make room. These controls
are defined in [Ollama's request API](https://github.com/ollama/ollama/blob/v0.34.0/api/types.go).
After warm-up, the runner checks `/api/ps` and records the loaded model's effective
context, digest and memory information in the manifest. A context size that differs
from `--context` stops initialization as `context_size_mismatch`. For example, the
tested server clamps a request for 512 tokens to 2,048; specify the intended supported
size explicitly instead. `--tokens` must be smaller than `--context`.

| Evidence | Recorded outcome |
|---|---|
| Explicit server context-overflow error | `truncated` / `context_limit`, result `*` |
| `done_reason: length` and generated-token count reaches the requested budget | `forfeit` / `output_limit` |
| `done_reason: length` without evidence that the output budget was reached | `truncated` / `generation_limit`, result `*` |
| Missing completion, unknown stop reason, malformed server data or connection failure | `infrastructure_failure`, result `*` |
| Normal `stop` with invalid move text | Existing malformed-response or illegal-move forfeit |

A length-limited answer is never applied, even if its partial text happens to be
a complete legal move. Thinking output consumes the backend's generation budget;
it can exhaust that budget before a final move is produced. The raw response and
its counters are saved. Ambiguous limits remain unscored instead of being guessed
from token estimates. Context and generation truncations should be excluded from
completed-game scores and reported separately.

The manifest records `strict-limits-v1` separately from the prompt version, so runs
with older limit policies can be distinguished. Structured server failures appear
in `move_failed` events; received partial responses, errors and elapsed time are
retained when available and displayed by the viewer. A connection failure before
the non-streaming response arrives cannot preserve tokens the server never sent.

The controls were tested with local GGUF inference on Ollama 0.34.0 and 0.34.1. Other backends
and future versions still need validation; matching a version floor alone does not
establish identical behavior. If a backend reports an unfamiliar context error,
it remains an infrastructure failure with the original error saved.

### Legal-move assisted mode

Add `--mode legal-moves` to either the single-game or batch command:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness --mode legal-moves
.\.venv\Scripts\python.exe -m chess_harness.batch --mode legal-moves --pairs 1 --max-plies 8 --seed 1000
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --mode legal-moves
./.venv/bin/python -m chess_harness.batch --engine "$STOCKFISH" --mode legal-moves --pairs 1 --max-plies 8 --seed 1000
```

The model receives the same board, FEN and SAN history, followed by every legal
move for the side to move in a sorted UCI list. The list includes castling,
en passant and all promotion choices when legal, and is regenerated each turn.
Sorting is lexical, not an engine ranking. The model is instructed to select
exactly one listed move; there are no evaluations, tools, constrained decoding,
repair attempts or fallback moves. Malformed answers and moves outside the list
still forfeit under the same referee rules.

The assisted system prompt asks the model to consider threats and the opponent's
reply, protect its pieces, and develop inactive pieces. It discourages pointless
back-and-forth moves while allowing repetition for tactical reasons, defense or
a draw. This is advice, not an enforced rule: the model can still repeat or blunder.
See the [prompt-tuning results and limitations](docs/stage2-prompt-tuning.md).

Assisted runs record `mode: legal-moves` and `prompt_version: legal-moves-v2`.
The mode appears in the viewer, Markdown/JSON reports and the PGN event label.
Exact prompts, including the complete list, are stored in `move_requested` events.
The list consumes the existing context budget; backend prompt-token totals are
reported as before. There is no separate estimate of list-only token cost or
automatic increase to the budget. Generation and time limits are unchanged.

Batch resume uses the saved mode and its corresponding prompt version; it cannot
switch an existing batch between assisted and unassisted. Start a new batch after
changing prompt versions; this version will not resume a `legal-moves-v1` batch.
Old runs remain viewable and reportable. Use a new batch for each mode.
Explicit `--mode unassisted` and the default retain the Stage 1 prompt.
For a controlled comparison, keep colors, seeds, starting position, model, engine
settings and budgets matched, and use the same ply cap in both modes. The
eight-ply command above is a smoke check, not the full baseline comparison.

Each run has its own directory under `runs/`, containing `manifest.json` (configuration,
model inventory/digests and versions), `events.jsonl` (requests, raw responses and timing),
`game.pgn`, and `summary.json`. PGN is updated after each legal move. Initialization
failures produce a manifest, events and summary without a PGN. Ctrl+C during play
records an interrupted game. Exit code 1 indicates interruption or infrastructure
failure; completed games, forfeits and deliberate truncation return 0.

## Run a batch

The batch command runs color-balanced pairs sequentially: the LLM plays White,
then Black, using the same starting position and seed for both games. The next
pair increments the seed by one. `--pairs 5` means **10 games**, and is the default.
Each game starts with a fresh board and Stockfish process. Ollama is warmed and
its effective context is checked before each game, outside that game's timing.
Only one game runs at a time; there are no parallel model requests from the scheduler.

Start with a short two-game smoke test, then remove `--max-plies 2` for normal play:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness.batch --pairs 1 --max-plies 2
.\.venv\Scripts\python.exe -m chess_harness.batch --pairs 5 --seed 100
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness.batch --engine "$STOCKFISH" --pairs 1 --max-plies 2
./.venv/bin/python -m chess_harness.batch --engine "$STOCKFISH" --pairs 5 --seed 100
```

All single-game options also apply, except `--llm-color`, which the schedule controls.
For example, `--model`, `--fen`, `--tokens`, `--context`, `--temperature`,
`--engine-nodes`, and `--output` configure the whole batch. With `--seed 100`, the
first pair uses 100, the second 101, and so on. Seeds are sent to Ollama; they do
not seed Stockfish. Recorded seeds and settings do not guarantee identical
outputs across runs or environments, or diverse games at temperature zero.

Before starting any game, the runner saves the entire schedule. The output is:

```text
runs/
  batches/<batch-id>/
    batch.json                 # Fixed configuration, schedule, colors and seeds
    progress.json              # Latest batch status and individual game summaries
  <batch-id>-000001/            # First game: LLM White
    manifest.json
    events.jsonl
    game.pgn
    summary.json
  <batch-id>-000002/            # Second game: LLM Black
    ...
```

The schedule's color and seed override the base configuration for each game;
each game's manifest records its effective configuration and batch/pair membership.
`progress.json` is replaced atomically before and after each game, keeping earlier
results and distinguishing pending games from the active game. Individual game
events and PGNs remain the source for detailed replay. Setup failures may have
no PGN, as with single games.

Keep the existing viewer running with **Follow newest game** selected to watch
the batch advance. The game folders use the same layout as single runs, so no
viewer configuration change is needed. For a custom `--output`, use that directory
as the viewer's `--runs` value. Batch metadata does not appear as a game.

Completed games, forfeits, and truncations advance the schedule. Infrastructure
failures stop it; Ctrl+C records interruption and leaves later games pending.
Exit code 0 means the schedule finished, including any forfeits or truncations;
exit code 1 means it stopped because of interruption or infrastructure failure.
A batch marked `completed` does not mean every game had a scored chess result.

### Resume a batch

Pass the batch directory printed at startup (replace `<batch-id>`):

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness.batch --resume "runs/batches/<batch-id>"
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness.batch --resume "runs/batches/<batch-id>"
```

Resume uses the saved settings, colors and seeds; configuration overrides are
rejected. Finished games, including forfeits and deliberate truncations, are
skipped. Interrupted or infrastructure-failed games restart from the original
position in a new `-attempt-0002` (then `0003`, etc.) run folder. All earlier
attempts remain intact and appear in `progress.json`. Resume does not retry an
individual move or feed earlier failures back to the model. Another failure stops
the batch again; there is no automatic retry loop.

Recovery checks each attempt's saved summary or complete terminal event, handling
a crash between game completion and the batch checkpoint. A saved `running`
status alone is not evidence of a live process. An operating-system file lock
prevents two runners from executing the same batch, and is released on process
exit. The `.lock` file stays in place; do not delete it to bypass a running process.
Use a local filesystem; network filesystem lock semantics have not been validated.

The first initialized model pins its digest, engine hash, Ollama version, Python
version and package versions in `identity.json`. Later games verify that identity
before making a move. Changed environments, prompt versions or limit policies
require a new batch; settings are never silently replaced during resume. Invalid
or contradictory saved records stop recovery for inspection. Running the normal
command without `--resume` always creates a new batch.

### Report a batch

After the batch stops, generate a human-readable report and machine-readable JSON:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness.report --batch "runs/batches/<batch-id>"
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness.report --batch "runs/batches/<batch-id>"
```

This prints the report and writes `report.md` and `report.json` in the batch
directory. Use `--output /path/to/report-directory` to export elsewhere. The
reporter holds the same batch lock as the runner, so it refuses an active batch;
the live viewer remains available while games run. Reports do not modify game
records or resume play. They can summarize an interrupted batch, but are saved
snapshots: regenerate after resuming to include the new results.

Reports include status and termination-reason counts, LLM wins/draws/losses overall
and by color, legal-move rate, move latency, recorded token usage, and per-game
results. Counts of illegal moves, malformed responses, timeouts, output limits,
context limits and infrastructure failures remain separate termination reasons.

- **Score rate:** `(wins + 0.5 * draws) / scored games`, from the LLM's perspective.
  Completed chess games and forfeits are scored. Truncations, infrastructure
  failures, interruptions and pending games are excluded, never counted as draws.
- **Legal-move rate:** applied LLM moves divided by requested LLM turns. This is
  an operational success rate: requests ending in timeout/failure are in the
  denominator too. The raw turn counts are always included.
- **Latency:** recorded LLM response, timeout and failure durations, excluding
  warm-up and engine moves. Mean, median and nearest-rank p95 use the recorded
  samples only; missing durations are not invented. The JSON includes sample count,
  total, minimum and maximum. No samples means `null`, not zero.
- **Attempts:** each scheduled game contributes at most its latest attempt to
  scores and turn metrics. Superseded attempts stay on disk and are counted
  separately, preventing retries from inflating the benchmark sample size.
- **Tokens:** sums of the backend's reported prompt and generation counters;
  thinking tokens are included when the backend includes them. Missing counters
  are not estimated. The report records how many responses supplied usage.

Configuration and available runtime identities accompany the JSON. A report
warns if multiple identities or unfinished games are present. A small batch is
a pipeline/legality check, not an Elo estimate or a statistically stable ranking.

## Tests

Run the referee, adapter, batch-scheduling, and viewer checks:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**Linux/macOS:**

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

These tests use scripted players and mocked HTTP requests, so they do not require
a running Ollama service or an installed Stockfish executable. The separate
`check_stockfish.py` command exercises your real engine installation.

Optional JavaScript controller regression checks (Node.js, no npm packages required):

```text
node --test tests/test_viewer_ui.cjs
```

The viewer has also been checked in the Windows in-app browser, including replay,
live event updates and recovery after a server outage. See
[the browser validation record](docs/viewer-validation.md) for coverage and limits.
