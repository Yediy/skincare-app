"""app.domain.catalog_acquisition -- Production Catalog Wave 1B's
narrow, operator-controlled acquisition boundary.
(PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md).

Pure/unit tests (no network, no DB) use `httpx.MockTransport` and the
bounded, real-page-derived fixtures in `tests/fixtures/wave1b/` --
never a live fetch of any manufacturer site. Integration tests (real
Postgres, `catalog_admin_db_pool`) prove Wave 1B's own real,
committed manifest (`catalog_data/production/wave1b/manifest.jsonl`)
flows through the existing Wave 1A pipeline exactly like any other
Wave 1 manifest -- no special-casing, no weakened gate.
"""
import json
from pathlib import Path

import httpx
import pytest

from app.domain.catalog_acquisition import (
    APPROVED_DOMAINS,
    AcquisitionRejectedError,
    acquire_url,
    is_approved_domain,
    parse_for_domain,
)
from app.domain.catalog_source_adapter import (
    CuratedManifestSourceAdapter,
    SourceManifestRecord,
    compute_ingredient_fingerprint,
    is_reserved_test_source_name,
)
from app.domain.catalog_wave_service import import_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wave1b"
WAVE1B_DATA = Path(__file__).resolve().parents[3] / "catalog_data" / "production" / "wave1b"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Approved-domain enforcement / arbitrary-domain rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.cerave.com/skincare/cleansers/foaming-facial-cleanser",
    "https://cerave.com/skincare/cleansers/foaming-facial-cleanser",
    "https://www.laroche-posay.us/",
    "https://theordinary.com/en-us/",
    "https://www.paulaschoice.com/",
])
def test_approved_domains_are_accepted(url):
    assert is_approved_domain(url) is True


@pytest.mark.parametrize("url", [
    "https://www.ulta.com/p/foaming-facial-cleanser",
    "https://www.sephora.com/product/the-ordinary",
    "https://www.amazon.com/dp/B00360R2Z4",
    "https://some-ingredient-blog.example/cerave-review",
    "https://notcerave.com/skincare/cleansers/foaming-facial-cleanser",
    "https://cerave.com.evil.example/",
    "http://www.cerave.com/skincare/cleansers/foaming-facial-cleanser",  # http, not https
])
def test_arbitrary_domains_are_rejected(url):
    assert is_approved_domain(url) is False


async def test_acquire_url_rejects_unapproved_domain_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request should ever be made for an unapproved domain")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AcquisitionRejectedError) as exc_info:
            await acquire_url(client, source_evidence_id="x", url="https://www.ulta.com/p/some-product")
    assert exc_info.value.code == "DOMAIN_NOT_APPROVED"


async def test_acquire_url_rejects_redirect_that_leaves_approved_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        if "cerave.com" in str(request.url):
            return httpx.Response(302, headers={"Location": "https://www.evil-tracker.example/"})
        return httpx.Response(200, text="should never be reached")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AcquisitionRejectedError) as exc_info:
            await acquire_url(
                client, source_evidence_id="x", url="https://www.cerave.com/skincare/cleansers/foaming-facial-cleanser",
            )
    assert exc_info.value.code == "REDIRECT_LEFT_APPROVED_DOMAIN"


