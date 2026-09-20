# Authored validator contract, version 1

Step 1 of the [authored-validator phase](stage3-authored-validators.md) provides
plain-data parsing, rules primitives, and independent factual verification.
It does not generate, import, or execute validator source. No CLI playing mode
uses this contract yet. The existing referee and player modes are unchanged.

## Function and request

The future isolated worker will call Qwen's function:

```python
def validate(position, history, candidate):
    # Qwen supplies the checking algorithm.
    # Return a JSON-compatible object with contract_version, facts, heuristics.
    ...
```

The transport request has exactly these fields:

```json
{
  "contract_version": "authored-validator-v1",
  "rules_api_version": "rules-board-v1",
  "position": {
    "fen": "rnbqkbnr/pppppp1p/6p1/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
  },
  "history": {
    "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "moves": ["e2e4", "g7g6"]
  },
  "candidate": "d1h5"
}
```

Only standard chess is supported. FEN must have all six fields, including both
move counters. UCI moves must be exact lowercase strings, with lowercase
promotion suffixes; whitespace, SAN, null moves, and Chess960 are unsupported.

`history.moves` is complete from the supplied starting FEN; a custom starting
position is allowed. The host cannot infer missing history before that start.
Every history move must be legal and must precede any terminal outcome under
the referee's `outcome(claim_draw=True)` policy. Reconstructing the history must
produce the same canonical FEN as `position.fen`, including counters, side to
move, and castling rights. Canonicalization normalizes irrelevant en-passant
targets to `-`, as the harness already does. Retain the reconstructed move stack:
FEN alone cannot establish repetition.

A request must describe a nonterminal position and a legal candidate. The
request carries no fixture IDs, expected answers, engine evaluations, paths, or
other games' information. Unknown fields are rejected rather than ignored.

## Rules API

`chess_harness.validator_rules.RulesBoard` exposes `rules-board-v1`. Qwen's
function can build its own independent board with
`RulesBoard.from_input(position, history)`. The host can additionally validate
the candidate and both versions with `RulesBoard.from_request(request)`.

| Method | Result / behavior |
| --- | --- |
| `copy()` | Independent board retaining full supplied history |
| `fen()` | Canonical six-field FEN |
| `side_to_move()` | `"white"` or `"black"` |
| `legal_moves()` | Sorted list of legal UCI strings; empty after a terminal outcome |
| `push(uci)` | Apply one legal move to this board; reject terminal continuations |
| `piece_at(square)` | Piece symbol or `None`; uppercase White, lowercase Black |
| `capture(uci)` | `None`, or `capturing_piece`, `captured_piece`, `capture_square` |
| `is_castling(uci)` | Whether this legal move castles |
| `is_en_passant(uci)` | Whether this legal move captures en passant |
| `promotion(uci)` | Promoted piece symbol with mover's color, or `None` |
| `is_check()` | Whether the side to move is in check |
| `is_checkmate()` | Whether the position is checkmate |
| `outcome()` | `None`, or `{"result": "...", "reason": "..."}` using referee draw policy |

All move-taking methods check legality and terminal state before operating.
Inspection does not apply the move. Capture identities are measured before the
move: a promoting captor is still a pawn, and en passant's capture square is
the removed pawn's square, not the destination. Accessors return detached data.
`from_input` permits inspecting terminal positions; `from_request` rejects them
because no candidate may be played there.

The API supports copying and exploring deeper lines; Qwen supplies the loops,
branches, and search logic. No tactical checker, candidate ranking, score,
engine wrapper, or strongest-reply selection is provided. The API is not a
security boundary: generated source must wait for Step 2's verified isolation.

## Result and factual vocabulary

For the example request above, a valid result is:

```json
{
  "contract_version": "authored-validator-v1",
  "facts": [
    {
      "id": "f1",
      "kind": "capture_available",
      "line": ["d1h5", "g6h5"],
      "capturing_piece": "p",
      "captured_piece": "Q",
      "capture_square": "h5"
    }
  ],
  "heuristics": [
    {
      "fact_ids": ["f1"],
      "interpretation": "The queen can be captured; assess whether there is compensation."
    }
  ]
}
```

Each fact has a unique `id`, a `kind`, and a `line`. IDs begin with an ASCII
letter and contain at most 32 ASCII letters, digits, underscores, or hyphens.
Each line starts with the requested candidate. Version 1 supports only:

| Kind | Witness | Other required fields |
| --- | --- | --- |
| `candidate_checkmate` | Exactly the candidate; it delivers checkmate | None |
| `reply_checkmate` | Candidate, then a legal opponent reply that delivers checkmate | None |
| `capture_available` | Candidate, then a legal opponent capture | `capturing_piece`, `captured_piece`, `capture_square` |

