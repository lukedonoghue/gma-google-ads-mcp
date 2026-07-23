"""Deterministic behavior tests for Wasted-Spend Finder."""

from __future__ import annotations

import unittest

from ads_mcp.skill_runs.wasted_spend_finder import evaluate_wasted_spend


def search_term(
    text: str,
    *,
    cost=60_000_000,
    clicks=8,
    impressions=120,
    conversions=0,
    all_conversions=0,
    campaign_id="101",
    campaign_name="Search | Core",
    status="NONE",
    lane="spend_risk",
):
    return {
        "search_term": text,
        "status": status,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "channel_type": "SEARCH",
        "ad_group_id": "201",
        "ad_group_name": "Core",
        "match_type": "BROAD",
        "lane": lane,
        "cost_micros": cost,
        "conversions": conversions,
        "all_conversions": all_conversions,
        "conversions_value": 0,
        "clicks": clicks,
        "impressions": impressions,
    }


def snapshot(*terms, brand=True, goals=True, outcomes=True):
    return {
        "customer_id": "1234567890",
        "account_name": "Body Temple Spa",
        "currency": "USD",
        "analysis_start": "2026-06-23",
        "analysis_end": "2026-07-22",
        "campaigns": [
            {
                "id": "101",
                "name": "Search | Core",
                "status": "ENABLED",
                "channel_type": "SEARCH",
                "cost_micros": 500_000_000,
            }
        ],
        "search_terms": list(terms),
        "existing_negatives": [],
        "goal_scope_verified": goals,
        "effective_conversion_actions": {"101": ["Qualified lead"]},
        "average_cpa_micros": 50_000_000,
        "average_order_value": None,
        "coverage_gaps": [],
        "_brand_terms": ["Body Temple Spa", "BTS"] if brand else [],
        "_outcomes": outcomes,
    }


def run(data, **overrides):
    return evaluate_wasted_spend(
        data,
        business_mode="lead_gen",
        brand_terms=overrides.pop("brand_terms", data["_brand_terms"]),
        outcome_quality_confirmed=overrides.pop(
            "outcome_quality_confirmed", data["_outcomes"]
        ),
        **overrides,
    )


