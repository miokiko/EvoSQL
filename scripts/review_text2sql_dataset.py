#!/usr/bin/env python3
"""Record human Text2SQL dataset reviews and issue a signed release certificate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evoagent.text2sql.dataset_review import (
    REVIEW_CHECKS,
    DatasetReviewStore,
    finalize_dataset_review,
    read_review_signing_key,
    verify_review_certificate,
)
from evoagent.text2sql.evaluation import load_dataset


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "evaluation" / "datasets" / "text2sql_v1",
    )
    root.add_argument(
        "--store",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "text2sql"
        / "review"
        / "text2sql_v1_review.sqlite3",
    )
    root.add_argument(
        "--key-file",
        type=Path,
        default=None,
        help="Human-held HMAC key (at least 32 bytes); keep it outside the repository.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")

    next_case = commands.add_parser("next")
    next_case.add_argument("--limit", type=int, default=1)
    next_case.add_argument(
        "--split", choices=("train", "validation", "sealed_holdout"), default=""
    )

    show = commands.add_parser("show")
    show.add_argument("--case-id", required=True)

    packet = commands.add_parser("packet")
    packet.add_argument("--output", type=Path, required=True)
    packet.add_argument("--limit", type=int, default=20)
    packet.add_argument(
        "--split", choices=("train", "validation", "sealed_holdout"), default=""
    )

    review = commands.add_parser("review")
    review.add_argument("--case-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--verdict", choices=("approve", "reject"), required=True)
    for check in REVIEW_CHECKS:
        review.add_argument(
            "--" + check.replace("_", "-"),
            choices=("pass", "fail", "na"),
            required=True,
        )
    review.add_argument("--notes", default="")

    attest_all = commands.add_parser("attest-all-approved")
    attest_all.add_argument("--reviewer", required=True)
    attest_all.add_argument(
        "--human-attested",
        action="store_true",
        required=True,
        help="Required acknowledgement that the named human reviewed and approved all 240 cases.",
    )
    attest_all.add_argument("--notes", default="")

    finalize = commands.add_parser("finalize")
    finalize.add_argument(
        "--certificate-output",
        type=Path,
        default=None,
        help="Defaults to <dataset>/review_certificate.json.",
    )

    commands.add_parser("verify")
    return root


def _case_map(bundle) -> dict[str, dict]:
    return {case.case_id: dict(case.as_dict()) for case in bundle.cases}


def _packet_item(case: dict) -> dict:
    return {
        **case,
        "human_review_rubric": {
            "question_sql_match": "中文问题与 Gold SQL 的业务含义是否一致",
            "result_semantics": "Gold SQL 的结果形状、聚合、排序与问题要求是否一致",
            "schema_grounding": "表、字段、过滤值是否真实存在且选择正确",
            "join_correctness": "Join 路径、键与结果粒度是否正确；无 Join 时可填 na",
        },
        "allowed_verdicts": ["approve", "reject"],
        "allowed_check_values": ["pass", "fail", "na"],
    }


def main() -> int:
    args = _parser().parse_args()
    key = read_review_signing_key(args.key_file) if args.key_file is not None else None
    bundle = load_dataset(args.dataset, review_signing_key=key)
    cases = _case_map(bundle)
    with DatasetReviewStore(
        args.store,
        dataset_id=bundle.dataset_id,
        dataset_sha256=bundle.dataset_sha256,
        database_snapshot_id=bundle.database_snapshot_id,
        cases=list(cases.values()),
    ) as store:
        if args.command in {"init", "status"}:
            output = store.status()
        elif args.command == "next":
            case_ids = store.next_case_ids(args.limit, args.split)
            output = {
                "case_ids": list(case_ids),
                "cases": [_packet_item(cases[case_id]) for case_id in case_ids],
            }
        elif args.command == "show":
            if args.case_id not in cases:
                raise ValueError("unknown review case_id: %s" % args.case_id)
            output = _packet_item(cases[args.case_id])
        elif args.command == "packet":
            case_ids = store.next_case_ids(args.limit, args.split)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8") as handle:
                for case_id in case_ids:
                    handle.write(
                        json.dumps(_packet_item(cases[case_id]), ensure_ascii=False) + "\n"
                    )
            output = {
                "output": str(args.output.resolve()),
                "case_count": len(case_ids),
                "case_ids": list(case_ids),
            }
        elif args.command == "review":
            checklist = {
                check: getattr(args, check)
                for check in REVIEW_CHECKS
            }
            event = store.record_review(
                args.case_id, args.reviewer, args.verdict, checklist, args.notes
            )
            output = {"recorded": event, "progress": store.status()}
        elif args.command == "attest-all-approved":
            output = store.attest_all_approved(
                args.reviewer, args.human_attested, args.notes
            )
        elif args.command == "finalize":
            if args.key_file is None:
                raise ValueError("finalize requires --key-file held by the human reviewer")
            certificate = store.build_certificate(key)
            verify_review_certificate(
                certificate,
                key,
                dataset_id=bundle.dataset_id,
                dataset_sha256=bundle.dataset_sha256,
                database_snapshot_id=bundle.database_snapshot_id,
                cases=list(cases.values()),
            )
            destination = args.certificate_output or args.dataset / "review_certificate.json"
            manifest = finalize_dataset_review(args.dataset, destination, certificate)
            verified = load_dataset(args.dataset, review_signing_key=key)
            output = {
                "certificate": str(destination.resolve()),
                "release_eligible": manifest["release_eligible"],
                "human_reviewed_cases": manifest["human_reviewed_cases"],
                "review_evidence": verified.review_evidence,
            }
        elif args.command == "verify":
            if args.key_file is None:
                raise ValueError("verify requires --key-file")
            verified = load_dataset(args.dataset, review_signing_key=key)
            output = {
                "dataset_id": verified.dataset_id,
                "dataset_sha256": verified.dataset_sha256,
                "review_evidence": verified.review_evidence,
            }
        else:
            raise AssertionError("unreachable command")
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
