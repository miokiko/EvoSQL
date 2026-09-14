#!/usr/bin/env python3
"""Run the source-case gate for one Experience-driven Policy candidate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.config import Settings
from evoagent.llm import JsonChatClient
from evoagent.text2sql.agentic import Text2SQLAgenticEngine
from evoagent.text2sql.evolution import Text2SQLEvolutionStore
from evoagent.text2sql.policy import require_single_skill_change
from evoagent.text2sql.target_replay import (
    build_replay_identity,
    run_target_replay,
    validate_target_replay_artifact,
)
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("JSON root must be an object: %s" % path.name)
    return value


def _policy_record(store: Any, version: str) -> Mapping[str, Any]:
    """Use the future rich Store contract, with a read-only legacy fallback."""

    getter = getattr(store, "policy_record", None)
    if callable(getter):
        value = getter(version)
        if isinstance(value, Mapping):
            return value
        raise ValueError("Policy record must be an object")
    for value in store.list_policies():
        if str(value.get("policy_version") or "") == version:
            return value
    raise ValueError("unknown Policy candidate: %s" % version)


def _sequence_of_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(
        dict.fromkeys(
            str(item).strip()
            for item in value
            if str(item).strip().startswith("memory-")
        )
    )


def _candidate_source_ids(
    store: Any,
    candidate_record: Mapping[str, Any],
    requested_ids: Sequence[str],
) -> tuple[str, ...]:
    candidate_version = str(candidate_record.get("policy_version") or "")
    parent_version = str(candidate_record.get("parent_version") or "")
    metadata = candidate_record.get("proposal_metadata")
    if not isinstance(metadata, Mapping):
        metadata = candidate_record.get("proposal_metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    metadata_ids = _sequence_of_ids(metadata.get("memory_ids"))
    if metadata:
        if (
            metadata.get("contract") != "ExperiencePolicyProposal/v1"
            or metadata.get("target_replay_required") is not True
            or (
                metadata.get("source")
                and metadata.get("source") != "confirmed-experiences"
            )
        ):
            raise ValueError("Policy candidate is not an Experience-driven proposal")
        inferred = metadata_ids
    else:
        # Older Store projections do not expose proposal_metadata_json.  The
        # exact set of newly compiled sources is still derivable for the MVP's
        # one-Agent prompt-only candidate.  Once policy_record() is available,
        # the authoritative metadata branch above is always used.
        candidate_ids = set(store.policy_source_memory_ids(candidate_version))
        parent_ids = set(store.policy_source_memory_ids(parent_version))
        inferred = tuple(sorted(candidate_ids.difference(parent_ids)))

    selected = _sequence_of_ids(requested_ids) if requested_ids else inferred
    if not selected:
        raise ValueError("Policy candidate has no source Experience ids")
    if inferred and set(selected) != set(inferred):
        raise ValueError(
            "target replay must cover every source Experience exactly once"
        )
    return selected


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Replay the parent and candidate Policy against every confirmed "
            "source Experience before the independent release evaluation."
        )
    )
    root.add_argument("--candidate", required=True, help="Candidate Policy version")
    root.add_argument(
        "--actor",
        default="target-replay-cli",
        help="Audit actor recorded with the immutable replay artifact",
    )
    root.add_argument(
        "--memory-id",
        action="append",
        default=[],
        help=(
            "Explicit source Experience id; repeat for all sources. Normally "
            "resolved from candidate proposal metadata."
        ),
    )
    root.add_argument("--principal", action="append", default=["local-user"])
    root.add_argument(
        "--database",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_SQLITE_PATH",
                "database/evo_text2sql_eval.sqlite3",
            )
        ),
    )
    root.add_argument(
        "--snapshot",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_SCHEMA_SNAPSHOT",
                "artifacts/text2sql/schema/database_snapshot.json",
            )
        ),
    )
    root.add_argument(
        "--vanna-index-root",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_VANNA_ROOT",
                "artifacts/text2sql/vanna",
            )
        ),
    )
    root.add_argument(
        "--evolution-store",
        type=Path,
        default=_project_path(
            os.getenv(
                "EVOAGENT_TEXT2SQL_EVOLUTION_STORE",
                "artifacts/text2sql/evolution/evolution.sqlite3",
            )
        ),
    )
    root.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Artifact path; defaults to artifacts/text2sql/evolution/"
            "target_replays/<candidate>.json"
        ),
    )
    root.add_argument("--max-rows", type=int, default=200)
    root.add_argument("--timeout-ms", type=int, default=3000)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.max_rows <= 0 or args.timeout_ms <= 0:
        raise ValueError("max rows and timeout must be positive")
    settings = Settings.from_env()
    llm = settings.resolved_llm()
    if not llm:
        raise RuntimeError("a configured LLM is required for target replay")
    snapshot = _json(args.snapshot)
    client = JsonChatClient(
        str(llm["base_url"]),
        str(llm["api_key"]),
        str(llm["model"]),
        provider=str(llm["provider"]),
        timeout=settings.agent_time_budget_seconds,
        extra_headers=dict(llm.get("headers") or {}),
    )
    principals = tuple(
        sorted({str(item).strip() for item in args.principal if str(item).strip()})
    )
    if not principals:
        raise ValueError("at least one principal is required")

    candidate_version = str(args.candidate).strip()
    actor = str(args.actor).strip()
    if not actor:
        raise ValueError("target replay audit actor is required")
    with Text2SQLEvolutionStore(args.evolution_store, snapshot) as store:
        candidate_record = _policy_record(store, candidate_version)
        if str(candidate_record.get("status") or "") != "candidate":
            raise ValueError("target replay requires a Policy in candidate state")
        parent_version = str(candidate_record.get("parent_version") or "")
        target_skill = str(candidate_record.get("target_skill") or "")
        if not parent_version or not target_skill:
            raise ValueError("candidate Policy lineage is incomplete")

        parent_policy = store.get_policy(parent_version)
        candidate_policy = store.get_policy(candidate_version)
        require_single_skill_change(parent_policy, candidate_policy, target_skill)
        memory_ids = _candidate_source_ids(store, candidate_record, args.memory_id)
        lineage = store.validate_experience_policy_lineage(
            candidate_version,
            memory_ids,
        )
        memory_ids = tuple(lineage["memory_ids"])
        experiences = tuple(store.get_memory(memory_id) for memory_id in memory_ids)
        if any(
            str(item.get("state") or "") != "confirmed"
            or str((item.get("rule") or {}).get("target_agent") or "") != target_skill
            for item in experiences
        ):
            raise ValueError(
                "every source must be a confirmed Experience owned by target Agent"
            )
        traces = {
            str(item.get("memory_id") or ""): (
                store.get_query_trace(
                    str((item.get("rule") or {}).get("source_task_id") or ""),
                    int((item.get("rule") or {}).get("source_revision") or 1),
                )
            )
            for item in experiences
        }

        memory_bundle = store.runtime_memory_snapshot()
        memory_snapshot_id = str(memory_bundle.get("memory_snapshot_id") or "")
        vanna_version = VannaRetrieverOnly.current_index_version(args.vanna_index_root)
        if not vanna_version:
            raise RuntimeError("Vanna corpus is not built")

        def engine_for(policy):
            return Text2SQLAgenticEngine(
                client=client,
                database_path=args.database,
                snapshot=snapshot,
                vanna_index_root=args.vanna_index_root,
                vanna_index_version=vanna_version,
                principals=principals,
                memory_snapshot_id=memory_snapshot_id,
                policy_version=policy.version,
                policy_artifact=policy,
                policy_source_memory_ids=store.policy_source_memory_ids(policy.version),
                memory_snapshot_bundle=memory_bundle,
                checkpoint_store=None,
                token_budget=settings.agent_token_budget,
                time_budget=settings.agent_time_budget_seconds,
                max_rows=args.max_rows,
                timeout_ms=args.timeout_ms,
            )

        parent_engine = engine_for(parent_policy)
        candidate_engine = engine_for(candidate_policy)
        identity = build_replay_identity(
            parent_version_pins=parent_engine.version_pins,
            candidate_version_pins=candidate_engine.version_pins,
            parent_runtime=parent_engine.runtime_identity,
            candidate_runtime=candidate_engine.runtime_identity,
            model={
                "provider": str(llm["provider"]),
                "model": str(llm["model"]),
                "temperature": 0,
            },
            principals=principals,
        )
        artifact = run_target_replay(
            experiences,
            traces,
            lambda question, task_id: parent_engine.run(question, task_id=task_id),
            lambda question, task_id: candidate_engine.run(question, task_id=task_id),
            parent_policy_version=parent_version,
            candidate_policy_version=candidate_version,
            replay_identity=identity,
        )
        artifact = validate_target_replay_artifact(
            artifact, candidate_policy_version=candidate_version
        )

        output = args.output or (
            PROJECT_ROOT
            / "artifacts"
            / "text2sql"
            / "evolution"
            / "target_replays"
            / (candidate_version + ".json")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        recorder = getattr(store, "record_target_replay", None)
        persisted = False
        if callable(recorder):
            recorder(
                candidate_version,
                artifact,
                created_by=actor,
                artifact_path=_portable_path(output),
            )
            persisted = True

    print(
        json.dumps(
            {
                "status": artifact["status"],
                "candidate_policy_version": candidate_version,
                "parent_policy_version": parent_version,
                "source_experience_count": artifact["summary"][
                    "source_experience_count"
                ],
                "artifact_sha256": artifact["artifact_sha256"],
                "artifact_path": _portable_path(output),
                "persisted": persisted,
                "next_step": (
                    "run_independent_policy_evaluation"
                    if artifact["status"] == "passed"
                    else "revise_policy_candidate"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if artifact["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
