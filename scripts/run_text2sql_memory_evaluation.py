#!/usr/bin/env python3
"""Run a resumable full-dataset gate for one approved Semantic Memory."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.evaluation import load_dataset
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.memory_release import REQUIRED_MEMORY_EVALUATION_SPLITS


def _count_outcomes(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if json.loads(line).get("record_type") == "outcome":
                count += 1
    except (OSError, json.JSONDecodeError):
        return count
    return count


def _run(
    command: list[str],
    *,
    log_path: Path,
    checkpoint_path: Path,
    evolution_store: Path,
    snapshot: dict,
    job_id: str,
    phase: str,
    offset: int,
) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        while process.poll() is None:
            with Text2SQLEvolutionStore(evolution_store, snapshot) as store:
                store.update_memory_evaluation_job(
                    job_id,
                    status="running",
                    phase=phase,
                    progress_current=offset + _count_outcomes(checkpoint_path),
                )
            time.sleep(2)
        if process.returncode:
            raise RuntimeError(
                "%s evaluation exited with status %d"
                % (phase, process.returncode)
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--memory-id", required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "evaluation" / "datasets" / "text2sql_v1",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "schema"
        / "database_snapshot.json",
    )
    parser.add_argument(
        "--evolution-store",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "evolution"
        / "evolution.sqlite3",
    )
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    try:
        with Text2SQLEvolutionStore(args.evolution_store, snapshot) as store:
            job = store.get_memory_evaluation_job(args.job_id)
            if job["memory_id"] != args.memory_id:
                raise ValueError("job does not match memory candidate")
            store.update_memory_evaluation_job(
                args.job_id, status="running", phase="preparing"
            )
        baseline_artifact = Path(job["baseline_artifact"])
        candidate_artifact = Path(job["candidate_artifact"])
        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_artifact.parent.mkdir(parents=True, exist_ok=True)
        runner = PROJECT_ROOT / "scripts" / "run_text2sql_evaluation.py"
        common = [
            sys.executable,
            str(runner),
            "--dataset",
            str(args.dataset),
            "--snapshot",
            str(args.snapshot),
            "--evolution-store",
            str(args.evolution_store),
            "--split",
            "train",
            "--split",
            "validation",
            "--split",
            "sealed_holdout",
            "--max-cases",
            "0",
            "--workers",
            str(max(1, args.workers)),
        ]
        offset = 0
        if not baseline_artifact.exists():
            baseline_checkpoint = baseline_artifact.with_suffix(
                baseline_artifact.suffix + ".checkpoint.jsonl"
            )
            _run(
                [
                    *common,
                    "--output",
                    str(baseline_artifact),
                    "--checkpoint",
                    str(baseline_checkpoint),
                ],
                log_path=log_path,
                checkpoint_path=baseline_checkpoint,
                evolution_store=args.evolution_store,
                snapshot=snapshot,
                job_id=args.job_id,
                phase="baseline",
                offset=0,
            )
            offset = 240
        candidate_checkpoint = candidate_artifact.with_suffix(
            candidate_artifact.suffix + ".checkpoint.jsonl"
        )
        _run(
            [
                *common,
                "--memory-candidate-id",
                args.memory_id,
                "--output",
                str(candidate_artifact),
                "--checkpoint",
                str(candidate_checkpoint),
            ],
            log_path=log_path,
            checkpoint_path=candidate_checkpoint,
            evolution_store=args.evolution_store,
            snapshot=snapshot,
            job_id=args.job_id,
            phase="candidate",
            offset=offset,
        )
        baseline = json.loads(baseline_artifact.read_text(encoding="utf-8"))
        candidate = json.loads(candidate_artifact.read_text(encoding="utf-8"))
        bundle = load_dataset(args.dataset, REQUIRED_MEMORY_EVALUATION_SPLITS)
        manifest = json.loads(
            (args.dataset / "manifest.json").read_text(encoding="utf-8")
        )
        with Text2SQLEvolutionStore(args.evolution_store, snapshot) as store:
            result = store.record_memory_evaluation(
                args.memory_id,
                args.job_id,
                manifest,
                baseline,
                candidate,
                bundle.review_evidence,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        try:
            with Text2SQLEvolutionStore(args.evolution_store, snapshot) as store:
                store.update_memory_evaluation_job(
                    args.job_id,
                    status="failed",
                    phase="failed",
                    error=str(exc),
                )
        except Exception:
            pass
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
