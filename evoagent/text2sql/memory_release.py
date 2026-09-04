"""Helpers shared by the Semantic Memory evaluation launcher and worker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional


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
) -> Optional[Path]:
    """Return the newest complete 240-case artifact with identical runtime pins."""

    candidates = []
    if not evaluation_root.exists():
        return None
    for path in evaluation_root.glob("*.json"):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if artifact.get("status") != "complete":
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
        candidates.append(path)
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None
