"""End-to-end contract test for the deterministic Skill 1 runtime adapter."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from key_value.aio.stores.memory import MemoryStore

from ads_mcp.gma_runtime import (
    render_run,
    run_skill,
    save_prepared_scope,
    validate_run_result,
)
from ads_mcp.skill_runs.instant_account_audit import (
    evaluate_instant_account_audit,
)
from tests.instant_account_audit_test import snapshot


class FakeInstantAuditService:
    async def run(self, **_kwargs):
        result = evaluate_instant_account_audit(
            snapshot(),
            business_mode="lead_gen",
            target_cpa_micros=50_000_000,
            outcome_quality_confirmed=False,
            brand_terms=["Test Account"],
        )
        result["data_receipt"] = {
            "source": "GMA 13 Skills Google Ads",
            "retrieved_at": "2026-07-23T12:00:00Z",
            "data_through": "2026-07-22",
            "analysis_start": "2026-06-23",
            "analysis_end": "2026-07-22",
        }
        result["change_plan"] = {
            "id": "gma_22222222222222222222222222222222",
            "status": "draft",
            "review_url": "https://example.test/instant-audit-plan",
            "applyable_action_ids": [],
        }
        return result


class InstantAuditRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_skill_one_is_validated_persisted_and_rendered(self):
        store = MemoryStore()
        owner = lambda: "owner-test"
        scope_hash = "a" * 64
        prepared = await save_prepared_scope(
            {
                "contract_version": "gma-scope/1.0",
                "scope_id": f"scope_{scope_hash[:24]}",
                "scope_hash": scope_hash,
                "scope": {
                    "customer_id": "1234567890",
                    "login_customer_id": "9876543210",
                    "account_name": "Test Account",
                    "currency": "USD",
                    "time_zone": "America/Chicago",
                    "analysis_start": "2026-06-23",
                    "analysis_end": "2026-07-22",
                    "business_mode": "lead_gen",
                    "campaigns": [
                        {
                            "id": "101",
                            "name": "Search | Core",
                            "type": "SEARCH",
                            "status": "ENABLED",
                            "spend_micros": 300_000_000,
                        }
                    ],
                },
                "confirmation_required": True,
            },
            store=store,
            owner_resolver=owner,
        )
        with patch(
            "ads_mcp.gma_runtime.InstantAccountAuditRunService",
            return_value=FakeInstantAuditService(),
        ):
            result = await run_skill(
                module_id="instant_account_audit",
                scope_id=prepared["scope_id"],
                confirmed_scope_hash=prepared["scope_hash"],
                business_inputs={
                    "target_cpa": 50,
                    "outcome_quality_confirmed": False,
                    "brand_terms": ["Test Account"],
                },
                store=store,
                owner_resolver=owner,
            )

        self.assertEqual(result["module"]["number"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["checks"]), 41)
        self.assertEqual(result["assessment"]["audit_summary"]["total_criteria"], 41)
        self.assertEqual(result["change_plan"]["applyable_action_ids"], [])
        self.assertTrue(
            any(
                action["id"] == "REC-IA-OUTCOME"
                for action in result["recovery_actions"]
            )
        )
        validate_run_result(result)
        rendered = await render_run(
            result["run_id"],
            store=store,
            owner_resolver=owner,
        )
        self.assertIn("# Skill 1 — Instant Account Audit", rendered["content"])
        self.assertIn("A1 — Auto-tagging is enabled", rendered["content"])
        self.assertIn("REC-IA-OUTCOME", rendered["content"])
        self.assertIn("Google Ads has **not** been changed", rendered["content"])


if __name__ == "__main__":
    unittest.main()
