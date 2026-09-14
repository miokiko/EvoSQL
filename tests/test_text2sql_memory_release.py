import json
import tempfile
import unittest
from pathlib import Path

from evoagent.text2sql.agentic import build_runtime_identity
from evoagent.text2sql.memory_release import find_matching_baseline


class MemoryReleaseBaselineIdentityTests(unittest.TestCase):
    def test_baseline_reuse_requires_exact_runtime_identity(self):
        model = {"provider": "test", "model": "model", "temperature": 0}
        pins = {
            "database_snapshot_id": "db-current",
            "wiki_index_version": "knowledge-current",
            "vanna_index_version": "knowledge-current",
            "memory_snapshot_id": "memory-current",
            "policy_version": "policy-current",
        }
        runtime = dict(
            build_runtime_identity(
                token_budget=5000,
                time_budget=60,
                policy_source_memory_ids=("memory-compiled",),
            )
        )
        artifact = {
            "contract_version": 2,
            "status": "complete",
            "evaluated_case_count": 240,
            "evaluated_splits": ["train", "validation", "sealed_holdout"],
            "dataset_id": "dataset-current",
            "dataset_sha256": "sha-current",
            "memory_candidate_id": "",
            "model": model,
            "principals": ["local-user"],
            "report": {"version_pins": pins},
            "runtime": {**runtime, "build_version": "stale-build"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "baseline.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            self.assertIsNone(
                find_matching_baseline(
                    root,
                    dataset_id="dataset-current",
                    dataset_sha256="sha-current",
                    model=model,
                    version_pins=pins,
                    runtime=runtime,
                    principals=("local-user",),
                )
            )

            artifact["runtime"] = runtime
            path.write_text(json.dumps(artifact), encoding="utf-8")
            self.assertEqual(
                find_matching_baseline(
                    root,
                    dataset_id="dataset-current",
                    dataset_sha256="sha-current",
                    model=model,
                    version_pins=pins,
                    runtime=runtime,
                    principals=("local-user",),
                ),
                path,
            )

            artifact["principals"] = ["different-user"]
            path.write_text(json.dumps(artifact), encoding="utf-8")
            self.assertIsNone(
                find_matching_baseline(
                    root,
                    dataset_id="dataset-current",
                    dataset_sha256="sha-current",
                    model=model,
                    version_pins=pins,
                    runtime=runtime,
                    principals=("local-user",),
                )
            )


if __name__ == "__main__":
    unittest.main()