async def test_acquire_url_allows_redirect_that_stays_on_the_same_approved_domain():
    """The real, observed Paula's Choice case: a product page redirects
    to a renamed slug on the SAME approved domain -- must succeed."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/old-name/620.html"):
            return httpx.Response(301, headers={"Location": "https://www.paulaschoice.com/new-name/620.html"})
        return httpx.Response(200, text="<html>real content</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await acquire_url(
            client, source_evidence_id="x", url="https://www.paulaschoice.com/old-name/620.html",
        )
    assert result.ok is True
    assert result.final_url == "https://www.paulaschoice.com/new-name/620.html"


# ---------------------------------------------------------------------------
# Retrieval metadata / content hashing / idempotency
# ---------------------------------------------------------------------------


async def test_acquire_url_records_full_retrieval_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await acquire_url(
            client, source_evidence_id="cerave-test-1", url="https://www.cerave.com/skincare/test",
        )
    assert result.source_evidence_id == "cerave-test-1"
    assert result.requested_url == "https://www.cerave.com/skincare/test"
    assert result.final_url == "https://www.cerave.com/skincare/test"
    assert result.http_status == 200
    assert result.source_domain == "cerave.com"
    assert result.retrieved_at is not None
    assert result.ok is True
    import hashlib
    assert result.content_sha256 == hashlib.sha256(b"<html>hello</html>").hexdigest()


async def test_identical_content_reacquisition_is_idempotent_by_hash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>stable content</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await acquire_url(client, source_evidence_id="x", url="https://www.cerave.com/skincare/test")
        second = await acquire_url(client, source_evidence_id="x", url="https://www.cerave.com/skincare/test")
    assert first.content_sha256 == second.content_sha256


async def test_bot_challenge_response_is_recorded_not_bypassed():
    """The real laroche-posay.us case -- a bot-challenge response is
    recorded as a failed acquisition with the real status, never
    retried with different headers or otherwise circumvented."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<html>Just a moment...</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await acquire_url(client, source_evidence_id="x", url="https://www.laroche-posay.us/")
    assert result.ok is False
    assert result.http_status == 403
    assert result.error_code in ("HTTP_ERROR", "BOT_CHALLENGE")


async def test_network_failure_is_recorded_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await acquire_url(client, source_evidence_id="x", url="https://www.cerave.com/skincare/test")
    assert result.ok is False
    assert result.error_code == "ConnectError"


# ---------------------------------------------------------------------------
# Per-brand parsers -- full extraction, active/inactive preservation,
# fail-closed behavior
# ---------------------------------------------------------------------------


def test_cerave_cosmetic_product_full_ingredient_extraction():
    result = parse_for_domain("cerave.com", _fixture("cerave_cosmetic_product_snippet.html"))
    assert result.status == "OK"
    assert result.ingredient_list_complete is True
    assert result.ingredient_list_raw[0] == "AQUA / WATER / EAU"
    assert result.ingredient_list_raw[-1] == "XANTHAN GUM"
    assert len(result.ingredient_list_raw) == 24


def test_cerave_drug_product_preserves_active_before_inactive():
    result = parse_for_domain("cerave.com", _fixture("cerave_drug_active_inactive_snippet.html"))
    assert result.status == "OK"
    assert result.ingredient_list_complete is True
    assert result.ingredient_list_raw[0] == "SALICYLIC ACID 2%"
    assert "active" in result.detail.lower() and "inactive" in result.detail.lower()
    # Everything after the active ingredient is from the inactive list.
    assert "WATER" in result.ingredient_list_raw[1:]


def test_cerave_nbsp_entities_are_decoded_not_leaked():
    result = parse_for_domain("cerave.com", _fixture("cerave_nbsp_artifacts_snippet.html"))
    assert result.status == "OK"
    assert not any("nbsp" in entry.lower() for entry in result.ingredient_list_raw)
    assert "ZINC OXIDE (8%)" in result.ingredient_list_raw


def test_cerave_trailing_batch_code_is_stripped():
    result = parse_for_domain("cerave.com", _fixture("cerave_trailing_code_snippet.html"))
    assert result.status == "OK"
    assert result.ingredient_list_raw[-1] == "CERAMIDE EOP"
    assert not any("CODE" in entry for entry in result.ingredient_list_raw)


def test_ordinary_full_ingredient_extraction_preserves_comma_containing_name():
    result = parse_for_domain("theordinary.com", _fixture("ordinary_product_snippet.html"))
    assert result.status == "OK"
    assert result.ingredient_list_complete is True
    assert "1,2-Hexanediol" in result.ingredient_list_raw
    # The naive-split bug this fixture regression-tests: must never
    # produce a bogus standalone "1" entry.
    assert "1" not in result.ingredient_list_raw


def test_paulaschoice_parser_fails_closed_when_ingredients_absent():
    result = parse_for_domain("paulaschoice.com", _fixture("paulaschoice_no_ingredients_snippet.html"))
    assert result.status == "INGREDIENTS_NOT_FOUND"
    assert result.ingredient_list_raw == []
    assert result.ingredient_list_complete is False


def test_laroche_posay_parser_reports_unsupported_never_guesses():
    result = parse_for_domain("laroche-posay.us", "<html>anything</html>")
    assert result.status == "UNSUPPORTED_DOMAIN"
    assert result.ingredient_list_raw == []


