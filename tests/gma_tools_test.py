"""Presentation-boundary tests for the customer-facing GMA tools."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ads_mcp.tools.gma import _with_authoritative_report


class GmaToolPresentationTest(unittest.TestCase):
    def test_run_response_carries_the_finished_report_and_recovery_rule(self):
        canonical = {
            "contract_version": "gma-run-result/1.0",
            "run_id": "run_0123456789abcdef0123456789abcdef",
            "runtime_version": "1.0.0-test",
            "methodology_version": "gma-budget-v1.0.0",
            "module": {"id": "budget_reallocator", "number": 12},
            "status": "blocked",
            "scope": {"account_name": "Test"},
            "data_receipt": {"source": "Google Ads"},
            "coverage": {"campaigns_analyzed": 1},
            "assessment": {"state": "blocked"},
            "checks": [{"id": "BR-101"}],
            "recommendations": [],
            "recovery_actions": [{"id": "REC-OUTCOME-QUALITY"}],
            "change_plan": {
                "id": "gma_test",
                "status": "draft",
                "review_url": "https://example.test/private?token=secret",
                "applyable_action_ids": [],
            },
            "core_signature": "a" * 64,
        }
        rendered = (
            "# Skill 12 — Budget Reallocator\n\n"
            "## Recovery plan\n\n"
            "### REC-OUTCOME-QUALITY — Confirm genuine leads"
        )

        with patch(
            "ads_mcp.tools.gma.render_run_result",
            return_value=rendered,
        ):
            response = _with_authoritative_report(canonical)

        self.assertEqual(response["run_id"], canonical["run_id"])
        self.assertNotIn("checks", response)
        self.assertEqual(
            response["full_result_tool"]["run_id"], canonical["run_id"]
        )
        self.assertEqual(response["full_result_tool"]["name"], "gma_get_run")
        self.assertNotIn("review_url", response["change_plan"])
        self.assertNotIn(
            "https://example.test/private",
            response["authoritative_report"]["content"],
        )
        self.assertEqual(response["authoritative_report"]["content"], rendered)
        self.assertIn(
            "Do not omit",
            response["authoritative_report"]["presentation_rule"],
        )
        self.assertIn(
            "Recovery plan",
            response["authoritative_report"]["presentation_rule"],
        )


if __name__ == "__main__":
    unittest.main()
