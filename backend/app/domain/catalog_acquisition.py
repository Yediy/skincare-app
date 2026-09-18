"""Production Catalog Wave 1B -- controlled, operator-invoked
acquisition of real manufacturer product pages
(PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md).

This is explicitly **not** a crawler. There is no discovery, no
recursive link traversal, no sitemap-driven enumeration at runtime, and
no retailer/aggregator fetching anywhere in this module. The only input
`acquire_url()` accepts is one exact URL an operator has already
decided to fetch (declared in `catalog_data/production/wave1b/
sources.json` -- see that file's own header) -- the same "bytes an
operator already has decided to fetch" posture
`CatalogIngestionService.import_file()` and Wave 1's own
`CatalogSourceAdapter` already establish one layer up, extended one
layer further back to the acquisition step itself.

Domain allowlist (Section "Initial approved manufacturer domains"):
only `cerave.com`, `laroche-posay.us`, `theordinary.com`,
`paulaschoice.com` (and their `www.` subdomains) may ever be fetched by
this module. Checked before EVERY request `acquire_url()` issues --
the original URL and each individual redirect hop alike -- so an
off-domain destination is never sent a request at all, whether it's
the URL an operator supplied or one a same-domain page happened to
redirect to. A redirect must additionally stay on the SAME
registrable domain as the original request; two independently
approved manufacturer domains redirecting into each other is refused
just as an unapproved domain would be (see `acquire_url()`'s own
docstring for the full rule).

No authentication bypass, no anti-bot circumvention: `acquire_url()`
sends one plain GET with a standard, honestly-identifying User-Agent
and respects whatever the server returns, including a bot-challenge
response (recorded as a failed acquisition with the real HTTP status,
never retried with evasive headers, never solved). See
PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md's own "known source
limitations" section for exactly where this happened in practice
(`laroche-posay.us` returned an active Cloudflare bot challenge for
every request, including the plain homepage, and was excluded from
this pass entirely rather than circumvented).

Per-brand ingredient parsers are deliberately small, explicit, and
conservative (Section "Parser design") -- each one recognizes exactly
the one page structure it was written against and returns a
`PARSE_FAILED`/`INGREDIENTS_NOT_FOUND` result (never a guess) the
moment that structure isn't present. No universal HTML scraper exists
in this module.
"""
from __future__ import annotations

import hashlib
import html as html_module
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

# ---------------------------------------------------------------------------
# Approved domain allowlist
# ---------------------------------------------------------------------------

APPROVED_DOMAINS = frozenset({
    "cerave.com",
    "laroche-posay.us",
    "theordinary.com",
    "paulaschoice.com",
})

_USER_AGENT = "SkincareAppCatalogAcquisition/1.0 (+contact: catalog-admin; manual operator-controlled fetch, not a crawler)"


class AcquisitionRejectedError(Exception):
    """Raised before any HTTP request is made (`code='DOMAIN_NOT_APPROVED'`)
    or after a redirect resolves off the approved allowlist
    (`code='REDIRECT_LEFT_APPROVED_DOMAIN'`) -- never a raw message
    string a caller would have to parse."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def _registrable_domain(netloc: str) -> str:
    """Strips a leading `www.` only -- deliberately not a full public-
    suffix-list implementation (this module only ever needs to compare
    against four known, hardcoded domains, never arbitrary ones)."""
    host = netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_approved_domain(url: str) -> bool:
    """True only when `url`'s host is exactly one of `APPROVED_DOMAINS`
    (or its `www.` subdomain) -- never a substring/suffix match (that
    would let `notcerave.com` or `cerave.com.evil.example` pass)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    return _registrable_domain(parsed.netloc) in APPROVED_DOMAINS