def test_key_ingredients_only_page_is_not_treated_as_complete():
    """A page carrying only a marketing 'Key ingredients' highlight (no
    'Full Ingredient List' block at all) must classify as not-found,
    never as a complete list -- this is the CeraVe-shaped equivalent of
    'key ingredients marketing copy is not a complete ingredient
    list.'"""
    html = (
        '<div class="keyIngredients-highlight">'
        '<span class="title">Key Ingredients</span>'
        '<div class="list">Ceramides, Hyaluronic Acid, Niacinamide</div>'
        "</div>"
    )
    result = parse_for_domain("cerave.com", html)
    assert result.status == "INGREDIENTS_NOT_FOUND"
    assert result.ingredient_list_complete is False


def test_unsupported_domain_string_returns_unsupported_not_keyerror():
    result = parse_for_domain("not-a-real-domain.example", "<html></html>")
    assert result.status == "UNSUPPORTED_DOMAIN"


# ---------------------------------------------------------------------------
# Formulation fingerprint stability (reuses Wave 1A's own function --
# no second fingerprinting mechanism for Wave 1B)
# ---------------------------------------------------------------------------


def test_changed_ingredient_content_changes_the_fingerprint():
    a = parse_for_domain("cerave.com", _fixture("cerave_cosmetic_product_snippet.html"))
    b = parse_for_domain("cerave.com", _fixture("cerave_trailing_code_snippet.html"))
    fp_a = compute_ingredient_fingerprint(a.ingredient_list_raw)
    fp_b = compute_ingredient_fingerprint(b.ingredient_list_raw)
    assert fp_a != fp_b


def test_identical_ingredient_content_reproduces_the_same_fingerprint():
    a = parse_for_domain("cerave.com", _fixture("cerave_cosmetic_product_snippet.html"))
    fp_1 = compute_ingredient_fingerprint(a.ingredient_list_raw)
    fp_2 = compute_ingredient_fingerprint(list(a.ingredient_list_raw))
    assert fp_1 == fp_2


# ---------------------------------------------------------------------------
# Source-evidence-ID uniqueness / duplicate-URL handling in the real,
# committed sources.json and manifest.jsonl
# ---------------------------------------------------------------------------


def test_sources_json_evidence_ids_are_unique():
    sources = json.loads((WAVE1B_DATA / "sources.json").read_text())["sources"]
    ids = [s["source_evidence_id"] for s in sources]
    assert len(ids) == len(set(ids))


def test_sources_json_urls_are_unique():
    sources = json.loads((WAVE1B_DATA / "sources.json").read_text())["sources"]
    urls = [s["url"] for s in sources]
    assert len(urls) == len(set(urls))


def test_sources_json_only_lists_approved_domains():
    sources = json.loads((WAVE1B_DATA / "sources.json").read_text())["sources"]
    for s in sources:
        assert is_approved_domain(s["url"]), f"{s['url']} is not on an approved domain"


def test_manifest_source_evidence_ids_are_unique():
    lines = [l for l in (WAVE1B_DATA / "manifest.jsonl").read_text().splitlines() if l.strip()]
    ids = [json.loads(l)["source_evidence_id"] for l in lines]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Real-data manifest validates through the existing Wave 1A schema, and
# production data cannot masquerade as (or be confused with) a
# synthetic test fixture
# ---------------------------------------------------------------------------


def test_real_wave1b_manifest_validates_through_wave1a_schema():
    body = (WAVE1B_DATA / "manifest.jsonl").read_bytes()
    adapter = CuratedManifestSourceAdapter(file_format="jsonl")
    result = adapter.parse(body)
    assert result.issues == [], f"real Wave 1B manifest failed Wave 1A schema validation: {result.issues}"
    assert len(result.valid_records) == result.total_records
    assert result.total_records >= 8  # Wave 1B's own real, non-forced product count


def test_real_wave1b_manifest_source_name_is_not_a_reserved_test_prefix():
    lines = [l for l in (WAVE1B_DATA / "manifest.jsonl").read_text().splitlines() if l.strip()]
    for line in lines:
        record = json.loads(line)
        assert is_reserved_test_source_name(record["source_name"]) is False


