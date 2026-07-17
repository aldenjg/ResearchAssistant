from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from orchestrator import (
    FixturePipelineError,
    RunBudgets,
    inspect_run,
    run_fixture_pipeline,
    run_live,
)
from providers.llm import (
    LLMProvider,
    OpenAICompatibleLLMProvider,
    missing_llm_configuration,
    model_map_from_env,
)
from providers.scraper import ScraperProvider, UrllibScraperProvider
from providers.search import (
    SearchProvider,
    SearchProviderError,
    build_search_provider,
    missing_search_configuration,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-fixture":
        return _run_fixture_command(args.fixture_dir, args.output_dir)
    if args.command == "inspect-run":
        return _inspect_run_command(args.db_path, args.run_id)
    if args.command == "run":
        return _run_live_command(args)
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
    run_parser = subparsers.add_parser(
        "run",
        help="Run a live provider-backed research run against the web.",
    )
    run_parser.add_argument("claim", type=str, help="The exact claim text to research.")
    run_parser.add_argument("--db", type=Path, default=Path("live_runs.sqlite3"))
    run_parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Existing run ID to resume after a failure or cancellation.",
    )
    run_parser.add_argument(
        "--search-vendor",
        type=str,
        default=None,
        help="Search vendor (brave or serper); defaults to SEARCH_PROVIDER env or brave.",
    )
    run_parser.add_argument(
        "--max-model-attempts",
        type=int,
        default=None,
        help="Model-attempt budget for the run (default 120).",
    )
    return parser


def _build_live_providers(
    search_vendor: str | None,
) -> tuple[LLMProvider, SearchProvider, ScraperProvider]:
    """Assemble live providers from environment configuration.

    Kept as a separate seam so tests can substitute fake providers.
    """
    llm = OpenAICompatibleLLMProvider(model_map=model_map_from_env())
    search = build_search_provider(search_vendor)
    scraper = UrllibScraperProvider()
    return llm, search, scraper


def _missing_live_configuration(search_vendor: str | None) -> list[str]:
    return [*missing_llm_configuration(), *missing_search_configuration(search_vendor)]


def _run_live_command(args: argparse.Namespace) -> int:
    problems = _missing_live_configuration(args.search_vendor)
    if problems:
        print("live run configuration is incomplete:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        print("Set the variables in .env (see .env.example) and retry.", file=sys.stderr)
        return 2

    try:
        run_id = UUID(args.run_id) if args.run_id else None
    except ValueError:
        print(f"invalid run id: {args.run_id}", file=sys.stderr)
        return 2
    try:
        llm, search, scraper = _build_live_providers(args.search_vendor)
    except (SearchProviderError, ValueError) as exc:
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2

    budgets = (
        RunBudgets(max_model_attempts=args.max_model_attempts)
        if args.max_model_attempts is not None
        else None
    )
    result = run_live(
        raw_claim=args.claim,
        db_path=str(args.db),
        llm_provider=llm,
        search_provider=search,
        scraper_provider=scraper,
        budgets=budgets,
        run_id=run_id,
    )

    print(f"run_id: {result.run_id}")
    print(f"status: {result.status}")
    print(f"database: {args.db}")
    print(f"ledger records: {result.ledger_count}")
    if result.status == "released":
        print(f"rendered hash: {result.rendered_brief_hash}")
        print("final brief:")
        print(result.final_brief, end="" if result.final_brief.endswith("\n") else "\n")
        return 0
    if result.status == "blocked":
        print("rendered hash: none")
        print("release blocked by the final validator:")
        for error in result.validation_result.errors:
            print(f"- {error.code.value} at {error.location}: {error.message}")
        return 0
    print(f"failure reason: {result.failure_reason}")
    print(f'resume with: python cli.py run "{args.claim}" --db {args.db} --run-id {result.run_id}')
    return 1


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
