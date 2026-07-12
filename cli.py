from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from orchestrator import FixturePipelineError, inspect_run, run_fixture_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-fixture":
        return _run_fixture_command(args.fixture_dir, args.output_dir)
    if args.command == "inspect-run":
        return _inspect_run_command(args.db_path, args.run_id)
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Debate Research Agent System CLI")
    subparsers = parser.add_subparsers(dest="command")
    run_fixture = subparsers.add_parser(
        "run-fixture",
        help="Run a deterministic offline fixture pipeline.",
    )
    run_fixture.add_argument("fixture_dir", type=Path)
    run_fixture.add_argument("--output-dir", type=Path, default=None)
    inspect_parser = subparsers.add_parser(
        "inspect-run",
        help="Inspect the persisted state of a possibly partial run.",
    )
    inspect_parser.add_argument("db_path", type=Path)
    inspect_parser.add_argument("run_id", type=str)
    return parser


def _inspect_run_command(db_path: Path, run_id: str) -> int:
    try:
        inspection = inspect_run(str(db_path), UUID(run_id))
    except ValueError:
        print(f"invalid run id: {run_id}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"run not found: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"inspection error: {exc}", file=sys.stderr)
        return 1

    print(f"run_id: {inspection.run_id}")
    print(f"status: {inspection.status.value}")
    print(f"current_stage: {inspection.current_stage.value}")
    print(f"raw_claim: {inspection.raw_claim}")
    print(f"retrieval_attempts: {inspection.retrieval_count}")
    print(f"snapshots: {inspection.snapshot_count}")
    print(f"provisional_extractions: {inspection.provisional_count}")
    print(f"candidates: {inspection.candidate_count}")
    print(f"analyst_decisions: {inspection.analyst_decision_count}")
    print(f"statement_drafts: {inspection.draft_count}")
    print(f"statement_reviews: {inspection.review_count}")
    print(f"ledger_records: {inspection.ledger_count}")
    print(f"model_attempts: {inspection.model_attempt_count}")
    print(f"has_synthesis: {inspection.has_synthesis}")
    print(f"has_validation: {inspection.has_validation}")
    return 0


def _run_fixture_command(fixture_dir: Path, output_dir: Path | None) -> int:
    try:
        result = run_fixture_pipeline(fixture_dir, output_dir=output_dir)
    except FixturePipelineError as exc:
        print(f"fixture pipeline error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"unexpected pipeline error: {exc}", file=sys.stderr)
        return 1

    print(f"run_id: {result.run_id}")
    print(f"result: {result.status}")
    print(f"database: {result.db_path}")
    print(f"audit: {result.audit_path}")
    if result.status == "released":
        print(f"rendered hash: {result.rendered_brief_hash}")
        print("final brief:")
        print(result.final_brief, end="" if result.final_brief.endswith("\n") else "\n")
    else:
        print("rendered hash: none")
        print("validation errors:")
        for error in result.validation_result.errors:
            print(f"- {error.code.value} at {error.location}: {error.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