Piece fields use single symbols, e.g. `Q` for a white queen and `p` for a black
pawn. Kings cannot be captured. Free-form factual descriptions, caller-supplied
verification flags, execution statuses, costs, and winner/score fields are not
accepted. Mate winners follow from replay rather than redundant generated data.

A capture fact means the opponent *can* choose that capture immediately. It
does not mean the capture is forced or the candidate is bad. Longer cooperative
lines cannot substantiate an immediate claim. Universal negatives such as
"no reply wins material" and move-quality claims such as "safe" or "best" are
not factual kinds in this version.

Each heuristic contains exactly `fact_ids` and `interpretation`. References must
be unique within the heuristic and name existing facts; an empty reference list
is allowed. Interpretations are unverified generated text, even when their
referenced facts are verified. Successful verification returns them unchanged.
Empty findings are valid data and mean nothing was reported, not that a move is
safe. Later development acceptance must reject useless always-empty functions.

## Host entry points and boundaries

From `chess_harness.validator_contract`:

- `request_schema()` and `result_schema()` return detached JSON Schemas for
  structural validation; cross-reference checks and chess semantics are separate.
- `parse_request(raw)` and `parse_result(raw)` accept bounded UTF-8 JSON bytes,
  validate structure, and return detached plain dictionaries.
- `validate_request(value)` and `validate_result(value)` validate already-decoded
  plain dictionaries. Result validation also checks IDs and heuristic references.
- `validate_context(position, history)` validates and detaches the context shape.

From `chess_harness.validator_verify`:

- `verify_result(request, result)` validates the request and result, reconstructs
  the history, and independently replays each factual witness from the root.
- `verify_result_bytes(request, raw)` also performs strict byte parsing.

Verification returns a detached result only if every fact passes. One false
fact rejects the entire result. The verifier never finds alternative replies,
adds missing facts, repairs findings, or judges heuristics. A verified result
does not mutate or replace the real referee board and does not select a move.
No source loading, `exec`, model call, or engine analysis occurs in these modules.

## Bounds and failures

| Bound | Version 1 value |
| --- | --- |
| Raw request | 128 KiB |
| Raw result | 64 KiB |
| JSON nesting | 16 container levels |
| History | 2,048 plies; reject longer histories without truncation |
| Each FEN | 256 characters |
| Facts / heuristics | 64 / 32 |
| Interpretation | 512 Unicode characters |
| Fact witness | 1 or 2 plies according to kind |

Byte limits include raw whitespace and are checked before decoding/parsing.
Already-decoded data must also fit the compact UTF-8 serialization limit.
Arrays and strings have explicit caps. Parsing rejects duplicate keys (including
nested duplicates), trailing content, non-finite numbers, invalid UTF-8,
unpaired Unicode surrogates, and extra fields. Plain-data validation rejects
custom Python value/key types. These are contract limits, not sandbox resource
controls; CPU, memory, process, and streamed-output enforcement arrive in Step 2.

Failures raise `ValidatorContractError`, a `ValueError` with `reason`, `path`,
and a diagnostic message. Callers should depend on reason/path, not message text.

| Reason | Meaning |
| --- | --- |
| `invalid_json`, `duplicate_json_key`, `structure_limit` | Invalid wire encoding or nesting |
| `input_limit`, `output_limit` | Byte budget exceeded |
| `invalid_shape`, `unsupported_version` | Wrong fields, values, sizes, types, or versions |
| `duplicate_fact_id`, `invalid_reference` | Invalid factual identity or heuristic reference |
| `invalid_fen`, `position_mismatch` | Invalid position or inconsistent history |
| `invalid_move`, `illegal_move`, `terminal_position`, `invalid_square` | Rules API/request violation |
| `invalid_finding` | A witness or reported fact failed independent verification |

Paths such as `$.history.moves[2]` or `$.facts[0].capture_square` identify the
failure. Document-level wire errors use `$`. This step raises errors only; the
later player integration decides how to stop and record affected games. Workers
will never be allowed to declare their own successful execution or resource use.

## Acceptance checks

Run the focused tests with the project's Python:

```text
python -m unittest discover -s tests -p "test_validator_*.py" -v
```

The tests cover strict parsing, custom starts, counter mismatches, complete
history, identical FENs with different repetition outcomes, claimable draws,
pinned/illegal captures, both-color en passant, promotion, castling, check versus
mate/stalemate, false witnesses, whole-result rejection, capture/heuristic
separation, copy isolation, and absence of engine calls in verification.
Existing baseline regression tests remain part of acceptance. Unit fixtures
exercise contract correctness and do not select a generated validator using
held-out benchmark answers.
