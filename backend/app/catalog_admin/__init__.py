"""Catalog ingestion/administration CLI (CATALOG_INGESTION_ARCHITECTURE.md).
Thin argparse adapter over app/domain/catalog_ingestion_service.py,
app/domain/catalog_publication_service.py, and
app/domain/catalog_review_service.py -- business logic lives in those
domain services, never in an argparse handler (see cli.py's own
docstring). No HTTP route in this pass exposes any of this; this
package is the only way to mutate the catalog this pass ships.
"""
