"""Local batch locking and recovery of append-only game records."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time


def atomic_write(path, text):
    """Publish a complete UTF-8 file, tolerating brief Windows reader locks.

    Unique same-directory temporary files keep replacement atomic. Only Windows
    access/sharing/lock violations are retried, for at most 775 ms total delay;
    persistent failures still reach the caller and leave the previous file intact.
    """
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        for attempt, delay in enumerate((0.025, 0.05, 0.1, 0.2, 0.4, 0)):
            try:
                temporary.replace(path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(delay)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Do not mask the original write/replacement error.


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def batch_lock(directory):
    # Keep the file: unlinking a locked file can allow a second lock on a new inode.
    # The OS releases this advisory lock when the process exits, including a crash.
    with (directory / ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Batch is locked by another process; stop that runner before resuming or reporting.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def events(directory):
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n")]


def saved_summary(directory):
    path = directory / "summary.json"
    if path.exists():
        return read_json(path)
    # A process can die between writing the terminal event and writing the summary.
    for event in reversed(events(directory)):
        if event["type"] in ("game_finished", "initialization_failed"):
            return {key: value for key, value in event.items()
                    if key not in ("type", "sequence", "time", "raw")}
    return None


def runtime_identity(manifest):
    loaded = manifest.get("loaded_model", {})
    if not loaded.get("digest"):
        return None
    identity = {"engine_sha256": manifest["engine_sha256"],
            "model_digest": loaded["digest"],
            "ollama_version": manifest["ollama_version"]["version"],
            "packages": manifest["packages"], "python": manifest["python"]}
    if "validator_artifact" in manifest:
        identity["validator_artifact_id"] = manifest["validator_artifact"]["artifact_id"]
    return identity
