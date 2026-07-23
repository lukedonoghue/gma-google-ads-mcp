"""Tests for the small GMA router and validated run-result boundary."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from key_value.aio.stores.memory import MemoryStore

from ads_mcp.gma_runtime import (
    AccountGateway,
    EXPECTED_PLUGIN_VERSION,
    GmaRuntimeError,
    ScopeGateway,
    get_run,
    preflight,
    render_run,
    run_skill,
    save_prepared_scope,
    validate_run_result,
)


class AccountQueryService:
    def search(self, *, customer_id, query):
        self.customer_id = customer_id
        if "FROM customer LIMIT 1" in query:
            return [
                SimpleNamespace(
                    customer=SimpleNamespace(
                        id=9876543210,
                        descriptive_name="PPC Navigator",
                        manager=True,
                        currency_code="USD",
                        time_zone="America/New_York",
                    )
                )
            ]
        if "FROM customer_client" in query:
            return [
                SimpleNamespace(
                    customer_client=SimpleNamespace(
                        id=1234567890,
                        descriptive_name="Body Temple Spa",
                        manager=False,
                        status="ENABLED",
                        currency_code="USD",
                        time_zone="America/New_York",
                    )
                ),
                SimpleNamespace(
                    customer_client=SimpleNamespace(
                        id=1111111111,
                        descriptive_name="Nested Manager",
                        manager=True,
                        status="ENABLED",
                        currency_code="USD",
                        time_zone="America/New_York",
                    )
                ),
            ]
        raise AssertionError(query)


class ScopeQueryService:
    def __init__(self):
        self.queries = []

    def search(self, *, customer_id, query):
        self.queries.append(query)
        if "FROM customer LIMIT 1" in query:
            return [
                SimpleNamespace(
                    customer=SimpleNamespace(
                        id=1234567890,
                        descriptive_name="Body Temple Spa",
                        manager=False,
                        currency_code="USD",
                        time_zone="America/Chicago",
                    )
                )
            ]
        if "metrics.cost_micros FROM campaign" in query:
            return [
                SimpleNamespace(
                    campaign=SimpleNamespace(id=202),
                    metrics=SimpleNamespace(cost_micros=200_000_000),
                ),
                SimpleNamespace(
                    campaign=SimpleNamespace(id=303),
                    metrics=SimpleNamespace(cost_micros=200_000_000),
                ),
            ]
        if "FROM campaign WHERE" in query:
            return [
                SimpleNamespace(
                    campaign=SimpleNamespace(
                        id=101,
                        name="Zero Spend",
                        status="ENABLED",
                        advertising_channel_type="SEARCH",
                        bidding_strategy="",
                        bidding_strategy_type="MAXIMIZE_CONVERSIONS",
                        target_cpa=SimpleNamespace(target_cpa_micros=0),
                        maximize_conversions=SimpleNamespace(target_cpa_micros=0),
                        target_roas=SimpleNamespace(target_roas=0),
                        maximize_conversion_value=SimpleNamespace(target_roas=0),
                    )
                ),
                SimpleNamespace(
                    campaign=SimpleNamespace(
                        id=202,
                        name="Paused Spender",
                        status="PAUSED",
                        advertising_channel_type="SEARCH",
                        bidding_strategy="",
                        bidding_strategy_type="TARGET_CPA",
                        target_cpa=SimpleNamespace(
                            target_cpa_micros=10_000_000
                        ),
                        maximize_conversions=SimpleNamespace(target_cpa_micros=0),
                        target_roas=SimpleNamespace(target_roas=0),
                        maximize_conversion_value=SimpleNamespace(target_roas=0),
                    )
                ),
                SimpleNamespace(
                    campaign=SimpleNamespace(
                        id=303,
                        name="Enabled Spender",
                        status="ENABLED",
                        advertising_channel_type="SEARCH",
                        bidding_strategy="",
                        bidding_strategy_type="TARGET_CPA",
                        target_cpa=SimpleNamespace(
                            target_cpa_micros=50_000_000
                        ),
                        maximize_conversions=SimpleNamespace(target_cpa_micros=0),
                        target_roas=SimpleNamespace(target_roas=0),
                        maximize_conversion_value=SimpleNamespace(target_roas=0),
                    )
                ),
            ]
        if "FROM bidding_strategy" in query:
            return []
        if "FROM accessible_bidding_strategy" in query:
            return []
        if "FROM conversion_action" in query:
            return [
                SimpleNamespace(
                    conversion_action=SimpleNamespace(
                        category="SUBMIT_LEAD_FORM",
                        status="ENABLED",
                        primary_for_goal=True,
                    )
                )
            ]
        raise AssertionError(query)


def prepared_scope():
    return {
        "customer_id": "1234567890",
        "login_customer_id": "9876543210",
        "account_name": "Test Account",
        "currency": "USD",
        "time_zone": "America/Chicago",
        "analysis_start": "2026-06-21",
        "analysis_end": "2026-07-20",
        "business_mode": "lead_gen",
        "campaigns": [
            {
                "id": "101",
                "name": "Search | Weak",
                "type": "SEARCH",
                "status": "ENABLED",
                "spend_micros": 30_000_000,
            },
            {
                "id": "202",
                "name": "Search | Proven",
                "type": "SEARCH",
                "status": "ENABLED",
                "spend_micros": 40_000_000,
            },
        ],
    }


def campaign_check(campaign_id, name, *, recipient=False, donor=False):
    return {
        "campaign_id": campaign_id,
        "campaign_name": name,
        "status": "ENABLED",
        "channel_type": "SEARCH",
        "efficiency": {"status": "profitable", "actual": 40, "target": 50},
        "constraint": "budget_limited" if recipient else "not_materially_limited",
        "recipient_eligible": recipient,
        "donor_eligible": donor,
        "holds": [],
        "routes": [],
        "clicks_per_day": 5.0,
        "conversion_volume": 40.0,
        "scaling_volume_floor": 30,
        "bidding_strategy_type": "TARGET_CPA",
        "search_impression_share": 0.4,
        "search_budget_lost_impression_share": 0.3,
        "search_rank_lost_impression_share": 0.05,
    }


def budget_action(action_id, campaign_id, current, proposed):
    return {
        "id": action_id,
        "priority": 1,
        "severity": "high",
        "evidence_label": "Calculated from live Google Ads data",
        "entity": f"Campaign {campaign_id} daily budget",
        "resource_name": f"customers/1234567890/campaignBudgets/{campaign_id}",
        "operation_type": "set_campaign_budget_amount",
        "current_value": {"amount_micros": current},
        "proposed_value": {"amount_micros": proposed},
        "reason": "The campaign passed the deterministic budget gate.",
        "evidence_summary": "Live CPA, volume and impression-share evidence.",
        "details": "Change is capped at 20% and reviewed again after 14 days.",
        "expected_impact": "Directional estimate based on the selected window.",
        "estimate": {"label": "estimate", "basis": "30-day average"},
        "risk": "medium",
        "reversible": True,
        "applyability": "applyable",
        "source_skill": "budget-reallocator",
    }


def raw_budget_result():
    return {
        "status": "recommendations_ready",
        "conclusion": "Two exact budget changes passed the gates.",
        "campaigns_analyzed": 2,
        "campaign_results": [
            campaign_check("101", "Search | Weak", donor=True),
            campaign_check("202", "Search | Proven", recipient=True),
        ],
        "recommendations": [
            budget_action("BR-001", "101", 20_000_000, 16_000_000),
            budget_action("BR-002", "202", 20_000_000, 24_000_000),
        ],
        "holds": [],
        "coverage_gaps": [],
        "structural_budget_check": {"monthly_cap_micros": 1_216_000_000},
        "data_receipt": {
            "source": "GMA 13 Skills Google Ads",
            "retrieved_at": "2026-07-21T15:00:00Z",
            "data_through": "2026-07-20",
            "analysis_start": "2026-06-21",
            "analysis_end": "2026-07-20",
        },
        "change_plan": {
            "id": "gma_00000000000000000000000000000000",
            "status": "draft",
            "review_url": "https://example.test/change-plan",
            "applyable_action_ids": ["BR-001", "BR-002"],
        },
    }


class FakeBudgetService:
    async def run(self, **_kwargs):
        return raw_budget_result()


class GmaRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.owner = lambda: "owner-one"

    async def _save_scope(self):
        scope_hash = "a" * 64
        prepared = {
            "contract_version": "gma-scope/1.0",
            "scope_id": f"scope_{scope_hash[:24]}",
            "scope_hash": scope_hash,
            "scope": prepared_scope(),
            "confirmation_required": True,
        }
        await save_prepared_scope(
            prepared,
            store=self.store,
            owner_resolver=self.owner,
        )
        return prepared

    def test_account_discovery_returns_only_advertisers_inside_access_root(self):
        service = AccountQueryService()
        with (
            patch(
                "ads_mcp.gma_runtime.utils.get_access_root_customer_id",
                return_value="9876543210",
            ),
            patch(
                "ads_mcp.gma_runtime.utils.get_enforced_login_customer_id",
                return_value="9999999999",
            ),
            patch(
                "ads_mcp.gma_runtime.utils.get_googleads_service",
                return_value=service,
            ),
        ):
            result = AccountGateway().list_advertisers()

        self.assertEqual(
            [item["customer_id"] for item in result["accounts"]], ["1234567890"]
        )
        self.assertEqual(result["accounts"][0]["login_customer_id"], "9876543210")
        self.assertFalse(result["selection_required"])

    def test_scope_keeps_paused_and_zero_spend_campaigns_and_sorts_on_fixed_30d_spend(self):
        service = ScopeQueryService()
        with (
            patch(
                "ads_mcp.gma_runtime.utils.get_googleads_service",
                return_value=service,
            ),
            patch("ads_mcp.gma_runtime.utils.enforce_customer_access_root"),
        ):
            result = ScopeGateway(
                now_fn=lambda: datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
            ).prepare(
                customer_id="1234567890",
                analysis_start="2026-01-01",
                analysis_end="2026-06-30",
            )

        self.assertEqual(
            [item["name"] for item in result["scope"]["campaigns"]],
            ["Enabled Spender", "Paused Spender", "Zero Spend"],
        )
        self.assertEqual(result["scope"]["campaigns"][2]["spend_micros"], 0)
        self.assertEqual(result["scope"]["campaign_spend_window_start"], "2026-06-22")
        self.assertEqual(result["scope"]["campaign_spend_window_end"], "2026-07-21")
        suggestion = result["scope"]["goal_context"]["selected_suggestion"]
        self.assertEqual(suggestion["target_cpa_micros"], 50_000_000)
        self.assertEqual(suggestion["campaign_ids"], ["303"])
        inventory_query = next(
            query for query in service.queries if "FROM campaign WHERE" in query
        )
        self.assertIn(
            "campaign.maximize_conversions.target_cpa_micros",
            inventory_query,
        )
        self.assertIn(
            "campaign.maximize_conversion_value.target_roas",
            inventory_query,
        )
        spend_query = next(
            query for query in service.queries if "metrics.cost_micros FROM campaign" in query
        )
        self.assertIn("BETWEEN '2026-06-22' AND '2026-07-21'", spend_query)
        self.assertNotIn("2026-01-01", spend_query)

    def test_preflight_makes_stale_host_package_visible(self):
        result = preflight("0.2.3")
        self.assertEqual(
            result["connector"]["expected_plugin_version"], EXPECTED_PLUGIN_VERSION
        )
        self.assertFalse(result["connector"]["package_match"])
        available = [
            module["id"]
            for module in result["modules"]
            if module["runtime_status"] == "available"
        ]
        self.assertEqual(available, ["budget_reallocator"])

    async def test_run_skill_returns_validated_stable_contract(self):
        prepared = await self._save_scope()
        with patch(
            "ads_mcp.gma_runtime.BudgetReallocatorRunService",
            return_value=FakeBudgetService(),
        ):
            result = await run_skill(
                module_id="budget_reallocator",
                scope_id=prepared["scope_id"],
                confirmed_scope_hash=prepared["scope_hash"],
                business_inputs={
                    "target_cpa": 50,
                    "outcome_quality_confirmed": True,
                },
                store=self.store,
                owner_resolver=self.owner,
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["module"]["number"], 12)
        self.assertEqual(
            result["change_plan"]["applyable_action_ids"], ["BR-001", "BR-002"]
        )
        self.assertEqual(len(result["core_signature"]), 64)
        validate_run_result(result)
        fetched = await get_run(
            result["run_id"], store=self.store, owner_resolver=self.owner
        )
        self.assertEqual(fetched["core_signature"], result["core_signature"])
        rendered = await render_run(
            result["run_id"], store=self.store, owner_resolver=self.owner
        )
        self.assertIn("# Skill 12 — Budget Reallocator", rendered["content"])
        self.assertIn("Google Ads has **not** been changed", rendered["content"])

    async def test_run_rejects_a_scope_hash_the_user_did_not_confirm(self):
        prepared = await self._save_scope()
        with self.assertRaisesRegex(GmaRuntimeError, "does not match"):
            await run_skill(
                module_id="budget_reallocator",
                scope_id=prepared["scope_id"],
                confirmed_scope_hash="b" * 64,
                business_inputs={},
                store=self.store,
                owner_resolver=self.owner,
            )

    async def test_unported_module_fails_instead_of_using_prompt_fallback(self):
        prepared = await self._save_scope()
        with self.assertRaisesRegex(GmaRuntimeError, "not yet ported"):
            await run_skill(
                module_id="red_flag_radar",
                scope_id=prepared["scope_id"],
                confirmed_scope_hash=prepared["scope_hash"],
                business_inputs={},
                store=self.store,
                owner_resolver=self.owner,
            )

    async def test_hybrid_budget_scope_requires_separate_campaign_groups(self):
        scope = prepared_scope()
        scope["business_mode"] = "hybrid"
        scope_hash = "c" * 64
        await save_prepared_scope(
            {
                "contract_version": "gma-scope/1.0",
                "scope_id": f"scope_{scope_hash[:24]}",
                "scope_hash": scope_hash,
                "scope": scope,
                "confirmation_required": True,
            },
            store=self.store,
            owner_resolver=self.owner,
        )
        with self.assertRaisesRegex(GmaRuntimeError, "separate ecommerce and lead-gen"):
            await run_skill(
                module_id="budget_reallocator",
                scope_id=f"scope_{scope_hash[:24]}",
                confirmed_scope_hash=scope_hash,
                business_inputs={},
                store=self.store,
                owner_resolver=self.owner,
            )

    async def test_schema_rejects_unknown_fields(self):
        prepared = await self._save_scope()
        with patch(
            "ads_mcp.gma_runtime.BudgetReallocatorRunService",
            return_value=FakeBudgetService(),
        ):
            result = await run_skill(
                module_id="budget_reallocator",
                scope_id=prepared["scope_id"],
                confirmed_scope_hash=prepared["scope_hash"],
                business_inputs={"target_cpa": 50},
                store=self.store,
                owner_resolver=self.owner,
            )
        result["model_invented_field"] = True
        with self.assertRaisesRegex(GmaRuntimeError, "unknown fields"):
            validate_run_result(result)


if __name__ == "__main__":
    unittest.main()
