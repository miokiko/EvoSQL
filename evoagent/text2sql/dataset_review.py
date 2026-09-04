"""Human review ledger and signed release certificate for Text2SQL datasets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


REVIEW_CHECKS = (
    "question_sql_match",
    "result_semantics",
    "schema_grounding",
    "join_correctness",
)
CHECK_VALUES = frozenset({"pass", "fail", "na"})
CERTIFICATE_KIND = "text2sql_dataset_human_review"
CERTIFICATE_CONTRACT_VERSION = 1
REVIEW_KEY_ENV = "EVOAGENT_TEXT2SQL_REVIEW_KEY_FILE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def case_fingerprint(case: Mapping[str, Any]) -> str:
    """Bind a review decision to every semantic field in one dataset case."""

    return _sha256(_canonical(dict(case)).encode("utf-8"))


def read_review_signing_key(path: Optional[Path] = None) -> bytes:
    if path is None and not os.getenv(REVIEW_KEY_ENV):
        from ..config import load_dotenv

        load_dotenv()
    configured = path or (Path(os.environ[REVIEW_KEY_ENV]) if os.getenv(REVIEW_KEY_ENV) else None)
    if configured is None:
        raise ValueError(
            "human-reviewed dataset requires --review-key-file or %s" % REVIEW_KEY_ENV
        )
    key = configured.expanduser().resolve().read_bytes()
    if len(key) < 32:
        raise ValueError("review signing key must contain at least 32 bytes")
    return key


def _certificate_signature(body: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical(body).encode("utf-8"), hashlib.sha256).hexdigest()


def certificate_file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def verify_review_certificate(
    certificate: Mapping[str, Any],
    signing_key: bytes,
    *,
    dataset_id: str,
    dataset_sha256: str,
    database_snapshot_id: str,
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Verify HMAC authenticity, complete case coverage, and all review decisions."""

    if certificate.get("contract_version") != CERTIFICATE_CONTRACT_VERSION:
        raise ValueError("unsupported dataset review certificate contract")
    if certificate.get("certificate_kind") != CERTIFICATE_KIND:
        raise ValueError("invalid dataset review certificate kind")
    body = {key: value for key, value in certificate.items() if key != "signature"}
    signing = body.get("signing") or {}
    if signing.get("algorithm") != "hmac-sha256":
        raise ValueError("unsupported dataset review signature algorithm")
    expected_key_id = _sha256(signing_key)[:20]
    if signing.get("key_id") != expected_key_id:
        raise ValueError("dataset review signing key does not match certificate")
    expected_signature = _certificate_signature(body, signing_key)
    if not hmac.compare_digest(str(certificate.get("signature") or ""), expected_signature):
        raise ValueError("dataset review certificate signature mismatch")

    expected_identity = (dataset_id, dataset_sha256, database_snapshot_id)
    actual_identity = (
        certificate.get("dataset_id"),
        certificate.get("dataset_sha256"),
        certificate.get("database_snapshot_id"),
    )
    if actual_identity != expected_identity:
        raise ValueError("dataset review certificate dataset identity mismatch")

    expected_cases = {str(case["case_id"]): case_fingerprint(case) for case in cases}
    join_cases = {
        str(case["case_id"])
        for case in cases
        if case.get("required_relationships")
    }
    attestations = certificate.get("case_attestations") or ()
    by_case: dict[str, Mapping[str, Any]] = {}
    for item in attestations:
        if not isinstance(item, Mapping) or not item.get("case_id"):
            raise ValueError("invalid dataset review case attestation")
        case_id = str(item["case_id"])
        if case_id in by_case:
            raise ValueError("duplicate dataset review case attestation: %s" % case_id)
        by_case[case_id] = item
    if set(by_case) != set(expected_cases):
        raise ValueError("dataset review certificate does not cover the complete dataset")
    for case_id, expected_hash in expected_cases.items():
        item = by_case[case_id]
        if item.get("case_sha256") != expected_hash or item.get("verdict") != "approve":
            raise ValueError("dataset review attestation is not approved: %s" % case_id)
        checklist = item.get("checklist") or {}
        if any(checklist.get(name) not in {"pass", "na"} for name in REVIEW_CHECKS):
            raise ValueError("dataset review checklist is incomplete: %s" % case_id)
        if any(checklist.get(name) != "pass" for name in REVIEW_CHECKS[:3]):
            raise ValueError("required dataset review check is not passed: %s" % case_id)
        if case_id in join_cases and checklist.get("join_correctness") != "pass":
            raise ValueError("join review check is not passed: %s" % case_id)
        if not str(item.get("reviewer") or "").strip() or not item.get("event_hash"):
            raise ValueError("dataset review attribution is incomplete: %s" % case_id)

    if int(certificate.get("case_count") or 0) != len(expected_cases):
        raise ValueError("dataset review certificate case count mismatch")
    if int(certificate.get("approved_case_count") or 0) != len(expected_cases):
        raise ValueError("dataset review certificate approval count mismatch")
    return {
        "verified": True,
        "certificate_kind": CERTIFICATE_KIND,
        "key_id": expected_key_id,
        "reviewed_case_count": len(expected_cases),
        "dataset_sha256": dataset_sha256,
        "chain_head": str(certificate.get("review_chain_head") or ""),
    }


