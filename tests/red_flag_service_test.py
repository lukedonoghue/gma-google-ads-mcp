"""Service-boundary tests for Red-Flag Radar."""

from __future__ import annotations

import unittest

from ads_mcp.skill_runs.red_flag_service import RedFlagRadarRunService


class FakeGateway:
    def fetch_snapshot(self, **kwargs):
        self.kwargs = kwargs
        metrics = {
            "cost_micros": 100_000_000,
            "conversions": 10,
            "conversions_value": 0,
            "clicks": 100,
            "impressions": 1_000,
            "search_impression_share": 0.5,
            "search_budget_lost_impression_share": 0.2,
        }
        return {
            "customer_id": "1234567890",
            "login_customer_id": "9876543210",
            "account_name": "Test Account",
            "currency": "USD",
            "time_zone": "America/Chicago",
            "analysis_start": "2026-06-23",
            "analysis_end": "2026-07-22",
            "retrieved_at": "2026-07-23T09:00:00Z",
            "data_through": "2026-07-22",
            "source": "GMA 13 Skills Google Ads",
            "core_coverage_gaps": [],
            "coverage_gaps": ["Landing-page HTML was not crawled."],
            "campaigns": [
                {
                    "id": "101",
                    "name": "Search | Core",
                    "status": "ENABLED",
                    "channel_type": "SEARCH",
                    "cost_micros": 400_000_000,
                    "conversions": 20,
                    "conversions_value": 0,
                    "clicks": 400,
                    "impressions": 4_000,
                    "search_budget_lost_impression_share": 0.2,
                    "goal_scope_verified": True,
                    "goal_scope": "campaign_specific",
                    "effective_conversion_actions": ["Qualified lead"],
                    "recent_material_change_at": None,
                    "policy": {
                        "verified": True,
                        "active_ads": 2,
                        "disapproved": 1,
                        "limited": 0,
                    },
                    "latest_period": metrics,
                    "previous_period": metrics,
                }
            ],
        }


class FakeChangesets:
    async def create(self, plan):
        self.plan = plan
        return {
            "id": "gma_22222222222222222222222222222222",
            "status": "draft",
            "review_url": "https://example.test/review",
        }


class RedFlagServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_service_creates_review_only_plan_from_authoritative_result(self):
        gateway = FakeGateway()
        changesets = FakeChangesets()
        service = RedFlagRadarRunService(
            gateway=gateway,
            changesets=changesets,
        )

        result = await service.run(
            customer_id="1234567890",
            login_customer_id="9876543210",
            business_mode="lead_gen",
            target_cpa=50,
            outcome_quality_confirmed=True,
            analysis_start="2026-06-23",
            analysis_end="2026-07-22",
            campaign_ids=["101"],
        )

        self.assertEqual(result["change_plan"]["applyable_action_ids"], [])
        self.assertEqual(changesets.plan["skills"], ["red-flag-radar"])
        self.assertTrue(changesets.plan["actions"])
        self.assertTrue(
            all(
                action["operation_type"] == "advisory"
                for action in changesets.plan["actions"]
            )
        )
        self.assertEqual(gateway.kwargs["campaign_ids"], ["101"])


if __name__ == "__main__":
    unittest.main()
