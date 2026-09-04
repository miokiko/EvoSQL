import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.dataset_review import (
    DatasetReviewStore,
    finalize_dataset_review,
    verify_review_certificate,
)
from evoagent.text2sql.evaluation import load_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = PROJECT_ROOT / "evaluation" / "datasets" / "text2sql_v1"


class Text2SQLDatasetReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset"
        shutil.copytree(DATASET, self.dataset)
        manifest_path = self.dataset / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "review_status": "machine_validated_pending_human_review",
                "human_reviewed_cases": 0,
                "release_eligible": False,
            }
        )
        manifest.pop("review_certificate", None)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        certificate = self.dataset / "review_certificate.json"
        if certificate.exists():
            certificate.unlink()
        self.bundle = load_dataset(self.dataset)
        self.cases = [dict(case.as_dict()) for case in self.bundle.cases]
        self.key = b"human-review-test-key-material-32-bytes!!"
        self.store = DatasetReviewStore(
            self.root / "review.sqlite3",
            dataset_id=self.bundle.dataset_id,
            dataset_sha256=self.bundle.dataset_sha256,
            database_snapshot_id=self.bundle.database_snapshot_id,
            cases=self.cases,
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def _checklist(case, failure=""):
        return {
            "question_sql_match": "fail" if failure == "question_sql_match" else "pass",
            "result_semantics": "fail" if failure == "result_semantics" else "pass",
            "schema_grounding": "fail" if failure == "schema_grounding" else "pass",
            "join_correctness": (
                "fail"
                if failure == "join_correctness"
                else "pass" if case["required_relationships"] else "na"
            ),
        }

    def test_review_requires_complete_semantic_checklist_and_tracks_progress(self):
        case = self.cases[0]
        with self.assertRaisesRegex(ValueError, "cannot contain a failed"):
            self.store.record_review(
                case["case_id"],
                "reviewer-a",
                "approve",
                self._checklist(case, "schema_grounding"),
            )
        self.store.record_review(
            case["case_id"],
            "reviewer-a",
            "reject",
            self._checklist(case, "schema_grounding"),
            "字段语义与问题不一致",
        )
        status = self.store.status()
        self.assertEqual(status["rejected"], 1)
        self.assertEqual(status["pending"], 239)
        self.assertFalse(status["release_ready"])
        self.store.record_review(
            case["case_id"],
            "reviewer-a",
            "approve",
            self._checklist(case),
            "复核修正后的题目",
        )
        self.assertEqual(self.store.status()["approved"], 1)

    def test_event_chain_detects_modified_review_evidence(self):
        case = self.cases[0]
        self.store.record_review(
            case["case_id"], "reviewer-a", "approve", self._checklist(case)
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE review_events SET reviewer='forged' WHERE case_id=?",
                (case["case_id"],),
            )
        with self.assertRaisesRegex(ValueError, "chain verification failed"):
            self.store.verify_chain()

    def test_bulk_approval_requires_explicit_human_attestation(self):
        with self.assertRaisesRegex(ValueError, "explicit human"):
            self.store.attest_all_approved("匿名审核员", False)
        result = self.store.attest_all_approved(
            "匿名审核员", True, "人工逐题复核后确认全部通过"
        )
        self.assertEqual(result["recorded_events"], 240)
        self.assertTrue(result["progress"]["release_ready"])
        self.assertEqual(result["progress"]["reviewers"], {"匿名审核员": 240})

    def test_full_signed_certificate_is_required_and_tamper_evident(self):
        for case in self.cases:
            self.store.record_review(
                case["case_id"], "reviewer-a", "approve", self._checklist(case)
            )
        certificate = self.store.build_certificate(self.key)
        evidence = verify_review_certificate(
            certificate,
            self.key,
            dataset_id=self.bundle.dataset_id,
            dataset_sha256=self.bundle.dataset_sha256,
            database_snapshot_id=self.bundle.database_snapshot_id,
            cases=self.cases,
        )
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["reviewed_case_count"], 240)

        destination = self.dataset / "review_certificate.json"
        manifest = finalize_dataset_review(self.dataset, destination, certificate)
        self.assertTrue(manifest["release_eligible"])
        verified = load_dataset(self.dataset, review_signing_key=self.key)
        self.assertTrue(verified.review_evidence["verified"])

        tampered = json.loads(destination.read_text(encoding="utf-8"))
        tampered["case_attestations"][0]["reviewer"] = "forged"
        destination.write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest["review_certificate"]["sha256"] = __import__("hashlib").sha256(
            destination.read_bytes()
        ).hexdigest()
        (self.dataset / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "signature mismatch"):
            load_dataset(self.dataset, review_signing_key=self.key)

    def test_manifest_boolean_without_certificate_is_rejected(self):
        manifest_path = self.dataset / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "review_status": "human_reviewed",
                "human_reviewed_cases": 240,
                "release_eligible": True,
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "missing its review certificate"):
            load_dataset(self.dataset, review_signing_key=self.key)


if __name__ == "__main__":
    unittest.main()
