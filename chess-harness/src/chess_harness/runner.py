"""Run one isolated game, shared by both command-line entry points."""
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from pathlib import Path
import platform
import uuid

import chess
import httpx

from .game import Recorder, run_game
from .players import EnginePlayer, OllamaPlayer
from .limits import PlayerFailure, require_supported_ollama
from .storage import runtime_identity
from .suites import initial_board


def new_run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def run_match(config, directory, *, batch=None, expected_identity=None):
    """Create fresh players and logs; return the referee's unmodified summary."""
    recorder = Recorder(directory, mode=config["mode"])
    manifest = {"run_id": directory.name, "config": config, "python": platform.python_version()}
    if batch is not None:
        manifest["batch"] = batch
    recorder.write("manifest.json", manifest)
    print(f"Run: {recorder.directory}", flush=True)
    engine = None
    try:
        manifest["packages"] = {name: version(name) for name in ("chess", "httpx", "llm-chess-harness")}
        manifest["engine_sha256"] = hashlib.sha256(Path(config["engine"]["path"]).read_bytes()).hexdigest()
        recorder.write("manifest.json", manifest)
        with httpx.Client(base_url=config["llm"]["url"], timeout=10, trust_env=False) as client:
            for key, path in (("ollama_version", "/api/version"), ("models", "/api/tags")):
                response = client.get(path)
                response.raise_for_status()
                manifest[key] = response.json()
        recorder.write("manifest.json", manifest)
        require_supported_ollama(manifest["ollama_version"].get("version"))
        engine = EnginePlayer(config["engine"])
        manifest["engine_id"] = engine.engine.id
        recorder.write("manifest.json", manifest)
        llm = OllamaPlayer({**config["llm"], "mode": config["mode"]})
        print("Warming model (excluded from game timing)...", flush=True)
        recorder.event("warmup_completed", response=llm.warmup())
        manifest["loaded_model"] = llm.verify_loaded_context()
        recorder.write("manifest.json", manifest)
        if expected_identity is not None and runtime_identity(manifest) != expected_identity:
            raise PlayerFailure("environment_mismatch", "Model, engine, or runtime differs from the batch's first initialized game. Start a new batch for changed environments.")
        recorder.event("context_verified", requested=config["llm"]["context"],
                       effective=manifest["loaded_model"]["context_length"])
        color = config["llm_color"] == "white"
        summary = run_game(initial_board(config), {color: llm, not color: engine}, color, config["max_plies"], recorder)
    except (Exception, KeyboardInterrupt) as exc:
        summary = {"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "infrastructure_failure",
                   "result": "*", "reason": exc.reason if isinstance(exc, PlayerFailure) else "initialization_failure", "error": str(exc)}
        recorder.event("initialization_failed", **summary, raw=exc.raw if isinstance(exc, PlayerFailure) else None)
        recorder.write("summary.json", summary)
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception as exc:
                recorder.event("cleanup_error", message=str(exc))
    print(f"{summary['status']}: {summary['reason']} ({summary['result']})")
    return summary
