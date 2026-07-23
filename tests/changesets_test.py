"""Safety and lifecycle tests for controlled Google Ads changesets."""

from __future__ import annotations

import os
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from key_value.aio.stores.memory import MemoryStore
from google.auth.credentials import AnonymousCredentials
from google.ads.googleads.client import GoogleAdsClient

from ads_mcp.changeset_store import MemoryReplayGuard
from ads_mcp.changesets import (
    ChangesetError,
    ChangesetService,
    GoogleAdsMutationGateway,
)
from ads_mcp.review_ui import render_change_plan


class FakeGateway:
    def __init__(self):
        self.values = {
            "A-001": {"status": "ENABLED"},
            "A-002": {"amount_micros": 10_000_000},
        }
        self.mutations = []
        self.highest_enabled_budget_micros = 25_000_000
        self.recent_budget_changes = {}

    def read_current_values(self, _customer_id, _login_customer_id, actions):
        return {action["id"]: deepcopy(self.values[action["id"]]) for action in actions}

    def mutate(
        self,
        _customer_id,
        _login_customer_id,
        actions,
        *,
        validate_only,
    ):
        self.mutations.append(
            {
                "ids": [action["id"] for action in actions],
                "validate_only": validate_only,
            }
        )
        if not validate_only:
            for action in actions:
                self.values[action["id"]] = deepcopy(action["proposed_value"])
        return {
            "validate_only": validate_only,
            "partial_failure": False,
            "operation_count": len(actions),
            "resource_names": [action["resource_name"] for action in actions],
        }

    def recent_change_events(self, _customer_id, _login_customer_id, resource_names):
        return [
            {
                "change_date_time": "2026-07-21 15:00:00",
                "change_resource_name": resource_name,
                "operation": "UPDATE",
                "changed_fields": ["status"],
                "client_type": "GOOGLE_ADS_API",
            }
            for resource_name in resource_names
        ]

    def budget_policy_context(self, _customer_id, _login_customer_id, _actions):
        return {
            "highest_enabled_budget_micros": self.highest_enabled_budget_micros,
            "recent_budget_changes": deepcopy(self.recent_budget_changes),
        }


def sample_plan(*, run_mode="interactive"):
    return {
        "customer_id": "1234567890",
        "login_customer_id": "9876543210",
        "account_name": "Test Ads Account",
        "currency": "USD",
        "time_zone": "America/Chicago",
        "analysis_start": "2026-06-21",
        "analysis_end": "2026-07-20",
        "analysis_label": "Last 30 days",
        "campaign_scope": "selected",
        "campaigns": [{"id": "456", "name": "Search | Test"}],
        "skills": ["red-flag-radar", "budget-reallocator"],
        "run_mode": run_mode,
        "coverage_gaps": ["Auction Insights requires a manual export"],
        "data_quality_holds": [],
        "actions": [
            {
                "id": "A-001",
                "priority": 1,
                "severity": "high",
                "evidence_label": "Observed",
                "entity": "Search | Test campaign",
                "resource_name": "customers/1234567890/campaigns/456",
                "operation_type": "pause_campaign",
                "current_value": {"status": "ENABLED"},
                "proposed_value": {"status": "PAUSED"},
                "reason": "Spend is continuing after the tracked conversion broke.",
                "evidence_summary": "$420 spend and zero valid primary conversions, 2026-06-21 to 2026-07-20.",
                "details": "Observed tracking failure; pause is reversible and prevents further unmeasured spend.",
                "expected_impact": "Stops additional unmeasured spend while tracking is repaired.",
                "risk": "medium",
                "reversible": True,
                "applyability": "applyable",
            },
            {
                "id": "A-002",
                "priority": 2,
                "severity": "medium",
                "evidence_label": "Derived",
                "entity": "Search | Test budget",
                "resource_name": "customers/1234567890/campaignBudgets/789",
                "operation_type": "set_campaign_budget_amount",
                "current_value": {"amount_micros": 10_000_000},
                "proposed_value": {"amount_micros": 12_000_000},
                "reason": "The campaign is profitable and consistently budget constrained.",
                "evidence_summary": "CPA $32 vs $45 target and >20% budget-lost IS, 2026-06-21 to 2026-07-20.",
                "details": "The exact $2/day increase stays within the confirmed account budget.",
                "expected_impact": "More eligible traffic at the current efficiency range.",
                "estimate": {
                    "label": "estimate",
                    "added_clicks_per_day": 3.2,
                },
                "risk": "low",
                "reversible": True,
                "applyability": "applyable",
            },
            {
                "id": "F-001",
                "priority": 3,
                "severity": "low",
                "evidence_label": "Manual verification needed",
                "entity": "Landing page",
                "resource_name": "",
                "operation_type": "change_landing_page",
                "current_value": {"headline": "Old"},
                "proposed_value": {"headline": "New"},
                "reason": "The page headline does not match the highest-volume query theme.",
                "evidence_summary": "42% of paid clicks share the unmatched theme in the selected window.",
                "details": "Site edits are outside the Google Ads connector and require a separate owner.",
                "expected_impact": "May improve message match after a controlled CRO test.",
                "risk": "medium",
                "reversible": True,
                "applyability": "advisory",
            },
        ],
    }


class ChangesetServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.store = MemoryStore()
        self.service = ChangesetService(
            store=self.store,
            replay_guard=MemoryReplayGuard(),
            gateway=self.gateway,
            owner_resolver=lambda: "owner-one",
            now_fn=lambda: datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc),
        )

    async def test_create_preserves_reason_evidence_detail_and_hides_owner(self):
        created = await self.service.create(sample_plan())

        self.assertNotIn("owner_id", created)
        self.assertNotIn("review_token_hash", created)
        self.assertIn("/changesets/", created["review_url"])
        self.assertEqual(created["analysis_label"], "Last 30 days")
        self.assertEqual(created["campaigns"][0]["id"], "456")
        self.assertIn("Spend is continuing", created["actions"][0]["reason"])
        self.assertIn("$420 spend", created["actions"][0]["evidence_summary"])
        self.assertIn("reversible", created["actions"][0]["details"])
        self.assertEqual(created["actions"][2]["operation_type"], "advisory")
        self.assertEqual(created["actions"][1]["source_skill"], "budget-reallocator")
        self.assertEqual(created["actions"][1]["estimate"]["label"], "estimate")

        stored = await self.store.get(created["id"])
        self.assertEqual(stored["owner_id"], "owner-one")
        self.assertIn("review_token_hash", stored)

    async def test_read_only_specialist_task_never_enters_apply_path(self):
        plan = sample_plan()
        plan["skills"] = ["red-flag-radar"]
        plan["actions"] = [
            {
                "id": "RF-001",
                "priority": 1,
                "severity": "critical",
                "evidence_label": "Calculated from live Google Ads data",
                "entity": "Resolve a disapproved ad",
                "resource_name": "",
                "operation_type": "advisory",
                "current_value": {},
                "proposed_value": {},
                "reason": "An active ad is disapproved.",
                "evidence_summary": "One disapproved active ad was returned.",
                "details": "Review Policy details and resolve or appeal the ad.",
                "expected_impact": "May restore delivery.",
                "estimate": {"label": "directional"},
                "risk": "low",
                "reversible": True,
                "applyability": "task",
                "source_skill": "red-flag-radar",
            }
        ]

        created = await self.service.create(plan)

        self.assertEqual(created["actions"][0]["applyability"], "task")
        self.assertEqual(created["actions"][0]["operation_type"], "advisory")
        self.assertEqual(created["validation"]["status"], "not_validated")
        self.assertEqual(created["application"]["status"], "not_applied")

    async def test_rejects_unallowlisted_applyable_action(self):
        plan = sample_plan()
        plan["actions"][0]["operation_type"] = "delete_campaign"
        with self.assertRaisesRegex(ChangesetError, "not allowlisted"):
            await self.service.create(plan)

    async def test_rejects_resource_from_another_customer(self):
        plan = sample_plan()
        plan["actions"][0]["resource_name"] = "customers/1111111111/campaigns/456"
        with self.assertRaisesRegex(ChangesetError, "must belong"):
            await self.service.create(plan)

    async def test_rejects_budget_increase_above_twenty_percent(self):
        plan = sample_plan()
        plan["actions"][1]["proposed_value"] = {"amount_micros": 12_000_001}
        with self.assertRaisesRegex(ChangesetError, "may not exceed 20%"):
            await self.service.create(plan)

    async def test_rejects_budget_decrease_above_fifty_percent(self):
        plan = sample_plan()
        plan["actions"][1]["proposed_value"] = {"amount_micros": 4_999_999}
        with self.assertRaisesRegex(ChangesetError, "may not exceed 50%"):
            await self.service.create(plan)

    async def test_validate_rejects_budget_above_account_ceiling(self):
        created = await self.service.create(sample_plan())
        self.gateway.highest_enabled_budget_micros = 5_000_000
        with patch.dict(
            os.environ, {"GMA_ENABLE_CHANGESET_VALIDATION": "1"}, clear=False
        ):
            with self.assertRaisesRegex(ChangesetError, "budget ceiling"):
                await self.service.validate(created["id"], ["A-002"])
        self.assertEqual(self.gateway.mutations, [])

    async def test_validate_rejects_budget_changed_within_twenty_four_hours(self):
        created = await self.service.create(sample_plan())
        self.gateway.recent_budget_changes = {
            "customers/1234567890/campaignBudgets/789": "2026-07-21T14:30:00Z"
        }
        with patch.dict(
            os.environ, {"GMA_ENABLE_CHANGESET_VALIDATION": "1"}, clear=False
        ):
            with self.assertRaisesRegex(ChangesetError, "last 24 hours"):
                await self.service.validate(created["id"], ["A-002"])
        self.assertEqual(self.gateway.mutations, [])

    async def test_scheduled_plan_cannot_validate(self):
        created = await self.service.create(sample_plan(run_mode="scheduled_analysis"))
        with patch.dict(
            os.environ, {"GMA_ENABLE_CHANGESET_VALIDATION": "1"}, clear=False
        ):
            with self.assertRaisesRegex(ChangesetError, "Scheduled analysis"):
                await self.service.validate(created["id"], ["A-001"])
        self.assertEqual(self.gateway.mutations, [])

    async def test_full_atomic_lifecycle(self):
        created = await self.service.create(sample_plan())
        environment = {
            "GMA_ENABLE_CHANGESET_VALIDATION": "1",
            "GMA_ENABLE_MUTATIONS": "1",
            "GMA_ALLOW_LIVE_MUTATIONS": "1",
            "GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED": "1",
            "GMA_LIVE_MUTATION_CUSTOMER_IDS": "1234567890",
        }
        with patch.dict(os.environ, environment, clear=False):
            validated = await self.service.validate(created["id"], ["A-001", "A-002"])
            approved = await self.service.approve(
                created["id"],
                validated["operation_hash"],
            )
            applied = await self.service.apply(created["id"])
            verified = await self.service.verify(created["id"])

        self.assertEqual(applied["status"], "applied_unverified")
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(
            self.gateway.mutations,
            [
                {"ids": ["A-001", "A-002"], "validate_only": True},
                {"ids": ["A-001", "A-002"], "validate_only": False},
            ],
        )
        fetched = await self.service.get(created["id"])
        self.assertNotIn("approval_token_hash", fetched["approval"])
        self.assertTrue(fetched["approval"]["consumed"])

    async def test_apply_rechecks_and_rejects_drift(self):
        created = await self.service.create(sample_plan())
        environment = {
            "GMA_ENABLE_CHANGESET_VALIDATION": "1",
            "GMA_ENABLE_MUTATIONS": "1",
            "GMA_ALLOW_LIVE_MUTATIONS": "1",
            "GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED": "1",
            "GMA_LIVE_MUTATION_CUSTOMER_IDS": "1234567890",
        }
        with patch.dict(os.environ, environment, clear=False):
            validated = await self.service.validate(created["id"], ["A-001"])
            approved = await self.service.approve(
                created["id"],
                validated["operation_hash"],
            )
            self.gateway.values["A-001"] = {"status": "PAUSED"}
            with self.assertRaisesRegex(ChangesetError, "changed after validation"):
                await self.service.apply(created["id"])

        self.assertEqual(
            self.gateway.mutations,
            [{"ids": ["A-001"], "validate_only": True}],
        )

    async def test_immediate_budget_cooldown_blocks_a_second_changeset(self):
        environment = {
            "GMA_ENABLE_CHANGESET_VALIDATION": "1",
            "GMA_ENABLE_MUTATIONS": "1",
            "GMA_ALLOW_LIVE_MUTATIONS": "1",
            "GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED": "1",
            "GMA_LIVE_MUTATION_CUSTOMER_IDS": "1234567890",
        }
        first = await self.service.create(sample_plan())
        with patch.dict(os.environ, environment, clear=False):
            validated = await self.service.validate(first["id"], ["A-002"])
            await self.service.approve(first["id"], validated["operation_hash"])
            await self.service.apply(first["id"])

            second_plan = sample_plan()
            second_plan["actions"][1]["current_value"] = {"amount_micros": 12_000_000}
            second_plan["actions"][1]["proposed_value"] = {"amount_micros": 14_400_000}
            second = await self.service.create(second_plan)
            validated = await self.service.validate(second["id"], ["A-002"])
            await self.service.approve(second["id"], validated["operation_hash"])
            with self.assertRaisesRegex(ChangesetError, "within the last 24 hours"):
                await self.service.apply(second["id"])

        self.assertEqual(
            self.gateway.mutations,
            [
                {"ids": ["A-002"], "validate_only": True},
                {"ids": ["A-002"], "validate_only": False},
                {"ids": ["A-002"], "validate_only": True},
            ],
        )

    async def test_apply_requires_every_server_side_flag(self):
        created = await self.service.create(sample_plan())
        with patch.dict(
            os.environ, {"GMA_ENABLE_CHANGESET_VALIDATION": "1"}, clear=False
        ):
            validated = await self.service.validate(created["id"], ["A-001"])
            approved = await self.service.approve(
                created["id"],
                validated["operation_hash"],
            )
            with patch.dict(
                os.environ,
                {
                    "GMA_ENABLE_MUTATIONS": "0",
                    "GMA_ALLOW_LIVE_MUTATIONS": "0",
                    "GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED": "0",
                    "GMA_LIVE_MUTATION_CUSTOMER_IDS": "",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ChangesetError, "globally disabled"):
                    await self.service.apply(created["id"])
        self.assertEqual(
            self.gateway.mutations,
            [{"ids": ["A-001"], "validate_only": True}],
        )

    async def test_owner_isolation_returns_not_found(self):
        created = await self.service.create(sample_plan())
        other = ChangesetService(
            store=self.store,
            replay_guard=MemoryReplayGuard(),
            gateway=self.gateway,
            owner_resolver=lambda: "owner-two",
            now_fn=lambda: datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ChangesetError, "not found"):
            await other.get(created["id"])

    async def test_private_review_selection_binds_validation_and_host_approval(self):
        created = await self.service.create(sample_plan())
        token = parse_qs(urlparse(created["review_url"]).query)["token"][0]
        selected = await self.service.select_for_review(created["id"], token, ["A-002"])
        self.assertEqual(selected["selected_action_ids"], ["A-002"])

        with patch.dict(
            os.environ, {"GMA_ENABLE_CHANGESET_VALIDATION": "1"}, clear=False
        ):
            with self.assertRaisesRegex(ChangesetError, "user's Change Plan selection"):
                await self.service.validate(created["id"], ["A-001"])
            validated = await self.service.validate(created["id"], ["A-002"])
            approved = await self.service.approve(
                created["id"], validated["operation_hash"]
            )

        self.assertEqual(approved["operation_hash"], validated["operation_hash"])
        self.assertNotIn("approval_token", approved)
        self.assertNotIn("approval_confirmation", validated)

    async def test_review_page_escapes_account_content_and_has_expandable_details(self):
        plan = sample_plan()
        plan["actions"][0]["reason"] = "<script>alert(1)</script>"
        created = await self.service.create(plan)
        rendered = render_change_plan(created, "review-token")

        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("<details>", rendered)
        self.assertIn("Evidence summary", rendered)
        self.assertIn("Save selected items for validation", rendered)

    def test_allowlisted_operations_build_only_exact_update_masks(self):
        client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="test-token",
            use_proto_plus=True,
        )
        actions = sample_plan()["actions"][:2]
        operations = GoogleAdsMutationGateway._build_operations(client, actions)

        self.assertEqual(
            list(operations[0].campaign_operation.update_mask.paths), ["status"]
        )
        self.assertEqual(
            operations[0].campaign_operation.update.resource_name,
            "customers/1234567890/campaigns/456",
        )
        self.assertEqual(
            list(operations[1].campaign_budget_operation.update_mask.paths),
            ["amount_micros"],
        )
        self.assertEqual(
            operations[1].campaign_budget_operation.update.amount_micros,
            12_000_000,
        )


if __name__ == "__main__":
    unittest.main()
