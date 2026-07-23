"""Tests for configured CPA/ROAS target discovery and confirmation defaults."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from ads_mcp.goal_targets import (
    build_goal_context,
    build_goal_suggestion,
    campaign_goal,
    normalize_portfolio_strategies,
)


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def campaign(
    campaign_id: int,
    name: str,
    *,
    status: str = "ENABLED",
    strategy_type: str = "TARGET_CPA",
    target_cpa_micros: int = 0,
    target_roas: float = 0,
    portfolio: str = "",
):
    return ns(
        id=campaign_id,
        name=name,
        status=status,
        bidding_strategy=portfolio,
        bidding_strategy_type=strategy_type,
        target_cpa=ns(target_cpa_micros=target_cpa_micros),
        maximize_conversions=ns(target_cpa_micros=target_cpa_micros),
        target_roas=ns(target_roas=target_roas),
        maximize_conversion_value=ns(target_roas=target_roas),
    )


class GoalTargetsTest(unittest.TestCase):
    def test_campaign_target_cpa_is_normalized(self):
        result = campaign_goal(
            campaign(
                101,
                "Lead Search",
                strategy_type="MAXIMIZE_CONVERSIONS",
                target_cpa_micros=42_500_000,
            ),
            spend_micros=200_000_000,
            portfolio_strategies={},
        )

        self.assertEqual(result["goal_type"], "target_cpa")
        self.assertEqual(result["target_cpa_micros"], 42_500_000)
        self.assertEqual(result["source"], "campaign")

    def test_manager_portfolio_target_overrides_empty_campaign_fields(self):
        resource = "customers/9999999999/biddingStrategies/77"
        rows = [
            ns(
                accessible_bidding_strategy=ns(
                    id=77,
                    resource_name=(
                        "customers/1234567890/accessibleBiddingStrategies/77"
                    ),
                    name="Manager ROAS",
                    type_="MAXIMIZE_CONVERSION_VALUE",
                    target_cpa=ns(target_cpa_micros=0),
                    maximize_conversions=ns(target_cpa_micros=0),
                    target_roas=ns(target_roas=0),
                    maximize_conversion_value=ns(target_roas=4.5),
                )
            )
        ]
        strategies = normalize_portfolio_strategies(
            bidding_strategy_rows=[],
            accessible_strategy_rows=rows,
        )
        result = campaign_goal(
            campaign(
                202,
                "Shopping",
                strategy_type="MAXIMIZE_CONVERSION_VALUE",
                portfolio=resource,
            ),
            spend_micros=800_000_000,
            portfolio_strategies=strategies,
        )

        self.assertEqual(result["goal_type"], "target_roas")
        self.assertEqual(result["target_roas"], 4.5)
        self.assertEqual(result["target_roas_percent"], 450.0)
        self.assertEqual(result["source"], "manager_portfolio")
        self.assertEqual(result["strategy_name"], "Manager ROAS")

    def test_paused_campaign_does_not_influence_cpa_suggestion(self):
        goals = [
            campaign_goal(
                campaign(101, "Enabled", target_cpa_micros=50_000_000),
                spend_micros=200_000_000,
                portfolio_strategies={},
            ),
            campaign_goal(
                campaign(
                    202,
                    "Paused",
                    status="PAUSED",
                    target_cpa_micros=10_000_000,
                ),
                spend_micros=900_000_000,
                portfolio_strategies={},
            ),
        ]
        result = build_goal_suggestion(
            goals,
            business_mode="lead_gen",
            currency="USD",
        )

        self.assertEqual(result["target_cpa_micros"], 50_000_000)
        self.assertEqual(result["display_value"], "USD 50.00")
        self.assertEqual(result["campaign_ids"], ["101"])
        self.assertTrue(result["confirmation_required"])
        self.assertFalse(result["business_goal_verified"])

    def test_ecommerce_uses_spend_weighted_roas_target(self):
        goals = [
            campaign_goal(
                campaign(
                    101,
                    "High spend",
                    strategy_type="TARGET_ROAS",
                    target_roas=3.0,
                ),
                spend_micros=750_000_000,
                portfolio_strategies={},
            ),
            campaign_goal(
                campaign(
                    202,
                    "Low spend",
                    strategy_type="MAXIMIZE_CONVERSION_VALUE",
                    target_roas=5.0,
                ),
                spend_micros=250_000_000,
                portfolio_strategies={},
            ),
        ]
        result = build_goal_suggestion(
            goals,
            business_mode="ecommerce",
            currency="USD",
        )

        self.assertEqual(result["target_roas"], 3.0)
        self.assertEqual(result["target_roas_percent"], 300.0)
        self.assertEqual(result["display_value"], "3.00× (300%)")
        self.assertEqual(
            result["basis"], "spend_weighted_median_of_configured_targets"
        )
        self.assertEqual(result["spend_coverage_percent"], 100.0)

    def test_no_configured_cpa_uses_reported_actual_as_reference_only(self):
        goals = [
            campaign_goal(
                campaign(
                    101,
                    "Lead Search",
                    strategy_type="MAXIMIZE_CONVERSIONS",
                ),
                spend_micros=600_000_000,
                reported_conversions=20,
                portfolio_strategies={},
            ),
            campaign_goal(
                campaign(
                    202,
                    "Lead Search Two",
                    strategy_type="MAXIMIZE_CONVERSIONS",
                ),
                spend_micros=400_000_000,
                reported_conversions=5,
                portfolio_strategies={},
            ),
            campaign_goal(
                campaign(
                    303,
                    "Lead Search No Conversions",
                    strategy_type="MAXIMIZE_CONVERSIONS",
                ),
                spend_micros=100_000_000,
                reported_conversions=0,
                portfolio_strategies={},
            ),
        ]
        result = build_goal_suggestion(
            goals,
            business_mode="lead_gen",
            currency="USD",
        )

        self.assertEqual(result["status"], "reference_only")
        self.assertEqual(result["source"], "observed_google_ads_performance")
        self.assertEqual(result["target_cpa_micros"], 44_000_000)
        self.assertEqual(result["display_value"], "USD 44.00")
        self.assertEqual(result["campaign_count"], 3)
        self.assertEqual(result["spend_coverage_percent"], 100.0)
        self.assertEqual(
            result["basis"], "reported_actual_cpa_no_configured_target"
        )
        self.assertTrue(result["confirmation_required"])
        self.assertFalse(result["business_goal_verified"])

    def test_no_configured_roas_uses_reported_actual_as_reference_only(self):
        goals = [
            campaign_goal(
                campaign(
                    101,
                    "Shopping",
                    strategy_type="MAXIMIZE_CONVERSION_VALUE",
                ),
                spend_micros=1_000_000_000,
                reported_conversions=10,
                reported_conversion_value=3_500,
                portfolio_strategies={},
            )
        ]
        result = build_goal_suggestion(
            goals,
            business_mode="ecommerce",
            currency="USD",
        )

        self.assertEqual(result["status"], "reference_only")
        self.assertEqual(result["target_roas"], 3.5)
        self.assertEqual(result["target_roas_percent"], 350.0)
        self.assertEqual(result["display_value"], "3.50× (350%)")
        self.assertEqual(
            result["basis"], "reported_actual_roas_no_configured_target"
        )

    def test_context_returns_both_modes_and_selected_suggestion(self):
        rows = [
            ns(
                campaign=campaign(
                    101,
                    "Lead Search",
                    strategy_type="TARGET_CPA",
                    target_cpa_micros=60_000_000,
                )
            )
        ]
        result = build_goal_context(
            rows,
            spend_by_campaign={"101": 100_000_000},
            bidding_strategy_rows=[],
            accessible_strategy_rows=[],
            business_mode="lead_gen",
            inferred_business_mode="lead_gen",
            currency="USD",
            coverage_gaps=[],
        )

        self.assertEqual(
            result["selected_suggestion"]["target_cpa_micros"], 60_000_000
        )
        self.assertEqual(
            result["suggestions"]["ecommerce"]["status"], "unavailable"
        )


if __name__ == "__main__":
    unittest.main()