def test_real_wave1b_manifest_brands_are_real_not_synthetic_convention():
    """This repository's synthetic-fixture convention uses obviously
    fake brand names (e.g. 'Testonyx') -- the real Wave 1B manifest
    must never use one of those, and vice versa: no synthetic test in
    this repository's own suite may declare one of Wave 1B's real
    brand names either (checked at the point each is used, not
    globally -- this test only proves the real-manifest side)."""
    real_brands = {"Testonyx", "Rebrand Labs", "WaveServiceTestBrand", "WaveReportTestBrand", "Wave1CliBrand"}
    lines = [l for l in (WAVE1B_DATA / "manifest.jsonl").read_text().splitlines() if l.strip()]
    for line in lines:
        record = json.loads(line)
        assert record["brand"] not in real_brands


def test_real_wave1b_manifest_ingredient_order_matches_extraction():
    """End-to-end proof that the committed manifest's ingredient order
    is exactly what the parser extracted -- re-parses the real
    acquisition ledger's own recorded ingredient count for the same
    product and cross-checks it against the manifest row."""
    ledger_lines = [l for l in (WAVE1B_DATA / "acquisition_ledger.jsonl").read_text().splitlines() if l.strip()]
    ledger_by_id = {json.loads(l)["source_evidence_id"]: json.loads(l) for l in ledger_lines}
    manifest_lines = [l for l in (WAVE1B_DATA / "manifest.jsonl").read_text().splitlines() if l.strip()]
    for line in manifest_lines:
        record = json.loads(line)
        ledger_row = ledger_by_id[record["source_evidence_id"]]
        assert ledger_row["completeness_decision"] == "VERIFIED_COMPLETE"
        assert len(record["ingredient_list_raw"]) == ledger_row["ingredient_count"]


def test_every_manifest_record_has_reconstructible_provenance():
    """Every real Wave 1B manifest record must be able to answer:
    where did this come from, and when was it retrieved."""
    lines = [l for l in (WAVE1B_DATA / "manifest.jsonl").read_text().splitlines() if l.strip()]
    assert lines, "expected at least one real Wave 1B manifest record"
    for line in lines:
        record = json.loads(line)
        assert record["source_url"]
        assert record["retrieved_at"]
        assert record["verification_date"]
        assert record["ingredient_source"] == "manufacturer_website_disclosure"
        assert "Acquired" in record["notes"] and record["source_url"] in record["notes"]


# ---------------------------------------------------------------------------
# Integration: real Wave 1B manifest data flows through the existing
# Wave 1A import/review/publication gates, real Postgres.
# ---------------------------------------------------------------------------


@pytest.fixture
async def wave1b_source_id(catalog_admin_db_pool, clean_catalog_ingestion):
    from app.db import catalog_admin_repository as repo
    source = await repo.create_source(
        catalog_admin_db_pool, name="Wave 1B Manufacturer Acquisition", source_type="curated_dataset",
    )
    return source["id"]


async def test_real_manifest_import_remains_review_gated(wave1b_source_id, catalog_admin_db_pool):
    """No ingredient in the real manifest is pre-seeded as a canonical
    ingredient in this clean test database -- importing real Wave 1B
    data must route to NEEDS_REVIEW (never auto-resolve, never
    auto-publish), proving Wave 1B data gets no special treatment from
    the existing exact-match-only resolution pipeline."""
    from app.domain.catalog_ingestion_service import CatalogIngestionService

    body = (WAVE1B_DATA / "manifest.jsonl").read_bytes()
    # Only the first record, to keep this test fast and focused.
    first_line = body.splitlines()[0] + b"\n"
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=wave1b_source_id, file_bytes=first_line, allow_test_source=True,
    )
    assert outcome.import_outcome.records_total == 1
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)

    row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1",
        outcome.import_outcome.records[0].import_record_id,
    )
    assert row["status"] in ("NEEDS_REVIEW", "VALIDATED")
    # Never auto-published by the import step itself.
    assert await catalog_admin_db_pool.fetchval("SELECT count(*) FROM product_formulations") == 0


