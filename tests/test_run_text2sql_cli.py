import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import run_text2sql


class RunText2SQLCliTests(unittest.TestCase):
    @staticmethod
    def _settings():
        return SimpleNamespace(
            agent_token_budget=4096,
            agent_time_budget_seconds=30,
            resolved_llm=lambda: {
                "base_url": "https://example.invalid/v1",
                "api_key": "test-key",
                "model": "test-model",
                "provider": "test",
                "headers": {},
            },
        )

    @staticmethod
    def _failure_argv(root, task_id):
        snapshot_path = RunText2SQLCliTests._snapshot_path()
        return [
            "run_text2sql.py",
            "有多少案例？",
            "--database",
            str(root / "database.sqlite3"),
            "--snapshot",
            str(snapshot_path),
            "--vanna-index-root",
            str(root / "vanna"),
            "--evolution-store",
            str(root / "evolution.sqlite3"),
            "--checkpoint-store",
            str(root / "runtime.sqlite3"),
            "--task-id",
            task_id,
            "--session-id",
            "failure-test",
        ]

    @staticmethod
    def _snapshot_path():
        return (
            run_text2sql.PROJECT_ROOT
            / "artifacts"
            / "text2sql"
            / "schema"
            / "database_snapshot.json"
        )

    def test_lane_task_id_isolated_by_lane_and_policy(self):
        stable = run_text2sql._lane_task_id("request-1", "stable", "policy-v1")
        candidate = run_text2sql._lane_task_id(
            "request-1", "candidate", "policy-v2"
        )
        next_stable = run_text2sql._lane_task_id(
            "request-1", "stable", "policy-v2"
        )

        self.assertEqual(stable, "request-1:stable:policy-v1")
        self.assertEqual(len({stable, candidate, next_stable}), 3)
        with self.assertRaisesRegex(ValueError, "unknown Text2SQL release lane"):
            run_text2sql._lane_task_id("request-1", "other", "policy-v1")

    def test_main_uses_checkpoint_env_and_injects_store_into_both_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(
                json.dumps({"snapshot_id": "snapshot-v1"}), encoding="utf-8"
            )
            database_path = root / "database.sqlite3"
            evolution_path = root / "evolution.sqlite3"
            checkpoint_path = root / "runtime.sqlite3"
            checkpoint_store = object()
            engine_calls = []
            run_calls = []

            class FakeEngine:
                def __init__(self, **kwargs):
                    engine_calls.append(kwargs)
                    self.policy_version = kwargs["policy_version"]
                    self.version_pins = {"policy_version": self.policy_version}
                    self.runtime_identity = {"build_version": "test"}

                def run(self, question, task_id=""):
                    run_calls.append((self.policy_version, question, task_id))
                    return {"status": "success", "answer": {}}

            evolution = Mock()
            evolution.__enter__ = Mock(return_value=evolution)
            evolution.__exit__ = Mock(return_value=False)
            evolution.active_policy_version = "policy-stable"
            evolution.memory_snapshot_id = "memory-v1"
            evolution.runtime_memory_snapshot.return_value = {
                "memory_snapshot_id": "memory-v1",
                "items": {},
            }
            evolution.get_policy.side_effect = lambda version: SimpleNamespace(
                version=version
            )
            evolution.stable_memory = Mock()

            class FakeRelease:
                def __init__(self, store):
                    self.store = store

                def execute(
                    self,
                    question,
                    task_id,
                    stable_runner,
                    candidate_runner_factory,
                    version_pins,
                    evaluation_identity,
                ):
                    self.assertions = (task_id, version_pins, evaluation_identity)
                    stable = stable_runner(question)
                    candidate_runner_factory("policy-candidate")(question)
                    return stable

            settings = SimpleNamespace(
                agent_token_budget=4096,
                agent_time_budget_seconds=30,
                resolved_llm=lambda: {
                    "base_url": "https://example.invalid/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                    "provider": "test",
                    "headers": {},
                },
            )
            argv = [
                "run_text2sql.py",
                "有多少案例？",
                "--database",
                str(database_path),
                "--snapshot",
                str(snapshot_path),
                "--evolution-store",
                str(evolution_path),
                "--task-id",
                "request-0001",
            ]

            with (
                patch.object(run_text2sql.Settings, "from_env", return_value=settings),
                patch.object(run_text2sql, "JsonChatClient", return_value=object()),
                patch.object(
                    run_text2sql,
                    "Text2SQLRuntimeCheckpointStore",
                    return_value=checkpoint_store,
                ) as checkpoint_constructor,
                patch.object(
                    run_text2sql,
                    "Text2SQLEvolutionStore",
                    return_value=evolution,
                ),
                patch.object(run_text2sql, "Text2SQLAgenticEngine", FakeEngine),
                patch.object(run_text2sql, "Text2SQLShadowReleaseManager", FakeRelease),
                patch.dict(
                    "os.environ",
                    {"EVOAGENT_TEXT2SQL_CHECKPOINT_STORE": str(checkpoint_path)},
                ),
                patch("sys.argv", argv),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = run_text2sql.main()

            self.assertEqual(status, 0)
            checkpoint_constructor.assert_called_once_with(checkpoint_path)
            self.assertEqual(len(engine_calls), 2)
            self.assertTrue(
                all(call["checkpoint_store"] is checkpoint_store for call in engine_calls)
            )
            self.assertEqual(
                run_calls,
                [
                    (
                        "policy-stable",
                        "有多少案例？",
                        "request-0001:stable:policy-stable",
                    ),
                    (
                        "policy-candidate",
                        "有多少案例？",
                        "request-0001:candidate:policy-candidate",
                    ),
                ],
            )
            self.assertEqual(json.loads(output.getvalue())["task_id"], "request-0001")

    def test_engine_construction_failure_records_minimal_trace_and_reraises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "request-engine-failure"
            argv = self._failure_argv(root, task_id)

            with (
                patch.object(
                    run_text2sql.Settings,
                    "from_env",
                    return_value=self._settings(),
                ),
                patch.object(
                    run_text2sql,
                    "JsonChatClient",
                    return_value=SimpleNamespace(provider="test", model="test-model"),
                ),
                patch.object(
                    run_text2sql,
                    "VannaRetrieverOnly",
                ) as retriever,
                patch.object(
                    run_text2sql,
                    "Text2SQLAgenticEngine",
                    side_effect=RuntimeError("provider payload must not be persisted"),
                ),
                patch("sys.argv", argv),
            ):
                retriever.current_index_version.return_value = "vanna-v1"
                with self.assertRaisesRegex(RuntimeError, "provider payload"):
                    run_text2sql.main()

            snapshot = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
            with run_text2sql.Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as store:
                trace = store.get_query_trace(task_id)

            self.assertEqual(trace["status"], "error")
            self.assertEqual(trace["question"], "有多少案例？")
            self.assertEqual(trace["origin"], "cli")
            self.assertEqual(trace["source_lane"], "stable")
            self.assertEqual(
                trace["execution"],
                {
                    "last_node": "cli-engine-construction",
                    "error_category": "RuntimeError",
                },
            )
            self.assertEqual(
                trace["version_pins"]["database_snapshot_id"],
                snapshot["snapshot_id"],
            )
            self.assertEqual(trace["version_pins"]["vanna_index_version"], "vanna-v1")
            self.assertTrue(trace["version_pins"]["memory_snapshot_id"])
            self.assertTrue(trace["version_pins"]["policy_version"])
            self.assertNotIn("provider payload", json.dumps(trace, ensure_ascii=False))

    def test_release_failure_records_engine_pins_and_reraises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "request-release-failure"
            argv = self._failure_argv(root, task_id)
            engine_pins = {
                "database_snapshot_id": "snapshot-from-engine",
                "wiki_index_version": "wiki-from-engine",
                "vanna_index_version": "vanna-from-engine",
                "memory_snapshot_id": "memory-from-engine",
                "policy_version": "policy-from-engine",
            }

            class FakeEngine:
                def __init__(self, **kwargs):
                    self.policy_version = kwargs["policy_version"]
                    self.version_pins = dict(engine_pins)
                    self.runtime_identity = {"build_version": "test"}

            class FailingRelease:
                def __init__(self, _store):
                    pass

                def execute(self, *_args, **_kwargs):
                    raise ValueError("release payload must not be persisted")

            with (
                patch.object(
                    run_text2sql.Settings,
                    "from_env",
                    return_value=self._settings(),
                ),
                patch.object(
                    run_text2sql,
                    "JsonChatClient",
                    return_value=SimpleNamespace(provider="test", model="test-model"),
                ),
                patch.object(
                    run_text2sql.VannaRetrieverOnly,
                    "current_index_version",
                    return_value="vanna-v1",
                ),
                patch.object(run_text2sql, "Text2SQLAgenticEngine", FakeEngine),
                patch.object(
                    run_text2sql,
                    "Text2SQLShadowReleaseManager",
                    FailingRelease,
                ),
                patch("sys.argv", argv),
            ):
                with self.assertRaisesRegex(ValueError, "release payload"):
                    run_text2sql.main()

            snapshot = json.loads(self._snapshot_path().read_text(encoding="utf-8"))
            with run_text2sql.Text2SQLEvolutionStore(
                root / "evolution.sqlite3", snapshot
            ) as store:
                trace = store.get_query_trace(task_id)

            self.assertEqual(trace["status"], "error")
            self.assertEqual(
                trace["execution"],
                {
                    "last_node": "cli-release-execution",
                    "error_category": "ValueError",
                },
            )
            self.assertEqual(trace["version_pins"], engine_pins)
            self.assertNotIn("release payload", json.dumps(trace, ensure_ascii=False))

    def test_failure_recording_never_replaces_the_original_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = self._failure_argv(root, "request-side-effect-failure")

            with (
                patch.object(
                    run_text2sql.Settings,
                    "from_env",
                    return_value=self._settings(),
                ),
                patch.object(run_text2sql, "JsonChatClient", return_value=object()),
                patch.object(
                    run_text2sql.VannaRetrieverOnly,
                    "current_index_version",
                    return_value="vanna-v1",
                ),
                patch.object(
                    run_text2sql,
                    "Text2SQLAgenticEngine",
                    side_effect=KeyError("original engine failure"),
                ),
                patch.object(
                    run_text2sql,
                    "finalize_run",
                    side_effect=RuntimeError("trace sink failure"),
                ),
                patch("sys.argv", argv),
            ):
                with self.assertRaisesRegex(KeyError, "original engine failure"):
                    run_text2sql.main()


if __name__ == "__main__":
    unittest.main()
