# Viewer browser validation

Performed on Windows in the Codex in-app browser on September 14, 2026.
This validates the viewer, not model playing strength. Linux and macOS execution
and responsive/mobile layouts have not been verified.

## Checked in the browser

- Board pieces, coordinate labels and last-move highlighting render correctly.
- Board flip updates orientation, player names and color indicators.
- Switching between saved games updates the board, prompt version, outcome and responses.
- Starting position, next/previous navigation, move selection and the timeline slider
  display the selected recorded position.
- Replay can be started and paused; Live returns to the latest recorded position.
- A newly created run appears automatically when following the newest game.
- Pending turns show the player's name and elapsed time.
- Incoming events do not move the displayed board away from an earlier replay position.
- Castling moves both the king and rook in the displayed position.
- Thinking output can be expanded. Rejected responses remain visible without
  changing the board.
- A stopped viewer server produces the disconnected status; restarting it recovers
  without reloading the browser page.
- Page tools can read the current position and seek to a recorded ply. An out-of-range
  seek fails without changing the displayed position.

Existing real Ollama/Stockfish games were used for saved-game checks. A temporary
scripted fixture explicitly marked `VIEWER QA ONLY` supplied longer move sequences
and controllable live events. It was removed after validation.

## Defect found and fixed

Replaying a different position during a server outage caused that board image
request to fail. On reconnect, the game snapshot was unchanged, so the page never
retried the image and displayed a broken board despite reporting Connected.

The image error handler now invalidates its request cache, and a successful poll
retries an invalidated board even when no new game event has arrived. Repeating
the outage, replay navigation and restart confirmed recovery without a page reload.

## Repeatable regression checks

The Python suite tests snapshot handling, partial event appends, HTTP endpoints,
path restrictions and game behavior:

```text
python -m unittest discover -s tests -v
```

Use the project's virtual-environment Python for this command. Two isolated
JavaScript controller regressions cover image recovery and preserving replay state
during incoming updates. They require Node.js and no npm dependencies:

```text
node --test tests/test_viewer_ui.cjs
```