class WastedSpendFinderTest(unittest.TestCase):
    def test_brand_and_converted_terms_are_never_excluded(self):
        data = snapshot(
            search_term("body temple spa massage"),
            search_term("massage training", conversions=1),
            search_term("massage training cost"),
        )

        result = run(data, confirmed_irrelevant_themes=["education"])
        checks = {item["criterion"]: item for item in result["checks"]}

        self.assertEqual(checks["body temple spa massage"]["status"], "keep")
        self.assertEqual(checks["massage training"]["status"], "flag")
        self.assertEqual(checks["massage training cost"]["status"], "flag")
        self.assertFalse(result["recommendations"])

    def test_confirmed_irrelevant_theme_creates_a_bounded_task_and_waste_claim(
        self,
    ):
        data = snapshot(search_term("massage jobs near me", cost=75_000_000))

        result = run(data, confirmed_irrelevant_themes=["jobs"])

        self.assertEqual(result["checks"][0]["status"], "exclude")
        self.assertEqual(len(result["recommendations"]), 1)
        action = result["recommendations"][0]
        self.assertEqual(action["proposed_value"]["negative_keyword"], "jobs")
        self.assertEqual(action["proposed_value"]["match_type"], "BROAD")
        self.assertEqual(action["applyability"], "task")
        self.assertEqual(
            result["assessment_details"][
                "identified_spend_on_irrelevant_queries_micros"
            ],
            75_000_000,
        )

    def test_unknown_business_intent_becomes_a_specific_recovery_task(self):
        data = snapshot(search_term("massage course online"))

        result = run(data)

        self.assertEqual(result["checks"][0]["status"], "edge_case")
        self.assertFalse(result["recommendations"])
        recovery = {item["id"]: item for item in result["recovery_actions"]}[
            "REC-WS-CONFIRM-INTENT"
        ]
        self.assertIn("EDGE CASE", recovery["steps"][0])
        self.assertEqual(
            recovery["follow_up"]["module_id"], "wasted_spend_finder"
        )

    def test_missing_brand_terms_blocks_actions_with_a_clear_fix(self):
        data = snapshot(search_term("massage jobs"), brand=False)

        result = run(
            data,
            brand_terms=[],
            confirmed_irrelevant_themes=["jobs"],
        )

        self.assertEqual(result["status"], "hold")
        self.assertFalse(result["recommendations"])
        self.assertIn(
            "REC-WS-CONFIRM-BRAND",
            {item["id"] for item in result["recovery_actions"]},
        )

    def test_existing_negative_is_not_duplicated(self):
        data = snapshot(search_term("massage jobs"))
        data["existing_negatives"] = [
            {
                "text": "massage jobs",
                "match_type": "EXACT",
                "scope": "campaign",
                "campaign_ids": ["101"],
                "ad_group_ids": [],
                "list_name": "Existing campaign negative",
            }
        ]

        result = run(data, confirmed_irrelevant_themes=["jobs"])

        self.assertEqual(result["checks"][0]["status"], "already_covered")
        self.assertFalse(result["recommendations"])

    def test_broad_existing_negative_covers_the_term_but_unattached_list_does_not(
        self,
    ):
        data = snapshot(search_term("massage jobs near me"))
        data["existing_negatives"] = [
            {
                "text": "jobs",
                "match_type": "BROAD",
                "scope": "shared_list",
                "campaign_ids": ["101"],
                "ad_group_ids": [],
                "list_name": "Jobs & Careers",
            }
        ]
        result = run(data, confirmed_irrelevant_themes=["jobs"])
        self.assertEqual(result["checks"][0]["status"], "already_covered")

        data["existing_negatives"][0]["campaign_ids"] = []
        result = run(data, confirmed_irrelevant_themes=["jobs"])
        self.assertEqual(result["checks"][0]["status"], "exclude")
        self.assertEqual(len(result["recommendations"]), 1)

    def test_ad_group_negative_only_covers_its_own_ad_group(self):
        data = snapshot(search_term("massage jobs"))
        data["existing_negatives"] = [
            {
                "text": "jobs",
                "match_type": "BROAD",
                "scope": "ad_group",
                "campaign_ids": ["101"],
                "ad_group_ids": ["999"],
                "list_name": "Other ad group",
            }
        ]

        result = run(data, confirmed_irrelevant_themes=["jobs"])

        self.assertEqual(result["checks"][0]["status"], "exclude")
        self.assertEqual(len(result["recommendations"]), 1)

    def test_performance_only_negative_is_held_until_outcomes_and_goals_are_verified(
        self,
    ):
        data = snapshot(
            search_term("deep tissue massage athens"),
            goals=False,
            outcomes=False,
        )

        result = run(data)

        self.assertEqual(result["checks"][0]["status"], "flag")
        self.assertFalse(result["recommendations"])
        self.assertIn(
            "REC-WS-CONFIRM-OUTCOMES",
            {item["id"] for item in result["recovery_actions"]},
        )

    def test_performance_only_negative_is_exact_after_both_measurement_gates_pass(
        self,
    ):
        data = snapshot(search_term("deep tissue massage athens"))

        result = run(data)

        self.assertEqual(result["checks"][0]["status"], "exclude")
        self.assertEqual(
            result["recommendations"][0]["proposed_value"]["match_type"],
            "EXACT",
        )
        self.assertEqual(
            result["recommendations"][0]["proposed_value"]["negative_keyword"],
            "deep tissue massage athens",
        )

    def test_no_candidate_still_returns_a_monitoring_next_step(self):
        data = snapshot(
            search_term(
                "best massage spa",
                cost=5_000_000,
                clicks=1,
                impressions=20,
            )
        )

        result = run(data)

        self.assertEqual(result["status"], "no_change")
        self.assertIn(
            "REC-WS-MONITOR",
            {item["id"] for item in result["recovery_actions"]},
        )


if __name__ == "__main__":
    unittest.main()
