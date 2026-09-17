"""Explicit inference failure types and Ollama limit evidence."""
import json
import re

MIN_OLLAMA_VERSION = (0, 34, 0)
LIMIT_POLICY_VERSION = "strict-limits-v1"


class PlayerFailure(RuntimeError):
    def __init__(self, reason, message, *, raw=None, elapsed_seconds=None):
        super().__init__(message)
        self.reason = reason
        self.raw = raw
        self.elapsed_seconds = elapsed_seconds


def require_supported_ollama(version):
    # Older servers may silently ignore unknown request fields. This is the
    # oldest release whose truncate/shift behavior this adapter has validated.
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\+[^\s]+)?", version or "") if isinstance(version, str) else None
    if not match or tuple(map(int, match.groups())) < MIN_OLLAMA_VERSION:
        raise PlayerFailure("unsupported_ollama_version",
                            f"Strict limits require a stable Ollama >=0.34.0; server reported {version!r}.")


def context_error(raw):
    """Recognize explicit context errors, including JSON nested in error strings.

    Unknown errors stay infrastructure failures; do not infer context exhaustion
    from a generic 400/500 or a reference to memory allocation.
    """
    if isinstance(raw, dict):
        if raw.get("type") == "exceed_context_size_error":
            return True
        return any(context_error(raw[key]) for key in ("error", "message") if key in raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{"):
            try:
                return context_error(json.loads(stripped))
            except ValueError:
                pass
        message = stripped.lower()
        return any(phrase in message for phrase in (
            "the input length exceeds the context length",
            "the prompt is longer than the context length",
            "exceeds the available context size",
        ))
    return False


def cutoff_reason(raw, token_budget):
    if raw.get("done_reason") != "length":
        return None
    count = raw.get("eval_count")
    if type(count) is int and count >= token_budget:
        return "output_limit"
    # Some runners use 'length' for both output and context limits. If usage
    # doesn't establish which one fired, retain an explicitly ambiguous outcome.
    return "generation_limit"
