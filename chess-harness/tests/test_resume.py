import argparse
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import queue
import threading
import unittest
from unittest.mock import patch
import httpx

from chess_harness import batch
from chess_harness.cli import add_game_arguments, game_config
from chess_harness.game import Recorder
from chess_harness.players import prompt_version
from chess_harness.storage import batch_lock, read_json
from chess_harness.runner import run_match


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        engine = self.root / "engine"
        engine.write_bytes(b"test")
        parser = argparse.ArgumentParser()
        add_game_arguments(parser)
        self.config = game_config(parser, parser.parse_args(["--engine", str(engine)]))
        self.directory = self.root / "batches" / "test-batch"
        self.addCleanup(patch.stopall)
        patch("chess_harness.batch.new_run_id", return_value="test-batch").start()

    def play(self, config, directory, *, batch, **kwargs):
        record = Recorder(directory)
        record.write("manifest.json", {"config": config, "batch": batch})
        summary = {"status": "completed", "reason": "checkmate", "result": "1-0", "plies": 2}
        record.event("game_finished", **summary)
        record.write("summary.json", summary)
        return summary

    def start(self, side_effect=None, pairs=1):
        with patch("chess_harness.batch.run_match", side_effect=side_effect or self.play), redirect_stdout(io.StringIO()):
            return batch.run_batch(self.config, self.root, pairs)

    def resume(self):
        with redirect_stdout(io.StringIO()):
            return batch.resume_batch(self.directory)

    def test_finished_batch_is_noop_including_forfeits_and_truncations(self):
        def play(config, directory, **kwargs):
            summary = self.play(config, directory, **kwargs)
            summary.update(status="forfeit" if config["llm_color"] == "white" else "truncated",
                           reason="illegal_move" if config["llm_color"] == "white" else "max_plies",
                           result="0-1" if config["llm_color"] == "white" else "*")
            (directory / "summary.json").write_text(json.dumps(summary))
            return summary
        self.start(play)
        with patch("chess_harness.batch.run_match") as run:
            result = self.resume()
        run.assert_not_called()
        self.assertEqual([g["status"] for g in result["games"]], ["forfeit", "truncated"])

    def test_restart_interrupted_attempt_preserves_logs_and_settings(self):
        def crash(config, directory, **kwargs):
            if config["llm_color"] == "white":
                return self.play(config, directory, **kwargs)
            recorder = Recorder(directory)
            recorder.write("manifest.json", {"config": config, "batch": kwargs["batch"]})
            recorder.event("move_requested", ply=1, player="test")
            raise KeyboardInterrupt()
        self.start(crash)
        old = self.root / "test-batch-000002"
        before = (old / "events.jsonl").read_bytes()
        with patch("chess_harness.batch.run_match", side_effect=self.play) as run:
            result = self.resume()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[1].name, "test-batch-000002-attempt-0002")
        self.assertEqual(run.call_args.args[0]["initial_fen"], self.config["initial_fen"])
        self.assertEqual(run.call_args.args[0]["llm_color"], "black")
        self.assertEqual(run.call_args.args[0]["llm"]["seed"], self.config["llm"]["seed"])
        self.assertEqual((old / "events.jsonl").read_bytes(), before)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["games"][1]["attempts"]), 2)

    def test_assisted_resume_preserves_mode_and_accepts_its_prompt_version(self):
        self.config.update(mode="legal-moves", prompt_version=prompt_version("legal-moves"))
        self.test_restart_interrupted_attempt_preserves_logs_and_settings()

    def test_old_assisted_prompt_cannot_resume_under_new_prompt(self):
        self.config.update(mode="legal-moves", prompt_version="legal-moves-v1")
        self.start()
        with patch("chess_harness.batch.run_match") as run:
            with self.assertRaisesRegex(ValueError, "Prompt"):
                self.resume()
        run.assert_not_called()

    def test_terminal_event_recovers_lagging_checkpoint(self):
        self.start()
        game = self.root / "test-batch-000002"
        (game / "summary.json").unlink()
        # Ignore an unfinished append after the complete terminal event.
        with (game / "events.jsonl").open("ab") as stream:
            stream.write(b'{"unfinished":')
        progress_path = self.directory / "progress.json"
        progress = read_json(progress_path)
        progress["games"][1] = {"run_id": "test-batch-000002", "status": "running"}
        progress_path.write_text(json.dumps(progress))
        with patch("chess_harness.batch.run_match") as run:
            result = self.resume()
        run.assert_not_called()
        self.assertEqual(result["games"][1]["summary"]["reason"], "checkmate")

    def test_missing_progress_recovers_existing_attempts(self):
        self.start()
        (self.directory / "progress.json").unlink()
        with patch("chess_harness.batch.run_match") as run:
            self.assertEqual(self.resume()["status"], "completed")
        run.assert_not_called()

    def test_lock_excludes_another_process_and_releases(self):
        self.directory.mkdir(parents=True)
        code = "from pathlib import Path; from chess_harness.storage import batch_lock; import sys\nwith batch_lock(Path(sys.argv[1])): print('acquired')"
        with batch_lock(self.directory):
            blocked = subprocess.run([sys.executable, "-c", code, str(self.directory)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("locked by another process", blocked.stderr)
        released = subprocess.run([sys.executable, "-c", code, str(self.directory)], capture_output=True, text=True, timeout=10)
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_hard_kill_releases_lock_and_preserves_partial_attempt(self):
        config_path = self.root / "child-config.json"
        config_path.write_text(json.dumps(self.config))
        script = '''
import json, sys, time
from pathlib import Path
from unittest.mock import patch
from chess_harness.batch import run_batch
from chess_harness.game import Recorder
root = Path(sys.argv[1])
config = json.loads((root / "child-config.json").read_text())
def play(config, directory, *, batch):
    recorder = Recorder(directory)
    recorder.write("manifest.json", {"config": config, "batch": batch})
    recorder.event("move_requested", fen=config["initial_fen"], ply=1, player="test")
    print("READY_TO_KILL", flush=True)
    time.sleep(60)
with patch("chess_harness.batch.new_run_id", return_value="test-batch"), patch("chess_harness.batch.run_match", play):
    run_batch(config, root, 1)
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()
        def read_output():
            for line in child.stdout:
                lines.put(line)
            lines.put("EOF")
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            while True:
                line = lines.get(timeout=10)
                self.assertNotEqual(line, "EOF", "child exited before creating a partial game")
                if "READY_TO_KILL" in line:
                    break
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            reader.join(timeout=2)
            child.stdout.close()
        before = (self.root / "test-batch-000001/events.jsonl").read_bytes()
        with patch("chess_harness.batch.run_match", side_effect=self.play):
            result = self.resume()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["games"][0]["current_run_id"], "test-batch-000001-attempt-0002")
        self.assertEqual((self.root / "test-batch-000001/events.jsonl").read_bytes(), before)

    def test_resume_rejects_overrides_policy_drift_and_unsafe_paths(self):
        self.start()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            batch.main(["--resume", str(self.directory), "--seed", "20"])
        plan_path = self.directory / "batch.json"
        plan = read_json(plan_path)
        original = deepcopy(plan)
        plan["config"]["prompt_version"] = "future-prompt"
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "policy changed"):
            self.resume()
        original["games"][0]["run_id"] = "../escape"
        plan_path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "schedule"):
            self.resume()

    def test_failed_attempt_is_retried_once_per_resume(self):
        def fail(config, directory, **kwargs):
            self.play(config, directory, **kwargs)
            summary = {"status": "infrastructure_failure", "reason": "service_down", "result": "*"}
            (directory / "summary.json").write_text(json.dumps(summary))
            return summary
        self.start(fail)
        with patch("chess_harness.batch.run_match", side_effect=fail) as run:
            result = self.resume()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["games"][0]["attempts"]), 2)
        self.assertEqual(result["games"][1]["status"], "pending")

    def test_runtime_change_stops_before_any_move_and_closes_engine(self):
        def get(client, path):
            return httpx.Response(200, json={"version": "0.34.0"} if path == "/api/version" else {"models": []},
                                  request=httpx.Request("GET", "http://localhost" + path))
        with patch("httpx.Client.get", get), patch("chess_harness.runner.EnginePlayer") as engine, \
                patch("chess_harness.runner.OllamaPlayer") as model, patch("chess_harness.runner.run_game") as game, \
                redirect_stdout(io.StringIO()):
            engine.return_value.engine.id = {"name": "test"}
            model.return_value.warmup.return_value = {"done": True}
            model.return_value.verify_loaded_context.return_value = {"digest": "new-model", "context_length": 4096}
            result = run_match(self.config, self.root / "changed", expected_identity={"model_digest": "old-model"})
        self.assertEqual(result["reason"], "environment_mismatch")
        self.assertEqual(result["status"], "infrastructure_failure")
        game.assert_not_called()
        engine.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
