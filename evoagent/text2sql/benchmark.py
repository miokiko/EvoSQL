"""Append-only checkpoints for resumable, cost-bounded Text2SQL benchmarks."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ResumableEvaluationCheckpoint:
    """Hash-chain one outcome per line so interrupted evaluation can resume safely."""

    def __init__(self, path: Path, identity: Mapping[str, Any]) -> None:
        self.path = path.resolve()
        self.identity = dict(identity)
        self.run_id = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _hash(record: Mapping[str, Any]) -> str:
        return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()

    def _records(self) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        previous = "GENESIS"
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "evaluation checkpoint has an invalid line: %d" % line_number
                ) from exc
            claimed = str(record.pop("record_hash", ""))
            if record.get("previous_hash") != previous:
                raise ValueError("evaluation checkpoint hash chain is broken")
            expected = self._hash(record)
            if not claimed or claimed != expected:
                raise ValueError("evaluation checkpoint record hash mismatch")
            record = {**record, "record_hash": claimed}
            records.append(record)
            previous = claimed
        return records

    def _append(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        records = self._records()
        previous = records[-1]["record_hash"] if records else "GENESIS"
        body = {**dict(value), "previous_hash": previous}
        record = {**body, "record_hash": self._hash(body)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def start(self, resume: bool = False) -> Sequence[Mapping[str, Any]]:
        records = self._records()
        if records:
            if not resume:
                raise ValueError("evaluation checkpoint already exists; use --resume")
            header = records[0]
            if header.get("record_type") != "header" or header.get("identity") != self.identity:
                raise ValueError("evaluation checkpoint identity mismatch")
            self.run_id = str(
                header.get("evaluation_run_id")
                or "evaluation-run-legacy-%s" % header["record_hash"][:24]
            )
            if any(record.get("record_type") == "complete" for record in records):
                raise ValueError("evaluation checkpoint is already complete")
            outcomes = [
                dict(record.get("outcome") or {})
                for record in records
                if record.get("record_type") == "outcome"
            ]
            case_ids = [str(item.get("case_id") or "") for item in outcomes]
            if "" in case_ids or len(case_ids) != len(set(case_ids)):
                raise ValueError("evaluation checkpoint contains duplicate outcomes")
            return tuple(outcomes)
        self.run_id = "evaluation-run-%s" % uuid.uuid4().hex
        self._append(
            {
                "record_type": "header",
                "contract_version": 1,
                "created_at": _now(),
                "evaluation_run_id": self.run_id,
                "identity": self.identity,
            }
        )
        return ()

    def append_outcome(self, outcome: Mapping[str, Any]) -> None:
        records = self._records()
        if not records or records[0].get("identity") != self.identity:
            raise ValueError("evaluation checkpoint has not been initialized")
        if any(record.get("record_type") == "complete" for record in records):
            raise ValueError("evaluation checkpoint is already complete")
        case_id = str(outcome.get("case_id") or "")
        if not case_id:
            raise ValueError("checkpoint outcome requires case_id")
        if any(
            (record.get("outcome") or {}).get("case_id") == case_id
            for record in records
            if record.get("record_type") == "outcome"
        ):
            raise ValueError("checkpoint already contains case_id: %s" % case_id)
        self._append(
            {
                "record_type": "outcome",
                "created_at": _now(),
                "outcome": dict(outcome),
            }
        )

    def mark_complete(self, artifact: Mapping[str, Any]) -> None:
        digest = hashlib.sha256(_canonical(dict(artifact)).encode("utf-8")).hexdigest()
        self._append(
            {
                "record_type": "complete",
                "created_at": _now(),
                "artifact_sha256": digest,
                "evaluated_case_count": int(artifact.get("evaluated_case_count") or 0),
            }
        )
