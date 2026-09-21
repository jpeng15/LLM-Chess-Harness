"""Explicitly enabled, isolated validator checks; never enables a game mode."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
import sys

from .validator_contract import MAX_INPUT_BYTES, ValidatorContractError, parse_request


MAX_SOURCE_BYTES = 64 * 1024


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("--enable-authored-validators", action="store_true",
                             help="explicitly enable the isolated authored-validator backend")
        command.add_argument("--image", help="pinned local image ID: sha256:<64 lowercase hex digits>")
        command.add_argument("--docker-context", default="desktop-linux")
        command.add_argument("--output", type=Path,
                             help="save the invocation report to a new JSON file instead of stdout")
        if name == "run":
            command.add_argument("--source", type=Path)
            command.add_argument("--request", type=Path)
    return parser


def _read_bounded(path, limit, reason, field):
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValidatorContractError(reason, field, f"File exceeds the {limit}-byte limit")
    return content


def _invoke(args):
    """Read only bounded inputs; backend code is imported after the CLI gate."""
    try:
        if args.command == "run":
            source = _read_bounded(args.source, MAX_SOURCE_BYTES, "source_too_large", "$.source")
            raw = _read_bounded(args.request, MAX_INPUT_BYTES, "input_too_large", "$.request")
            request = parse_request(raw)
    except ValidatorContractError as exc:
        return {"status": "error", "reason": exc.reason, "path": exc.path, "message": str(exc)}
    except OSError as exc:
        return {"status": "error", "reason": "input_error", "message": str(exc)}

    try:
        from .validator_sandbox import DockerSandbox, SandboxConfig

        backend = DockerSandbox(SandboxConfig(enabled=True, image=args.image,
                                              docker_context=args.docker_context))
        if args.command == "preflight":
            return backend.preflight()
        # The backend repeats preflight and requires its acceptance suite before
        # execution. A CLI invocation never bypasses those checks or runs locally.
        return backend.run(source, request)
    except ValidatorContractError as exc:
        return {"status": "error", "reason": exc.reason, "path": exc.path, "message": str(exc)}
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        return {"status": "error", "reason": "sandbox_error", "message": str(exc)}


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    # This gate precedes file access, backend imports, and Docker discovery.
    if not args.enable_authored_validators:
        parser.error("Authored validators are disabled; explicitly pass --enable-authored-validators")
    if args.image is None:
        parser.error("--image is required when authored validators are enabled")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be a pinned sha256:<64 lowercase hex digits> image ID")
    if args.command == "run" and (args.source is None or args.request is None):
        parser.error("run requires --source and --request")

    try:
        with ExitStack() as stack:
            # Reserve an explicit destination before doing work. Exclusive
            # creation prevents overwriting a source, request, or prior report.
            output = (stack.enter_context(args.output.open("x", encoding="utf-8", newline="\n"))
                      if args.output is not None else sys.stdout)
            report = _invoke(args)
            output.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
            output.flush()
            return 0 if report.get("status") == "ok" else 1
    except OSError as exc:
        print(json.dumps({"status": "error", "reason": "report_output_error", "message": str(exc)}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