@dataclass
class AcquisitionResult:
    """One record of one fetch attempt -- the acquisition ledger's own
    unit (`catalog_data/production/wave1b/acquisition_ledger.jsonl`).
    Always produced, whether the fetch succeeded or not -- a rejected/
    failed acquisition is still evidence of what was attempted, when,
    and why it didn't yield usable data."""

    source_evidence_id: str
    requested_url: str
    final_url: Optional[str]
    http_status: Optional[int]
    retrieved_at: datetime
    content_sha256: Optional[str]
    source_domain: str
    ok: bool
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    body_bytes: Optional[bytes] = field(default=None, repr=False)


MAX_REDIRECTS = 5

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


async def acquire_url(
    client: httpx.AsyncClient, *, source_evidence_id: str, url: str, timeout_seconds: float = 20.0,
) -> AcquisitionResult:
    """Fetches exactly one operator-supplied URL. Rejects outright
    (`AcquisitionRejectedError`, no request made) if `url` itself is
    not on an approved domain.

    Independent-review Blocker 1: redirects are followed MANUALLY, one
    hop at a time (`follow_redirects=False` on every request this
    function issues), with the destination of EVERY hop -- including
    the very first URL -- validated BEFORE any request to it is ever
    sent. Passing `follow_redirects=True` to httpx (the prior design)
    lets httpx complete the ENTIRE redirect chain, including a request
    to an off-domain destination, before this function ever gets a
    chance to inspect `response.url` -- by then the prohibited host has
    already been contacted. That is exactly the mistake this rewrite
    closes: the off-domain host is never sent a request at all, not
    merely "the result is discarded afterward."

    Every hop must additionally stay on the SAME registrable domain as
    the ORIGINAL request -- not merely "some approved domain" (Section
    "Required architecture"): `cerave.com` redirecting to
    `theordinary.com` is rejected even though both are independently
    approved, since nothing about this acquisition ever asked for
    manufacturer B's content when it requested manufacturer A's page.
    A same-domain redirect (a manufacturer renaming a product slug,
    `www.` normalization, a relative `Location` header) is fine and
    expected -- a real case this pass encountered, see the Wave 1B doc.

    Bounded to `MAX_REDIRECTS` hops (`TOO_MANY_REDIRECTS`) -- an
    unbounded manual redirect loop would itself be a new failure mode
    this rewrite must not introduce.

    Never raises for an ordinary HTTP-level failure (4xx/5xx, a bot
    challenge, a timeout, a redirect status with no usable `Location`)
    -- those are recorded as `ok=False` results with the real status/
    error captured, exactly like any other piece of acquisition
    evidence. `AcquisitionRejectedError` is reserved for the domain-
    allowlist violations above, where trusting the response at all
    would be the actual mistake."""
    retrieved_at = datetime.now(timezone.utc)
    original_domain = _registrable_domain(urlparse(url).netloc)
    current_url = url
    hop = 0

    while True:
        if not is_approved_domain(current_url):
            if hop == 0:
                raise AcquisitionRejectedError(
                    f"{current_url!r} is not on an approved manufacturer domain ({sorted(APPROVED_DOMAINS)})",
                    code="DOMAIN_NOT_APPROVED",
                )
            raise AcquisitionRejectedError(
                f"request to {url!r} redirected to {current_url!r}, which is off the approved domain allowlist",
                code="REDIRECT_LEFT_APPROVED_DOMAIN",
            )
        current_domain = _registrable_domain(urlparse(current_url).netloc)
        if current_domain != original_domain:
            raise AcquisitionRejectedError(
                f"request to {url!r} redirected to {current_url!r} (domain {current_domain!r}), which "
                f"leaves the original request's own domain ({original_domain!r}) -- a redirect between "
                "two INDEPENDENTLY approved manufacturer domains is not the same manufacturer's own "
                "content and is never followed",
                code="REDIRECT_LEFT_APPROVED_DOMAIN",
            )

        try:
            response = await client.get(
                current_url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=timeout_seconds, follow_redirects=False,
            )
        except httpx.HTTPError as e:
            return AcquisitionResult(
                source_evidence_id=source_evidence_id, requested_url=url, final_url=None, http_status=None,
                retrieved_at=retrieved_at, content_sha256=None, source_domain=original_domain, ok=False,
                error_code=e.__class__.__name__, error_detail=str(e),
            )

        if response.status_code in _REDIRECT_STATUS_CODES:
            location = response.headers.get("location")
            if not location:
                return AcquisitionResult(
                    source_evidence_id=source_evidence_id, requested_url=url, final_url=current_url,
                    http_status=response.status_code, retrieved_at=retrieved_at, content_sha256=None,
                    source_domain=original_domain, ok=False, error_code="HTTP_ERROR",
                    error_detail=f"HTTP {response.status_code} redirect with no Location header",
                )
            hop += 1
            if hop > MAX_REDIRECTS:
                raise AcquisitionRejectedError(
                    f"request to {url!r} exceeded {MAX_REDIRECTS} redirects", code="TOO_MANY_REDIRECTS",
                )
            current_url = urljoin(current_url, location)
            continue

        break

    final_url = current_url
    body = response.content
    content_sha256 = hashlib.sha256(body).hexdigest()

    if response.status_code >= 400 or "cf-mitigated" in {k.lower() for k in response.headers}:
        return AcquisitionResult(
            source_evidence_id=source_evidence_id, requested_url=url, final_url=final_url,
            http_status=response.status_code, retrieved_at=retrieved_at, content_sha256=content_sha256,
            source_domain=original_domain, ok=False,
            error_code="HTTP_ERROR" if response.status_code >= 400 else "BOT_CHALLENGE",
            error_detail=f"HTTP {response.status_code}",
        )

    return AcquisitionResult(
        source_evidence_id=source_evidence_id, requested_url=url, final_url=final_url,
        http_status=response.status_code, retrieved_at=retrieved_at, content_sha256=content_sha256,
        source_domain=original_domain, ok=True, body_bytes=body,
    )


