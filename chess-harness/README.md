# LLM Chess Harness

Stage 1 will run unassisted LLMs against UCI chess engines, with saved games,
live viewing, and benchmarks. The Python environment, package scaffold, and
Stockfish installation are ready, along with a command-line single-game runner.
Live viewing and batch benchmarks are later milestones.

## Environment

Requires Python 3.12 or newer. Run these commands from this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
```

The initial environment was created with Codex's bundled Python 3.12.14 because
Python was not on the shell's PATH. A virtual environment depends on its base
Python installation; recreate it with your own Python if that runtime is removed.

No activation or PowerShell execution-policy change is required. Use
`.\.venv\Scripts\python.exe` to run Python in this project.

## Dependencies

- `chess`: chess rules, board state, PGN, and UCI engine communication.
- `httpx`: HTTP requests to the local Ollama server.

`pyproject.toml` declares supported dependency ranges; `requirements.lock.txt`
records the exact installed versions for this setup, including build tooling.

The model runs in the separate Ollama service. This environment does not need
PyTorch or a CUDA toolkit. Stockfish runs on the CPU.

## Stockfish

Stockfish 19's official Windows x86-64 universal build is installed under
`engines/stockfish-19/`. The archive was checked against the release's SHA-256
digest. Download provenance and the executable path are in `stockfish-install.json`.
The extracted distribution retains its license and accompanying files.
Engine downloads are ignored by Git and must be installed separately on a new checkout.

Check the engine from this directory:

```powershell
.\.venv\Scripts\python.exe scripts\check_stockfish.py
```

The check uses one CPU thread, 64 MiB hash, and a 10,000-node search. It verifies
a legal opening move and prints the engine's supported strength controls.
Use `--engine PATH` to check another UCI executable. These are smoke-check settings;
the game runner exposes separate match strength and search budget options.

## Run a game

Start Ollama with `qwen3.5:9b` installed, then run:

```powershell
.\.venv\Scripts\python.exe -m chess_harness
```

Defaults: LLM plays White, thinking disabled, 4,096-token context, 64 output
tokens, 60-second deadline per LLM turn, and no retries. Stockfish uses skill 0,
one thread, 64 MiB hash and 10,000 nodes per turn. Skill 0 is still a strong
opponent and is not a human Elo rating. Warm-up has a separate 180-second timeout.

For a short smoke test or a different configuration:

```powershell
.\.venv\Scripts\python.exe -m chess_harness --max-plies 8
.\.venv\Scripts\python.exe -m chess_harness --llm-color black --engine-skill 5
.\.venv\Scripts\python.exe -m chess_harness --think --tokens 2048 --move-seconds 120
.\.venv\Scripts\python.exe -m chess_harness --help
```

The referee supplies FEN, a text board, and SAN history, but no legal moves or
tools. Each turn is a fresh request. Only surrounding whitespace is ignored:
prose, invalid notation, and illegal moves cause a forfeit. LLM deadlines also
cause forfeits; engine or service failures are recorded as infrastructure failures.
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

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
