"""Service-boundary tests for Wasted-Spend Finder."""

from __future__ import annotations

import unittest
from inspect import signature

from ads_mcp.skill_runs.wasted_spend_service import (
    GoogleAdsWastedSpendGateway,
    WastedSpendFinderRunService,
)


class FakeGateway:
    def fetch_snapshot(self, **kwargs):
        self.kwargs = kwargs
        return {
            "customer_id": "1234567890",
            "login_customer_id": "9876543210",
            "account_name": "Body Temple Spa",
            "currency": "USD",
            "time_zone": "America/New_York",
            "analysis_start": "2026-06-23",
            "analysis_end": "2026-07-22",
            "retrieved_at": "2026-07-23T09:00:00Z",
            "data_through": "2026-07-22",
            "source": "GMA 13 Skills Google Ads",
            "campaigns": [
                {
                    "id": "101",
                    "name": "Search | Core",
                    "status": "ENABLED",
                    "channel_type": "SEARCH",
                    "cost_micros": 500_000_000,
                }
            ],
            "search_terms": [
                {
                    "search_term": "massage jobs",
                    "status": "NONE",
                    "campaign_id": "101",
                    "campaign_name": "Search | Core",
                    "channel_type": "SEARCH",
                    "ad_group_id": "201",
                    "ad_group_name": "Core",
                    "match_type": "BROAD",
                    "lane": "spend_risk",
                    "cost_micros": 75_000_000,
                    "conversions": 0,
                    "all_conversions": 0,
                    "conversions_value": 0,
                    "clicks": 9,
                    "impressions": 150,
                }
            ],
            "existing_negatives": [],
            "goal_scope_verified": True,
            "effective_conversion_actions": {"101": ["Qualified lead"]},
            "average_cpa_micros": 50_000_000,
            "average_order_value": None,
            "coverage_gaps": [],
        }


class FakeChangesets:
    async def create(self, plan):
        self.plan = plan
        return {
            "id": "gma_33333333333333333333333333333333",
            "status": "draft",
            "review_url": "https://example.test/review",
        }


class WastedSpendServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_pmax_status_map_is_required_only_by_the_pmax_query(self):
        self.assertNotIn(
            "statuses",
            signature(GoogleAdsWastedSpendGateway._search_term_rows).parameters,
        )
        self.assertIn(
            "statuses",
            signature(GoogleAdsWastedSpendGateway._pmax_rows).parameters,
        )

    async def test_service_creates_read_only_tasks_from_authoritative_result(
        self,
    ):
        gateway = FakeGateway()
        changesets = FakeChangesets()
        service = WastedSpendFinderRunService(
            gateway=gateway,
            changesets=changesets,
        )

        result = await service.run(
            customer_id="1234567890",
            login_customer_id="9876543210",
            business_mode="lead_gen",
            analysis_start="2026-06-23",
            analysis_end="2026-07-22",
            campaign_ids=["101"],
            brand_terms=["Body Temple Spa"],
            confirmed_irrelevant_themes=["jobs"],
            outcome_quality_confirmed=True,
        )

        self.assertEqual(result["change_plan"]["applyable_action_ids"], [])
        self.assertEqual(changesets.plan["skills"], ["wasted-spend-finder"])
        self.assertEqual(changesets.plan["actions"][0]["applyability"], "task")
        self.assertEqual(
            changesets.plan["actions"][0]["operation_type"], "advisory"
        )
        self.assertEqual(gateway.kwargs["campaign_ids"], ["101"])


if __name__ == "__main__":
    unittest.main()