# ---------------------------------------------------------------------------
# Per-brand ingredient extraction -- small, explicit, conservative.
# Each function recognizes exactly the one page structure it was
# written against (verified live on 2026-09-18 -- see
# PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md) and fails closed the moment
# that structure is missing. No universal scraper.
# ---------------------------------------------------------------------------


@dataclass
class IngredientDetail:
    """Independent-review Blocker 3: the clean chemical identity
    (`raw_name`, never concentration-bearing) plus a structured
    `declared_concentration`/`concentration_unit`, parallel by
    `position` to `ParsedIngredients.ingredient_list_raw`'s verbatim
    disclosure text -- see `catalog_source_adapter.IngredientDetail`,
    the manifest-schema twin of this acquisition-side dataclass (kept
    as two separate types deliberately: this one is this module's own
    internal parse result, never a manifest object itself; `run_
    acquisition.py` converts one into the other)."""

    position: int
    raw_name: str
    declared_concentration: Optional[float] = None
    concentration_unit: Optional[str] = None
    section: Optional[str] = None  # "active" | "inactive" | None


@dataclass
class ParsedIngredients:
    status: str  # "OK" | "PARSE_FAILED" | "INGREDIENTS_NOT_FOUND" | "UNSUPPORTED_DOMAIN"
    ingredient_list_raw: List[str] = field(default_factory=list)
    ingredient_details: List[IngredientDetail] = field(default_factory=list)
    ingredient_list_complete: bool = False
    detail: str = ""


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_CODE_RE = re.compile(r"\s*-\s*\(CODE[^)]*\)\s*$", re.IGNORECASE)


def _strip_html(fragment: str) -> str:
    """Strips tags, then decodes HTML entities (`&nbsp;`, `&amp;`, ...)
    -- entity decoding must run AFTER tag stripping (an entity could in
    principle decode to `<`/`>` text) and its result (a real Unicode
    non-breaking space, U+00A0, for `&nbsp;`) is folded into ordinary
    whitespace by the final `_WHITESPACE_RE` pass, so `&nbsp;ZINC
    OXIDE&nbsp;` (observed verbatim on one CeraVe page, wrapping a
    hyperlinked ingredient name) becomes clean `ZINC OXIDE`, never
    leaking a raw entity or an invisible non-breaking space into stored
    evidence."""
    unescaped = html_module.unescape(_TAG_RE.sub("", fragment))
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


