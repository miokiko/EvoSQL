import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from evoagent.runtime import AgentRuntime, RuntimeNode
from evoagent.text2sql.checkpoint_store import (
    Text2SQLCheckpointBusy,
    Text2SQLCheckpointCorruptionError,
    Text2SQLCheckpointIdentityError,
    Text2SQLRuntimeCheckpointStore,
)


class Text2SQLRuntimeCheckpointStoreTests(unittest.TestCase):
    def test_runtime_rejects_duplicate_node_names_before_execution(self):
        calls = []
        runtime = AgentRuntime(max_steps=2, timeout_seconds=5)

        def nodes():
            yield RuntimeNode(
                "duplicate", lambda _state: calls.append("first") or {}
            )
            yield RuntimeNode(
                "duplicate", lambda _state: calls.append("second") or {}
            )

        with self.assertRaisesRegex(ValueError, "node names must be unique"):
            runtime.execute({}, nodes())
        self.assertEqual(calls, [])

    def test_runtime_rejects_checkpoint_bound_graph_sequence_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = Text2SQLRuntimeCheckpointStore(path)
            identity = {
                "question_sha256": "q1",
                "runtime": {"nodes": ["n1", "n2"]},
            }
            session = store.acquire("task-1", identity)
            calls = []
            runtime = AgentRuntime(max_steps=2, timeout_seconds=5)

            class Adapter:
                def __init__(self, value):
                    self.session = value

            with self.assertRaisesRegex(ValueError, "checkpoint-bound graph"):
                runtime.execute(
                    {},
                    (
                        RuntimeNode(
                            "n1", lambda _state: calls.append("n1") or {}
                        ),
                        RuntimeNode(
                            "different", lambda _state: calls.append("different") or {}
                        ),
                    ),
                    task_id="task-1",
                    checkpoint_store=Adapter(session),
                )
            self.assertEqual(calls, [])
            self.assertEqual(store.inspect("task-1")["checkpoint_count"], 0)
            session.fail("graph mismatch", {})

    def test_runtime_accepts_exact_checkpoint_bound_graph_and_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Text2SQLRuntimeCheckpointStore(
                Path(directory) / "runtime.sqlite3"
            )
            identity = {
                "question_sha256": "q1",
                "runtime": {"nodes": ["n1", "n2"]},
            }
            session = store.acquire("task-1", identity)
            runtime = AgentRuntime(max_steps=2, timeout_seconds=5)

            state = runtime.execute(
                {},
                (
                    RuntimeNode("n1", lambda _state: {"value": 1}),
                    RuntimeNode("n2", lambda value: {"value": value["value"] + 1}),
                ),
                task_id="task-1",
                checkpoint_store=session,
            )
            session.complete({"status": "success", "value": state["value"]}, {})

            self.assertEqual(state["value"], 2)
            self.assertEqual(store.inspect("task-1")["status"], "completed")

    def test_failed_run_releases_lease_and_restores_node_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            identity = {"question_sha256": "q1", "runtime": {"nodes": ["n1"]}}
            store = Text2SQLRuntimeCheckpointStore(path)
            session = store.acquire("task-1", identity)
            execution = {"llm_calls": 1, "model_call_log": [], "tool_call_log": []}
            session.save_checkpoint(
                "task-1", "n1", {"answer": 1}, execution=execution
            )
            session.fail("interrupted", execution)

            restarted = Text2SQLRuntimeCheckpointStore(path)
            resumed = restarted.acquire("task-1", identity)
            self.assertEqual(
                resumed.load_checkpoints("task-1")["n1"]["state"],
                {"answer": 1},
            )
            self.assertEqual(resumed.execution["llm_calls"], 1)
            resumed.complete({"status": "success"}, execution)

            cached = restarted.acquire("task-1", identity)
            self.assertEqual(cached.cached_result, {"status": "success"})
            self.assertEqual(restarted.inspect("task-1")["status"], "completed")

    def test_active_lease_prevents_concurrent_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Text2SQLRuntimeCheckpointStore(
                Path(directory) / "runtime.sqlite3"
            )
            identity = {"question_sha256": "q1"}
            first = store.acquire("task-1", identity)
            with self.assertRaises(Text2SQLCheckpointBusy):
                store.acquire("task-1", identity)
            first.fail("cancelled", {})
            replacement = store.acquire("task-1", identity)
            replacement.fail("cancelled again", {})

    def test_task_id_reuse_with_identity_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Text2SQLRuntimeCheckpointStore(
                Path(directory) / "runtime.sqlite3"
            )
            session = store.acquire("task-1", {"question_sha256": "q1"})
            session.fail("interrupted", {})
            with self.assertRaises(Text2SQLCheckpointIdentityError):
                store.acquire("task-1", {"question_sha256": "q2"})

    def test_expired_process_lease_can_be_taken_over(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = Text2SQLRuntimeCheckpointStore(path)
            identity = {"question_sha256": "q1"}
            abandoned = store.acquire("task-1", identity)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE text2sql_checkpoint_runs SET lease_expires_at=0"
                )
            replacement = store.acquire("task-1", identity)
            with self.assertRaises(Text2SQLCheckpointBusy):
                abandoned.save_checkpoint("task-1", "n1", {})
            replacement.fail("replacement finished", {})

    def test_tampered_state_and_non_prefix_nodes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = Text2SQLRuntimeCheckpointStore(path)
            identity = {
                "question_sha256": "q1",
                "runtime": {"nodes": ["n1", "n2"]},
            }
            session = store.acquire("tampered", identity)
            session.save_checkpoint("tampered", "n1", {"answer": 1})
            session.fail("interrupted", {})
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE text2sql_runtime_checkpoints SET state_json=? "
                    "WHERE node='n1'",
                    ('{"answer":2}',),
                )
            resumed = store.acquire("tampered", identity)
            with self.assertRaises(Text2SQLCheckpointCorruptionError):
                resumed.load_checkpoints("tampered")

            prefix = store.acquire("bad-prefix", identity)
            with self.assertRaises(Text2SQLCheckpointCorruptionError):
                prefix.save_checkpoint("bad-prefix", "n2", {})
            prefix.fail("invalid writer", {})

    def test_tampered_completed_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = Text2SQLRuntimeCheckpointStore(path)
            identity = {"question_sha256": "q1"}
            session = store.acquire("task-1", identity)
            session.complete({"status": "success"}, {})
            with sqlite3.connect(path) as connection:
                changed = '{"status":"failed"}'
                self.assertNotEqual(
                    hashlib.sha256(changed.encode("utf-8")).hexdigest(),
                    connection.execute(
                        "SELECT result_sha256 FROM text2sql_checkpoint_runs"
                    ).fetchone()[0],
                )
                connection.execute(
                    "UPDATE text2sql_checkpoint_runs SET result_json=?", (changed,)
                )
            with self.assertRaises(Text2SQLCheckpointCorruptionError):
                store.acquire("task-1", identity)

    def test_bound_graph_cannot_complete_with_missing_or_failed_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            store = Text2SQLRuntimeCheckpointStore(path)
            identity = {
                "question_sha256": "q1",
                "runtime": {"nodes": ["n1", "n2"]},
            }

            missing = store.acquire("missing-tail", identity)
            missing.save_checkpoint("missing-tail", "n1", {"value": 1})
            with self.assertRaisesRegex(
                Text2SQLCheckpointCorruptionError,
                "before every bound runtime node is completed",
            ):
                missing.complete({"status": "success"}, {})
            self.assertEqual(store.inspect("missing-tail")["status"], "running")
            missing.fail("missing tail", {})

            failed = store.acquire("failed-tail", identity)
            failed.save_checkpoint("failed-tail", "n1", {"value": 1})
            failed.save_checkpoint(
                "failed-tail", "n2", {}, status="failed", error="boom"
            )
            with self.assertRaisesRegex(
                Text2SQLCheckpointCorruptionError,
                "before every bound runtime node is completed",
            ):
                failed.complete({"status": "success"}, {})
            self.assertEqual(store.inspect("failed-tail")["status"], "running")
            failed.fail("failed tail", {})

    def test_legacy_session_without_node_order_can_complete_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Text2SQLRuntimeCheckpointStore(
                Path(directory) / "runtime.sqlite3"
            )
            session = store.acquire("legacy", {"question_sha256": "q1"})
            session.complete({"status": "success"}, {})
            self.assertEqual(store.inspect("legacy")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