class DatasetReviewStore:
    """Append-only, hash-chained review decisions for one immutable dataset."""

    def __init__(
        self,
        path: Path,
        *,
        dataset_id: str,
        dataset_sha256: str,
        database_snapshot_id: str,
        cases: Sequence[Mapping[str, Any]],
    ) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self._bind_dataset(dataset_id, dataset_sha256, database_snapshot_id, cases)
        self.verify_chain()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "DatasetReviewStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_cases(
                    case_id TEXT PRIMARY KEY,
                    split TEXT NOT NULL,
                    category TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    case_sha256 TEXT NOT NULL,
                    has_join INTEGER NOT NULL CHECK(has_join IN (0,1)),
                    ordinal INTEGER NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS review_events(
                    event_index INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    case_id TEXT NOT NULL REFERENCES review_cases(case_id),
                    reviewer TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('approve','reject')),
                    question_sql_match TEXT NOT NULL,
                    result_semantics TEXT NOT NULL,
                    schema_grounding TEXT NOT NULL,
                    join_correctness TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_review_events_case
                    ON review_events(case_id,event_index);
                """
            )

    def _meta(self) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key,value FROM review_meta")
        }

    def _bind_dataset(
        self,
        dataset_id: str,
        dataset_sha256: str,
        database_snapshot_id: str,
        cases: Sequence[Mapping[str, Any]],
    ) -> None:
        existing = self._meta()
        expected = {
            "contract_version": "1",
            "dataset_id": dataset_id,
            "dataset_sha256": dataset_sha256,
            "database_snapshot_id": database_snapshot_id,
            "case_count": str(len(cases)),
        }
        if existing:
            if any(existing.get(key) != value for key, value in expected.items()):
                raise ValueError("review store is bound to a different dataset")
        else:
            expected["review_store_id"] = "dataset-review-%s" % uuid.uuid4().hex
            expected["created_at"] = _now()
            with self.connection:
                self.connection.executemany(
                    "INSERT INTO review_meta(key,value) VALUES (?,?)", expected.items()
                )
        current = {
            str(row["case_id"]): str(row["case_sha256"])
            for row in self.connection.execute("SELECT case_id,case_sha256 FROM review_cases")
        }
        expected_cases = {str(case["case_id"]): case_fingerprint(case) for case in cases}
        if current:
            if current != expected_cases:
                raise ValueError("review store case fingerprints do not match dataset")
            return
        rows = []
        for ordinal, case in enumerate(sorted(cases, key=lambda value: str(value["case_id"])), 1):
            rows.append(
                (
                    str(case["case_id"]),
                    str(case["split"]),
                    str(case["category"]),
                    str(case["difficulty"]),
                    case_fingerprint(case),
                    int(bool(case.get("required_relationships"))),
                    ordinal,
                )
            )
        with self.connection:
            self.connection.executemany(
                """INSERT INTO review_cases(
                    case_id,split,category,difficulty,case_sha256,has_join,ordinal
                ) VALUES (?,?,?,?,?,?,?)""",
                rows,
            )

    @property
    def review_store_id(self) -> str:
        return self._meta()["review_store_id"]

    def verify_chain(self) -> str:
        previous = "GENESIS"
        for row in self.connection.execute("SELECT * FROM review_events ORDER BY event_index"):
            payload = {
                "review_store_id": self.review_store_id,
                "event_id": row["event_id"],
                "case_id": row["case_id"],
                "reviewer": row["reviewer"],
                "verdict": row["verdict"],
                "checklist": {name: row[name] for name in REVIEW_CHECKS},
                "notes": row["notes"],
                "reviewed_at": row["reviewed_at"],
                "previous_hash": row["previous_hash"],
            }
            expected = _sha256(_canonical(payload).encode("utf-8"))
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                raise ValueError("dataset review event chain verification failed")
            previous = expected
        return previous

    def record_review(
        self,
        case_id: str,
        reviewer: str,
        verdict: str,
        checklist: Mapping[str, str],
        notes: str = "",
    ) -> Mapping[str, Any]:
        reviewer = reviewer.strip()
        notes = notes.strip()
        if not reviewer:
            raise ValueError("reviewer identity is required")
        if verdict not in {"approve", "reject"}:
            raise ValueError("review verdict must be approve or reject")
        row = self.connection.execute(
            "SELECT has_join FROM review_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if not row:
            raise ValueError("unknown review case_id: %s" % case_id)
        normalized = {name: str(checklist.get(name) or "") for name in REVIEW_CHECKS}
        if any(value not in CHECK_VALUES for value in normalized.values()):
            raise ValueError("all review checklist values must be pass, fail, or na")
        if any(normalized[name] == "na" for name in REVIEW_CHECKS[:3]):
            raise ValueError("question, result, and schema checks cannot be na")
        if row["has_join"] and normalized["join_correctness"] == "na":
            raise ValueError("join cases require an explicit join correctness decision")
        if verdict == "approve" and any(value == "fail" for value in normalized.values()):
            raise ValueError("an approved case cannot contain a failed check")
        if verdict == "approve" and any(normalized[name] != "pass" for name in REVIEW_CHECKS[:3]):
            raise ValueError("approved cases must pass question, result, and schema checks")
        if verdict == "reject" and not any(value == "fail" for value in normalized.values()):
            raise ValueError("a rejected case must identify at least one failed check")
        if verdict == "reject" and not notes:
            raise ValueError("a rejected case requires review notes")

        previous = self.verify_chain()
        event_id = "review-event-%s" % uuid.uuid4().hex
        reviewed_at = _now()
        payload = {
            "review_store_id": self.review_store_id,
            "event_id": event_id,
            "case_id": case_id,
            "reviewer": reviewer[:200],
            "verdict": verdict,
            "checklist": normalized,
            "notes": notes[:4000],
            "reviewed_at": reviewed_at,
            "previous_hash": previous,
        }
        event_hash = _sha256(_canonical(payload).encode("utf-8"))
        with self.connection:
            self.connection.execute(
                """INSERT INTO review_events(
                    event_id,case_id,reviewer,verdict,question_sql_match,
                    result_semantics,schema_grounding,join_correctness,notes,
                    reviewed_at,previous_hash,event_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    case_id,
                    payload["reviewer"],
                    verdict,
                    normalized["question_sql_match"],
                    normalized["result_semantics"],
                    normalized["schema_grounding"],
                    normalized["join_correctness"],
                    payload["notes"],
                    reviewed_at,
                    previous,
                    event_hash,
                ),
            )
        return {"event_id": event_id, "event_hash": event_hash, "case_id": case_id}

    def _latest_rows(self) -> dict[str, sqlite3.Row]:
        return {
            str(row["case_id"]): row
            for row in self.connection.execute(
                """SELECT event.* FROM review_events event
                JOIN (
                    SELECT case_id,MAX(event_index) AS event_index
                    FROM review_events GROUP BY case_id
                ) latest ON latest.event_index=event.event_index"""
            )
        }

    def status(self) -> Mapping[str, Any]:
        chain_head = self.verify_chain()
        latest = self._latest_rows()
        cases = list(self.connection.execute("SELECT * FROM review_cases ORDER BY ordinal"))
        approved = [
            case
            for case in cases
            if case["case_id"] in latest and latest[case["case_id"]]["verdict"] == "approve"
        ]
        rejected = [
            case
            for case in cases
            if case["case_id"] in latest and latest[case["case_id"]]["verdict"] == "reject"
        ]
        pending = [case for case in cases if case["case_id"] not in latest]
        reviewers = Counter(str(row["reviewer"]) for row in latest.values())
        return {
            "review_store_id": self.review_store_id,
            "dataset_id": self._meta()["dataset_id"],
            "dataset_sha256": self._meta()["dataset_sha256"],
            "case_count": len(cases),
            "approved": len(approved),
            "rejected": len(rejected),
            "pending": len(pending),
            "release_ready": len(approved) == len(cases) and not rejected and not pending,
            "reviewers": dict(sorted(reviewers.items())),
            "by_split": {
                split: {
                    "total": sum(case["split"] == split for case in cases),
                    "approved": sum(case["split"] == split for case in approved),
                    "rejected": sum(case["split"] == split for case in rejected),
                    "pending": sum(case["split"] == split for case in pending),
                }
                for split in sorted({str(case["split"]) for case in cases})
            },
            "review_chain_head": chain_head,
        }

    def next_case_ids(self, limit: int = 1, split: str = "") -> Sequence[str]:
        latest = self._latest_rows()
        cases = list(self.connection.execute("SELECT * FROM review_cases ORDER BY ordinal"))
        if split:
            cases = [case for case in cases if case["split"] == split]
        pending = [str(case["case_id"]) for case in cases if case["case_id"] not in latest]
        rejected = [
            str(case["case_id"])
            for case in cases
            if case["case_id"] in latest and latest[case["case_id"]]["verdict"] == "reject"
        ]
        return tuple((pending + rejected)[: max(0, int(limit))])

    def attest_all_approved(
        self,
        reviewer: str,
        human_attested: bool,
        notes: str = "",
    ) -> Mapping[str, Any]:
        """Materialize an explicit human statement that every remaining case passed review."""

        if not human_attested:
            raise ValueError("explicit human full-dataset attestation is required")
        reviewer = reviewer.strip()
        if not reviewer:
            raise ValueError("reviewer identity is required")
        remaining = self.next_case_ids(limit=int(self._meta()["case_count"]))
        rows = {
            str(row["case_id"]): bool(row["has_join"])
            for row in self.connection.execute(
                "SELECT case_id,has_join FROM review_cases"
            )
        }
        created = []
        for case_id in remaining:
            created.append(
                self.record_review(
                    case_id,
                    reviewer,
                    "approve",
                    {
                        "question_sql_match": "pass",
                        "result_semantics": "pass",
                        "schema_grounding": "pass",
                        "join_correctness": "pass" if rows[case_id] else "na",
                    },
                    notes
                    or "Reviewer attested that the complete dataset was manually checked and approved.",
                )
            )
        return {
            "reviewer": reviewer,
            "recorded_events": len(created),
            "progress": self.status(),
        }

    def build_certificate(self, signing_key: bytes) -> Mapping[str, Any]:
        if len(signing_key) < 32:
            raise ValueError("review signing key must contain at least 32 bytes")
        status = self.status()
        if not status["release_ready"]:
            raise ValueError(
                "dataset review is incomplete: approved=%d rejected=%d pending=%d"
                % (status["approved"], status["rejected"], status["pending"])
            )
        latest = self._latest_rows()
        cases = list(self.connection.execute("SELECT * FROM review_cases ORDER BY case_id"))
        attestations = []
        for case in cases:
            row = latest[str(case["case_id"])]
            attestations.append(
                {
                    "case_id": str(case["case_id"]),
                    "case_sha256": str(case["case_sha256"]),
                    "verdict": "approve",
                    "reviewer": str(row["reviewer"]),
                    "reviewed_at": str(row["reviewed_at"]),
                    "checklist": {name: str(row[name]) for name in REVIEW_CHECKS},
                    "event_hash": str(row["event_hash"]),
                }
            )
        meta = self._meta()
        body = {
            "contract_version": CERTIFICATE_CONTRACT_VERSION,
            "certificate_kind": CERTIFICATE_KIND,
            "dataset_id": meta["dataset_id"],
            "dataset_sha256": meta["dataset_sha256"],
            "database_snapshot_id": meta["database_snapshot_id"],
            "review_store_id": self.review_store_id,
            "review_policy": {
                "required_approvals_per_case": 1,
                "required_checks": list(REVIEW_CHECKS),
            },
            "case_count": len(attestations),
            "approved_case_count": len(attestations),
            "reviewers": sorted({item["reviewer"] for item in attestations}),
            "review_chain_head": status["review_chain_head"],
            "created_at": _now(),
            "case_attestations": attestations,
            "signing": {
                "algorithm": "hmac-sha256",
                "key_id": _sha256(signing_key)[:20],
            },
        }
        return {**body, "signature": _certificate_signature(body, signing_key)}


def finalize_dataset_review(
    dataset_root: Path,
    certificate_path: Path,
    certificate: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Write the signed certificate and make the manifest release eligible atomically enough locally."""

    dataset_root = dataset_root.resolve()
    certificate_path = certificate_path.resolve()
    try:
        relative = certificate_path.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError("review certificate must be stored inside the dataset directory") from exc
    certificate_path.write_text(
        json.dumps(certificate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path = dataset_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != certificate.get("dataset_id") or manifest.get(
        "dataset_sha256"
    ) != certificate.get("dataset_sha256"):
        raise ValueError("certificate does not match dataset manifest")
    manifest.update(
        {
            "review_status": "human_reviewed",
            "human_reviewed_cases": int(certificate["approved_case_count"]),
            "release_eligible": True,
            "review_certificate": {
                "path": str(relative),
                "sha256": certificate_file_sha256(certificate_path),
                "key_id": (certificate.get("signing") or {}).get("key_id"),
            },
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