_SPLIT_COMMA_RE = re.compile(r",(?!\d)")


def _split_ingredient_list(text: str) -> List[str]:
    """Splits a comma-separated INCI disclosure into individual, cleaned
    entries -- strips a single trailing period on the whole list
    (standard INCI-list punctuation, not part of the last ingredient's
    own name) and any trailing manufacturer batch/formula code
    (`- (CODE ...)`, observed on one CeraVe page) before splitting.

    Splits on a comma only when NOT immediately followed by a digit --
    a real INCI ingredient separator is always "comma, space"
    (space right after the comma), while a numeric locant inside a
    single ingredient's own chemical name (e.g. `1,2-Hexanediol`,
    `1,3-Propanediol`) never has a space there. A naive `str.split(",")`
    would incorrectly shatter `1,2-Hexanediol` into the two bogus
    entries `"1"` and `"2-Hexanediol"` -- caught on a real page during
    this pass's own acquisition (see
    PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md), fixed here rather than
    silently shipping corrupted evidence."""
    cleaned = _TRAILING_CODE_RE.sub("", text).strip()
    if cleaned.endswith("."):
        cleaned = cleaned[:-1]
    entries = [_WHITESPACE_RE.sub(" ", e).strip() for e in _SPLIT_COMMA_RE.split(cleaned)]
    return [e for e in entries if e]


# Independent-review Blocker 3: a manufacturer's own disclosed text
# sometimes bakes a declared concentration directly into the
# ingredient string -- both forms observed on real, live-fetched
# pages this pass acquired: a trailing "NAME N%" (no parentheses,
# e.g. CeraVe's "SALICYLIC ACID 2%") and a parenthetical "NAME (N%)"
# (e.g. CeraVe's "HOMOSALATE (10%)"). Both are recognized and split
# into (clean_name, concentration_value, "%") -- the concentration
# never stays fused into what becomes an ingredient's own canonical
# identity one layer up (see catalog_source_adapter.IngredientDetail).
# An entry with neither shape (the overwhelming majority of any real
# INCI list) is returned unchanged with concentration=None -- this is
# a real, narrow pattern match, never a guess.
_CONCENTRATION_PAREN_RE = re.compile(r"^(.*?)\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*$")
_CONCENTRATION_TRAILING_RE = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)?)\s*%\s*$")


def _extract_concentration(entry: str) -> Tuple[str, Optional[float], Optional[str]]:
    for pattern in (_CONCENTRATION_PAREN_RE, _CONCENTRATION_TRAILING_RE):
        m = pattern.match(entry)
        if not m:
            continue
        name = m.group(1).strip()
        if not name:
            continue
        try:
            return name, float(m.group(2)), "%"
        except ValueError:
            continue
    return entry, None, None


def _build_plain_details(entries: List[str], *, section: Optional[str] = None, start_position: int = 1) -> List[IngredientDetail]:
    """Builds parallel `IngredientDetail`s for a flat list of already-
    split, already-cleaned disclosure entries -- `start_position` lets
    a caller concatenate an active-section block and an inactive-
    section block (see `parse_cerave_page`) while keeping one
    contiguous 1-based position sequence across both."""
    details = []
    for i, entry in enumerate(entries):
        name, concentration, unit = _extract_concentration(entry)
        details.append(IngredientDetail(
            position=start_position + i, raw_name=name, declared_concentration=concentration,
            concentration_unit=unit, section=section,
        ))
    return details


