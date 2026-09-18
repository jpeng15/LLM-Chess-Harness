# Assisted prompt tuning

Tested 2026-09-17 local time (run IDs use UTC on September 18).

`legal-moves-v2` retains the original notation instructions and complete sorted
move list. It adds general advice about threats, the opponent's reply, piece
safety, development and avoiding pointless reversals. Repetition remains allowed
for defense or a draw. The unassisted prompt and all generation budgets are
unchanged. No engine evaluations or position-specific recommendations reach Qwen.

This is an experimental behavioral adjustment, **not a demonstrated increase in
chess strength**. It does not eliminate repetition or tactical blunders.

## Saved-position comparison

Seven positions were selected from the rook-shuffling game
`20260918T040426Z-502fffd3-000002`, retaining each original request's full history,
seed and generation settings. Five candidate prompts were explored. Stronger
anti-repetition reminders caused two forced-mate blunders; a replacement system
prompt also performed poorly on response validity in full games. The selected
addition preserves the original notation guidance.

These are tuning positions, not a held-out evaluation set. The selected prompt
changed two of seven choices; all fourteen baseline/selected answers were legal.

| Ply | Original answer | Selected answer | Original move evaluation | Selected move evaluation |
|---:|---|---|---:|---:|
| 24 | a2a1 | b7c6 | +779 | +726 |
| 28 | e8d8 | e8d8 | +1061 | +1061 |
| 40 | a1a2 | a1a2 | +493 | +493 |
| 42 | a2a1 | a2a1 | +600 | +600 |
| 44 | a1a2 | a1a2 | +386 | +386 |
| 48 | a1a2 | a1a2 | +25 | +25 |
| 52 | a1a2 | a8a2 | -131 | +16 |

Evaluations are centipawns from Black's perspective, using Stockfish 19 at skill
20, 100,000 nodes, one thread and 64 MB hash, restricting the root to the selected
move. Analysis used the saved FEN, without the earlier repetition history. These
finite-search estimates are diagnostics, not ground truth. Stockfish analysis
ran after Qwen answered and was never included in its prompts.

## Live games

Same model/runtime and configured budgets as the earlier baseline: Qwen3.5:9b,
thinking off, temperature 0, context 4096, output 64 tokens, 60-second move limit,
Stockfish skill 0 with 10,000 nodes, 300-ply cap; White/Black pairs with LLM seeds
1000 and 1001. Stockfish's reduced-strength play varies, so matching these seeds
does not reproduce identical opponent moves.

| Metric | Original v1 | Selected v2 |
|---|---:|---:|
| Games | 4 | 4 |
| Wins / draws / losses | 0 / 0 / 4 | 0 / 0 / 4 |
| Checkmate losses | 3 | 3 |
| Invalid-answer forfeits | 1 illegal | 1 malformed |
| Legal LLM moves / requests | 68 / 69 (98.6%) | 53 / 54 (98.1%) |
| Game lengths in plies | 2, 61, 46, 29 | 2, 43, 44, 19 |
| Immediate reversals by the LLM | 10 | 3 |
| Rook reversals by the LLM | 9 | 0 |

A reversal means a legal LLM move exactly undoes its previous own move's origin
and destination, with an opponent turn between them. This counts justified
defensive repetitions too. Shorter games offer fewer opportunities to repeat;
these counts alone do not establish improvement. The results do not show better
win rate, legality or survival, and the saved positions still show shuffling.

Original batch: `20260918T040426Z-502fffd3`.
Selected-prompt batch: `20260918T044825Z-160113e7`. Its first initialization
attempt hit a Windows access-denied error replacing the manifest. Resume completed
the schedule; the failed attempt is preserved and excluded from game metrics.

The rejected replacement-prompt trial `20260918T044744Z-1a8f125e` also used the
development label `legal-moves-v2` before the final text was selected. It is
excluded from the selected-prompt results above. Use exact logged messages when
inspecting these development trials. The final system prompt's SHA-256 is
`a0b13e9e99629945250f737a89b5c689ce311359ed841b95da611301d55a0678`.

Local diagnostic data (ignored by Git) is in `runs/prompt-tuning/position-ab.json`
(`candidate5` is the selected prompt) and `batch-comparison.json`; complete games
and requests remain in their run directories and are available in the viewer.

Validation: all 57 Python tests and 3 viewer JavaScript tests passed, including
the byte-for-byte unassisted prompt check, complete legal lists, unchanged
generation settings, strict forfeits, and rejection of old-version batch resume.

Further strength claims need a larger held-out position suite and more games.
Thinking mode with a separately measured token/time budget is a useful next
experiment; this change does not enable it automatically.
