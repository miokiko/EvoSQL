#!/usr/bin/env python3
"""Build and evaluate an isolated Vanna index for one Question-SQL candidate."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.evaluation import load_dataset
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.knowledge_store import KnowledgeStore
from evoagent.text2sql.memory_release import REQUIRED_MEMORY_EVALUATION_SPLITS
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


def _count_outcomes(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("record_type") == "outcome":
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
                store.update_experience_evaluation_job(
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


def _copy_knowledge(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        raise ValueError("candidate knowledge store already exists")
    with KnowledgeStore(source_path) as source:
        target = sqlite3.connect(target_path)
        try:
            source.connection.backup(target)
        finally:
            target.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--experience-id", required=True)
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
        "--knowledge-store",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "knowledge"
        / "knowledge.sqlite3",
    )
    parser.add_argument(
        "--vanna-root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "text2sql" / "vanna",
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
        with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
            job = evolution.get_experience_evaluation_job(args.job_id)
            experience = evolution.get_experience(args.experience_id)
            if job["experience_id"] != args.experience_id:
                raise ValueError("job does not match Question-SQL candidate")
            evidence_id = str(experience.get("knowledge_evidence_id") or "")
            if not evidence_id:
                raise ValueError("Question-SQL candidate has no staged evidence")
            evolution.update_experience_evaluation_job(
                args.job_id, status="running", phase="candidate_index"
            )
        baseline_artifact = Path(job["baseline_artifact"])
        candidate_artifact = Path(job["candidate_artifact"])
        candidate_store = Path(job["candidate_knowledge_store"])
        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)

        _copy_knowledge(args.knowledge_store, candidate_store)
        with KnowledgeStore(candidate_store) as knowledge:
            candidate_version = knowledge.review(
                evidence_id,
                "approve",
                str(job["requested_by"]),
                "Isolated Question-SQL evaluation candidate",
            )
            candidate_items = knowledge.stable_items_for_index()
            database_snapshot_id = knowledge.database_snapshot_id()
        VannaRetrieverOnly(
            args.vanna_root, candidate_version, enabled=True
        ).build(candidate_items, database_snapshot_id)
        with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
            evolution.update_experience_evaluation_job(
                args.job_id,
                status="running",
                phase="candidate_index_ready",
                candidate_vanna_version=candidate_version,
            )

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
                    "--knowledge-store",
                    str(args.knowledge_store),
                    "--vanna-index-root",
                    str(args.vanna_root),
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
                "--knowledge-store",
                str(candidate_store),
                "--vanna-index-root",
                str(args.vanna_root),
                "--experience-candidate-id",
                args.experience_id,
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
        with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
            result = evolution.record_experience_evaluation(
                args.experience_id,
                args.job_id,
                manifest,
                baseline,
                candidate,
                bundle.review_evidence,
            )
        if not result["eligible_for_activation"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        baseline_pins = dict((baseline.get("report") or {}).get("version_pins") or {})
        with KnowledgeStore(args.knowledge_store) as knowledge:
            if (
                knowledge.current_index_version("stable")
                != baseline_pins.get("wiki_index_version")
            ):
                raise RuntimeError(
                    "stable knowledge changed during evaluation; replay required"
                )
            activated_version = knowledge.review(
                evidence_id,
                "approve",
                str(job["requested_by"]),
                "240-case retrieval regression passed",
            )
        if activated_version != candidate_version:
            raise RuntimeError("candidate Vanna version does not match activated knowledge")
        if not VannaRetrieverOnly(
            args.vanna_root, activated_version, enabled=True
        ).status().get("ready"):
            raise RuntimeError("candidate Vanna index is not ready for activation")
        with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
            promoted = evolution.activate_experience(
                args.experience_id, evidence_id
            )
            evolution.update_experience_evaluation_job(
                args.job_id,
                status="promoted",
                phase="activated",
            )
        print(
            json.dumps(
                {
                    **result,
                    "experience_state": promoted["state"],
                    "stable_vanna_index_version": activated_version,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        try:
            with Text2SQLEvolutionStore(args.evolution_store, snapshot) as evolution:
                evolution.update_experience_evaluation_job(
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