def parse_cerave_page(html: str) -> ParsedIngredients:
    """CeraVe (`cerave.com`) product pages embed the full disclosed
    ingredient text, server-rendered, inside a
    `<div class="richtext keyIngredients-details__content">` block
    immediately following the "Full Ingredient List" toggle -- no
    JavaScript execution required. Two page shapes observed:

    1. Cosmetic products: one plain `<p>` (or `<p style="...">`)
       containing the full comma-separated INCI list.
    2. OTC drug products (e.g. an acne treatment with a Drug Facts
       panel): a `<p>` beginning "Active Ingredients:" followed by a
       `<p>` beginning "Inactive Ingredients:" -- both are folded into
       one ordered list, active ingredient(s) first (matching the
       manufacturer's own disclosed order), with the active/inactive
       split preserved in this result's `detail` field as source
       evidence (never silently merged without a record of which
       ingredients were active).

    A trailing disclaimer paragraph ("ingredient lists...are updated
    regularly...refer to the ingredient list on your product package")
    is recognized and excluded -- it is provenance-worthy text (kept
    verbatim in the acquisition ledger), never an ingredient entry.
    """
    marker = "keyIngredients-details__content"
    start = html.find(marker)
    if start < 0:
        return ParsedIngredients(status="INGREDIENTS_NOT_FOUND", detail="no keyIngredients-details__content block")

    # Bounded window -- the container's own closing </div> follows
    # shortly after its <p> children; 4000 chars comfortably covers
    # every real page observed without scanning the rest of the
    # document.
    window = html[start:start + 4000]
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", window, re.S)
    if not paragraphs:
        return ParsedIngredients(status="PARSE_FAILED", detail="keyIngredients block has no <p> children")

    active_text: Optional[str] = None
    inactive_text: Optional[str] = None
    plain_text: Optional[str] = None

    for raw_p in paragraphs:
        text = _strip_html(raw_p)
        if not text or text in ("&nbsp;",):
            continue
        lowered = text.lower()
        if "updated regularly" in lowered or "refer to the ingredient list" in lowered:
            continue  # disclaimer, not an ingredient entry
        if lowered.startswith("active ingredient"):
            active_text = re.sub(r"(?i)^active ingredients?:\s*", "", text)
            continue
        if lowered.startswith("inactive ingredient"):
            inactive_text = re.sub(r"(?i)^inactive ingredients?:\s*", "", text)
            continue
        if plain_text is None:
            plain_text = text

    if active_text or inactive_text:
        # Independent-review Blocker 2: a Drug Facts panel is a single
        # disclosure with TWO required sections. Finding only one
        # (a page layout this parser doesn't fully recognize, a
        # missing paragraph, a future markup change) must never be
        # reported as a complete ingredient list -- fail closed rather
        # than silently publishing a half-disclosure as if it were the
        # whole one.
        if not (active_text and inactive_text):
            missing = "Inactive Ingredients" if active_text else "Active Ingredients"
            return ParsedIngredients(
                status="PARSE_FAILED",
                detail=(
                    f"Drug Facts page markup found only the {'Active' if active_text else 'Inactive'} "
                    f"Ingredients section (missing {missing}) -- refusing to report a complete ingredient "
                    "list from a partial Drug Facts disclosure"
                ),
            )
        active_entries = _split_ingredient_list(active_text)
        inactive_entries = _split_ingredient_list(inactive_text)
        if not active_entries or not inactive_entries:
            return ParsedIngredients(
                status="PARSE_FAILED",
                detail="active/inactive labels found but no entries extracted from one or both sections",
            )
        entries = active_entries + inactive_entries
        details = (
            _build_plain_details(active_entries, section="active", start_position=1)
            + _build_plain_details(inactive_entries, section="inactive", start_position=len(active_entries) + 1)
        )
        return ParsedIngredients(
            status="OK", ingredient_list_raw=entries, ingredient_details=details, ingredient_list_complete=True,
            detail="Drug Facts label: active ingredient(s) listed first, then inactive ingredients, per FDA OTC labeling convention.",
        )

    if plain_text:
        entries = _split_ingredient_list(plain_text)
        if not entries:
            return ParsedIngredients(status="PARSE_FAILED", detail="ingredient paragraph found but no entries extracted")
        return ParsedIngredients(
            status="OK", ingredient_list_raw=entries, ingredient_details=_build_plain_details(entries),
            ingredient_list_complete=True, detail="",
        )

    return ParsedIngredients(status="INGREDIENTS_NOT_FOUND", detail="keyIngredients block present but no usable paragraph")


