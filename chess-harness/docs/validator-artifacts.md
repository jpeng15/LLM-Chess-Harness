# Frozen authored-validator artifacts

Stage 3 Step 4 freezes a successful development run into an immutable,
content-addressed artifact. This is a preparation operation: it does not start
a game or change any existing player mode.

Activate the project environment, then run the same command on Windows, Linux
or macOS:

```console
python -m chess_harness.validator freeze --enable-authored-validators --development runs/validator-development/example --artifacts runs/validator-artifacts --output runs/validator-freeze-report.json
```

The development directory must come from a completed, passing `validator generate`
run. Its selected attempt must have passed every current development case. The
explicit opt-in flag is required before files or a sandbox are accessed. The image
and Docker context come from the development record; `freeze` does not accept
overrides that could change the execution environment after development.

Actual freezing requires the verified sandbox described in
[validator-sandbox.md](validator-sandbox.md). Installing that runtime remains a
separate user action. Without it, freezing returns an explicit error and creates
no frozen artifact; it never executes source in the harness process.

## What freezing verifies

1. Check the development status, model digest, prompt version, selected attempt,
   development-suite hash, runtime source hash and fixed resource policy.
2. Load exact source bytes and every attempt report, including rejected attempts.
   Verify every recorded source hash and the agreement between each attempt file
   and the overall development record. Independently recheck the selected
   attempt's factual findings and required development cases.
3. Prepare the sandbox once, then execute the selected source on every development
   case twice. Each call uses a fresh isolated worker and the same frozen budgets.
   No generator call, held-out position, or engine analysis is involved.
4. Require both repetitions to match the accepted development findings exactly
   under canonical JSON comparison. Object-key order is irrelevant; fact IDs,
   array order and heuristic text must match. Equivalent facts with different IDs
   therefore fail this initial reproducibility requirement.
5. Write a complete staging directory and publish it under its artifact hash.
   Existing published artifacts are never overwritten.

Any failed call, missing cleanup evidence, invalid fact, changed runtime or
nondeterministic result stops freezing. The returned error report includes the
completed and failed repetition records and their available costs. Use `--output`
to preserve that report; output files are created exclusively.

## Artifact layout and identity

```text
<artifacts>/<artifact-id>/
  manifest.json
  validator.py
  development.json
  acceptance.json
  attempts/
    attempt-001/
      attempt.json
      source.py
    ...
```

An attempt without accepted UTF-8 source has no `source.py`; its raw generation
response and failure remain in `attempt.json`. All attempt records are preserved,
not just the selected one.

The manifest records the contract, rules API and sandbox policy versions; exact
selected source hash; trusted runtime source hash; pinned image; Docker context;
resource limits; development-suite hash; every provenance file's hash; and setup
costs. The artifact ID is SHA-256 of the canonical manifest excluding its own
`artifact_id` field. Changes to either source or recorded provenance create a
different identity. A second freeze can have a different identity because its
new acceptance timings and records are part of the provenance.

`load_artifact(path)` checks the directory name, manifest identity, current
versions/runtime/policy, source and every provenance file before returning
`FrozenValidator`. Its `.source` contains the exact verified bytes; `.manifest`
and `.development` return detached copies of verified metadata and development
records. Viewers can use these records without reopening unchecked files.
Callers pass these verified bytes to workers without
reopening the original development source path. Loading never executes source,
starts Docker, or calls a model.

Only fixed artifact paths are accepted. Unknown files, arbitrary manifest paths,
symbolic links, junctions, missing files, invalid JSON and oversized records are
rejected. Hashes provide integrity checks for the recorded local provenance;
they are not a signature from the model provider.

## Costs

The manifest separates:

- `setup_costs.generation_development`: the original run's generation, repairs,
  rejected attempts and development execution costs.
- `setup_costs.freeze_validation`: the two repetition passes, with CPU, worker
  wall time, invocation wall time, cleanup time, peak memory and preparation wall
  time. Detailed preparation evidence remains in `acceptance.json`.

Missing measurements remain unavailable. Worker wall time includes Python
startup through the supervisor observing worker exit; invocation and preparation
wall times also include their respective host/runtime work. These values overlap
and must not be added together as if they were independent charges. All of these
are setup costs, separate from later per-turn validation execution.

## Verification performed during implementation

Unit tests use fabricated model and sandbox responses; they do not compile or
execute generated source. Tests cover the generate-to-freeze interface, opt-in,
full provenance, immutable publication, tampering, runtime changes, failed and
nondeterministic repetitions, source snapshots, cost separation and path checks.
Real isolated acceptance remains pending until the sandbox runtime is installed
and verified.
