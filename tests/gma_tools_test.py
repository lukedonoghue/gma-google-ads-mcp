"""Presentation-boundary tests for the customer-facing GMA tools."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ads_mcp.tools.gma import _with_authoritative_report


class GmaToolPresentationTest(unittest.TestCase):
    def test_run_response_carries_the_finished_report_and_recovery_rule(self):
        canonical = {
            "run_id": "run_0123456789abcdef0123456789abcdef",
            "status": "blocked",
            "recovery_actions": [{"id": "REC-OUTCOME-QUALITY"}],
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
