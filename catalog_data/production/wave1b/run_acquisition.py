#!/usr/bin/env python3
"""Production Catalog Wave 1B -- manual, operator-invoked acquisition
runner (PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md).

Run by hand, by an operator who has read `sources.json` and decided to
acquire it:

    cd backend && python -m venv/... (existing project venv)
    python ../catalog_data/production/wave1b/run_acquisition.py

Never invoked by the application, a worker, a cron job, or CI -- this
pass explicitly excludes recurring/automatic acquisition (see
PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md's own "re-verification
procedure" section for how a human operator re-runs this deliberately,
later, to check for formulation changes).

Reads `sources.json` (the one, explicit, enumerated URL list this pass
was authorized to fetch -- see that file's own header), fetches each
URL through `app.domain.catalog_acquisition.acquire_url()` (domain-
allowlisted, no crawling, no anti-bot bypass), parses whatever comes
back through the matching per-brand parser, and writes three outputs:

  acquisition_ledger.jsonl  -- one record per URL, success or not
  evidence/<source_evidence_id>.txt  -- the bounded, extracted
                                          ingredient text only (never
                                          the raw HTML page) for every
                                          successful extraction
  manifest.jsonl            -- one Wave 1 SourceManifestRecord-shaped
                                 line per product whose extraction
                                 succeeded AND whose ingredient list is
                                 genuinely complete (ingredient_list_
                                 complete=True) -- a product that
                                 fetched fine but yielded only a
                                 marketing "key ingredients" list, or
                                 no list at all, is recorded in the
                                 ledger with an honest status and is
                                 NOT written to the manifest at all
                                 (never a manifest row claiming
                                 completeness it doesn't have).

A single respectful delay is applied between requests (`REQUEST_DELAY_
SECONDS`), honoring `laroche-posay.us`'s own published `Crawl-delay: 5`
even though every request to that domain returned a bot challenge
during this pass -- the delay is unconditional, not skipped for
domains that happened to work.
"""
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))

import httpx  # noqa: E402

from app.domain.catalog_acquisition import acquire_url, parse_for_domain  # noqa: E402

HERE = Path(__file__).resolve().parent
SOURCES_FILE = HERE / "sources.json"
LEDGER_FILE = HERE / "acquisition_ledger.jsonl"
MANIFEST_FILE = HERE / "manifest.jsonl"
EVIDENCE_DIR = HERE / "evidence"

REQUEST_DELAY_SECONDS = 5.0
SOURCE_NAME = "Wave 1B Manufacturer Acquisition"
JURISDICTION = "us"


def _manifest_ingredient_source(source_domain: str) -> str:
    # Maps the acquisition domain to Wave 1's own closed
    # `ingredient_source` vocabulary (app.domain.catalog_source_adapter).
    # Every domain in this pass's own approved list discloses ingredients
    # directly on the manufacturer's own product page -- the manufacturer-
    # website-disclosure tier, never the retailer/unverified tier.
    return "manufacturer_website_disclosure"


async def main() -> None:
    sources = json.loads(SOURCES_FILE.read_text())["sources"]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    ledger_rows = []
    manifest_rows = []

    async with httpx.AsyncClient() as client:
        for i, source in enumerate(sources):
            if i > 0:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            result = await acquire_url(client, source_evidence_id=source["source_evidence_id"], url=source["url"])

            row = {
                "source_evidence_id": result.source_evidence_id,
                "brand": source["brand"],
                "product_name": source["product_name"],
                "category": source["category"],
                "requested_url": result.requested_url,
                "final_url": result.final_url,
                "http_status": result.http_status,
                "retrieved_at": result.retrieved_at.isoformat(),
                "content_sha256": result.content_sha256,
                "source_domain": result.source_domain,
                "fetch_ok": result.ok,
                "fetch_error_code": result.error_code,
                "fetch_error_detail": result.error_detail,
                "extraction_status": None,
                "ingredient_count": 0,
                "ingredient_list_complete": False,
                "completeness_decision": None,
                "notes": source.get("_note", ""),
            }

            if result.ok and result.body_bytes is not None:
                html = result.body_bytes.decode("utf-8", errors="replace")
                parsed = parse_for_domain(result.source_domain, html)
                row["extraction_status"] = parsed.status
                row["ingredient_count"] = len(parsed.ingredient_list_raw)
                row["ingredient_list_complete"] = parsed.ingredient_list_complete
                if parsed.detail:
                    row["notes"] = (row["notes"] + " " + parsed.detail).strip()

                if parsed.status == "OK" and parsed.ingredient_list_complete and parsed.ingredient_list_raw:
                    row["completeness_decision"] = "VERIFIED_COMPLETE"
                    evidence_text = ", ".join(parsed.ingredient_list_raw) + "\n\nSource: " + result.final_url
                    (EVIDENCE_DIR / f"{result.source_evidence_id}.txt").write_text(evidence_text)

                    manifest_rows.append({
                        "source_evidence_id": result.source_evidence_id,
                        "source_name": SOURCE_NAME,
                        "source_type": "curated_dataset",
                        "source_url": source["url"],
                        "final_url": result.final_url,
                        "content_sha256": result.content_sha256,
                        "retrieved_at": result.retrieved_at.isoformat(),
                        "jurisdiction": JURISDICTION,
                        "brand": source["brand"],
                        "product_name": source["product_name"],
                        "category": source["category"],
                        "product_url": result.final_url,
                        "formulation_version_evidence": None,
                        "ingredient_list_raw": parsed.ingredient_list_raw,
                        # Independent-review Blocker 3: the structured,
                        # concentration-separated companion to
                        # ingredient_list_raw -- see
                        # app.domain.catalog_source_adapter.IngredientDetail's
                        # own docstring for why this is what actually
                        # drives canonical ingredient identity, never the
                        # verbatim (possibly concentration-bearing)
                        # disclosure string.
                        "ingredient_details": [
                            {
                                "position": d.position, "raw_name": d.raw_name,
                                "declared_concentration": d.declared_concentration,
                                "concentration_unit": d.concentration_unit, "section": d.section,
                            }
                            for d in parsed.ingredient_details
                        ],
                        "ingredient_list_complete": True,
                        "ingredient_source": _manifest_ingredient_source(result.source_domain),
                        "verification_date": date.today().isoformat(),
                        "upc": None, "gtin": None, "sku": None, "size_value": None, "size_unit": None,
                        "notes": (
                            f"Acquired {result.retrieved_at.date().isoformat()} from {result.final_url}. "
                            + (parsed.detail if parsed.detail else "Full ingredient list as disclosed by manufacturer.")
                        )[:1000],
                    })
                else:
                    row["completeness_decision"] = "INSUFFICIENT_SOURCE_DATA"
            else:
                row["extraction_status"] = "NOT_FETCHED"
                row["completeness_decision"] = "NOT_ACQUIRED"

            ledger_rows.append(row)
            print(f"{row['source_evidence_id']:45s} fetch_ok={row['fetch_ok']!s:5s} "
                  f"extraction={row['extraction_status']!s:25s} decision={row['completeness_decision']}")

    LEDGER_FILE.write_text("\n".join(json.dumps(r) for r in ledger_rows) + "\n")
    MANIFEST_FILE.write_text("\n".join(json.dumps(r) for r in manifest_rows) + "\n")
    print(f"\nledger: {len(ledger_rows)} rows -> {LEDGER_FILE}")
    print(f"manifest: {len(manifest_rows)} rows -> {MANIFEST_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
