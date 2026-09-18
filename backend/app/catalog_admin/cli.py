"""Argparse adapter for the catalog ingestion/administration CLI.
Every subcommand handler below does argument parsing and output
formatting ONLY -- the actual behavior lives in
app/domain/catalog_ingestion_service.py, app/domain/
catalog_publication_service.py, and app/domain/catalog_review_service.py.
`run()` takes an already-open pool (the dedicated
`skincare_catalog_admin` connection -- see app/catalog_admin/__main__.py
for how a real invocation obtains one) so it's directly testable
against a real Postgres test database without spawning a subprocess.
"""
import argparse
import dataclasses
import getpass
import json
import sys
from pathlib import Path
from typing import List
from uuid import UUID

import asyncpg

from app.db import catalog_admin_repository as repo
from app.domain.catalog_ingestion_service import CatalogIngestionService, ImportRejectedError
from app.domain.catalog_publication_service import CatalogPublicationService, PublicationError
from app.domain.catalog_review_service import CatalogReviewService, ReviewError
from app.domain.catalog_wave_report import compute_wave_report, list_review_required_for_source, list_verification_status_for_source
from app.domain.catalog_wave_service import WaveManifestRejectedError, import_manifest, validate_manifest_bytes


def _default_actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _print(obj, *, out=sys.stdout) -> None:
    if dataclasses.is_dataclass(obj):
        obj = dataclasses.asdict(obj)
    elif isinstance(obj, list):
        obj = [dataclasses.asdict(o) if dataclasses.is_dataclass(o) else o for o in obj]
    print(json.dumps(obj, default=str, indent=2), file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.catalog_admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-source", help="Register a new catalog ingestion source")
    p.add_argument("--name", required=True)
    p.add_argument("--source-type", required=True)
    p.add_argument("--description")

    p = sub.add_parser("import-file", help="Import a JSON/JSONL file of raw catalog records")
    p.add_argument("path")
    p.add_argument("--source-id", required=True)
    p.add_argument("--format", choices=["json", "jsonl"], default="jsonl")
    p.add_argument("--source-reference")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("validate-batch", help="Run identity/ingredient resolution + validation on a batch")
    p.add_argument("batch_id")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("batch-status", help="Show a batch's current lifecycle state and counts")
    p.add_argument("batch_id")

    p = sub.add_parser("review-list", help="List open human-review items")
    p.add_argument("--reason-code")

    p = sub.add_parser("review-show", help="Show one review item plus recomputed context")
    p.add_argument("review_item_id")

    p = sub.add_parser("resolve-ingredient", help="Resolve an UNKNOWN_INGREDIENT review item")
    p.add_argument("review_item_id")
    # Deliberately no --raw-name: the target raw string is derived from
    # the review item's own identity_key (see CatalogReviewService.
    # _resolve_target_raw_name), never a caller-supplied argument --
    # run `review-show <review_item_id>` first to see which raw string
    # this item represents (`context.unresolved_ingredient_names`).
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--map-to", metavar="INGREDIENT_ID", help="Map to an existing canonical ingredient")
    group.add_argument("--create-canonical", metavar="CANONICAL_NAME", help="Create a new canonical ingredient")
    p.add_argument("--ingredient-type")
    p.add_argument("--inci-name")
    p.add_argument("--actor", default=None)

    p = sub.add_parser("approve", help="Dismiss a non-ingredient review item (identity/market/SKU/etc.)")
    p.add_argument("review_item_id")
    p.add_argument("--resolution", default="IDENTITY_CONFIRMED",
                    help="MAPPED_TO_EXISTING_INGREDIENT|CREATED_NEW_INGREDIENT|CREATED_NEW_ALIAS|"
                         "REJECTED_SOURCE_VALUE|IDENTITY_CONFIRMED|IDENTITY_REJECTED|MANUAL_OVERRIDE")
    p.add_argument("--notes")
    p.add_argument("--actor", default=None)

    p = sub.add_parser("reject", help="Reject an import record via one of its review items")
    p.add_argument("review_item_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default=None)

    p = sub.add_parser("publish", help="Atomically publish a VALIDATED import record")
    p.add_argument("import_record_id")
    p.add_argument("--actor", default=None)
    p.add_argument("--dry-run", action="store_true")

    # -- Production Catalog Wave 1 (PRODUCTION_CATALOG_WAVE_1.md) --
    p = sub.add_parser("validate-source-manifest", help="Wave 1: schema-validate a curated source manifest (no DB access)")
    p.add_argument("path")
    p.add_argument("--format", choices=["json", "jsonl"], default="jsonl")

    p = sub.add_parser("import-source-manifest", help="Wave 1: import a curated source manifest through the existing ingestion pipeline")
    p.add_argument("path")
    p.add_argument("--source-id", required=True)
    p.add_argument("--format", choices=["json", "jsonl"], default="jsonl")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--allow-test-source", action="store_true",
        help="Required to import into a source whose registered name looks like a test fixture (test_/synthetic_/wave1_test_ prefix)",
    )

    p = sub.add_parser("inspect-wave", help="Wave 1: deterministic report for one source (or every registered source without --source-id)")
    p.add_argument("--source-id")

    p = sub.add_parser("report-review-required", help="Wave 1: open review items scoped to one source")
    p.add_argument("--source-id", required=True)

    p = sub.add_parser("report-verification-status", help="Wave 1: per-record VERIFIED/REVIEW_REQUIRED/INSUFFICIENT_SOURCE_DATA/REJECTED state for one source")
    p.add_argument("--source-id", required=True)

    return parser


async def run(argv: List[str], pool: asyncpg.Pool, *, out=sys.stdout, err=sys.stderr) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    actor = getattr(args, "actor", None) or _default_actor()

    try:
        if args.command == "create-source":
            result = await repo.create_source(
                pool, name=args.name, source_type=args.source_type, description=args.description,
            )
            _print(result, out=out)

        elif args.command == "import-file":
            file_bytes = Path(args.path).read_bytes()
            outcome = await CatalogIngestionService(pool).import_file(
                source_id=UUID(args.source_id), file_bytes=file_bytes, file_format=args.format,
                source_reference=args.source_reference, dry_run=args.dry_run,
            )
            _print(outcome, out=out)

        elif args.command == "validate-batch":
            outcomes = await CatalogIngestionService(pool).validate_batch(
                UUID(args.batch_id), dry_run=args.dry_run,
            )
            _print(outcomes, out=out)

        elif args.command == "batch-status":
            batch = await repo.get_batch(pool, UUID(args.batch_id))
            if batch is None:
                print(f"batch not found: {args.batch_id}", file=err)
                return 1
            _print(batch, out=out)

        elif args.command == "review-list":
            items = await CatalogReviewService(pool).list_open(reason_code=args.reason_code)
            _print(items, out=out)

        elif args.command == "review-show":
            detail = await CatalogReviewService(pool).show(UUID(args.review_item_id))
            _print({"review_item": detail.review_item, "context": detail.context}, out=out)

        elif args.command == "resolve-ingredient":
            service = CatalogReviewService(pool)
            if args.map_to:
                result = await service.map_ingredient(
                    UUID(args.review_item_id), ingredient_id=UUID(args.map_to), actor=actor,
                )
            else:
                result = await service.create_ingredient(
                    UUID(args.review_item_id), canonical_name=args.create_canonical,
                    ingredient_type=args.ingredient_type, inci_name=args.inci_name, actor=actor,
                )
            _print(result, out=out)

        elif args.command == "approve":
            result = await CatalogReviewService(pool).dismiss(
                UUID(args.review_item_id), resolution=args.resolution, actor=actor, notes=args.notes,
            )
            _print(result, out=out)

        elif args.command == "reject":
            result = await CatalogReviewService(pool).reject_import_record(
                UUID(args.review_item_id), reason=args.reason, actor=actor,
            )
            _print(result, out=out)

        elif args.command == "publish":
            outcome = await CatalogPublicationService(pool).publish(
                UUID(args.import_record_id), actor=actor, dry_run=args.dry_run,
            )
            _print(outcome, out=out)

        elif args.command == "validate-source-manifest":
            file_bytes = Path(args.path).read_bytes()
            report = validate_manifest_bytes(file_bytes, file_format=args.format)
            _print(report, out=out)

        elif args.command == "import-source-manifest":
            file_bytes = Path(args.path).read_bytes()
            outcome = await import_manifest(
                pool, source_id=UUID(args.source_id), file_bytes=file_bytes, file_format=args.format,
                dry_run=args.dry_run, allow_test_source=args.allow_test_source,
            )
            _print(outcome, out=out)

        elif args.command == "inspect-wave":
            report = await compute_wave_report(pool, source_id=UUID(args.source_id) if args.source_id else None)
            _print(report, out=out)

        elif args.command == "report-review-required":
            items = await list_review_required_for_source(pool, UUID(args.source_id))
            _print(items, out=out)

        elif args.command == "report-verification-status":
            statuses = await list_verification_status_for_source(pool, UUID(args.source_id))
            _print(statuses, out=out)

        else:
            print(f"unknown command: {args.command}", file=err)
            return 1
        return 0

    except (ImportRejectedError, PublicationError, ReviewError, WaveManifestRejectedError) as e:
        print(f"error [{e.code}]: {e}", file=err)
        return 1
    except FileNotFoundError as e:
        print(f"error [FILE_NOT_FOUND]: {e}", file=err)
        return 1
