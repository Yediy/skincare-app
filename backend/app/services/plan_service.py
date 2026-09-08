import asyncio
import logging
from typing import Dict, List, Any, Optional, Set
from datetime import datetime, timezone
from app.domain.priorities import PRIORITIES, CompatibilityRule, PRIORITY_SCHEMA_VERSION, PLANNER_VERSION

logger = logging.getLogger(__name__)


class PlanService:
    def __init__(self, monitoring_service=None):
        self.monitoring = monitoring_service

    def generate_plan(
        self,
        scores: Dict,
        insights: Dict,
        user_profile: Dict,
        capture_quality: float = 1.0
    ) -> Dict:
        priority_ranking = self._rank_priorities_by_severity(insights, scores)
        user_constraints = self._extract_user_constraints(user_profile)

        priority_ids = self._apply_compatibility_constraints(priority_ranking, user_constraints)

        categories = self._collect_categories(priority_ids)

        am_routine = self._build_am_routine(priority_ids, user_constraints)
        pm_routine = self._build_pm_routine(priority_ids, user_constraints)

        eye_care = self._build_eye_care(priority_ids, categories)
        facial_toning = self._build_facial_toning(priority_ids)
        lifestyle = self._build_lifestyle(priority_ids)

        plan = {
            "top_priorities": [
                {
                    "id": pid,
                    "label": PRIORITIES[pid].label,
                    "description": PRIORITIES[pid].description,
                    "pillar": PRIORITIES[pid].pillar,
                    "severity": priority_ranking[pid],
                    "display_order": PRIORITIES[pid].display_order
                }
                for pid in priority_ids[:3]
            ],
            "am_routine": am_routine,
            "pm_routine": pm_routine,
            "eye_care": eye_care,
            "facial_toning": facial_toning,
            "lifestyle": lifestyle,
            "capture_quality": {
                "score": capture_quality,
                "assessment": self._assess_capture_quality(capture_quality),
                "recommendations": self._get_capture_recommendations(capture_quality)
            },
            "disclaimers": self._generate_disclaimers(priority_ids, user_constraints),
            "metadata": {
                "all_priority_ids": priority_ids,
                "priority_severities": {pid: priority_ranking[pid] for pid in priority_ids},
                "all_categories": categories,
                "planner_version": PLANNER_VERSION,
                "priority_schema_version": PRIORITY_SCHEMA_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "user_constraints": user_constraints
            }
        }

        self._track_plan_metrics(plan, priority_ids)

        return plan

    def _rank_priorities_by_severity(self, insights: Dict, scores: Dict) -> Dict[str, float]:
        priority_scores = {}

        for pillar_name, pillar_insights in insights.items():
            priority_ids = pillar_insights.get("priority_ids", [])
            severities = pillar_insights.get("priority_severities", {})

            for pid in priority_ids:
                base_severity = severities.get(pid, 0.5)
                pillar_score = scores.get(f"{pillar_name}_score", 0.5)
                pillar_weight = (1.0 - pillar_score) * 0.3
                display_bonus = (10 - PRIORITIES[pid].display_order) * 0.05
                combined_score = base_severity + pillar_weight + display_bonus
                priority_scores[pid] = combined_score

        sorted_priorities = dict(sorted(priority_scores.items(), key=lambda x: x[1], reverse=True))
        return sorted_priorities

    def _extract_user_constraints(self, user_profile: Dict) -> Dict[str, Any]:
        return {
            "is_pregnant": user_profile.get("is_pregnant", False),
            "is_nursing": user_profile.get("is_nursing", False),
            "has_sensitive_skin": user_profile.get("has_sensitive_skin", False),
            "experience_level": user_profile.get("experience_level", "beginner"),
            "max_routine_steps": user_profile.get("max_routine_steps", 10),
            "allergies": user_profile.get("allergies", []),
            "avoid_ingredients": user_profile.get("avoid_ingredients", [])
        }

    def _apply_compatibility_constraints(self, priority_ranking: Dict[str, float], constraints: Dict[str, Any]) -> List[str]:
        filtered_priorities = []
        incompatible_categories = set()

        for pid, severity in priority_ranking.items():
            priority_def = PRIORITIES[pid]

            if constraints["is_pregnant"] or constraints["is_nursing"]:
                if CompatibilityRule.PREGNANCY_RESTRICTED in priority_def.compatibility_restrictions:
                    continue

            if CompatibilityRule.RETINOID_VS_EXFOLIANT in priority_def.compatibility_restrictions:
                if "retinoid" in priority_def.product_categories:
                    if "chemical_exfoliant" in incompatible_categories:
                        continue
                    incompatible_categories.add("chemical_exfoliant")
                elif "chemical_exfoliant" in priority_def.product_categories:
                    if "retinoid" in incompatible_categories:
                        continue
                    incompatible_categories.add("retinoid")

            if constraints["experience_level"] == "beginner":
                if priority_def.default_intensity == "advanced":
                    continue

            filtered_priorities.append(pid)

            if len(filtered_priorities) >= 5:
                break

        return filtered_priorities

    def _collect_categories(self, priority_ids: List[str]) -> List[str]:
        categories = []
        for pid in priority_ids:
            if pid in PRIORITIES:
                for cat in PRIORITIES[pid].product_categories:
                    if cat not in categories:
                        categories.append(cat)
        return categories

    def _build_am_routine(self, priority_ids: List[str], constraints: Dict[str, Any]) -> List[Dict]:
        routine = []
        step = 1
        cats = set(self._collect_categories(priority_ids))
        max_steps = constraints.get("max_routine_steps", 10)

        def add_step(category, action, why, priority="standard"):
            nonlocal step
            if step > max_steps:
                return
            routine.append({
                "step_number": step,
                "product_category": category,
                "action": action,
                "why": why,
                "priority": priority,
                "product_recommendations": []
            })
            step += 1

        add_step("cleanser", "Gentle cleansing to remove overnight buildup", "Prepares skin for active ingredients", "essential")

        if "vitamin_c_serum" in cats and step <= max_steps:
            add_step("vitamin_c_serum", "Apply vitamin C serum to face and neck", "Brightens tone and provides antioxidant protection", "standard")

        if "niacinamide_serum" in cats and step <= max_steps:
            add_step("niacinamide_serum", "Apply niacinamide serum", "Reduces redness and regulates oil", "standard")

        moisturizer = "light_moisturizer" if "light_moisturizer" in cats else "moisturizer"
        add_step(moisturizer, "Apply moisturizer evenly", "Hydrates and locks in active ingredients", "essential")

        add_step("sunscreen", "Apply broad-spectrum SPF 30+", "Prevents UV damage and protects improvements", "essential")

        if "caffeine_eye_serum" in cats and step <= max_steps:
            add_step("caffeine_eye_serum", "Pat gently around orbital bone", "Reduces puffiness and tired appearance", "optional")

        return routine

    def _build_pm_routine(self, priority_ids: List[str], constraints: Dict[str, Any]) -> List[Dict]:
        routine = []
        step = 1
        cats = set(self._collect_categories(priority_ids))
        max_steps = constraints.get("max_routine_steps", 10)

        def add_step(category, action, why, priority="standard"):
            nonlocal step
            if step > max_steps:
                return
            routine.append({
                "step_number": step,
                "product_category": category,
                "action": action,
                "why": why,
                "priority": priority,
                "product_recommendations": []
            })
            step += 1

        add_step("cleanser", "Thorough cleansing to remove SPF and buildup", "Essential for treatment absorption", "essential")

        has_retinoid = "retinoid" in cats
        has_exfoliant = "chemical_exfoliant" in cats

        if has_retinoid and has_exfoliant:
            if constraints.get("experience_level") == "beginner":
                add_step("retinoid", "Apply pea-sized amount (every other night to start)", "Improves texture and tone - start slow to build tolerance", "standard")
            else:
                add_step("retinoid", "Apply retinoid (Monday/Wednesday/Friday)", "Improves texture and tone", "standard")
                add_step("chemical_exfoliant", "Apply exfoliant (Tuesday/Thursday/Saturday)", "Smooths texture - alternate with retinoid to avoid over-exfoliation", "standard")
        elif has_retinoid:
            add_step("retinoid", "Apply pea-sized amount", "Improves texture and tone", "standard")
        elif has_exfoliant:
            add_step("chemical_exfoliant", "Apply exfoliant (2-3x per week)", "Smooths texture and promotes cell turnover", "standard")

        add_step("night_cream", "Apply rich night cream", "Provides deep hydration and overnight repair", "essential")

        if "peptide_eye_cream" in cats and step <= max_steps:
            add_step("peptide_eye_cream", "Pat around orbital bone", "Addresses dark circles and firmness", "optional")

        return routine

    def _build_eye_care(self, priority_ids: List[str], categories: List[str]) -> Optional[Dict]:
        eye_care_priorities = [pid for pid in priority_ids if pid in ["UNDER_EYE_SHADOWS", "PUFFINESS_REDUCTION"]]

        if not eye_care_priorities:
            return None

        return {
            "priorities_addressed": eye_care_priorities,
            "morning_products": [cat for cat in categories if "eye" in cat and "caffeine" in cat],
            "evening_products": [cat for cat in categories if "eye" in cat and "peptide" in cat],
            "techniques": [
                {"name": "Gentle Pat Application", "description": "Use ring finger to gently pat product around orbital bone", "frequency": "Daily, AM and PM"},
                {"name": "Cooling Application", "description": "Store eye products in fridge for added de-puffing effect", "frequency": "Optional, especially AM"}
            ],
            "lifestyle_tips": ["Sleep 7-9 hours nightly", "Reduce salt intake in evening", "Stay hydrated throughout day", "Sleep with head slightly elevated"]
        }

    def _build_facial_toning(self, priority_ids: List[str]) -> Optional[Dict]:
        exercises = []
        exercise_tags_seen = set()

        for pid in priority_ids:
            if pid in PRIORITIES:
                for tag in PRIORITIES[pid].exercise_tags:
                    if tag not in exercise_tags_seen:
                        exercise_tags_seen.add(tag)
                        exercises.append({
                            "tag": tag,
                            "name": self._get_exercise_name(tag),
                            "description": self._get_exercise_description(tag),
                            "frequency": "Daily, 5-10 minutes",
                            "difficulty": "beginner"
                        })

        if not exercises:
            return None

        return {
            "exercises": exercises,
            "general_tips": ["Perform exercises with clean hands on clean skin", "Use gentle, upward motions", "Avoid pulling or tugging on skin", "Combine with facial oil for easier glide"]
        }

    def _build_lifestyle(self, priority_ids: List[str]) -> Optional[Dict]:
        lifestyle_recs = []
        tags_seen = set()

        for pid in priority_ids:
            if pid in PRIORITIES:
                for tag in PRIORITIES[pid].lifestyle_tags:
                    if tag not in tags_seen:
                        tags_seen.add(tag)
                        lifestyle_recs.append({
                            "tag": tag,
                            "category": self._categorize_lifestyle_tag(tag),
                            "recommendation": self._get_lifestyle_recommendation(tag),
                            "priority_link": PRIORITIES[pid].label,
                            "impact": "high" if tag in ["sleep", "hydration", "stress"] else "medium"
                        })

        grouped = {}
        for rec in lifestyle_recs:
            category = rec["category"]
            if category not in grouped:
                grouped[category] = []
            grouped[category].append(rec)

        return {
            "recommendations_by_category": grouped,
            "quick_wins": [rec for rec in lifestyle_recs if rec["impact"] == "high"][:3]
        }

    def _assess_capture_quality(self, quality: float) -> str:
        if quality >= 0.9:
            return "Excellent"
        elif quality >= 0.75:
            return "Good"
        elif quality >= 0.6:
            return "Fair"
        else:
            return "Poor"

    def _get_capture_recommendations(self, quality: float) -> List[str]:
        if quality >= 0.9:
            return []
        recs = []
        if quality < 0.75:
            recs.append("Ensure face is well-lit with natural or bright lighting")
        if quality < 0.65:
            recs.append("Position camera at eye level, 12-18 inches away")
        if quality < 0.55:
            recs.append("Remove glasses and pull hair back from face")
            recs.append("Ensure camera lens is clean")
        return recs

    def _generate_disclaimers(self, priority_ids: List[str], constraints: Dict[str, Any]) -> List[str]:
        disclaimers = [
            "This plan is for cosmetic purposes only and is not medical advice.",
            "Consult a dermatologist if you have specific skin conditions or concerns.",
            "Patch test new products before full application.",
            "Results vary by individual and typically take 4-12 weeks to appear."
        ]

        if any(PRIORITIES[pid].pillar == "skin_health" for pid in priority_ids):
            if any("retinoid" in PRIORITIES[pid].product_categories for pid in priority_ids):
                disclaimers.append("Retinoids can cause initial dryness and sensitivity. Start with 2-3x per week and gradually increase.")

        if constraints.get("is_pregnant") or constraints.get("is_nursing"):
            disclaimers.append("Plan has been adjusted to exclude pregnancy/nursing contraindicated ingredients.")

        if constraints.get("has_sensitive_skin"):
            disclaimers.append("Recommendations have been adjusted for sensitive skin. Start slowly and monitor for reactions.")

        return disclaimers

    def _categorize_lifestyle_tag(self, tag: str) -> str:
        categories = {
            "sleep": "Sleep & Recovery", "sleep_elevation": "Sleep & Recovery", "sleep_position": "Sleep & Recovery",
            "hydration": "Hydration & Nutrition", "antioxidants": "Hydration & Nutrition", "protein": "Hydration & Nutrition",
            "reduce_fried_foods": "Hydration & Nutrition", "reduce_ultra_processed": "Hydration & Nutrition", "reduce_salt_evening": "Hydration & Nutrition",
            "stress": "Mental Health & Stress", "reduce_alcohol": "Substance Moderation", "reduce_dairy_test": "Dietary Adjustments",
            "strength_training": "Physical Activity", "chewing_awareness": "Postural Habits", "posture": "Postural Habits"
        }
        return categories.get(tag, "Other")

    def _get_exercise_name(self, tag: str) -> str:
        mapping = {
            "reduce_puffiness": "De-Puffing Lymphatic Drainage Massage",
            "facial_massage": "Circulation-Boosting Facial Massage",
            "definition": "Jawline & Neck Definition Exercises",
            "posture": "Posture Alignment Exercises",
            "symmetry": "Facial Symmetry Awareness Exercises"
        }
        return mapping.get(tag, tag.replace("_", " ").title())

    def _get_exercise_description(self, tag: str) -> str:
        mapping = {
            "reduce_puffiness": "Gentle lymphatic drainage techniques to reduce morning facial puffiness",
            "facial_massage": "Massage techniques to boost circulation and promote healthy glow",
            "definition": "Resistance exercises targeting jaw and neck muscle tone",
            "posture": "Alignment exercises to improve how facial structure is presented",
            "symmetry": "Awareness exercises to promote balanced facial muscle usage"
        }
        return mapping.get(tag, "")

    def _get_lifestyle_recommendation(self, tag: str) -> str:
        mapping = {
            "hydration": "Drink 8+ glasses of water daily for optimal skin hydration",
            "antioxidants": "Include berries, leafy greens, and nuts in daily diet",
            "sleep": "Aim for 7-9 hours of quality sleep nightly",
            "reduce_alcohol": "Limit alcohol consumption to reduce facial redness",
            "reduce_dairy_test": "Consider reducing dairy for 2 weeks to assess impact on redness",
            "stress": "Practice stress-reduction techniques like meditation or deep breathing",
            "reduce_fried_foods": "Minimize fried and high-fat foods to help control oil production",
            "reduce_salt_evening": "Reduce salt intake in evening meals to minimize morning puffiness",
            "strength_training": "Include resistance training 2-3x weekly for body composition",
            "protein": "Consume adequate protein (0.8-1g per lb bodyweight) daily",
            "reduce_ultra_processed": "Minimize ultra-processed foods, focus on whole foods",
            "chewing_awareness": "Be mindful of chewing evenly on both sides",
            "sleep_position": "Try sleeping on your back to promote facial symmetry",
            "sleep_elevation": "Sleep with head slightly elevated to reduce morning puffiness"
        }
        return mapping.get(tag, tag.replace("_", " ").title())

    def _track_plan_metrics(self, plan: Dict, priority_ids: List[str]):
        if not self.monitoring:
            return

        async def _record():
            try:
                for pid in priority_ids:
                    await self.monitoring.increment(f"priority.triggered.{pid}")

                await self.monitoring.observe("routine.am.steps", len(plan["am_routine"]))
                await self.monitoring.observe("routine.pm.steps", len(plan["pm_routine"]))
                await self.monitoring.gauge("plan.total_priorities", len(priority_ids))
                await self.monitoring.observe("capture.quality", plan["capture_quality"]["score"])
            except Exception as e:
                logger.warning(f"Plan metrics tracking failed (non-fatal): {e}")

        try:
            asyncio.create_task(_record())
        except RuntimeError:
            logger.debug("No event loop available for plan metrics tracking; skipped")
