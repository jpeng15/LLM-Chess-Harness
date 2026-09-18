"""Local batch locking and recovery of append-only game records."""
from contextlib import contextmanager
import json
import os


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
    return {"engine_sha256": manifest["engine_sha256"],
            "model_digest": loaded["digest"],
            "ollama_version": manifest["ollama_version"]["version"],
            "packages": manifest["packages"], "python": manifest["python"]}
