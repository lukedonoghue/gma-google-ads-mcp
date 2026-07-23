"""Deterministic behavior tests for Red-Flag Radar."""

from __future__ import annotations

import unittest

from ads_mcp.skill_runs.red_flag_radar import evaluate_red_flag_radar


def period(
    *,
    cost=100_000_000,
    conversions=10,
    value=0,
    clicks=100,
    impressions=1_000,
):
    return {
        "cost_micros": cost,
        "conversions": conversions,
        "conversions_value": value,
        "clicks": clicks,
        "impressions": impressions,
        "search_impression_share": 0.5,
        "search_budget_lost_impression_share": 0.2,
    }


def campaign(
    campaign_id="101",
    name="Search | Core",
    *,
    latest=None,
    previous=None,
    policy=None,
    goals=None,
    goal_verified=True,
    recent_change=None,
):
    return {
        "id": campaign_id,
        "name": name,
        "status": "ENABLED",
        "channel_type": "SEARCH",
        "cost_micros": 400_000_000,
        "conversions": 20,
        "conversions_value": 0,
        "clicks": 400,
        "impressions": 4_000,
        "search_budget_lost_impression_share": 0.2,
        "goal_scope_verified": goal_verified,
        "goal_scope": "campaign_specific",
        "effective_conversion_actions": goals or ["Qualified lead"],
        "recent_material_change_at": recent_change,
        "policy": policy
        or {
            "verified": True,
            "active_ads": 3,
            "disapproved": 0,
            "limited": 0,
        },
        "latest_period": latest or period(),
        "previous_period": previous or period(),
    }


def snapshot(*campaigns):
    return {
        "customer_id": "1234567890",
        "account_name": "Test Account",
        "currency": "USD",
        "analysis_start": "2026-06-23",
        "analysis_end": "2026-07-22",
        "campaigns": list(campaigns),
        "core_coverage_gaps": [],
        "coverage_gaps": [
            "Backend lead quality requires account-owner confirmation."
        ],
    }


class RedFlagRadarTest(unittest.TestCase):
    def test_detects_policy_failure_and_tracking_crosswire(self):
        item = campaign(
            latest=period(
                cost=130_000_000,
                conversions=4,
                clicks=105,
                impressions=1_020,
            ),
            previous=period(
                cost=100_000_000,
                conversions=10,
                clicks=100,
                impressions=1_000,
            ),
            policy={
                "verified": True,
                "active_ads": 3,
                "disapproved": 1,
                "limited": 0,
            },
        )

        result = evaluate_red_flag_radar(
            snapshot(item),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        by_criterion = {check["criterion"]: check for check in result["checks"]}
        self.assertEqual(
            by_criterion["Active ad policy eligibility"]["status"], "critical"
        )
        self.assertEqual(
            by_criterion["Latest complete 7 days versus previous 7 days"][
                "status"
            ],
            "critical",
        )
        self.assertEqual(result["status"], "findings_ready")
        self.assertTrue(
            any(
                "tracking break" in action["entity"].casefold()
                for action in result["recommendations"]
            )
        )
        self.assertTrue(
            any(
                "disapproved" in action["entity"].casefold()
                for action in result["recommendations"]
            )
        )

    def test_recent_change_downgrades_unfavorable_trend(self):
        item = campaign(
            latest=period(cost=130_000_000, conversions=5),
            previous=period(cost=100_000_000, conversions=10),
            recent_change="2026-07-18T12:00:00Z",
        )

        result = evaluate_red_flag_radar(
            snapshot(item),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        trend = next(
            check
            for check in result["checks"]
            if check["criterion"].startswith("Latest complete")
        )
        self.assertEqual(trend["status"], "warning")
        self.assertIn("2026-07-18", trend["decision"])

    def test_unavailable_checks_create_concrete_recovery_plan(self):
        item = campaign(
            policy={
                "verified": False,
                "active_ads": 0,
                "disapproved": 0,
                "limited": 0,
            },
            goals=[],
            goal_verified=False,
            latest=period(clicks=12, conversions=1),
            previous=period(clicks=10, conversions=1),
        )

        result = evaluate_red_flag_radar(
            snapshot(item),
            business_mode="lead_gen",
        )

        recovery_ids = {action["id"] for action in result["recovery_actions"]}
        self.assertIn("REC-POLICY-101", recovery_ids)
        self.assertIn("REC-GOALS-101", recovery_ids)
        self.assertIn("REC-VOLUME-101", recovery_ids)
        for action in result["recovery_actions"]:
            self.assertTrue(action["steps"])
            self.assertTrue(action["completion_signal"])
            self.assertTrue(action["selectable"])

    def test_suspect_lead_actions_request_confirmation(self):
        item = campaign(
            goals=[
                "Calls from ads",
                "YouTube channel subscriptions",
                "Local actions - Directions",
            ]
        )

        result = evaluate_red_flag_radar(
            snapshot(item),
            business_mode="lead_gen",
            outcome_quality_confirmed=False,
        )

        goal_check = next(
            check
            for check in result["checks"]
            if check["criterion"] == "Campaign-effective conversion goals"
        )
        self.assertEqual(goal_check["status"], "warning")
        self.assertTrue(
            any(
                action["id"] == "REC-OUTCOME-101"
                for action in result["recovery_actions"]
            )
        )

    def test_budget_capped_winner_routes_without_prescribing_amount(self):
        item = campaign()

        result = evaluate_red_flag_radar(
            snapshot(item),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        budget_action = next(
            action
            for action in result["recommendations"]
            if action["estimate"].get("route") == "budget_reallocator"
        )
        self.assertEqual(budget_action["operation_type"], "advisory")
        self.assertEqual(budget_action["current_value"], {})
        self.assertEqual(budget_action["proposed_value"], {})
        self.assertEqual(budget_action["applyability"], "task")

    def test_action_list_is_deterministically_capped(self):
        campaigns = []
        for index in range(10):
            campaigns.append(
                campaign(
                    str(100 + index),
                    f"Campaign {index}",
                    latest=period(cost=130_000_000, conversions=4),
                    previous=period(cost=100_000_000, conversions=10),
                    policy={
                        "verified": True,
                        "active_ads": 2,
                        "disapproved": 1,
                        "limited": 0,
                    },
                )
            )

        result = evaluate_red_flag_radar(
            snapshot(*campaigns),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(len(result["recommendations"]), 7)
        self.assertEqual(
            [action["id"] for action in result["recommendations"]],
            [f"RF-{index:03d}" for index in range(1, 8)],
        )


if __name__ == "__main__":
    unittest.main()
