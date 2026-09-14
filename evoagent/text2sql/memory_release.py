"""Helpers shared by the Semantic Memory evaluation launcher and worker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .evaluation import EVALUATION_ARTIFACT_CONTRACT_VERSION


REQUIRED_MEMORY_EVALUATION_SPLITS = (
    "train",
    "validation",
    "sealed_holdout",
)


def find_matching_baseline(
    evaluation_root: Path,
    *,
    dataset_id: str,
    dataset_sha256: str,
    model: Mapping[str, Any],
    version_pins: Mapping[str, Any],
    runtime: Mapping[str, Any],
    principals: Sequence[str],
) -> Optional[Path]:
    """Return the newest complete 240-case artifact with identical runtime identity."""

    candidates = []
    if not evaluation_root.exists():
        return None
    for path in evaluation_root.glob("*.json"):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            artifact.get("contract_version")
            != EVALUATION_ARTIFACT_CONTRACT_VERSION
            or artifact.get("status") != "complete"
        ):
            continue
        if int(artifact.get("evaluated_case_count") or 0) != 240:
            continue
        if set(artifact.get("evaluated_splits") or ()) != set(
            REQUIRED_MEMORY_EVALUATION_SPLITS
        ):
            continue
        if artifact.get("dataset_id") != dataset_id:
            continue
        if artifact.get("dataset_sha256") != dataset_sha256:
            continue
        if str(artifact.get("memory_candidate_id") or ""):
            continue
        artifact_model = dict(artifact.get("model") or {})
        if any(
            artifact_model.get(key) != model.get(key)
            for key in ("provider", "model", "temperature")
        ):
            continue
        pins = dict((artifact.get("report") or {}).get("version_pins") or {})
        if any(pins.get(key) != value for key, value in version_pins.items()):
            continue
        if dict(artifact.get("runtime") or {}) != dict(runtime):
            continue
        artifact_principals = artifact.get("principals")
        if (
            not isinstance(artifact_principals, Sequence)
            or isinstance(artifact_principals, (str, bytes, bytearray))
            or tuple(sorted(str(item) for item in artifact_principals))
            != tuple(sorted(str(item) for item in principals))
        ):
            continue
        candidates.append(path)
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None