_ORDINARY_INGREDIENTS_RE = re.compile(r'data-original-ingredients="([^"]*)"')


def parse_ordinary_page(html: str) -> ParsedIngredients:
    """The Ordinary (`theordinary.com`) product pages embed the
    authoritative disclosed ingredient list, server-rendered, in a
    `data-original-ingredients="..."` HTML attribute -- plain text, no
    nested markup, comma-separated, ending in a period."""
    m = _ORDINARY_INGREDIENTS_RE.search(html)
    if not m:
        return ParsedIngredients(status="INGREDIENTS_NOT_FOUND", detail="no data-original-ingredients attribute")
    text = m.group(1).strip()
    if not text:
        return ParsedIngredients(status="PARSE_FAILED", detail="data-original-ingredients attribute is empty")
    entries = _split_ingredient_list(text)
    if not entries:
        return ParsedIngredients(status="PARSE_FAILED", detail="data-original-ingredients present but no entries extracted")
    return ParsedIngredients(
        status="OK", ingredient_list_raw=entries, ingredient_details=_build_plain_details(entries),
        ingredient_list_complete=True, detail="",
    )


def parse_laroche_posay_page(html: str) -> ParsedIngredients:
    """`laroche-posay.us` returned an active Cloudflare bot challenge
    for every request attempted during this pass's acquisition run,
    including the plain homepage (no product page was ever actually
    retrieved -- see PRODUCTION_CATALOG_WAVE_1B_REAL_DATA.md). No page
    structure was ever verified, so no parser is written against one --
    writing a speculative parser for markup never actually observed
    would be exactly the "brittle universal scraper" this pass's brief
    warns against. This function exists only so the per-domain parser
    dispatch table (`PARSERS`) is complete and callers get an honest,
    typed "not supported" result rather than a KeyError."""
    return ParsedIngredients(status="UNSUPPORTED_DOMAIN", detail="laroche-posay.us blocked acquisition (Cloudflare bot challenge) -- no page structure was ever observed")


def parse_paulaschoice_page(html: str) -> ParsedIngredients:
    """`paulaschoice.com` product pages were successfully fetched
    (HTTP 200, real content, including a `application/ld+json` Product
    block with name/SKU/reviews) during this pass's acquisition run,
    but the full ingredient disclosure is not present anywhere in the
    server-rendered HTML on any page checked -- it is loaded by
    client-side JavaScript this module does not execute (no headless
    browser, no JS engine -- see this module's own docstring on why).
    This is a real, verified, current limitation, not a guess: this
    function actually looks for the disclosure and returns
    `INGREDIENTS_NOT_FOUND` (never a fabricated result) when, as
    observed on every real page fetched this pass, it isn't there."""
    m = re.search(r'"ingredients"\s*:\s*"([^"]*)"', html)
    if m and m.group(1).strip():
        entries = _split_ingredient_list(m.group(1).strip())
        if entries:
            return ParsedIngredients(
                status="OK", ingredient_list_raw=entries, ingredient_details=_build_plain_details(entries),
                ingredient_list_complete=True, detail="",
            )
    return ParsedIngredients(
        status="INGREDIENTS_NOT_FOUND",
        detail="ingredient disclosure not present in server-rendered HTML (loaded client-side; not fetched by this module)",
    )


PARSERS = {
    "cerave.com": parse_cerave_page,
    "laroche-posay.us": parse_laroche_posay_page,
    "theordinary.com": parse_ordinary_page,
    "paulaschoice.com": parse_paulaschoice_page,
}


def parse_for_domain(source_domain: str, html: str) -> ParsedIngredients:
    parser = PARSERS.get(source_domain)
    if parser is None:
        return ParsedIngredients(status="UNSUPPORTED_DOMAIN", detail=f"{source_domain!r} has no registered parser")
    return parser(html)
