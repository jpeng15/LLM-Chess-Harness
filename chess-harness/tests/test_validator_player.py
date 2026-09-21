"""Frozen-validator game tests with injected artifacts and sandbox results only."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import chess

from chess_harness import batch
from chess_harness.cli import add_game_arguments, game_config
from chess_harness.game import Recorder, run_game
from chess_harness.limits import PlayerFailure
from chess_harness.players import OllamaPlayer, prompt_version
from chess_harness.runner import run_match
from chess_harness.storage import events, runtime_identity
from chess_harness.tool_player import RulesToolPlayer
from chess_harness.validator_artifacts import ArtifactError, FrozenValidator
from chess_harness.validator_contract import CONTRACT_VERSION
from chess_harness.validator_player import AuthoredSession, AuthoredValidatorPlayer, configured_artifact, validation_request
from chess_harness.validator_rules import RulesBoard


ARTIFACT = FrozenValidator("f" * 64, b"# Inert test bytes: never executed.\r\n", {
    "source_sha256": "d" * 64, "image": "sha256:" + "a" * 64,
    "docker_context": "desktop-linux", "setup_costs": {"generation": {"calls": 1}}})
CONFIG = {"model": "test", "url": "http://localhost:11434", "mode": "authored-validator",
          "think": False, "tokens": 256, "context": 16384, "seconds": 10, "seed": 0, "temperature": 0,
          "tools": {"version": "authored-tools-v1", "calls": 4, "depth": 4, "minimum_calls": 1,
                    "output_budget": "per-turn"},
          "validator": {"enabled": True, "artifact": "unused-frozen-directory", "artifact_id": ARTIFACT.artifact_id}}
EMPTY = {"contract_version": CONTRACT_VERSION, "facts": [], "heuristics": []}


def validate(move="e2e4"):
    return {"action": "validate", "move": move}


def play(move="e2e4"):
    return {"action": "play", "move": move}


def simulate(position=0, move="e2e4"):
    return {"action": "simulate", "position": position, "move": move}


def response(action, count=20, reason="stop"):
    return {"done": True, "done_reason": reason, "eval_count": count, "prompt_eval_count": 100,
            "message": {"content": json.dumps(action)}}


def action_kinds(schema):
    return {item["properties"]["action"]["enum"][0] for item in schema["anyOf"]}


class FakeBackend:
    def __init__(self, result=None, handler=None):
        self.calls = []
        self.handler = handler
        self.result = result if result is not None else {
            "status": "ok", "reason": "completed", "findings": deepcopy(EMPTY), "cpu_seconds": 0.01,
            "peak_memory_bytes": 1000000, "execution_wall_seconds": 0.03, "worker_wall_seconds": 0.02,
            "cleanup_seconds": 0.005, "cleanup_confirmed": True, "stderr": "retained diagnostics"}

    def run_prepared(self, source, request, *, deadline, cancel_event):
        self.calls.append({"source": source, "request": deepcopy(request), "deadline": deadline, "cancel_event": cancel_event})
        return self.handler(source, request, deadline, cancel_event) if self.handler else deepcopy(self.result)


class AuthoredSessionTests(unittest.TestCase):
    def test_mandatory_validation_reserves_last_shared_call(self):
        session = AuthoredSession(chess.Board(), calls=2, depth=4)
        self.assertEqual(action_kinds(session.schema()), {"simulate", "validate"})
        with self.assertRaises(ValueError):
            session.validate(play())
        session.simulate(simulate())
        self.assertEqual(session.used, 1)
        self.assertEqual(action_kinds(session.schema()), {"validate"})
        with self.assertRaises(ValueError):
            session.simulate(simulate(1, "e7e5"))
        session.validate(validate("d2d4"))
        session.used += 1
        session.validated = True
        self.assertEqual(action_kinds(session.schema()), {"play"})
        session.validate(play("g1f3"))
        with self.assertRaises(ValueError):
            session.validate(validate())

    def test_one_call_allows_only_validation_then_root_play(self):
        session = AuthoredSession(chess.Board(), calls=1, depth=1)
        self.assertEqual(action_kinds(session.schema()), {"validate"})
        session.validate(validate())
        session.used, session.validated = 1, True
        session.validate(play("d2d4"))
        self.assertEqual(action_kinds(session.schema()), {"play"})

    def test_schema_rejects_extra_fields_bool_ids_branch_validation_and_illegal_moves(self):
        session = AuthoredSession(chess.Board(), calls=4, depth=1)
        for action in (None, [], {**validate(), "position": 0}, validate("e2e5"),
                       {**simulate(), "position": False}, simulate(100), {**simulate(), "extra": 1}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                session.validate(action)
        session.simulate(simulate())
        with self.assertRaises(ValueError):
            session.validate(simulate(1, "e7e5"))  # Depth limit applies independently.
        with self.assertRaises(ValueError):
            session.validate(validate("e7e5"))  # Always real position 0, not branch 1.


class AuthoredValidatorPlayerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.index = 0

    def player(self, backend=None, config=None):
        self.index += 1
        self.recorder = Recorder(self.root / f"game-{self.index}", mode="authored-validator")
        self.recorder.write("manifest.json", {"config": {"mode": "authored-validator", "llm": config or CONFIG, "llm_color": "white"}})
        return AuthoredValidatorPlayer(config or CONFIG, ARTIFACT, backend or FakeBackend(), self.recorder.event)

    def run_turn(self, actions, *, backend=None, config=None, board=None, loaded=None):
        player = self.player(backend, config)
        post = actions if callable(actions) else AsyncMock(side_effect=[response(action) for action in actions])
        with patch.object(player, "_post", post), patch("chess_harness.validator_player.load_artifact", **(
                {"side_effect": loaded} if loaded else {"return_value": ARTIFACT})), redirect_stdout(io.StringIO()):
            summary = run_game(board or chess.Board(), {chess.WHITE: player, chess.BLACK: type("UnusedOpponent", (), {"name": "opponent"})()},
                               chess.WHITE, 1, self.recorder)
        return summary, post, player

    def test_mode_and_enable_gates_do_not_read_artifacts_or_start_backend(self):
        backend = Mock()
        invalid = [{**CONFIG, "validator": {**CONFIG["validator"], "enabled": False}},
                   {**CONFIG, "validator": {}}, {**CONFIG, "mode": "unassisted"}]
        with patch("chess_harness.validator_player.load_artifact", side_effect=AssertionError("no artifact I/O")):
            for config in invalid:
                with self.subTest(config=config), self.assertRaises(PlayerFailure) as raised:
                    configured_artifact(config)
                self.assertEqual(raised.exception.reason, "validator_disabled")
                with self.assertRaises(ValueError):
                    AuthoredValidatorPlayer(config, ARTIFACT, backend)
        backend.run_prepared.assert_not_called()

    def test_validate_then_different_move_is_selected_by_model_and_applied_only_by_referee(self):
        backend = FakeBackend()
        summary, post, _ = self.run_turn([validate(), play("d2d4")], backend=backend)
        self.assertEqual((summary["status"], summary["reason"], summary["plies"]), ("truncated", "max_plies", 1))
        self.assertEqual(chess.Board(summary["final_fen"]).piece_at(chess.D4).symbol(), "P")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.calls[0]["request"]["candidate"], "e2e4")
        self.assertEqual(backend.calls[0]["source"], ARTIFACT.source)
        self.assertEqual(post.await_count, 2)
        rows = events(self.recorder.directory)
        self.assertEqual(len([row for row in rows if row["type"] == "validator_result"]), 1)
        self.assertEqual(len([row for row in rows if row["type"] == "move_applied"]), 1)

    def test_nested_simulations_preserve_real_board_and_validation_gets_full_root_history(self):
        board = chess.Board()
        for move in ("e2e4", "c7c5"):
            board.push_uci(move)
        before, history = board.fen(en_passant="fen"), list(board.move_stack)
        backend = FakeBackend()
        player = self.player(backend)
        actions = iter([simulate(0, "d2d4"), simulate(1, "c5d4"), validate("g1f3"), play("b1c3")])
        requests = []
        async def post(path, request, seconds):
            requests.append(deepcopy(request))
            return response(next(actions))
        with patch.object(player, "_post", post), patch("chess_harness.validator_player.load_artifact", return_value=ARTIFACT):
            reply = player.choose(board)
        self.assertEqual(reply.text, "b1c3")
        self.assertEqual(board.fen(en_passant="fen"), before)
        self.assertEqual(board.move_stack, history)
        supplied = backend.calls[0]["request"]
        self.assertEqual(supplied["history"]["moves"], ["e2e4", "c7c5"])
        self.assertEqual(supplied["position"]["fen"], before)
        self.assertEqual(RulesBoard.from_request(supplied).fen(), board.fen())
        self.assertEqual([body["options"]["num_predict"] for body in requests], [256, 236, 216, 196])
        self.assertIn('"position": 2', requests[2]["messages"][-1]["content"])
        self.assertIn("facts verified; heuristics unverified", requests[-1]["messages"][-1]["content"])
        self.assertIn('"attacks_on_occupied_squares"', requests[0]["messages"][1]["content"])

    def test_validation_request_retains_draw_relevant_history_and_custom_start(self):
        board = chess.Board("7k/8/8/8/8/8/8/R6K w - - 0 21")
        board.push_uci("a1a2")
        board.push_uci("h8g8")
        value = validation_request(board, "a2a1")
        self.assertEqual(value["history"]["initial_fen"], board.root().fen(en_passant="fen"))
        self.assertEqual(value["history"]["moves"], ["a1a2", "h8g8"])
        self.assertEqual(RulesBoard.from_request(value).fen(), board.fen())
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6"):
            board.push_uci(move)
        reconstructed = RulesBoard.from_request(validation_request(board, "f3g1"))
        reconstructed.push("f3g1")
        self.assertEqual(reconstructed.outcome()["reason"], "threefold_repetition")

    def test_shared_calls_include_validations_and_simulations_without_hidden_extra_budget(self):
        requests = []
        actions = iter([simulate(), validate("d2d4"), play("g1f3")])
        async def post(path, request, seconds):
            requests.append(deepcopy(request))
            return response(next(actions))
        summary, _, _ = self.run_turn(post, config={**CONFIG, "tools": {**CONFIG["tools"], "calls": 2}})
        self.assertEqual(summary["plies"], 1)
        self.assertEqual([action_kinds(body["format"]) for body in requests],
                         [{"simulate", "validate"}, {"validate"}, {"play"}])
        requests.clear()
        actions = iter([validate(), simulate(), play()])
        summary, _, _ = self.run_turn(post, config={**CONFIG, "tools": {**CONFIG["tools"], "calls": 2}})
        self.assertEqual(summary["plies"], 1)
        self.assertEqual(action_kinds(requests[-1]["format"]), {"play"})

    def test_bad_actions_never_invoke_validator_and_forfeit_under_existing_referee_policy(self):
        for action in (play(), validate("e2e5"), {**validate(), "position": 0}, simulate(False),
                       {"action": "evaluate", "move": "e2e4"}):
            with self.subTest(action=action):
                backend = FakeBackend()
                summary, post, _ = self.run_turn([action], backend=backend)
                self.assertEqual((summary["status"], summary["reason"]), ("forfeit", "invalid_tool_action"))
                self.assertEqual(summary["plies"], 0)
                self.assertFalse(backend.calls)
                self.assertEqual(post.await_count, 1)
        raw = response(validate())
        raw["message"]["content"] = '{"action":"validate","move":"e2e4","move":"d2d4"}'
        summary, _, _ = self.run_turn(AsyncMock(return_value=raw))
        self.assertEqual(summary["reason"], "invalid_tool_action")

    def test_output_limits_precede_validation_and_budget_is_shared_across_calls(self):
        for count, reason in ((256, "length"), (257, "stop")):
            backend = FakeBackend()
            summary, post, _ = self.run_turn(AsyncMock(return_value=response(validate(), count, reason)), backend=backend)
            self.assertEqual(summary["reason"], "output_limit")
            self.assertEqual(post.await_count, 1)
            self.assertFalse(backend.calls)
        raw = response(validate())
        raw.pop("eval_count")
        backend = FakeBackend()
        summary, _, _ = self.run_turn(AsyncMock(return_value=raw), backend=backend)
        self.assertEqual((summary["status"], summary["reason"]), ("infrastructure_failure", "tool_usage_missing"))
        self.assertFalse(backend.calls)
        summary, post, _ = self.run_turn(AsyncMock(side_effect=[response(validate(), 250), response(play(), 7)]))
        self.assertEqual(summary["reason"], "output_limit")
        self.assertEqual(post.await_count, 2)
        self.assertEqual(summary["plies"], 0)

    def test_independently_false_finding_rejects_entire_result_but_keeps_costs_and_diagnostics(self):
        result = deepcopy(FakeBackend().result)
        result["findings"]["facts"] = [{"id": "false", "kind": "candidate_checkmate", "line": ["e2e4"]}]
        summary, post, _ = self.run_turn([validate(), play()], backend=FakeBackend(result))
        self.assertEqual((summary["status"], summary["reason"], summary["result"]),
                         ("infrastructure_failure", "validator_invalid_finding", "*"))
        self.assertEqual(post.await_count, 1)
        execution = next(row["execution"] for row in events(self.recorder.directory) if row["type"] == "validator_result")
        self.assertNotIn("findings", execution)
        self.assertEqual(execution["cpu_seconds"], 0.01)
        self.assertGreaterEqual(execution["artifact_check_seconds"], 0)
        self.assertEqual(execution["stderr"], "retained diagnostics")
        self.assertTrue(execution["cleanup_confirmed"])
        self.assertIn("path", execution)

    def test_validator_errors_are_unscored_infrastructure_failures_without_retry_or_fallback(self):
        for reason in ("syntax_error", "code_error", "timeout", "memory_limit", "output_limit", "cleanup_failed"):
            with self.subTest(reason=reason):
                backend = FakeBackend({"status": "error", "reason": reason, "cpu_seconds": 0.01,
                                       "stderr": "partial", "cleanup_confirmed": reason != "cleanup_failed"})
                summary, post, _ = self.run_turn([validate(), play()], backend=backend)
                self.assertEqual((summary["status"], summary["result"], summary["plies"]), ("infrastructure_failure", "*", 0))
                self.assertEqual(summary["reason"], "validator_" + reason)
                self.assertEqual((post.await_count, len(backend.calls)), (1, 1))

    def test_turn_timeout_signals_cancellation_and_persists_cleanup_report_before_return(self):
        cancellation_observed = threading.Event()
        def until_cancel(source, request, deadline, cancel):
            if not cancel.wait(1):
                raise RuntimeError("test did not observe cooperative cancellation")
            cancellation_observed.set()
            return {"status": "error", "reason": "cancelled", "cleanup_confirmed": True,
                    "cpu_seconds": 0.01, "stderr": "diagnostic after cancellation", "cleanup_seconds": 0.005}
        summary, post, _ = self.run_turn([validate()], backend=FakeBackend(handler=until_cancel),
                                        config={**CONFIG, "seconds": 0.05})
        self.assertTrue(cancellation_observed.is_set())
        self.assertEqual((summary["status"], summary["reason"]), ("infrastructure_failure", "validator_timeout"))
        execution = next(row["execution"] for row in events(self.recorder.directory) if row["type"] == "validator_result")
        self.assertTrue(execution["cleanup_confirmed"])
        self.assertTrue(execution["turn_deadline_exceeded"])
        self.assertEqual(execution["stderr"], "diagnostic after cancellation")
        self.assertEqual(execution["cpu_seconds"], 0.01)
        self.assertEqual(post.await_count, 1)

    def test_cleanup_failure_is_not_hidden_by_an_expired_turn_deadline(self):
        def fail_cleanup(source, request, deadline, cancel):
            if not cancel.wait(1):
                raise RuntimeError("cancellation missing")
            return {"status": "error", "reason": "cleanup_failed", "cleanup_confirmed": False,
                    "execution_reason": "cancelled", "stderr": "container removal unconfirmed"}
        summary, _, _ = self.run_turn([validate()], backend=FakeBackend(handler=fail_cleanup), config={**CONFIG, "seconds": 0.05})
        self.assertEqual(summary["reason"], "validator_cleanup_failed")
        execution = next(row["execution"] for row in events(self.recorder.directory) if row["type"] == "validator_result")
        self.assertFalse(execution["cleanup_confirmed"])
        self.assertTrue(execution["turn_deadline_exceeded"])
        self.assertEqual(execution["execution_reason"], "cancelled")

    def test_artifact_is_reloaded_before_every_validation_and_tampering_stops_before_execution(self):
        backend = FakeBackend()
        summary, post, _ = self.run_turn([validate(), validate("d2d4"), play()], backend=backend,
                                        loaded=[ARTIFACT, ArtifactError("artifact_mismatch", "tampered provenance")])
        self.assertEqual(summary["reason"], "validator_artifact_mismatch")
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual((post.await_count, len(backend.calls)), (2, 1))
        results = [row for row in events(self.recorder.directory) if row["type"] == "validator_result"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[-1]["execution"]["reason"], "artifact_mismatch")
        self.assertTrue(all(row["execution"]["artifact_check_seconds"] >= 0 for row in results))

    def test_malformed_backend_reports_and_unexpected_exceptions_emit_explicit_failed_results(self):
        for value in (None, ["unexpected report"], {"status": "error", "reason": None},
                      {"status": "unknown", "reason": "completed"},
                      TypeError("backend protocol error"), AttributeError("bad field")):
            def malformed(source, request, deadline, cancel):
                if isinstance(value, BaseException):
                    raise value
                return value
            with self.subTest(value=value):
                summary, post, _ = self.run_turn([validate(), play()], backend=FakeBackend(handler=malformed))
                self.assertEqual((summary["status"], summary["reason"]),
                                 ("infrastructure_failure", "validator_execution_error"))
                rows = [row for row in events(self.recorder.directory) if row["type"] == "validator_result"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["execution"]["reason"], "execution_error")
                self.assertNotIn("findings", rows[0]["execution"])
                self.assertEqual(post.await_count, 1)

    def test_baseline_requests_and_policy_remain_distinct(self):
        baseline = {key: value for key, value in CONFIG.items() if key not in {"validator", "tools"}}
        with patch("chess_harness.validator_player.load_artifact", side_effect=AssertionError("baseline has no artifact access")):
            constrained = OllamaPlayer({**baseline, "mode": "constrained-legal"}).request(chess.Board())
            rules = RulesToolPlayer({**baseline, "mode": "rules-tools", "tools": {**CONFIG["tools"], "version": "rules-tools-v1"}}).request(chess.Board())
        self.assertEqual(constrained["format"]["required"], ["move"])
        self.assertEqual(action_kinds(rules["format"]), {"simulate"})
        self.assertNotIn("authored validator", rules["messages"][0]["content"])


class AuthoredIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = self.root / "engine"
        self.engine.write_bytes(b"fake engine identity")
        self.parser = argparse.ArgumentParser()
        add_game_arguments(self.parser)

    def config(self):
        with patch("chess_harness.validator_artifacts.load_artifact", return_value=ARTIFACT):
            return game_config(self.parser, self.parser.parse_args([
                "--engine", str(self.engine), "--mode", "authored-validator", "--enable-authored-validators",
                "--validator-artifact", str(self.root / "frozen"), "--no-think"]))

    def test_cli_gates_before_artifact_or_engine_io_and_records_frozen_identity(self):
        for args in (["--mode", "authored-validator"], ["--mode", "authored-validator", "--validator-artifact", "missing"],
                     ["--enable-authored-validators"], ["--mode", "rules-tools", "--validator-artifact", "missing"]):
            with self.subTest(args=args), patch("pathlib.Path.is_file", side_effect=AssertionError("no engine I/O")), \
                    patch("chess_harness.validator_artifacts.load_artifact", side_effect=AssertionError("no artifact I/O")), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                game_config(self.parser, self.parser.parse_args(args))
        config = self.config()
        self.assertEqual(config["validator"]["artifact_id"], ARTIFACT.artifact_id)
        self.assertTrue(config["validator"]["enabled"])
        self.assertEqual(config["tools"]["version"], "authored-tools-v1")
        self.assertEqual(config["tools"]["calls"], 4)
        self.assertEqual(config["llm"]["context"], 16384)
        self.assertEqual(config["prompt_version"], prompt_version("authored-validator"))

    def test_runner_failed_sandbox_preparation_never_starts_model_or_opponent(self):
        config = self.config()
        with patch("chess_harness.validator_player.load_artifact", return_value=ARTIFACT), \
                patch("chess_harness.validator_sandbox.DockerSandbox") as sandbox, \
                patch("chess_harness.runner.httpx.Client") as client, patch("chess_harness.runner.EnginePlayer") as engine, \
                redirect_stdout(io.StringIO()):
            sandbox.return_value.prepare.return_value = {"status": "error", "reason": "runtime_unavailable", "message": "not installed"}
            summary = run_match(config, self.root / "run")
        self.assertEqual((summary["status"], summary["reason"], summary["result"]),
                         ("infrastructure_failure", "validator_runtime_unavailable", "*"))
        client.assert_not_called()
        engine.assert_not_called()
        manifest = json.loads((self.root / "run" / "manifest.json").read_text())
        self.assertEqual(manifest["validator_artifact"]["artifact_id"], ARTIFACT.artifact_id)
        self.assertEqual(manifest["validator_artifact"]["source_text"], ARTIFACT.source.decode())
        self.assertTrue(any(row["type"] == "validator_preflight" for row in events(self.root / "run")))

    def test_runner_initializes_authored_player_and_plays_one_verified_turn_with_complete_records(self):
        config = self.config()
        config["max_plies"] = 1
        development = {"schema_version": 1, "status": "passed", "selected_attempt": 1,
                       "attempts": [{"index": 1, "status": "passed"}]}
        artifact = FrozenValidator(ARTIFACT.artifact_id, ARTIFACT.source, ARTIFACT.manifest, development)
        backend = FakeBackend()
        backend.prepare = Mock(return_value={"status": "ok", "reason": "ready", "checks": []})
        version_response = Mock()
        version_response.json.return_value = {"version": "0.34.1"}
        tags_response = Mock()
        tags_response.json.return_value = {"models": [{"name": config["llm"]["model"], "digest": "b" * 64}]}
        loaded = {"name": config["llm"]["model"], "digest": "b" * 64, "context_length": config["llm"]["context"]}
        with patch("chess_harness.validator_player.load_artifact", return_value=artifact) as load, \
                patch("chess_harness.validator_sandbox.DockerSandbox", return_value=backend) as sandbox, \
                patch("chess_harness.runner.httpx.Client") as http, \
                patch("chess_harness.runner.EnginePlayer") as engine, \
                patch.object(AuthoredValidatorPlayer, "warmup", return_value={"done": True}) as warmup, \
                patch.object(AuthoredValidatorPlayer, "verify_loaded_context", return_value=loaded) as context, \
                patch.object(AuthoredValidatorPlayer, "_post", new_callable=AsyncMock,
                             side_effect=[response(validate()), response(play("d2d4"))]) as post, \
                redirect_stdout(io.StringIO()):
            http.return_value.__enter__.return_value.get.side_effect = [version_response, tags_response]
            engine.return_value.name = "Fake opponent"
            engine.return_value.engine.id = {"name": "Fake opponent", "author": "test"}
            summary = run_match(config, self.root / "success")
        self.assertEqual((summary["status"], summary["reason"], summary["plies"]), ("truncated", "max_plies", 1))
        expected_board = chess.Board()
        expected_board.push_uci("d2d4")
        self.assertEqual(summary["final_fen"], expected_board.fen())
        self.assertEqual(post.await_count, 2)
        warmup.assert_called_once_with()
        context.assert_called_once_with()
        self.assertEqual(load.call_count, 2)  # Initialization and the actual candidate check.
        backend.prepare.assert_called_once_with()
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.calls[0]["source"], artifact.source)
        settings = sandbox.call_args.args[0]
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.image, artifact.manifest["image"])
        self.assertEqual(settings.docker_context, artifact.manifest["docker_context"])
        engine.assert_called_once_with(config["engine"])
        engine.return_value.close.assert_called_once_with()
        engine.return_value.choose.assert_not_called()
        engine.return_value.engine.play.assert_not_called()
        engine.return_value.engine.analyse.assert_not_called()
        manifest = json.loads((self.root / "success" / "manifest.json").read_text())
        self.assertEqual(manifest["config"]["mode"], "authored-validator")
        self.assertEqual(manifest["validator_artifact"]["development"], development)
        self.assertEqual(manifest["validator_artifact"]["source_text"], artifact.source.decode())
        self.assertEqual(manifest["validator_artifact"]["manifest"], artifact.manifest)
        self.assertEqual(manifest["validator_artifact"]["setup_costs"], artifact.manifest["setup_costs"])
        identity = runtime_identity(manifest)
        self.assertEqual(identity["validator_artifact_id"], artifact.artifact_id)
        self.assertEqual(identity["model_digest"], loaded["digest"])
        rows = events(self.root / "success")
        requested = next(row for row in rows if row["type"] == "validator_requested")
        result = next(row for row in rows if row["type"] == "validator_result")
        self.assertEqual(requested["input"]["candidate"], "e2e4")
        self.assertEqual(requested["input"]["history"]["moves"], [])
        self.assertEqual(result["input"], requested["input"])
        self.assertEqual(result["execution"]["status"], "ok")
        self.assertEqual(result["execution"]["findings"], EMPTY)
        self.assertEqual(result["execution"]["cpu_seconds"], 0.01)
        self.assertEqual(result["execution"]["cleanup_seconds"], 0.005)
        self.assertGreaterEqual(result["execution"]["artifact_check_seconds"], 0)
        self.assertEqual(sum(row["type"] == "move_applied" for row in rows), 1)
        self.assertEqual(json.loads((self.root / "success" / "summary.json").read_text()), summary)

    def test_runtime_identity_adds_artifact_without_changing_baseline_identity(self):
        manifest = {"loaded_model": {"digest": "model"}, "engine_sha256": "engine", "ollama_version": {"version": "0.34.1"},
                    "packages": {"chess": "1.11.2"}, "python": "3.12"}
        baseline = runtime_identity(manifest)
        self.assertNotIn("validator_artifact_id", baseline)
        manifest["validator_artifact"] = {"artifact_id": ARTIFACT.artifact_id}
        self.assertEqual(runtime_identity(manifest), {**baseline, "validator_artifact_id": ARTIFACT.artifact_id})

    def test_validator_infrastructure_failure_stops_batch_and_tampered_resume_cannot_start_game(self):
        config = self.config()
        def failed(config, directory, *, batch, **kwargs):
            record = Recorder(directory, mode="authored-validator")
            record.write("manifest.json", {"config": config, "batch": batch})
            summary = {"status": "infrastructure_failure", "reason": "validator_code_error", "result": "*", "plies": 0}
            record.write("summary.json", summary)
            return summary
        with patch("chess_harness.batch.new_run_id", return_value="validator-test"), \
                patch("chess_harness.validator_player.load_artifact", return_value=ARTIFACT), \
                patch("chess_harness.batch.run_match", side_effect=failed) as run, redirect_stdout(io.StringIO()):
            progress = batch.run_batch(config, self.root, pairs=1)
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["games"][1]["status"], "pending")
        run.assert_called_once()
        with patch("chess_harness.validator_player.load_artifact", side_effect=ArtifactError("artifact_mismatch", "tamper")), \
                patch("chess_harness.batch.run_match") as run, redirect_stdout(io.StringIO()):
            progress = batch.resume_batch(self.root / "batches" / "validator-test")
        run.assert_not_called()
        self.assertEqual((progress["status"], progress["reason"]), ("failed", "validator_artifact_mismatch"))
        self.assertEqual(json.loads((self.root / "batches" / "validator-test" / "progress.json").read_text()), progress)

    def test_completed_authored_batch_resume_still_verifies_frozen_artifact(self):
        config = self.config()
        def completed(config, directory, *, batch, **kwargs):
            record = Recorder(directory, mode="authored-validator")
            record.write("manifest.json", {"config": config, "batch": batch})
            summary = {"status": "completed", "reason": "checkmate", "result": "0-1", "plies": 2}
            record.write("summary.json", summary)
            return summary
        with patch("chess_harness.batch.new_run_id", return_value="completed-validator-test"), \
                patch("chess_harness.validator_player.load_artifact", return_value=ARTIFACT), \
                patch("chess_harness.batch.run_match", side_effect=completed), redirect_stdout(io.StringIO()):
            progress = batch.run_batch(config, self.root, pairs=1)
        self.assertEqual(progress["status"], "completed")
        with patch("chess_harness.validator_player.load_artifact", side_effect=ArtifactError("artifact_mismatch", "tamper")), \
                patch("chess_harness.batch.run_match") as run, redirect_stdout(io.StringIO()):
            progress = batch.resume_batch(self.root / "batches" / "completed-validator-test")
        self.assertEqual((progress["status"], progress["reason"]), ("failed", "validator_artifact_mismatch"))
        self.assertEqual([game["status"] for game in progress["games"]], ["completed", "completed"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
