"""Contract tests for the live Skill 12 run service."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from ads_mcp.skill_runs.budget_service import (
    BudgetReallocatorRunService,
    GoogleAdsBudgetGateway,
)


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


class FakeGateway:
    def fetch_snapshot(self, **_kwargs):
        return {
            "customer_id": "1234567890",
            "login_customer_id": "9876543210",
            "account_name": "Test Account",
            "currency": "USD",
            "time_zone": "America/Chicago",
            "analysis_start": "2026-06-21",
            "analysis_end": "2026-07-20",
            "retrieved_at": "2026-07-21T15:00:00+00:00",
            "data_through": "2026-07-20",
            "source": "GMA 13 Skills Google Ads",
            "coverage_gaps": [],
            "campaigns": [
                {
                    "id": "101",
                    "name": "Search | Weak",
                    "status": "ENABLED",
                    "channel_type": "SEARCH",
                    "budget_resource_name": "customers/1234567890/campaignBudgets/101",
                    "daily_budget_micros": 20_000_000,
                    "budget_explicitly_shared": False,
                    "cost_micros": 6_000_000_000,
                    "conversions": 50,
                    "conversions_value": 0,
                    "clicks": 300,
                    "impressions": 10_000,
                    "search_impression_share": 0.8,
                    "search_budget_lost_impression_share": 0.02,
                    "search_rank_lost_impression_share": 0.05,
                    "goal_scope_verified": True,
                    "change_history_verified": True,
                    "recent_material_change_at": None,
                },
                {
                    "id": "202",
                    "name": "Search | Proven",
                    "status": "ENABLED",
                    "channel_type": "SEARCH",
                    "budget_resource_name": "customers/1234567890/campaignBudgets/202",
                    "daily_budget_micros": 20_000_000,
                    "budget_explicitly_shared": False,
                    "cost_micros": 3_000_000_000,
                    "conversions": 100,
                    "conversions_value": 0,
                    "clicks": 300,
                    "impressions": 10_000,
                    "search_impression_share": 0.55,
                    "search_budget_lost_impression_share": 0.30,
                    "search_rank_lost_impression_share": 0.04,
                    "goal_scope_verified": True,
                    "change_history_verified": True,
                    "recent_material_change_at": None,
                },
            ],
        }


class FakeChangesets:
    def __init__(self):
        self.raw_plan = None

    async def create(self, raw_plan):
        self.raw_plan = raw_plan
        return {
            "id": "gma_00000000000000000000000000000000",
            "status": "draft",
            "review_url": "https://example.test/change-plan",
        }


class GoalQueryService:
    def search(self, *, customer_id, query):
        del customer_id
        if "FROM conversion_action" in query:
            return [
                ns(
                    conversion_action=ns(
                        resource_name="customers/1234567890/conversionActions/1",
                        name="Purchase",
                        category="PURCHASE",
                        origin="WEBSITE",
                        status="ENABLED",
                        primary_for_goal=True,
                    )
                ),
                ns(
                    conversion_action=ns(
                        resource_name="customers/1234567890/conversionActions/2",
                        name="Qualified lead",
                        category="SUBMIT_LEAD_FORM",
                        origin="WEBSITE",
                        status="ENABLED",
                        primary_for_goal=True,
                    )
                ),
                ns(
                    conversion_action=ns(
                        resource_name="customers/1234567890/conversionActions/3",
                        name="Imported closed deal",
                        category="PURCHASE",
                        origin="UPLOAD",
                        status="ENABLED",
                        primary_for_goal=False,
                    )
                ),
            ]
        if "FROM conversion_goal_campaign_config" in query:
            return [
                ns(
                    conversion_goal_campaign_config=ns(
                        campaign="customers/1234567890/campaigns/101",
                        goal_config_level="CUSTOMER",
                        custom_conversion_goal="",
                    )
                ),
                ns(
                    conversion_goal_campaign_config=ns(
                        campaign="customers/1234567890/campaigns/202",
                        goal_config_level="CAMPAIGN",
                        custom_conversion_goal="",
                    )
                ),
                ns(
                    conversion_goal_campaign_config=ns(
                        campaign="customers/1234567890/campaigns/303",
                        goal_config_level="CAMPAIGN",
                        custom_conversion_goal="customers/1234567890/customConversionGoals/9",
                    )
                ),
            ]
        if "FROM customer_conversion_goal" in query:
            return [
                ns(
                    customer_conversion_goal=ns(
                        category="PURCHASE", origin="WEBSITE", biddable=True
                    )
                )
            ]
        if "FROM campaign_conversion_goal" in query:
            return [
                ns(
                    campaign_conversion_goal=ns(
                        campaign="customers/1234567890/campaigns/202",
                        category="SUBMIT_LEAD_FORM",
                        origin="WEBSITE",
                        biddable=True,
                    )
                )
            ]
        if "FROM custom_conversion_goal" in query:
            return [
                ns(
                    custom_conversion_goal=ns(
                        status="ENABLED",
                        conversion_actions=["customers/1234567890/conversionActions/3"],
                    )
                )
            ]
        raise AssertionError(f"Unexpected query: {query}")


class BudgetRunServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_campaign_effective_goals_resolve_account_campaign_and_custom(self):
        coverage_gaps = []
        result = GoogleAdsBudgetGateway()._goal_context(
            GoalQueryService(),
            "1234567890",
            ["101", "202", "303"],
            coverage_gaps,
        )

        self.assertEqual(coverage_gaps, [])
        self.assertEqual(result["101"]["scope"], "account_default")
        self.assertEqual(result["101"]["actions"], ["Purchase"])
        self.assertEqual(result["202"]["scope"], "campaign_specific")
        self.assertEqual(result["202"]["actions"], ["Qualified lead"])
        self.assertEqual(result["303"]["scope"], "custom_goal")
        self.assertEqual(result["303"]["actions"], ["Imported closed deal"])
        self.assertTrue(result["303"]["verified"])

    async def test_live_run_returns_server_owned_actions_and_change_plan(self):
        changesets = FakeChangesets()
        service = BudgetReallocatorRunService(
            gateway=FakeGateway(), changesets=changesets
        )
        result = await service.run(
            customer_id="1234567890",
            login_customer_id="9876543210",
            business_mode="lead_gen",
            target_cpa=50,
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["status"], "recommendations_ready")
        self.assertEqual(
            result["change_plan"]["applyable_action_ids"], ["BR-001", "BR-002"]
        )
        self.assertEqual(result["data_receipt"]["source"], "GMA 13 Skills Google Ads")
        self.assertEqual(changesets.raw_plan["skills"], ["budget-reallocator"])
        self.assertEqual(
            [action["id"] for action in changesets.raw_plan["actions"]],
            ["BR-001", "BR-002"],
        )
        self.assertEqual(
            changesets.raw_plan["actions"][0]["operation_type"],
            "set_campaign_budget_amount",
        )


if __name__ == "__main__":
    unittest.main()
