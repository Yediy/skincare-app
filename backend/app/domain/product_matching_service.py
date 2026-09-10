"""ProductMatchingService (Part I, Phase 9/10) -- resolves PlanService's
abstract recommended categories to real catalog formulations, each
evaluated through SafetyEngine.evaluate_product_formulation(), never
through the category-level evaluate_offer() alone. See
PRODUCT_RECOMMENDATION_PIPELINE.md.

Deliberately async and deliberately NOT injected into PlanService:
catalog lookup is database-backed, and PlanService stays a plain,
synchronous function operating only on abstract categories (unchanged
this pass). The orchestration that calls both --
app/domain/analysis_service.py's perform_analysis() -- is where the
sync/async boundary is actually crossed, in exactly one place.

Commercial firewall (Phase 10): this module's only inputs are a
category string, market/region, and the user's own safety constraints.
There is no price/brand-preference/commission/popularity field
anywhere in its signature or in ProductMatch -- structurally, not
merely by omission, there is no code path here by which a commercial
factor could restore an excluded candidate or influence ranking. Rank
ordering ("compatibility ordering", never called "clinical efficacy
ranking" -- no scientific evidence data exists to support that claim)
uses only: SAFE before RESTRICTED, exact-market before global fallback,
verification freshness, then a stable deterministic tie-break.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg

from app.db.catalog_repository import (
    get_representative_sku_for_formulation,
    list_current_active_formulations_by_category,
)
from app.domain.safety_engine import RESTRICTED, SAFE, SafetyDecision, SafetyEngine

MATCHED = "MATCHED"
NO_SAFE_MATCH = "NO_SAFE_MATCH"


@dataclass
class ProductMatch:
    product_id: UUID
    formulation_id: UUID
    sku_id: Optional[UUID]
    brand: str
    product_name: str
    category: str
    market_or_region: str
    safety_status: str
    safety_decision: SafetyDecision
    verification_date: Optional[datetime]
    match_status: str  # MATCHED | NO_SAFE_MATCH
    # Provenance, not a ranking input from outside this module: True
    # when this candidate came from the market_or_region='global'
    # fallback query, not the caller's own requested market/region.
    market_fallback: bool = False
    rank_position: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": str(self.product_id),
            "formulation_id": str(self.formulation_id),
            "sku_id": str(self.sku_id) if self.sku_id else None,
            "brand": self.brand,
            "product_name": self.product_name,
            "category": self.category,
            "market_or_region": self.market_or_region,
            "safety_status": self.safety_status,
            "safety_decision": self.safety_decision.to_dict(),
            "verification_date": self.verification_date.isoformat() if self.verification_date else None,
            "match_status": self.match_status,
            "market_fallback": self.market_fallback,
            "rank_position": self.rank_position,
        }


class ProductMatchingService:
    def __init__(self, pool: asyncpg.Pool, safety_engine: SafetyEngine):
        self._pool = pool
        self._safety_engine = safety_engine

    async def find_compatible_products(
        self,
        category: str,
        constraints: Dict[str, Any],
        market_or_region: str = "global",
        max_results: int = 3,
    ) -> List[ProductMatch]:
        """Every candidate is evaluated through
        SafetyEngine.evaluate_product_formulation() -- the actual P0
        closure this service exists for. Returns up to max_results
        compatible (SAFE or RESTRICTED) matches, ranked by
        compatibility ordering; an empty list means no specific
        compatible product exists (Phase 11's required fallback
        behavior -- the caller must never substitute a generic/
        popular/default/affiliate product in that case)."""
        market_candidates = await list_current_active_formulations_by_category(
            self._pool, category, market_or_region
        )
        global_candidates: List[Dict[str, Any]] = []
        # Global fallback is only ever the *literal* market_or_region
        #='global' catalog rows -- never a silent substitution of some
        # other specific jurisdiction's formulation for the one the
        # caller actually asked about.
        if market_or_region != "global":
            global_candidates = await list_current_active_formulations_by_category(
                self._pool, category, "global"
            )

        evaluated: List[ProductMatch] = []
        for candidate, is_fallback in [(c, False) for c in market_candidates] + [(c, True) for c in global_candidates]:
            decision = await self._safety_engine.evaluate_product_formulation(
                self._pool, candidate["id"], constraints
            )
            sku = await get_representative_sku_for_formulation(self._pool, candidate["id"])
            evaluated.append(ProductMatch(
                product_id=candidate["product_id"],
                formulation_id=candidate["id"],
                sku_id=sku["id"] if sku else None,
                brand=candidate["brand_name"],
                product_name=candidate["product_name"],
                category=candidate["product_category"],
                market_or_region=candidate["market_or_region"],
                safety_status=decision.status,
                safety_decision=decision,
                verification_date=candidate["verified_at"],
                match_status=MATCHED if decision.status in (SAFE, RESTRICTED) else NO_SAFE_MATCH,
                market_fallback=is_fallback,
            ))

        compatible = [m for m in evaluated if m.match_status == MATCHED]
        compatible.sort(key=self._compatibility_ordering_key)

        ranked = compatible[:max_results]
        for position, match in enumerate(ranked, start=1):
            match.rank_position = position
        return ranked

    @staticmethod
    def _compatibility_ordering_key(match: ProductMatch):
        """Honest, deterministic ordering -- deliberately never called
        "clinical efficacy ranking" (no evidence data exists to support
        that claim). SAFE before RESTRICTED; exact-market before
        global fallback; freshest verification first; formulation_id
        as a final stable tie-break so ordering never depends on
        incidental list/query order."""
        verified_ts = match.verification_date.timestamp() if match.verification_date else float("-inf")
        return (
            0 if match.safety_status == SAFE else 1,
            1 if match.market_fallback else 0,
            -verified_ts,
            str(match.formulation_id),
        )
