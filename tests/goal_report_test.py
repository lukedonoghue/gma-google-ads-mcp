"""Tests for deterministic CPA/ROAS Goal Benchmark Reports."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ads_mcp.goal_report import GoalReportError, build_goal_report
from ads_mcp.industry_benchmarks import (
    get_benchmark_profile,
    list_benchmark_profiles,
)


def scope(
    *,
    business_mode: str = "lead_gen",
    campaign_goals=None,
):
    return {
        "customer_id": "1917205815",
        "account_name": "The Body Temple Spa",
        "currency": "USD",
        "business_mode": business_mode,
        "campaign_spend_window_start": "2026-06-23",
        "campaign_spend_window_end": "2026-07-22",
        "goal_context": {
            "campaign_goals": campaign_goals
            or [
                {
                    "campaign_id": "1",
                    "status": "ENABLED",
                    "spend_micros": 746_710_142,
                    "reported_conversions": 64,
                    "reported_conversion_value": 0,
                },
                {
                    "campaign_id": "2",
                    "status": "ENABLED",
                    "spend_micros": 630_358_871,
                    "reported_conversions": 41,
                    "reported_conversion_value": 0,
                },
                {
                    "campaign_id": "3",
                    "status": "ENABLED",
                    "spend_micros": 29_255_121,
                    "reported_conversions": 0,
                    "reported_conversion_value": 0,
                },
                {
                    "campaign_id": "4",
                    "status": "PAUSED",
                    "spend_micros": 254_000_000,
                    "reported_conversions": 1,
                    "reported_conversion_value": 0,
                },
            ],
            "coverage_gaps": [],
        },
    }


class IndustryBenchmarkTest(unittest.TestCase):
    def test_spa_profile_is_source_labelled_and_selectable(self):
        catalog = list_benchmark_profiles("lead_gen")
        ids = [item["id"] for item in catalog["profiles"]]
        self.assertIn("spa_wellness", ids)

        profile = get_benchmark_profile("spa_wellness", "lead_gen")
        self.assertEqual(profile["business_mode"], "lead_gen")
        self.assertEqual(len(profile["observations"]), 3)
        self.assertEqual(profile["sources"][0]["confidence"], "high")

    def test_profile_must_match_business_mode(self):
        self.assertIsNone(get_benchmark_profile("spa_wellness", "ecommerce"))


class GoalReportTest(unittest.TestCase):
    def test_body_temple_report_compares_three_separate_references(self):
        report = build_goal_report(
            scope(),
            industry_profile_id="spa_wellness",
            confirmed_target_cpa=50,
            now_fn=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(report["confirmed_goal"]["value"], 50)
        self.assertEqual(report["confirmed_goal"]["source"], "user_confirmed")
        self.assertTrue(
            report["confirmed_goal"]["authoritative_for_recommendations"]
        )
        self.assertFalse(report["confirmed_goal"]["persisted_to_google_ads"])
        self.assertEqual(report["account_reference"]["value"], 13.39)
        self.assertEqual(report["account_reference"]["spend"], 1406.32)
        self.assertEqual(
            report["account_reference"]["reported_conversions"], 105
        )
        self.assertEqual(report["industry_reference"]["range_low"], 39.25)
        self.assertEqual(report["industry_reference"]["range_high"], 67.36)
        self.assertEqual(report["industry_reference"]["reference_value"], 54.60)
        self.assertTrue(report["comparisons"]["account_meets_confirmed_goal"])
        self.assertEqual(
            report["comparisons"]["confirmed_goal_vs_industry"],
            "within_directional_range",
        )
        self.assertEqual(
            report["comparisons"]["account_vs_industry"],
            "better_than_directional_range",
        )
        self.assertTrue(
            report["assessment"]["outcome_quality_confirmation_required"]
        )
        self.assertFalse(
            report["assessment"]["industry_average_controls_recommendations"]
        )
        self.assertTrue(report["read_only"])
        self.assertEqual(len(report["core_signature"]), 64)

    def test_zero_conversion_spend_is_included_in_blended_cpa(self):
        report = build_goal_report(
            scope(
                campaign_goals=[
                    {
                        "campaign_id": "1",
                        "status": "ENABLED",
                        "spend_micros": 1_000_000_000,
                        "reported_conversions": 25,
                        "reported_conversion_value": 0,
                    },
                    {
                        "campaign_id": "2",
                        "status": "ENABLED",
                        "spend_micros": 100_000_000,
                        "reported_conversions": 0,
                        "reported_conversion_value": 0,
                    },
                ]
            ),
            industry_profile_id="personal_services",
            confirmed_target_cpa=50,
        )
        self.assertEqual(report["account_reference"]["value"], 44)
        self.assertEqual(report["account_reference"]["campaign_count"], 2)

    def test_lead_economics_calculates_break_even_cpa(self):
        report = build_goal_report(
            scope(),
            industry_profile_id="spa_wellness",
            confirmed_target_cpa=50,
            economics={
                "average_customer_value": 500,
                "gross_margin_percent": 60,
                "lead_to_sale_rate_percent": 20,
                "currency": "USD",
            },
        )
        self.assertEqual(report["economics_reference"]["value"], 60)

    def test_ecommerce_report_calculates_break_even_roas(self):
        report = build_goal_report(
            scope(
                business_mode="ecommerce",
                campaign_goals=[
                    {
                        "campaign_id": "1",
                        "status": "ENABLED",
                        "spend_micros": 100_000_000,
                        "reported_conversions": 5,
                        "reported_conversion_value": 450,
                    }
                ],
            ),
            industry_profile_id="ecommerce_beauty_personal_care",
            confirmed_target_roas=4,
            economics={"contribution_margin_percent": 40},
        )
        self.assertEqual(report["account_reference"]["value"], 4.5)
        self.assertEqual(report["economics_reference"]["value"], 2.5)
        self.assertTrue(report["comparisons"]["account_meets_confirmed_goal"])

    def test_wrong_target_type_and_profile_fail_closed(self):
        with self.assertRaisesRegex(GoalReportError, "CPA target"):
            build_goal_report(
                scope(),
                industry_profile_id="spa_wellness",
                confirmed_target_roas=4,
            )
        with self.assertRaisesRegex(GoalReportError, "Unknown or incompatible"):
            build_goal_report(
                scope(),
                industry_profile_id="ecommerce_general",
                confirmed_target_cpa=50,
            )


if __name__ == "__main__":
    unittest.main()
