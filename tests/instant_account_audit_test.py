"""Deterministic behavior tests for Skill 1 — Instant Account Audit."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from ads_mcp.skill_runs.instant_account_audit import (
    evaluate_instant_account_audit,
)
from ads_mcp.skill_runs.instant_audit_service import (
    GoogleAdsInstantAuditGateway,
    _change_history_bounds,
)


def campaign(
    campaign_id="101",
    name="Search | Core",
    *,
    latest_conversions=8,
    previous_conversions=8,
    latest_clicks=80,
    previous_clicks=80,
):
    return {
        "id": campaign_id,
        "name": name,
        "status": "ENABLED",
        "channel_type": "SEARCH",
        "cost_micros": 300_000_000,
        "conversions": 15,
        "all_conversions": 15,
        "conversions_value": 0,
        "clicks": 300,
        "impressions": 3_000,
        "bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
        "target_cpa_micros": 0,
        "target_roas": 0,
        "target_content_network": False,
        "search_impression_share": 0.60,
        "search_budget_lost_impression_share": 0.05,
        "search_rank_lost_impression_share": 0.10,
        "budget_explicitly_shared": False,
        "goal_scope_verified": True,
        "latest_period": {
            "cost_micros": 140_000_000,
            "conversions": latest_conversions,
            "clicks": latest_clicks,
            "impressions": 1_400,
        },
        "previous_period": {
            "cost_micros": 140_000_000,
            "conversions": previous_conversions,
            "clicks": previous_clicks,
            "impressions": 1_400,
        },
    }


def snapshot(item=None):
    item = item or campaign()
    return {
        "analysis_start": "2026-06-23",
        "analysis_end": "2026-07-22",
        "currency": "USD",
        "campaigns": [item],
        "coverage_gaps": [],
        "evidence": {
            "account_verified": True,
            "account": {
                "auto_tagging_enabled": True,
                "conversion_tracking_id": "123",
            },
            "conversion_actions_verified": True,
            "conversion_actions": [
                {
                    "name": "Qualified lead",
                    "category": "SUBMIT_LEAD_FORM",
                    "origin": "WEBSITE",
                    "type": "WEBPAGE",
                    "status": "ENABLED",
                    "primary_for_goal": True,
                    "attribution_model": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                    "default_value": 1,
                    "always_use_default_value": False,
                }
            ],
            "latest_totals": {
                "conversions": item["latest_period"]["conversions"],
                "clicks": item["latest_period"]["clicks"],
            },
            "previous_totals": {
                "conversions": item["previous_period"]["conversions"],
                "clicks": item["previous_period"]["clicks"],
            },
            "landing_pages_verified": True,
            "landing_pages": [],
            "change_history_verified": True,
            "bid_change_counts": {},
            "search_terms_verified": True,
            "search_terms": [],
            "negative_keywords_verified": True,
            "negative_keyword_count": 30,
            "ads_verified": True,
            "ads": [
                {
                    "campaign_id": item["id"],
                    "ad_group_id": "1",
                    "status": "ENABLED",
                    "approval_status": "APPROVED",
                    "ad_type": "RESPONSIVE_SEARCH_AD",
                    "ad_strength": "GOOD",
                    "ad_group_cost_micros": 300_000_000,
                }
            ],
            "keywords_verified": True,
            "keywords": [
                {"quality_score": 7, "impressions": 1_000},
                {"quality_score": 5, "impressions": 500},
            ],
            "devices_verified": True,
            "devices": [],
            "geos_verified": True,
            "geos": [],
            "schedules_verified": True,
            "schedules": [],
        },
    }


class InstantAccountAuditTest(unittest.TestCase):
    def test_returns_every_real_rubric_check_and_excludes_grey_rows(self):
        result = evaluate_instant_account_audit(
            snapshot(),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            brand_terms=["Test Account"],
        )

        self.assertEqual(len(result["checks"]), 41)
        self.assertEqual(
            {check["id"] for check in result["checks"]},
            {
                f"{section}{number}"
                for section, count in {
                    "A": 9,
                    "B": 4,
                    "C": 6,
                    "D": 4,
                    "E": 8,
                    "F": 3,
                    "G": 4,
                    "H": 3,
                }.items()
                for number in range(1, count + 1)
            },
        )
        self.assertEqual(result["assessment_details"]["total_criteria"], 41)
        self.assertLess(result["grade"]["achievable_points"], 100)
        self.assertTrue(
            any(check["status"] == "unavailable" for check in result["checks"])
        )

    def test_every_unavailable_check_has_a_specific_recovery(self):
        data = snapshot()
        data["evidence"]["search_terms_verified"] = False
        data["evidence"]["search_terms"] = []

        result = evaluate_instant_account_audit(
            data,
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            brand_terms=[],
        )

        unavailable = {
            check["id"]
            for check in result["checks"]
            if check["status"] == "unavailable"
        }
        resolved = {
            value
            for action in result["recovery_actions"]
            for value in action["resolves"]
        }
        self.assertTrue(unavailable.issubset(resolved))
        for action in result["recovery_actions"]:
            self.assertTrue(action["steps"])
            self.assertTrue(action["completion_signal"])
            self.assertTrue(action["selectable"])

    def test_tracking_circuit_breaker_suppresses_non_tracking_actions(self):
        item = campaign(
            latest_conversions=3,
            previous_conversions=10,
            latest_clicks=100,
            previous_clicks=100,
        )
        item["target_content_network"] = True

        result = evaluate_instant_account_audit(
            snapshot(item),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            brand_terms=["Test"],
        )

        self.assertEqual(result["status"], "hold")
        self.assertTrue(result["holds"])
        self.assertTrue(
            all(
                action["estimate"]["check_id"].startswith("A")
                for action in result["recommendations"]
            )
        )

    def test_unconfirmed_lead_quality_always_returns_a_resolvable_task(self):
        result = evaluate_instant_account_audit(
            snapshot(),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=False,
            brand_terms=["Test"],
        )

        task = next(
            action
            for action in result["recovery_actions"]
            if action["id"] == "REC-IA-OUTCOME"
        )
        self.assertEqual(task["status"], "needs_confirmation")
        self.assertIn("bookings", " ".join(task["steps"]).casefold())
        self.assertEqual(
            task["follow_up"]["kind"],
            "review_then_rerun",
        )
        self.assertTrue(result["holds"])
        self.assertTrue(
            result["assessment_details"]["outcome_quality_circuit_breaker"]
        )
        gated = {
            "A3",
            "B4",
            "C4",
            "D1",
            "D2",
            "F1",
            "F2",
            "F3",
            "H1",
            "H2",
            "H3",
        }
        checks = {check["id"]: check for check in result["checks"]}
        self.assertTrue(
            all(checks[check_id]["status"] == "unavailable" for check_id in gated)
        )
        resolved = {
            value
            for action in result["recovery_actions"]
            for value in action["resolves"]
        }
        self.assertTrue(gated.issubset(resolved))

    def test_unported_specialist_route_has_an_immediate_manual_fallback(self):
        data = snapshot()
        data["evidence"]["search_terms"] = [
            {
                "search_term": "massage near me",
                "status": "NONE",
                "campaign_id": "101",
                "cost_micros": 40_000_000,
                "conversions": 3,
                "all_conversions": 3,
                "clicks": 20,
            }
        ]
        data["evidence"]["keywords"] = [
            {"quality_score": 4, "impressions": 1_000}
        ]

        result = evaluate_instant_account_audit(
            data,
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            brand_terms=["Test"],
            available_module_ids=[
                "instant_account_audit",
                "red_flag_radar",
                "budget_reallocator",
            ],
        )

        checks = {check["id"]: check for check in result["checks"]}
        self.assertIn("Do this now:", checks["F3"]["next_step"])
        self.assertIn("Do this now:", checks["G4"]["next_step"])
        self.assertNotIn("Run Skill 4", checks["F3"]["next_step"])
        self.assertNotIn("Run Skill 6", checks["G4"]["next_step"])
        quality_action = next(
            action
            for action in result["recommendations"]
            if action["estimate"]["check_id"] == "G4"
        )
        self.assertEqual(
            quality_action["entity"],
            "Diagnose low Quality Score by component",
        )

    def test_optional_query_hides_provider_error_from_customer_gap(self):
        gateway = GoogleAdsInstantAuditGateway()
        gateway._search = Mock(side_effect=RuntimeError("private RPC details"))
        gaps = []

        rows, verified = gateway._optional_query(
            object(),
            "1234567890",
            "SELECT customer.id FROM customer",
            gap="Account evidence unavailable",
            gaps=gaps,
        )

        self.assertEqual(rows, [])
        self.assertFalse(verified)
        self.assertEqual(gaps, ["Account evidence unavailable"])

    def test_change_history_window_is_finite_and_api_safe(self):
        start, end = _change_history_bounds(
            datetime(2026, 7, 23, 12, 30, tzinfo=timezone.utc)
        )

        self.assertEqual(start, "2026-06-24 12:30:00")
        self.assertEqual(end, "2026-07-23 12:30:00")

    def test_search_waste_protects_a_root_that_converted(self):
        data = snapshot()
        data["campaigns"][0]["cost_micros"] = 600_000_000
        data["campaigns"][0]["conversions"] = 15
        data["evidence"]["search_terms"] = [
            {
                "search_term": "massage athens",
                "status": "NONE",
                "campaign_id": "101",
                "cost_micros": 100_000_000,
                "conversions": 1,
                "all_conversions": 1,
                "clicks": 10,
            },
            {
                "search_term": "massage athens ga",
                "status": "NONE",
                "campaign_id": "101",
                "cost_micros": 200_000_000,
                "conversions": 0,
                "all_conversions": 0,
                "clicks": 10,
            },
        ]

        result = evaluate_instant_account_audit(
            data,
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=True,
            brand_terms=["Test"],
        )

        waste = next(check for check in result["checks"] if check["id"] == "F1")
        self.assertEqual(waste["status"], "pass")
        self.assertIn("USD 0.00", waste["evidence"])


if __name__ == "__main__":
    unittest.main()
