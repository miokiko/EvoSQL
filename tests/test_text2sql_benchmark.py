import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.benchmark import ResumableEvaluationCheckpoint
from scripts.run_text2sql_evaluation import _fatal_provider_outcome


class ResumableText2SQLBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "run.checkpoint.jsonl"
        self.identity = {
            "dataset_sha256": "dataset-hash",
            "case_ids": ["case-1", "case-2"],
            "version_pins": {"policy_version": "policy-1"},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_checkpoint_resumes_without_rerunning_completed_cases(self):
        checkpoint = ResumableEvaluationCheckpoint(self.path, self.identity)
        self.assertEqual(checkpoint.start(), ())
        run_id = checkpoint.run_id
        self.assertTrue(run_id.startswith("evaluation-run-"))
        checkpoint.append_outcome(
            {"case_id": "case-1", "execution_accuracy": True, "total_tokens": 10}
        )
        resumed_checkpoint = ResumableEvaluationCheckpoint(self.path, self.identity)
        resumed = resumed_checkpoint.start(resume=True)
        self.assertEqual(resumed_checkpoint.run_id, run_id)
        self.assertEqual([item["case_id"] for item in resumed], ["case-1"])
        with self.assertRaisesRegex(ValueError, "use --resume"):
            checkpoint.start(resume=False)

    def test_new_checkpoint_gets_an_independent_runtime_namespace(self):
        first = ResumableEvaluationCheckpoint(self.path, self.identity)
        first.start()
        second = ResumableEvaluationCheckpoint(
            self.path.with_name("second.checkpoint.jsonl"), self.identity
        )
        second.start()
        self.assertNotEqual(first.run_id, second.run_id)

    def test_checkpoint_detects_identity_and_content_tampering(self):
        checkpoint = ResumableEvaluationCheckpoint(self.path, self.identity)
        checkpoint.start()
        checkpoint.append_outcome({"case_id": "case-1", "execution_accuracy": True})
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            ResumableEvaluationCheckpoint(
                self.path, {**self.identity, "dataset_sha256": "other"}
            ).start(resume=True)

        records = self.path.read_text(encoding="utf-8").splitlines()
        changed = json.loads(records[-1])
        changed["outcome"]["execution_accuracy"] = False
        records[-1] = json.dumps(changed)
        self.path.write_text("\n".join(records) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "record hash mismatch"):
            checkpoint.start(resume=True)

    def test_complete_checkpoint_cannot_be_resumed(self):
        checkpoint = ResumableEvaluationCheckpoint(self.path, self.identity)
        checkpoint.start()
        checkpoint.append_outcome({"case_id": "case-1"})
        checkpoint.mark_complete({"evaluated_case_count": 1})
        with self.assertRaisesRegex(ValueError, "already complete"):
            checkpoint.start(resume=True)

    def test_fatal_provider_outcomes_abort_but_transient_disconnects_do_not(self):
        self.assertTrue(
            _fatal_provider_outcome(
                {
                    "failure_kind": "FRAMEWORK_ERROR",
                    "framework_error": "HTTP 400 code=Arrearage overdue-payment",
                }
            )
        )
        self.assertFalse(
            _fatal_provider_outcome(
                {
                    "failure_kind": "FRAMEWORK_ERROR",
                    "framework_error": "Remote end closed connection without response",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