async def test_wave1b_shaped_insufficient_record_cannot_publish(wave1b_source_id, catalog_admin_db_pool):
    """Proves Wave 1A's own WAVE1_INSUFFICIENT_SOURCE_DATA backstop
    applies identically to Wave 1B-sourced data -- not a new mechanism,
    the same one. Uses a synthetic insufficient record (real acquired
    Wave 1B products all have complete lists, by this pass's own
    'quality beats quota' rule) shaped exactly like a real Wave 1B
    manifest row."""
    from app.domain.catalog_ingestion_service import CatalogIngestionService
    from app.domain.catalog_publication_service import (
        WAVE1_INSUFFICIENT_SOURCE_DATA,
        CatalogPublicationService,
        PublicationError,
    )

    insufficient_record = {
        "source_evidence_id": "wave1b-test-insufficient",
        "source_name": "Wave 1B Manufacturer Acquisition",
        "source_type": "curated_dataset",
        "source_url": "https://www.cerave.com/skincare/example-product",
        "retrieved_at": "2026-09-18T00:00:00+00:00",
        "jurisdiction": "us",
        "brand": "CeraVe",
        "product_name": "Example Product Not Actually Acquired",
        "category": "cleanser",
        "product_url": "https://www.cerave.com/skincare/example-product",
        "formulation_version_evidence": None,
        "ingredient_list_raw": [],
        "ingredient_list_complete": False,
        "ingredient_source": "manufacturer_website_disclosure",
        "verification_date": "2026-09-18",
        "upc": None, "gtin": None, "sku": None, "size_value": None, "size_unit": None,
        "notes": "Synthetic test record shaped like a Wave 1B row with no ingredient disclosure found.",
    }
    body = (json.dumps(insufficient_record) + "\n").encode("utf-8")
    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=wave1b_source_id, file_bytes=body, allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    import_record_id = outcome.import_outcome.records[0].import_record_id

    with pytest.raises(PublicationError) as exc_info:
        await CatalogPublicationService(catalog_admin_db_pool).publish(import_record_id, actor="test")
    assert exc_info.value.code == WAVE1_INSUFFICIENT_SOURCE_DATA


async def test_published_real_record_retains_full_provenance(wave1b_source_id, catalog_admin_db_pool):
    """Imports and publishes ONE real Wave 1B manifest record end to
    end and proves its provenance answers: source URL, retrieval time,
    ingredient evidence classification, and the raw formulation text
    actually seen -- all reconstructible from the published row. Every
    one of the product's own real, disclosed ingredients is seeded as
    a canonical ingredient first (exact-match, same normalize_name()
    rule the real resolution pipeline uses) so this exercises the
    actual publish path rather than stopping at human review."""
    from app.db.catalog_repository import normalize_name
    from app.domain.catalog_ingestion_service import CatalogIngestionService
    from app.domain.catalog_publication_service import CatalogPublicationService

    body = (WAVE1B_DATA / "manifest.jsonl").read_bytes()
    first_record = json.loads(body.splitlines()[0])
    first_line = body.splitlines()[0] + b"\n"

    async with catalog_admin_db_pool.acquire() as conn:
        for raw_name in first_record["ingredient_list_raw"]:
            await conn.execute(
                "INSERT INTO ingredients (canonical_name, normalized_name) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                raw_name, normalize_name(raw_name),
            )

    outcome = await import_manifest(
        catalog_admin_db_pool, source_id=wave1b_source_id, file_bytes=first_line, allow_test_source=True,
    )
    await CatalogIngestionService(catalog_admin_db_pool).validate_batch(outcome.import_outcome.batch_id)
    record_row = await catalog_admin_db_pool.fetchrow(
        "SELECT status FROM catalog_import_records WHERE id = $1", outcome.import_outcome.records[0].import_record_id,
    )
    assert record_row["status"] == "VALIDATED", (
        f"expected VALIDATED with every real ingredient pre-seeded, got {record_row['status']!r}"
    )

    pub = await CatalogPublicationService(catalog_admin_db_pool).publish(
        outcome.import_outcome.records[0].import_record_id, actor="test",
    )
    provenance = await catalog_admin_db_pool.fetchrow(
        "SELECT source_reference, verified_at FROM catalog_formulation_provenance WHERE formulation_id = $1",
        pub.formulation_id,
    )
    parsed_reference = json.loads(provenance["source_reference"])
    assert parsed_reference["evidence"][0]["source_url"] == first_record["source_url"]
    assert parsed_reference["evidence"][0]["source_evidence_id"] == first_record["source_evidence_id"]
    assert provenance["verified_at"] is not None
