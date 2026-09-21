# LLM Chess Harness

Stage 1 runs unassisted LLMs against UCI chess engines, with saved games,
live viewing, paired batch benchmarks, interruption recovery and aggregate reports.
The local Stage 1 workflow is complete; see the [acceptance checks and recorded
baseline](docs/stage1-validation.md). Stage 2's legal-move prompt assistance is
available through `--mode legal-moves`, with JSON-schema enforcement through
`--mode constrained-legal`; unassisted remains the default. The local
Stage 2 workflow includes fixed-position benchmarks, varied-start paired games,
saved-run comparisons, independent move-quality analysis and thinking-mode
experiments. Stage 3 begins with `--mode rules-tools`: model-directed hypothetical
move simulation and board inspection, using chess rules without engine advice.
The optional [LLM-authored validation-tools phase](docs/stage3-authored-validators.md)
now has its [versioned contract, rules API, and factual witness verifier](docs/validator-contract.md).
An [explicitly enabled isolated runner](docs/validator-sandbox.md) is also available
for preparation checks; real Docker acceptance is pending. Normal game commands
remain unassisted unless a `--mode` is selected. Authored-validator commands require
`--enable-authored-validators`; game integration and frozen artifacts are pending.
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
ollama pull qwen3.6:35b-a3b
ollama list
```

The harness connects to `http://localhost:11434` by default. Use `--url URL` for
another reachable Ollama server. Local inference speed and memory requirements
depend on your hardware; the RTX 5070 measurements from development do not apply
to every machine. Ollama's macOS documentation lists GPU support for Apple silicon
and CPU-only support for Intel Macs.

### Trying Qwen3.6 35B-A3B

