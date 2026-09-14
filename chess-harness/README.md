# LLM Chess Harness

Stage 1 will run unassisted LLMs against UCI chess engines, with saved games,
live viewing, and benchmarks. The Python environment, package scaffold, and
Stockfish integration are ready, along with a command-line single-game runner
and a local browser viewer with replay. Batch benchmarks are a later milestone. The setup below covers
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

The referee supplies FEN, a text board, and SAN history, but no legal moves or
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
Context must accommodate the growing history and any thinking output; long-game
context sizing and runtime truncation detection remain to be hardened before batch benchmarks.

Each run has its own directory under `runs/`, containing `manifest.json` (configuration,
model inventory/digests and versions), `events.jsonl` (requests, raw responses and timing),
`game.pgn`, and `summary.json`. PGN is updated after each legal move. Initialization
failures produce a manifest, events and summary without a PGN. Ctrl+C during play
records an interrupted game. Exit code 1 indicates interruption or infrastructure
failure; completed games, forfeits and deliberate truncation return 0.

Run the focused referee and deadline checks:

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
