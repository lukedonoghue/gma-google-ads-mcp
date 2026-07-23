"""Recommendation-quality tests for GMA Skill 12's deterministic engine."""

from __future__ import annotations

import unittest

from ads_mcp.skill_runs.budget_reallocator import evaluate_budget_reallocation


def campaign(
    campaign_id: str,
    name: str,
    *,
    budget: int,
    cost: int,
    conversions: float,
    lost_budget: float | None,
    lost_rank: float | None,
    search_is: float | None,
    conversions_value: float = 0,
    channel_type: str = "SEARCH",
    bidding_strategy_type: str = "MAXIMIZE_CONVERSIONS",
    average_cpc_micros: int = 2_000_000,
    goal_scope_verified: bool = True,
    goal_scope: str = "account_default",
    effective_conversion_actions: list[str] | None = None,
    shared: bool = False,
    recent_change: str | None = None,
    status: str = "ENABLED",
):
    return {
        "id": campaign_id,
        "name": name,
        "status": status,
        "channel_type": channel_type,
        "bidding_strategy_type": bidding_strategy_type,
        "budget_resource_name": f"customers/1234567890/campaignBudgets/{campaign_id}",
        "daily_budget_micros": budget,
        "budget_explicitly_shared": shared,
        "cost_micros": cost,
        "conversions": conversions,
        "conversions_value": conversions_value,
        "clicks": 300,
        "average_cpc_micros": average_cpc_micros,
        "impressions": 10_000,
        "search_impression_share": search_is,
        "search_budget_lost_impression_share": lost_budget,
        "search_rank_lost_impression_share": lost_rank,
        "goal_scope_verified": goal_scope_verified,
        "goal_scope": goal_scope,
        "effective_conversion_actions": effective_conversion_actions
        or ["Qualified lead"],
        "recent_material_change_at": recent_change,
    }


def snapshot(*campaigns):
    return {
        "analysis_start": "2026-06-21",
        "analysis_end": "2026-07-20",
        "campaigns": list(campaigns),
        "coverage_gaps": [],
    }