The harness defaults to the official Ollama model
[`qwen3.6:35b-a3b`](https://ollama.com/library/qwen3.6:35b-a3b).
Its Q4_K_M download is approximately 23 GB. The MoE architecture activates about
3B parameters per token, but all model weights still need storage and memory.
On a 12 GB GPU, expect a CPU/GPU split and significant system RAM use; unload
other models first. `ollama ps` shows the actual placement. The non-thinking
comparison below uses 4,096 context tokens; normal thinking runs default to 8,192.

Download once (on Windows, if `ollama` is not on PATH, use
`& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"` in its place):

```text
ollama pull qwen3.6:35b-a3b
```

Run a short assisted game from this directory:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness --model qwen3.6:35b-a3b --mode legal-moves --no-think --context 4096 --tokens 64 --move-seconds 60 --max-plies 20
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --model qwen3.6:35b-a3b --mode legal-moves --no-think --context 4096 --tokens 64 --move-seconds 60 --max-plies 20
```

Remove `--max-plies 20` for the normal 300-ply cap. Use the same `--model` option
with `chess_harness.batch` to benchmark it. Start a new batch for a different
model; saved batches cannot switch models on resume. These commands leave
thinking disabled and retain the existing budgets for comparison. Thinking-mode
experiments need a larger, separately recorded token/time budget; 64 output
tokens are not a suitable reasoning budget. To use the normal thinking defaults,
omit `--no-think`, `--context 4096`, `--tokens 64`, and `--move-seconds 60`.
The default model is Qwen3.6 35B-A3B.
The earlier Qwen3.5 9B benchmark records remain available, but replaying those
experiments requires downloading `qwen3.5:9b` again and selecting it explicitly.

## Fixed-position benchmarks and comparisons

Stage 2 includes a bundled 12-position suite, split into six development and six
validation positions. It covers openings, checks, mate in one, castling,
promotion, en passant, endgames and recent move reversals. Use development
positions for prompt tuning and reserve validation positions for later checks.
The small suite is a starting point, not an Elo test or comprehensive chess exam.

From this directory, run a single-decision probe of each position:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.benchmark --mode legal-moves --split development --seed 2100
.\.venv\Scripts\python.exe -m chess_harness.benchmark --mode legal-moves --split validation --seed 2100
```

On Linux/macOS, replace `.\.venv\Scripts\python.exe` with `./.venv/bin/python`
and add `--engine "$STOCKFISH"`. Use `--mode unassisted` for the baseline or
`--model NAME` for another installed model. Each probe uses the normal strict
referee and budgets and stops after one LLM decision. A successful nonterminal
move is recorded as `truncated: max_plies`; it is not a game result.

Reports and the exact suite snapshot/hash are saved to `runs/positions/<id>/`.
Individual probe runs, prompts, replies, PGNs and manifests are stored in `runs/`
and can be replayed in the viewer. Expected moves are scoring annotations only;
they never enter the model's input. Mate fixtures enumerate every mate-in-one
answer. Other fixtures measure legality and move quality without claiming that
one annotated move is the only good answer. Reports include token counts and
latency; missing token usage is not estimated. An infrastructure failure stops
the suite and preserves partial results. Start a new benchmark to rerun it;
position-suite resume is not implemented, and incomplete suites cannot be compared.

Supply `--suite PATH` for a custom JSON suite, following
[`positions.json`](src/chess_harness/positions.json): schema version 1, a name,
and positions with unique `id`, `split`, `fen`, optional legal UCI `moves` history,
and optional `expected_moves`. Positions already terminal under the draw policy
are rejected. History is preserved when prompting and replaying.

Full-game batches can cycle through the same fixtures, assigning both LLM colors
to each starting position before moving to the next:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.batch --mode legal-moves --positions src/chess_harness/positions.json --split validation --pairs 6 --seed 2100
```

The six pairs above cover all six validation positions. Use a custom suite of
opening positions for a conventional opening-balanced match. The suite is copied
into the batch plan; resume uses that snapshot even if the source file changes.
Previously saved single-start batches remain compatible.

Compare two complete position benchmarks or two game batches:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.compare --left runs/positions/LEFT_ID --right runs/positions/RIGHT_ID --output runs/comparisons/example
```

For games, pass `runs/batches/ID` directories instead. The output directory must
be new. Comparisons show results, latency, token usage where applicable, every
changed configuration field, runtime identities and paired position choices.
Position comparisons require the same suite bytes, split and case order. For
games, unmatched schedules and incomplete batches are flagged. Matching LLM
seeds does not seed Stockfish's reduced-strength move selection. Treat budget
changes, different runtimes and small samples as limitations, not proof of an
isolated model or prompt improvement.

File publication retries brief Windows access/sharing conflicts for up to 775 ms
of backoff. Persistent errors still surface and leave the previous published file
intact; the existing game-batch recovery path remains available.

## Post-game move-quality analysis

Analyze saved games after play finishes, using an independent Stockfish process
at skill 20 with strength limiting disabled. This never sends evaluations to
Qwen or changes its saved games. By default each search gets 100,000 nodes:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.analyze --batch runs/batches/BATCH_ID --output runs/analysis/example
.\.venv\Scripts\python.exe -m chess_harness.analyze --benchmark runs/positions/BENCHMARK_ID --output runs/analysis/positions-example
```

Use `--run runs/RUN_ID` for one game, `--engine PATH` to override the saved engine
path, and `--nodes N` to change analysis effort. The output directory must be new.
On Linux/macOS, use `./.venv/bin/python` and the appropriate engine path.

The analyzer replays and validates saved moves/FENs, preserves fixture and game
history, and compares each applied LLM move with the engine's preferred move.
It saves engine identity, node budgets, source-file hashes, principal variations,
centipawn loss, engine-detected missed/allowed mates and immediate reversals.
`--blunder-cp` defaults to 200: a blunder is a loss of at least that many
centipawns, loss of an engine-detected forced mate, or allowing a mate when the
best line avoids it. This is a configurable diagnostic, not a human rating.

Mate values stay separate from centipawn averages. Finite-search disagreements
(the chosen move scores higher in its separate search) are counted and their
negative losses clamp to zero. Invalid/unapplied answers are excluded from
move-quality averages and counted separately. Defensive repetitions can be good;
reversal counts are not automatically blunders. A failure leaves an explicitly
incomplete analysis; it cannot be used in a quality comparison.

Include quality metrics in a saved-run comparison by supplying both analyses:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.compare --left runs/positions/LEFT_ID --right runs/positions/RIGHT_ID --left-analysis runs/analysis/left --right-analysis runs/analysis/right --output runs/comparisons/quality-example
```

Analysis source runs and configurations must match the compared results, and
both analyses must use identical engine and scoring settings. These checks avoid
comparing different analysis budgets as if they were model improvements.

## Controlled assistance and thinking experiments

Run the three Stage 2 conditions on identical validation positions with one command:

```powershell
.\.venv\Scripts\python.exe -m chess_harness.experiment --split validation --seed 2200 --tokens 1024 --context 4096 --move-seconds 180
```

On Linux/macOS, use `./.venv/bin/python` and add `--engine "$STOCKFISH"`.
The default conditions are `unassisted`, `assisted`, and `assisted-thinking`.
An optional `constrained` condition uses schema-constrained legal moves with
thinking disabled; select `--conditions assisted constrained` to compare it
with prompt-only assistance. The suite,
model, seed, temperature and all budgets are held fixed; only the assistance
prompt and thinking flag change. Select a subset with
`--conditions assisted assisted-thinking`. Do not use `--mode` or `--think` with
this command; the named conditions set those fields.

Experiment defaults are 1,024 output tokens, 4,096 context tokens and 180 seconds
per decision. These are experiment budgets, not changes to normal game defaults.
Thinking uses the same output allowance as the final move and can exhaust it.
Output cutoffs remain forfeits; there are no extra retries or automatic budget
increases. Increase `--tokens`, `--context` and `--move-seconds` explicitly for a
separate experiment if needed, subject to local memory and latency limits.

Results go to `runs/experiments/<id>/experiment.md` and `experiment.json`, with
the immutable plan, individual condition reports, independent engine analyses
and all pairwise comparisons. Each condition's full position logs remain under
`runs/` for replay. `--analysis-nodes` controls the common post-hoc analysis budget.
The model/runtime identity is checked across conditions before generating moves.
Infrastructure failures stop later conditions and preserve partial outputs;
restart as a new experiment rather than silently mixing retries into the sample.

This is one sample per fixture per condition in a fixed execution order. Cache,
system load and model nondeterminism can affect measurements. Quality averages
exclude invalid moves and mate scores, so always read them alongside legality,
cutoffs and mate counts. Use more independent fixtures and seeds for broader
claims. Run development fixtures while tuning; once validation results influence
a prompt change, reserve a new validation set for that change.

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

Start Ollama with `qwen3.6:35b-a3b` installed, then run on **Windows**:

```powershell
.\.venv\Scripts\python.exe -m chess_harness
```

On **Linux or macOS**, use the `STOCKFISH` variable set above in the same terminal:

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH"
```

If you open another terminal, set `STOCKFISH` again or supply the full executable
path directly. Quote paths so directory names containing spaces work correctly.

Defaults: LLM plays White, thinking enabled, 8,192-token context (16,384 in rules-tool mode), 4,096 output
tokens (including thinking), 180-second deadline per LLM turn, and no retries. Stockfish uses skill 0,
one thread, 64 MiB hash and 10,000 nodes per turn. Skill 0 is still a strong
opponent and is not a human Elo rating. Warm-up has a separate 180-second timeout.

Use `--no-think` to disable reasoning; budget options remain independently
configurable. These defaults apply to new games, batches, and position benchmarks.
Resumed batches retain their saved configuration. Controlled experiments keep
their separate defaults and select thinking through named conditions.
Thinking can still exhaust its output budget; a cutoff remains a forfeit.

For a short smoke test or a different configuration:

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness --max-plies 8
.\.venv\Scripts\python.exe -m chess_harness --llm-color black --engine-skill 5
.\.venv\Scripts\python.exe -m chess_harness --no-think --tokens 64 --context 4096 --move-seconds 60
.\.venv\Scripts\python.exe -m chess_harness --help
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --max-plies 8
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --llm-color black --engine-skill 5
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --no-think --tokens 64 --context 4096 --move-seconds 60
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

### Schema-constrained legal moves

Use `--mode constrained-legal` to restrict Ollama's completed JSON response to
the legal moves in the current position. This constrains **output**, rather than
just asking the model to obey a list in its input. The model still chooses the
move; no engine evaluates or ranks its choices.

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness --mode constrained-legal --no-think --tokens 128 --context 4096 --move-seconds 60 --max-plies 20
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --mode constrained-legal --no-think --tokens 128 --context 4096 --move-seconds 60 --max-plies 20
```

These commands explicitly disable thinking and allow space for the JSON wrapper.
Remove `--max-plies 20` for the normal 300-ply cap. The same mode and budget
options work with `chess_harness.batch` and `chess_harness.benchmark`. Thinking
is a separate setting: selecting this mode alone does not disable it.

Each request passes a JSON schema in Ollama's `format` field. The required `move`
string has an `enum` containing every current legal UCI move, including all legal
promotion choices, castling and en passant. Extra properties are forbidden. The
schema is rebuilt every turn and included in the prompt and saved request.
The response must be exactly an object such as `{"move":"e2e4"}`.

The adapter checks the complete JSON object, rejects duplicate/extra keys and
non-string values, and verifies exact membership in the legal set. It forwards
only the validated move to the existing referee, which checks legality again.
Malformed JSON and illegal values forfeit; there are no repairs, retries or
fallback moves. Output cutoffs are checked before parsing and still forfeit,
even if the returned JSON appears complete. Timeouts and context limits keep
their existing policies. Constraints cannot guarantee a completed response or
good chess play, and a server/schema failure never silently switches to plain text.

Runs record `constrained-legal-v1` and appear as **Schema-constrained legal** in
the viewer and PGN. Events retain the original JSON in `raw.message.content`
alongside the extracted UCI move in `text`; the viewer has expandable raw output.
Old prompt modes and saved batches retain their original response protocol.
Start a new batch when changing modes and keep comparison budgets matched.

### Rules-only simulation tools (Stage 3)

In `--mode rules-tools`, Qwen can apply a hypothetical move to a temporary board,
inspect its legal replies, and continue that line or explore another candidate.
Stockfish remains the opponent. The simulation code uses only `python-chess`
rules; it supplies no engine searches, rankings, scores, or recommended replies.

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m chess_harness --mode rules-tools --no-think --tokens 1024 --context 16384 --move-seconds 90 --tool-calls 4 --tool-depth 4 --max-plies 8
```

**Linux/macOS:**

```bash
./.venv/bin/python -m chess_harness --engine "$STOCKFISH" --mode rules-tools --no-think --tokens 1024 --context 16384 --move-seconds 90 --tool-calls 4 --tool-depth 4 --max-plies 8
```

Use the same options with `chess_harness.batch` or `chess_harness.benchmark`.
The explicit `--no-think` disables thinking; the global defaults are unchanged.
Remove the short smoke-test ply cap to play a normal game.

Each real turn starts with **position 0**, a copy of the real board including its
history. Qwen returns a schema-constrained JSON action:

```json
{"action":"simulate","position":0,"move":"e2e4"}
```

The tool returns a new position ID, FEN, text board, side to move, complete legal
move list, piece counts, capture details, check status, and any terminal outcome.
Both the initial prompt and simulated positions include exact piece locations,
checking-piece squares, attack maps for occupied squares, pinned pieces, and
all legal captures, checks, castling moves and en passant moves for the side to
move. Attack maps include pinned pieces; they are not lists of legal captures or
judgments of whether a piece is safe. The model is told this distinction.
These facts are computed directly from board rules, without engine evaluation.
For example, Qwen could simulate `e7e5` from position 1, or simulate `d2d4` from
position 0 to explore a different branch. It chooses every hypothetical reply.
Castling, en passant, promotions, and history-dependent draws use the same rules
as the referee. A terminal branch cannot be extended; draw claims follow the
harness's automatic-claim policy.

After at least **one model-selected simulation**, Qwen can commit:

```json
{"action":"play","move":"e2e4"}
```

The final move must be legal at **position 0**, and may be a move that was not
simulated. Only that move reaches the real board. Hypothetical positions and IDs
are discarded after each turn. There is no fallback move or automatic ranking.
This uses Ollama's JSON-schema output with a local action dispatcher, not its
native function-calling protocol.

Defaults allow at most **4 simulations per turn**, with branches at most **4 plies
deep** (one move per simulation). `--tool-calls` accepts 1–16; `--tool-depth`
accepts 1–8. At the call limit the schema permits only a final move. The entire
turn has one `--move-seconds` deadline and one `--tokens` output budget shared
across all model calls, including reasoning when enabled. Each new call receives
the remaining output allowance. Conversation history grows within `--context`,
without truncation or shifting. A missing output-usage counter stops the run
because the shared budget cannot then be verified.

Rules-tool mode defaults to **16,384 context tokens** so the richer board facts
and hypothetical lines have more room. Other modes keep their existing context
defaults. `--context` can override this; the actual loaded context is verified
before play. A larger context window allows more retained information but does
not enable thinking or increase the output budget. It can increase memory use
and latency. Resumed batches always retain their recorded settings.

Invalid actions forfeit without repair/retry. Output exhaustion and timeouts
also forfeit; context overflow remains a truncated game. Tool results and raw
model requests/responses are saved incrementally, including before failures or
interruptions. Reports count one move decision per turn, sum usage from **every**
model call without double-counting the final response, and time the entire turn.
The viewer's expandable **Rules-tool exploration** shows hypothetical positions
separately from the actual game board and PGN. Manifests save the tool policy,
budgets, and `rules-tools-v2` prompt version for reproducibility.

Tool use does not guarantee better play: Qwen must identify useful lines and
judge them itself. Compare against `constrained-legal` with matched total budgets
before drawing conclusions about strength. The existing controlled experiment
command retains its Stage 2 conditions; rules-tool runs can be compared through
the normal batch/position reports and `chess_harness.compare`.

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