class BudgetReallocatorTest(unittest.TestCase):
    def test_builds_capped_zero_net_move_from_bad_donor_to_good_recipient(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "101",
                    "Search | Weak",
                    budget=20_000_000,
                    cost=6_000_000_000,
                    conversions=50,
                    lost_budget=0.02,
                    lost_rank=0.05,
                    search_is=0.80,
                ),
                campaign(
                    "202",
                    "Search | Proven",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                ),
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["status"], "recommendations_ready")
        self.assertEqual(result["applyable_action_ids"], ["BR-001", "BR-002"])
        actions = result["recommendations"]
        self.assertEqual(actions[0]["proposed_value"]["amount_micros"], 16_000_000)
        self.assertEqual(actions[1]["proposed_value"]["amount_micros"], 24_000_000)
        self.assertEqual(
            sum(
                action["proposed_value"]["amount_micros"]
                - action["current_value"]["amount_micros"]
                for action in actions
            ),
            0,
        )

    def test_tracking_uncertainty_blocks_all_budget_changes(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Proven",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=False,
        )

        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["applyable_action_ids"], [])
        self.assertIn("Outcome quality has not been confirmed", result["holds"])
        self.assertEqual(
            result["recovery_actions"][0]["id"], "REC-OUTCOME-QUALITY"
        )
        self.assertTrue(result["recovery_actions"][0]["selectable"])
        self.assertIn(
            "Qualified lead",
            " ".join(result["recovery_actions"][0]["steps"]),
        )
        self.assertEqual(
            result["recovery_actions"][0]["follow_up"]["module_id"],
            "budget_reallocator",
        )

    def test_each_recovery_task_claims_only_the_hold_it_can_resolve(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Multiple Holds",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                    status="PAUSED",
                    recent_change="2026-07-18",
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=False,
        )

        actions = {item["id"]: item for item in result["recovery_actions"]}
        self.assertEqual(
            actions["REC-OUTCOME-QUALITY"]["resolves"],
            ["Outcome quality has not been confirmed"],
        )
        self.assertEqual(
            actions["REC-RESOLVE-CAMPAIGN-STATUS"]["resolves"],
            [
                "Campaign is paused; only enabled campaigns can donate or receive budget"
            ],
        )
        self.assertEqual(
            actions["REC-WAIT-FOR-LEARNING"]["resolves"],
            ["Recent material change on 2026-07-18"],
        )

    def test_paused_campaigns_cannot_donate_or_receive_budget(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "101",
                    "Search | Paused Donor",
                    budget=20_000_000,
                    cost=6_000_000_000,
                    conversions=50,
                    lost_budget=0.02,
                    lost_rank=0.05,
                    search_is=0.80,
                    status="PAUSED",
                ),
                campaign(
                    "202",
                    "Search | Paused Recipient",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                    status="PAUSED",
                ),
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["applyable_action_ids"], [])
        self.assertTrue(
            all(
                "only enabled campaigns can donate or receive budget"
                in " ".join(row["holds"])
                for row in result["campaign_results"]
            )
        )
        self.assertIn(
            "REC-RESOLVE-CAMPAIGN-STATUS",
            [item["id"] for item in result["recovery_actions"]],
        )

    def test_mixed_rank_and_budget_constraint_routes_instead_of_scaling(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Mixed",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.25,
                    search_is=0.40,
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        row = result["campaign_results"][0]
        self.assertEqual(row["constraint"], "mixed_budget_and_rank")
        self.assertFalse(row["recipient_eligible"])
        self.assertIn("Quality Score Booster", row["routes"])
        self.assertEqual(result["applyable_action_ids"], [])
        self.assertIn(
            "REC-RUN-QUALITY-SCORE-BOOSTER",
            [item["id"] for item in result["recovery_actions"]],
        )

    def test_net_increase_is_advisory_until_user_confirms_new_spend(self):
        base = snapshot(
            campaign(
                "202",
                "Search | Proven",
                budget=20_000_000,
                cost=3_000_000_000,
                conversions=100,
                lost_budget=0.30,
                lost_rank=0.04,
                search_is=0.55,
            )
        )
        advisory = evaluate_budget_reallocation(
            base,
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )
        confirmed = evaluate_budget_reallocation(
            base,
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            allow_net_increase=True,
        )

        self.assertEqual(advisory["recommendations"][0]["applyability"], "advisory")
        self.assertEqual(advisory["applyable_action_ids"], [])
        self.assertEqual(confirmed["recommendations"][0]["applyability"], "applyable")
        self.assertEqual(
            confirmed["recommendations"][0]["proposed_value"]["amount_micros"],
            24_000_000,
        )

    def test_shared_budget_and_recent_change_are_explicit_holds(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Shared",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                    shared=True,
                    recent_change="2026-07-18",
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["status"], "hold")
        self.assertIn(
            "Campaign uses a shared budget; assess the pool before editing",
            result["holds"],
        )
        self.assertIn("Recent material change on 2026-07-18", result["holds"])
        wait = next(
            item
            for item in result["recovery_actions"]
            if item["id"] == "REC-WAIT-FOR-LEARNING"
        )
        self.assertEqual(wait["follow_up"]["not_before"], "2026-08-01")

    def test_low_volume_target_cpa_campaign_is_not_scaled(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Too Little Evidence",
                    budget=20_000_000,
                    cost=300_000_000,
                    conversions=10,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                    bidding_strategy_type="TARGET_CPA",
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        row = result["campaign_results"][0]
        self.assertFalse(row["recipient_eligible"])
        self.assertIn("30 required", " ".join(row["holds"]))
        self.assertEqual(result["applyable_action_ids"], [])
        self.assertIn(
            "REC-COLLECT-MORE-EVIDENCE",
            [item["id"] for item in result["recovery_actions"]],
        )

    def test_ecommerce_uses_roas_to_build_a_zero_net_move(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "101",
                    "Shopping | Weak",
                    budget=20_000_000,
                    cost=6_000_000_000,
                    conversions=60,
                    conversions_value=12_000,
                    lost_budget=0.02,
                    lost_rank=0.05,
                    search_is=0.80,
                    channel_type="SHOPPING",
                    bidding_strategy_type="TARGET_ROAS",
                ),
                campaign(
                    "202",
                    "Shopping | Proven",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=60,
                    conversions_value=15_000,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                    channel_type="SHOPPING",
                    bidding_strategy_type="TARGET_ROAS",
                ),
            ),
            business_mode="ecommerce",
            target_roas=4.0,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["applyable_action_ids"], ["BR-001", "BR-002"])
        self.assertIn(
            "ROAS 2.00 vs 4.00", result["recommendations"][0]["evidence_summary"]
        )
        self.assertIn(
            "ROAS 5.00 vs 4.00", result["recommendations"][1]["evidence_summary"]
        )

    def test_missing_business_target_blocks_budget_advice(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Target Unknown",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                )
            ),
            business_mode="lead_gen",
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["status"], "hold")
        self.assertIn("Business CPA or ROAS target is missing", result["holds"])
        self.assertEqual(
            result["recovery_actions"][0]["id"], "REC-CONFIRM-TARGET"
        )

    def test_performance_max_is_held_without_product_or_lead_quality_evidence(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "PMax | Broad Inventory",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=None,
                    lost_rank=None,
                    search_is=None,
                    channel_type="PERFORMANCE_MAX",
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        row = result["campaign_results"][0]
        self.assertFalse(row["donor_eligible"])
        self.assertFalse(row["recipient_eligible"])
        self.assertIn("Performance Max needs", " ".join(row["holds"]))
        self.assertIn(
            "REC-PMAX-EVIDENCE",
            [item["id"] for item in result["recovery_actions"]],
        )

    def test_ninety_percent_impression_share_is_a_demand_ceiling(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Saturated",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.20,
                    lost_rank=0.01,
                    search_is=0.90,
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["campaign_results"][0]["constraint"], "demand_ceiling")
        self.assertEqual(result["applyable_action_ids"], [])

    def test_confirmed_monthly_cap_blocks_a_net_increase_without_headroom(self):
        result = evaluate_budget_reallocation(
            snapshot(
                campaign(
                    "202",
                    "Search | Proven",
                    budget=20_000_000,
                    cost=3_000_000_000,
                    conversions=100,
                    lost_budget=0.30,
                    lost_rank=0.04,
                    search_is=0.55,
                )
            ),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            monthly_budget_micros=608_000_000,
            allow_net_increase=True,
        )

        self.assertEqual(result["applyable_action_ids"], [])
        self.assertEqual(result["recommendations"][0]["applyability"], "advisory")
        self.assertIn("no remaining headroom", result["recommendations"][0]["details"])

    def test_same_snapshot_produces_identical_authoritative_result(self):
        evidence = snapshot(
            campaign(
                "202",
                "Search | Proven",
                budget=20_000_000,
                cost=3_000_000_000,
                conversions=100,
                lost_budget=0.30,
                lost_rank=0.04,
                search_is=0.55,
            )
        )
        inputs = {
            "business_mode": "lead_gen",
            "target_cpa_micros": 50_000_000,
            "outcome_quality_confirmed": True,
        }

        self.assertEqual(
            evaluate_budget_reallocation(evidence, **inputs),
            evaluate_budget_reallocation(evidence, **inputs),
        )


if __name__ == "__main__":
    unittest.main()
