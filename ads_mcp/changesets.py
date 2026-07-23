"""Controlled Google Ads changesets.

This module deliberately exposes no general-purpose mutate method. It accepts a
bounded Change Plan, validates only typed allowlisted operations, binds approval
to the exact operation hash, rejects drift, and applies atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from ads_mcp import utils
from ads_mcp.changeset_store import (
    AsyncChangesetStore,
    ReplayGuard,
    get_changeset_store,
    get_replay_guard,
)

PLAN_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_ACTIONS = 50
MAX_CAMPAIGNS = 500
MAX_PLAN_BYTES = 256_000
BUDGET_MAX_INCREASE_PERCENT = 20
BUDGET_MAX_DECREASE_PERCENT = 50
BUDGET_ACCOUNT_CEILING_MULTIPLIER = 2
BUDGET_CHANGE_COOLDOWN = timedelta(hours=24)
BUDGET_CHANGE_COOLDOWN_SECONDS = int(BUDGET_CHANGE_COOLDOWN.total_seconds())

SUPPORTED_SKILLS = {
    "instant-account-audit",
    "wasted-spend-finder",
    "red-flag-radar",
    "structure-fixer",
    "bid-strategy-check",
    "quality-score-booster",
    "ad-copy-analyzer",
    "winning-keyword-promoter",
    "keyword-gap-finder",
    "competitor-spy",
    "landing-page-cro-audit",
    "budget-reallocator",
    "weekly-digest",
}

ALLOWLISTED_OPERATIONS = {
    "pause_campaign",
    "set_search_partners",
    "set_display_expansion",
    "set_campaign_budget_amount",
}

ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CAMPAIGN_RESOURCE_RE = re.compile(r"^customers/(\d{10})/campaigns/(\d+)$")
BUDGET_RESOURCE_RE = re.compile(r"^customers/(\d{10})/campaignBudgets/(\d+)$")
ENTITY_ID_RE = re.compile(r"^\d{1,20}$")


class ChangesetError(ValueError):
    """Safe user-facing failure for a controlled-changeset operation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ChangesetError("Changeset timestamp is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_text(
    value: Any, field: str, limit: int, *, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise ChangesetError(f"{field} must be text")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ChangesetError(f"{field} is required")
    if len(normalized) > limit:
        raise ChangesetError(f"{field} exceeds {limit} characters")
    return normalized


def _bounded_json_object(value: Any, field: str, limit: int) -> dict[str, Any]:
    """Accept only a small, portable JSON object for recommendation metadata."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ChangesetError(f"{field} must be an object")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ChangesetError(f"{field} must contain only valid JSON values") from error
    if len(encoded.encode()) > limit:
        raise ChangesetError(f"{field} exceeds {limit} bytes")
    return json.loads(encoded)


def _normalize_customer_id(value: Any, field: str = "customer_id") -> str:
    if not isinstance(value, str):
        raise ChangesetError(f"{field} must be a 10-digit Google Ads customer ID")
    normalized = value.replace("-", "").strip()
    if not re.fullmatch(r"\d{10}", normalized):
        raise ChangesetError(f"{field} must be a 10-digit Google Ads customer ID")
    return normalized


def _identity_owner_id() -> str:
    """Bind changesets to the current OAuth subject without storing identity data."""

    subject: str | None = None
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        claims = getattr(token, "claims", None) or {}
        subject = claims.get("sub") or claims.get("email")
    except RuntimeError:
        # Direct unit calls and local stdio do not have an HTTP auth context.
        subject = None

    if (
        not subject
        and os.environ.get("GMA_MCP_ENV", "development").lower() != "production"
    ):
        subject = os.environ.get("GMA_MCP_DEV_IDENTITY", "local-development")
    if not subject:
        raise ChangesetError("An authenticated Google identity is required")
    return hashlib.sha256(f"gma-changesets:{subject}".encode()).hexdigest()


def current_identity_owner_id() -> str:
    """Return the opaque owner binding used by GMA persisted state."""

    return _identity_owner_id()


def _operation_hash(plan: Mapping[str, Any], selected_ids: Sequence[str]) -> str:
    selected = set(selected_ids)
    operations = []
    for action in plan["actions"]:
        if action["id"] in selected:
            operations.append(
                {
                    "id": action["id"],
                    "operation_type": action["operation_type"],
                    "resource_name": action["resource_name"],
                    "current_value": action["current_value"],
                    "proposed_value": action["proposed_value"],
                }
            )
    payload = {
        "changeset_id": plan["id"],
        "customer_id": plan["customer_id"],
        "operations": operations,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    rendered = str(value)
    return rendered.rsplit(".", 1)[-1]


def _validate_value_shape(operation_type: str, current: Any, proposed: Any) -> None:
    if not isinstance(current, Mapping) or not isinstance(proposed, Mapping):
        raise ChangesetError(
            f"{operation_type} current_value and proposed_value must be objects"
        )
    if operation_type == "pause_campaign":
        if set(current) != {"status"} or set(proposed) != {"status"}:
            raise ChangesetError("pause_campaign accepts only the status field")
        if current["status"] != "ENABLED" or proposed["status"] != "PAUSED":
            raise ChangesetError("pause_campaign must change ENABLED to PAUSED")
    elif operation_type == "set_search_partners":
        field = "target_partner_search_network"
        if set(current) != {field} or set(proposed) != {field}:
            raise ChangesetError(f"set_search_partners accepts only {field}")
        if not isinstance(current[field], bool) or not isinstance(
            proposed[field], bool
        ):
            raise ChangesetError(f"{field} must be boolean")
        if current[field] == proposed[field]:
            raise ChangesetError("Search Partners proposed value must be different")
    elif operation_type == "set_display_expansion":
        field = "target_content_network"
        if set(current) != {field} or set(proposed) != {field}:
            raise ChangesetError(f"set_display_expansion accepts only {field}")
        if not isinstance(current[field], bool) or not isinstance(
            proposed[field], bool
        ):
            raise ChangesetError(f"{field} must be boolean")
        if current[field] == proposed[field]:
            raise ChangesetError("Display Expansion proposed value must be different")
    elif operation_type == "set_campaign_budget_amount":
        field = "amount_micros"
        if set(current) != {field} or set(proposed) != {field}:
            raise ChangesetError(f"set_campaign_budget_amount accepts only {field}")
        for value in (current[field], proposed[field]):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ChangesetError("Budget amount_micros must be a positive integer")
        if current[field] == proposed[field]:
            raise ChangesetError("Budget proposed value must be different")
        current_amount = current[field]
        proposed_amount = proposed[field]
        if proposed_amount > current_amount:
            if proposed_amount * 100 > current_amount * (
                100 + BUDGET_MAX_INCREASE_PERCENT
            ):
                raise ChangesetError(
                    "A budget increase may not exceed 20% in one apply"
                )
        elif proposed_amount * 100 < current_amount * (
            100 - BUDGET_MAX_DECREASE_PERCENT
        ):
            raise ChangesetError("A budget decrease may not exceed 50% in one apply")


def _assert_budget_policy(
    actions: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    now: datetime,
) -> None:
    """Enforce account-aware budget gates outside model control."""

    budget_actions = [
        action
        for action in actions
        if action["operation_type"] == "set_campaign_budget_amount"
    ]
    if not budget_actions:
        return

    highest = context.get("highest_enabled_budget_micros")
    if isinstance(highest, bool) or not isinstance(highest, int) or highest <= 0:
        raise ChangesetError(
            "Unable to verify the account budget ceiling; budget changes are blocked"
        )

    ceiling = highest * BUDGET_ACCOUNT_CEILING_MULTIPLIER
    recent = context.get("recent_budget_changes") or {}
    if not isinstance(recent, Mapping):
        raise ChangesetError(
            "Unable to verify recent budget changes; budget changes are blocked"
        )

    for action in budget_actions:
        proposed = action["proposed_value"]["amount_micros"]
        if proposed > ceiling:
            raise ChangesetError(
                f"{action['id']} exceeds the server budget ceiling of "
                f"{ceiling} micros"
            )
        changed_at_raw = recent.get(action["resource_name"])
        if not changed_at_raw:
            continue
        changed_at = _parse_datetime(str(changed_at_raw))
        if now - changed_at < BUDGET_CHANGE_COOLDOWN:
            raise ChangesetError(
                f"{action['id']} changed within the last 24 hours; wait before another budget apply"
            )


def _normalize_action(
    raw: Mapping[str, Any], customer_id: str, source_skills: Sequence[str]
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ChangesetError("Each action must be an object")
    action_id = _bounded_text(raw.get("id"), "action.id", 64)
    if not ACTION_ID_RE.fullmatch(action_id):
        raise ChangesetError("action.id may contain only letters, numbers, _ and -")
    applyability = raw.get("applyability", "advisory")
    if applyability not in {
        "applyable",
        "advisory",
        "task",
        "blocked",
        "hold",
        "monitor",
    }:
        raise ChangesetError(f"Invalid applyability for {action_id}")
    operation_type = raw.get("operation_type", "advisory")
    if applyability == "applyable" and operation_type not in ALLOWLISTED_OPERATIONS:
        raise ChangesetError(f"{action_id} uses an operation that is not allowlisted")
    if applyability != "applyable":
        operation_type = "advisory"

    resource_name = _bounded_text(
        raw.get("resource_name", ""),
        "action.resource_name",
        220,
        allow_empty=applyability != "applyable",
    )
    current_value = deepcopy(raw.get("current_value"))
    proposed_value = deepcopy(raw.get("proposed_value"))
    if applyability == "applyable":
        matcher = (
            BUDGET_RESOURCE_RE
            if operation_type == "set_campaign_budget_amount"
            else CAMPAIGN_RESOURCE_RE
        )
        match = matcher.fullmatch(resource_name)
        if not match or match.group(1) != customer_id:
            raise ChangesetError(
                f"{action_id} resource_name must belong to customer {customer_id}"
            )
        _validate_value_shape(operation_type, current_value, proposed_value)

    details = raw.get("details")
    if isinstance(details, Sequence) and not isinstance(details, (str, bytes)):
        details = "\n".join(str(item) for item in details)
    source_skill = raw.get("source_skill")
    if not source_skill:
        preferred_skill = {
            "set_campaign_budget_amount": "budget-reallocator",
        }.get(operation_type)
        source_skill = (
            preferred_skill if preferred_skill in source_skills else source_skills[0]
        )
    source_skill = _bounded_text(source_skill, "action.source_skill", 64)
    if source_skill not in SUPPORTED_SKILLS or source_skill not in source_skills:
        raise ChangesetError(
            f"{action_id} source_skill must be one of the plan's source skills"
        )
    return {
        "id": action_id,
        "priority": int(raw.get("priority", 3)),
        "severity": _bounded_text(raw.get("severity", "medium"), "action.severity", 24),
        "evidence_label": _bounded_text(
            raw.get("evidence_label", "Observed"), "action.evidence_label", 40
        ),
        "entity": _bounded_text(raw.get("entity"), "action.entity", 240),
        "resource_name": resource_name,
        "operation_type": operation_type,
        "current_value": current_value,
        "proposed_value": proposed_value,
        "reason": _bounded_text(raw.get("reason"), "action.reason", 500),
        "evidence_summary": _bounded_text(
            raw.get("evidence_summary"), "action.evidence_summary", 1_500
        ),
        "details": _bounded_text(details, "action.details", 8_000),
        "expected_impact": _bounded_text(
            raw.get("expected_impact", "Direction only; no guaranteed uplift"),
            "action.expected_impact",
            600,
        ),
        "estimate": _bounded_json_object(raw.get("estimate"), "action.estimate", 4_000),
        "source_skill": source_skill,
        "risk": _bounded_text(raw.get("risk", "medium"), "action.risk", 32),
        "reversible": bool(raw.get("reversible", False)),
        "applyability": applyability,
        "validation_status": "not_validated",
        "approval_status": "not_approved",
        "implementation_status": "not_applied",
    }


def _normalize_recovery_action(
    raw: Mapping[str, Any], campaign_ids: set[str]
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ChangesetError("Each recovery action must be an object")
    action_id = _bounded_text(raw.get("id"), "recovery_action.id", 64)
    if not re.fullmatch(r"REC-[A-Z0-9-]{1,60}", action_id):
        raise ChangesetError("Recovery action IDs must use the REC- prefix")
    steps = raw.get("steps") or []
    if (
        not isinstance(steps, list)
        or not steps
        or not all(isinstance(step, str) and step.strip() for step in steps)
    ):
        raise ChangesetError(f"{action_id} must contain concrete recovery steps")
    applies_to = raw.get("applies_to") or []
    if not isinstance(applies_to, list) or not applies_to:
        raise ChangesetError(f"{action_id} must identify affected campaigns")
    normalized_scope = []
    for entity in applies_to:
        if not isinstance(entity, Mapping):
            raise ChangesetError(f"{action_id} has an invalid affected campaign")
        campaign_id = str(entity.get("campaign_id", "")).strip()
        if campaign_id not in campaign_ids:
            raise ChangesetError(
                f"{action_id} references a campaign outside the Change Plan"
            )
        normalized_scope.append(
            {
                "campaign_id": campaign_id,
                "campaign_name": _bounded_text(
                    entity.get("campaign_name"),
                    "recovery_action.campaign_name",
                    250,
                ),
            }
        )
    follow_up = raw.get("follow_up") or {}
    if not isinstance(follow_up, Mapping):
        raise ChangesetError(f"{action_id} has an invalid follow-up")
    return {
        "id": action_id,
        "priority": int(raw.get("priority", 3)),
        "type": _bounded_text(raw.get("type"), "recovery_action.type", 64),
        "status": _bounded_text(raw.get("status"), "recovery_action.status", 32),
        "title": _bounded_text(raw.get("title"), "recovery_action.title", 250),
        "reason": _bounded_text(raw.get("reason"), "recovery_action.reason", 1_500),
        "steps": [
            _bounded_text(step, "recovery_action.step", 1_500)
            for step in steps[:8]
        ],
        "applies_to": normalized_scope,
        "resolves": [
            _bounded_text(item, "recovery_action.resolves", 500)
            for item in (raw.get("resolves") or [])[:20]
        ],
        "completion_signal": _bounded_text(
            raw.get("completion_signal"),
            "recovery_action.completion_signal",
            1_500,
        ),
        "owner": _bounded_text(raw.get("owner"), "recovery_action.owner", 40),
        "follow_up": {
            "kind": _bounded_text(
                follow_up.get("kind"), "recovery_action.follow_up.kind", 64
            ),
            "module_id": (
                _bounded_text(
                    follow_up.get("module_id"),
                    "recovery_action.follow_up.module_id",
                    64,
                )
                if follow_up.get("module_id")
                else None
            ),
            "not_before": (
                _bounded_text(
                    follow_up.get("not_before"),
                    "recovery_action.follow_up.not_before",
                    10,
                )
                if follow_up.get("not_before")
                else None
            ),
        },
        "selectable": True,
        "completion_status": "open",
    }


def _normalize_plan(
    raw: Mapping[str, Any], owner_id: str, now: datetime
) -> dict[str, Any]:
    customer_id = _normalize_customer_id(raw.get("customer_id"))
    login_customer_id = raw.get("login_customer_id")
    if login_customer_id:
        login_customer_id = _normalize_customer_id(
            login_customer_id, "login_customer_id"
        )
    run_mode = raw.get("run_mode", "interactive")
    if run_mode not in {"interactive", "scheduled_analysis"}:
        raise ChangesetError("run_mode must be interactive or scheduled_analysis")
    try:
        analysis_start = date.fromisoformat(str(raw.get("analysis_start")))
        analysis_end = date.fromisoformat(str(raw.get("analysis_end")))
    except ValueError as error:
        raise ChangesetError(
            "analysis_start and analysis_end must be YYYY-MM-DD"
        ) from error
    if analysis_end < analysis_start:
        raise ChangesetError("analysis_end cannot be before analysis_start")

    skills = raw.get("skills") or []
    if not isinstance(skills, list) or not skills:
        raise ChangesetError("At least one source skill is required")
    if not all(skill in SUPPORTED_SKILLS for skill in skills):
        raise ChangesetError("One or more source skills are not recognized")

    campaigns = raw.get("campaigns") or []
    if not isinstance(campaigns, list) or len(campaigns) > MAX_CAMPAIGNS:
        raise ChangesetError(f"campaigns must contain at most {MAX_CAMPAIGNS} items")
    normalized_campaigns = []
    for campaign in campaigns:
        if not isinstance(campaign, Mapping):
            raise ChangesetError("Each campaign must be an object")
        campaign_id = str(campaign.get("id", "")).strip()
        if not ENTITY_ID_RE.fullmatch(campaign_id):
            raise ChangesetError("Campaign IDs must be numeric")
        normalized_campaigns.append(
            {
                "id": campaign_id,
                "name": _bounded_text(campaign.get("name"), "campaign.name", 250),
            }
        )

    raw_actions = raw.get("actions") or []
    if not isinstance(raw_actions, list) or len(raw_actions) > MAX_ACTIONS:
        raise ChangesetError(f"actions must contain at most {MAX_ACTIONS} items")
    actions = [_normalize_action(action, customer_id, skills) for action in raw_actions]
    action_ids = [action["id"] for action in actions]
    if len(action_ids) != len(set(action_ids)):
        raise ChangesetError("Action IDs must be unique within a changeset")
    raw_recovery_actions = raw.get("recovery_actions") or []
    if (
        not isinstance(raw_recovery_actions, list)
        or len(raw_recovery_actions) > MAX_ACTIONS
    ):
        raise ChangesetError(
            f"recovery_actions must contain at most {MAX_ACTIONS} items"
        )
    recovery_actions = [
        _normalize_recovery_action(
            action,
            {campaign["id"] for campaign in normalized_campaigns},
        )
        for action in raw_recovery_actions
    ]
    recovery_ids = [action["id"] for action in recovery_actions]
    if len(recovery_ids) != len(set(recovery_ids)):
        raise ChangesetError("Recovery action IDs must be unique within a changeset")
    if set(recovery_ids).intersection(action_ids):
        raise ChangesetError("Recovery and Google Ads action IDs must be distinct")

    changeset_id = f"gma_{uuid.uuid4().hex}"
    created_at = _iso(now)
    expires_at = _iso(now + timedelta(seconds=PLAN_TTL_SECONDS))
    plan = {
        "id": changeset_id,
        "owner_id": owner_id,
        "version": 1,
        "status": "draft",
        "customer_id": customer_id,
        "login_customer_id": login_customer_id,
        "account_name": _bounded_text(raw.get("account_name"), "account_name", 250),
        "currency": _bounded_text(raw.get("currency"), "currency", 8),
        "time_zone": _bounded_text(raw.get("time_zone"), "time_zone", 80),
        "analysis_start": analysis_start.isoformat(),
        "analysis_end": analysis_end.isoformat(),
        "analysis_label": _bounded_text(
            raw.get("analysis_label", "Custom"), "analysis_label", 80
        ),
        "campaign_scope": raw.get("campaign_scope", "all_eligible"),
        "campaigns": normalized_campaigns,
        "skills": skills,
        "run_mode": run_mode,
        "coverage_gaps": [
            _bounded_text(item, "coverage_gap", 500)
            for item in (raw.get("coverage_gaps") or [])[:30]
        ],
        "data_quality_holds": [
            _bounded_text(item, "data_quality_hold", 500)
            for item in (raw.get("data_quality_holds") or [])[:30]
        ],
        "actions": actions,
        "recovery_actions": recovery_actions,
        "selected_action_ids": [],
        "operation_hash": None,
        "validation": {"status": "not_validated"},
        "approval": {"status": "not_approved"},
        "application": {"status": "not_applied"},
        "verification": {"status": "not_verified"},
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": expires_at,
    }
    if len(json.dumps(plan, separators=(",", ":")).encode()) > MAX_PLAN_BYTES:
        raise ChangesetError("Changeset exceeds the maximum stored size")
    return plan


def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    public = deepcopy(dict(plan))
    public.pop("owner_id", None)
    public.pop("review_token_hash", None)
    public.get("approval", {}).pop("approval_token_hash", None)
    return public


class GoogleAdsMutationGateway:
    """Narrow Google Ads adapter for the initial reversible allowlist."""

    def _services(self, login_customer_id: str | None):
        client = utils.get_googleads_client(login_customer_id=login_customer_id)
        service = client.get_service("GoogleAdsService")
        return client, service

    @staticmethod
    def _one_row(service, customer_id: str, query: str):
        rows = list(service.search(customer_id=customer_id, query=query))
        if len(rows) != 1:
            raise ChangesetError(
                "A targeted Google Ads resource was not found uniquely"
            )
        return rows[0]

    def read_current_values(
        self,
        customer_id: str,
        login_customer_id: str | None,
        actions: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        _, service = self._services(login_customer_id)
        utils.enforce_customer_access_root(service, customer_id)
        values: dict[str, dict[str, Any]] = {}
        for action in actions:
            resource_name = action["resource_name"]
            operation_type = action["operation_type"]
            if operation_type == "set_campaign_budget_amount":
                query = (
                    "SELECT campaign_budget.resource_name, "
                    "campaign_budget.amount_micros FROM campaign_budget "
                    f"WHERE campaign_budget.resource_name = '{resource_name}' LIMIT 1"
                )
                row = self._one_row(service, customer_id, query)
                values[action["id"]] = {
                    "amount_micros": int(row.campaign_budget.amount_micros)
                }
            else:
                query = (
                    "SELECT campaign.resource_name, campaign.status, "
                    "campaign.network_settings.target_partner_search_network, "
                    "campaign.network_settings.target_content_network FROM campaign "
                    f"WHERE campaign.resource_name = '{resource_name}' LIMIT 1"
                )
                row = self._one_row(service, customer_id, query)
                if operation_type == "pause_campaign":
                    value = {"status": _enum_name(row.campaign.status)}
                elif operation_type == "set_search_partners":
                    value = {
                        "target_partner_search_network": bool(
                            row.campaign.network_settings.target_partner_search_network
                        )
                    }
                elif operation_type == "set_display_expansion":
                    value = {
                        "target_content_network": bool(
                            row.campaign.network_settings.target_content_network
                        )
                    }
                else:
                    raise ChangesetError(
                        "Unsupported operation reached the mutation gateway"
                    )
                values[action["id"]] = value
        return values

    @staticmethod
    def _build_operations(client, actions: Sequence[Mapping[str, Any]]):
        operations = []
        for action in actions:
            operation = client.get_type("MutateOperation")
            operation_type = action["operation_type"]
            proposed = action["proposed_value"]
            if operation_type == "set_campaign_budget_amount":
                update = operation.campaign_budget_operation.update
                update.resource_name = action["resource_name"]
                update.amount_micros = proposed["amount_micros"]
                operation.campaign_budget_operation.update_mask.paths.append(
                    "amount_micros"
                )
            else:
                update = operation.campaign_operation.update
                update.resource_name = action["resource_name"]
                if operation_type == "pause_campaign":
                    update.status = client.enums.CampaignStatusEnum.PAUSED
                    path = "status"
                elif operation_type == "set_search_partners":
                    update.network_settings.target_partner_search_network = proposed[
                        "target_partner_search_network"
                    ]
                    path = "network_settings.target_partner_search_network"
                elif operation_type == "set_display_expansion":
                    update.network_settings.target_content_network = proposed[
                        "target_content_network"
                    ]
                    path = "network_settings.target_content_network"
                else:
                    raise ChangesetError(
                        "Unsupported operation reached the mutation gateway"
                    )
                operation.campaign_operation.update_mask.paths.append(path)
            operations.append(operation)
        return operations

    def mutate(
        self,
        customer_id: str,
        login_customer_id: str | None,
        actions: Sequence[Mapping[str, Any]],
        *,
        validate_only: bool,
    ) -> dict[str, Any]:
        client, service = self._services(login_customer_id)
        utils.enforce_customer_access_root(service, customer_id)
        request = client.get_type("MutateGoogleAdsRequest")
        request.customer_id = customer_id
        request.partial_failure = False
        request.validate_only = validate_only
        request.mutate_operations.extend(self._build_operations(client, actions))
        response = service.mutate(request=request)
        resource_names = []
        for result in getattr(response, "mutate_operation_responses", []):
            field_name = result._pb.WhichOneof("response")
            if field_name:
                resource_name = getattr(
                    getattr(result, field_name), "resource_name", ""
                )
                if resource_name:
                    resource_names.append(resource_name)
        return {
            "validate_only": validate_only,
            "partial_failure": False,
            "operation_count": len(actions),
            "resource_names": resource_names,
        }

    def recent_change_events(
        self,
        customer_id: str,
        login_customer_id: str | None,
        resource_names: Iterable[str],
    ) -> list[dict[str, Any]]:
        names = set(resource_names)
        if not names:
            return []
        _, service = self._services(login_customer_id)
        utils.enforce_customer_access_root(service, customer_id)
        query = (
            "SELECT change_event.change_date_time, "
            "change_event.change_resource_name, "
            "change_event.change_resource_type, "
            "change_event.resource_change_operation, "
            "change_event.changed_fields, change_event.client_type "
            "FROM change_event "
            "WHERE change_event.change_date_time DURING LAST_14_DAYS "
            "ORDER BY change_event.change_date_time DESC LIMIT 100"
        )
        events = []
        for row in service.search(customer_id=customer_id, query=query):
            event = row.change_event
            if event.change_resource_name not in names:
                continue
            events.append(
                {
                    "change_date_time": event.change_date_time,
                    "change_resource_name": event.change_resource_name,
                    "change_resource_type": _enum_name(event.change_resource_type),
                    "operation": _enum_name(event.resource_change_operation),
                    "changed_fields": list(event.changed_fields.paths),
                    "client_type": _enum_name(event.client_type),
                }
            )
        return events

    def budget_policy_context(
        self,
        customer_id: str,
        login_customer_id: str | None,
        actions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return live account facts required by the budget safety policy."""

        budget_actions = [
            action
            for action in actions
            if action["operation_type"] == "set_campaign_budget_amount"
        ]
        if not budget_actions:
            return {
                "highest_enabled_budget_micros": None,
                "recent_budget_changes": {},
            }

        _, service = self._services(login_customer_id)
        utils.enforce_customer_access_root(service, customer_id)
        query = (
            "SELECT campaign_budget.amount_micros FROM campaign_budget "
            "WHERE campaign_budget.status = 'ENABLED' "
            "ORDER BY campaign_budget.amount_micros DESC LIMIT 1"
        )
        rows = list(service.search(customer_id=customer_id, query=query))
        if not rows:
            raise ChangesetError(
                "Unable to find an enabled budget for the account safety ceiling"
            )
        highest = int(rows[0].campaign_budget.amount_micros)

        events = self.recent_change_events(
            customer_id,
            login_customer_id,
            [action["resource_name"] for action in budget_actions],
        )
        recent: dict[str, str] = {}
        for event in events:
            fields = set(event.get("changed_fields") or [])
            if (
                "amount_micros" not in fields
                and "campaign_budget.amount_micros" not in fields
            ):
                continue
            resource_name = str(event.get("change_resource_name", ""))
            if resource_name and resource_name not in recent:
                recent[resource_name] = str(event.get("change_date_time", ""))
        return {
            "highest_enabled_budget_micros": highest,
            "recent_budget_changes": recent,
        }


class ChangesetService:
    def __init__(
        self,
        *,
        store: AsyncChangesetStore | None = None,
        replay_guard: ReplayGuard | None = None,
        gateway: GoogleAdsMutationGateway | None = None,
        owner_resolver: Callable[[], str] = _identity_owner_id,
        now_fn: Callable[[], datetime] = _now,
    ) -> None:
        self._store = store or get_changeset_store()
        self._replay_guard = replay_guard or get_replay_guard()
        self._gateway = gateway or GoogleAdsMutationGateway()
        self._owner_resolver = owner_resolver
        self._now_fn = now_fn

    def capabilities(self) -> dict[str, Any]:
        allowed_customers = _live_customer_allowlist()
        return {
            "changesets_enabled": _enabled("GMA_ENABLE_CHANGESETS", True),
            "validation_enabled": _enabled("GMA_ENABLE_CHANGESET_VALIDATION"),
            "live_mutations_enabled": _enabled("GMA_ENABLE_MUTATIONS")
            and _enabled("GMA_ALLOW_LIVE_MUTATIONS"),
            "developer_token_ad_management_confirmed": _enabled(
                "GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED"
            ),
            "live_customer_count": len(allowed_customers),
            "allowlisted_operations": sorted(ALLOWLISTED_OPERATIONS),
            "scheduled_apply_allowed": False,
            "atomic_apply": True,
        }

    async def create(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not _enabled("GMA_ENABLE_CHANGESETS", True):
            raise ChangesetError("Changeset creation is disabled")
        now = self._now_fn()
        plan = _normalize_plan(raw, self._owner_resolver(), now)
        review_token = secrets.token_urlsafe(32)
        plan["review_token_hash"] = hashlib.sha256(review_token.encode()).hexdigest()
        await self._store.put(plan["id"], plan, ttl=PLAN_TTL_SECONDS)
        public = _public_plan(plan)
        base_url = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080")
        public["review_url"] = (
            f"{base_url.rstrip('/')}/changesets/{plan['id']}/review?token={review_token}"
        )
        return public

    async def _owned(self, changeset_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"gma_[0-9a-f]{32}", changeset_id):
            raise ChangesetError("Invalid changeset ID")
        plan = await self._store.get(changeset_id)
        if not plan or plan.get("owner_id") != self._owner_resolver():
            raise ChangesetError("Changeset not found")
        if _parse_datetime(plan["expires_at"]) <= self._now_fn():
            raise ChangesetError("Changeset has expired; rerun the analysis")
        return plan

    async def get(self, changeset_id: str) -> dict[str, Any]:
        return _public_plan(await self._owned(changeset_id))

    async def _reviewed(self, changeset_id: str, review_token: str) -> dict[str, Any]:
        if not re.fullmatch(r"gma_[0-9a-f]{32}", changeset_id):
            raise ChangesetError("Invalid changeset ID")
        plan = await self._store.get(changeset_id)
        supplied_hash = hashlib.sha256(review_token.encode()).hexdigest()
        if not plan or not secrets.compare_digest(
            supplied_hash, str(plan.get("review_token_hash", ""))
        ):
            raise ChangesetError("Change Plan link is invalid")
        if _parse_datetime(plan["expires_at"]) <= self._now_fn():
            raise ChangesetError("Change Plan link has expired")
        return plan

    async def get_for_review(
        self, changeset_id: str, review_token: str
    ) -> dict[str, Any]:
        return _public_plan(await self._reviewed(changeset_id, review_token))

    async def select_for_review(
        self,
        changeset_id: str,
        review_token: str,
        selected_action_ids: Sequence[str],
    ) -> dict[str, Any]:
        plan = await self._reviewed(changeset_id, review_token)
        if plan["run_mode"] != "interactive":
            raise ChangesetError("Scheduled analysis plans are review-only")
        self._selected_actions(plan, selected_action_ids)
        for action in plan["actions"]:
            action["validation_status"] = "not_validated"
            action["approval_status"] = "not_approved"
            action["implementation_status"] = "not_applied"
        plan["selected_action_ids"] = list(selected_action_ids)
        plan["review_selection"] = {
            "status": "selected_for_validation",
            "selected_at": _iso(self._now_fn()),
            "selected_action_ids": list(selected_action_ids),
        }
        plan["operation_hash"] = None
        plan["status"] = "draft"
        plan["validation"] = {"status": "not_validated"}
        plan["approval"] = {"status": "not_approved"}
        plan["application"] = {"status": "not_applied"}
        await self._save(plan)
        return _public_plan(plan)

    async def _save(self, plan: dict[str, Any]) -> None:
        plan["version"] = int(plan.get("version", 0)) + 1
        plan["updated_at"] = _iso(self._now_fn())
        remaining = max(
            1,
            int((_parse_datetime(plan["expires_at"]) - self._now_fn()).total_seconds()),
        )
        await self._store.put(plan["id"], plan, ttl=remaining)

    def _enforce_budget_policy(
        self, plan: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]
    ) -> None:
        if not any(
            action["operation_type"] == "set_campaign_budget_amount"
            for action in actions
        ):
            return
        context = self._gateway.budget_policy_context(
            plan["customer_id"], plan.get("login_customer_id"), actions
        )
        _assert_budget_policy(actions, context, self._now_fn())

    async def _claim_budget_cooldowns(
        self, plan: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]
    ) -> None:
        """Atomically reserve each budget resource before a live mutation.

        Change Event reporting can lag. These durable claims close that window so
        two approved plans cannot change the same budget within 24 hours.
        """

        resources = sorted(
            {
                action["resource_name"]
                for action in actions
                if action["operation_type"] == "set_campaign_budget_amount"
            }
        )
        for resource_name in resources:
            cooldown_key = hashlib.sha256(
                (
                    "gma-budget-cooldown:" f"{plan['customer_id']}:{resource_name}"
                ).encode()
            ).hexdigest()
            claimed = await self._replay_guard.claim(
                cooldown_key,
                ttl_seconds=BUDGET_CHANGE_COOLDOWN_SECONDS,
            )
            if not claimed:
                raise ChangesetError(
                    "This campaign budget was already changed by GMA within the "
                    "last 24 hours; wait before applying another budget change"
                )

    @staticmethod
    def _selected_actions(plan: Mapping[str, Any], selected_ids: Sequence[str]):
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise ChangesetError("Select one or more unique action IDs")
        by_id = {action["id"]: action for action in plan["actions"]}
        selected = []
        for action_id in selected_ids:
            action = by_id.get(action_id)
            if not action:
                raise ChangesetError(f"Unknown action ID: {action_id}")
            if action["applyability"] != "applyable":
                raise ChangesetError(
                    f"{action_id} is advisory or blocked, not applyable"
                )
            selected.append(action)
        return selected

    async def validate(
        self, changeset_id: str, selected_action_ids: Sequence[str]
    ) -> dict[str, Any]:
        if not _enabled("GMA_ENABLE_CHANGESET_VALIDATION"):
            raise ChangesetError("Google Ads mutation validation is disabled")
        plan = await self._owned(changeset_id)
        if plan["run_mode"] != "interactive":
            raise ChangesetError(
                "Scheduled analysis can never validate changes for apply"
            )
        actions = self._selected_actions(plan, selected_action_ids)
        canonical_selected_ids = [
            action["id"]
            for action in plan["actions"]
            if action["id"] in set(selected_action_ids)
        ]
        review_selection = plan.get("review_selection") or {}
        if review_selection.get("status") == "selected_for_validation" and set(
            canonical_selected_ids
        ) != set(review_selection.get("selected_action_ids") or []):
            raise ChangesetError(
                "Selected action IDs do not match the user's Change Plan selection"
            )
        current = self._gateway.read_current_values(
            plan["customer_id"], plan.get("login_customer_id"), actions
        )
        drift = [
            action["id"]
            for action in actions
            if current.get(action["id"]) != action["current_value"]
        ]
        if drift:
            plan["status"] = "validation_failed"
            plan["validation"] = {
                "status": "drifted",
                "checked_at": _iso(self._now_fn()),
                "drifted_action_ids": drift,
            }
            await self._save(plan)
            raise ChangesetError(
                "Current Google Ads values changed for: " + ", ".join(drift)
            )
        self._enforce_budget_policy(plan, actions)
        result = self._gateway.mutate(
            plan["customer_id"],
            plan.get("login_customer_id"),
            actions,
            validate_only=True,
        )
        operation_hash = _operation_hash(plan, canonical_selected_ids)
        selected = set(canonical_selected_ids)
        for action in plan["actions"]:
            action["validation_status"] = (
                "passed" if action["id"] in selected else "not_selected"
            )
            action["approval_status"] = "not_approved"
        plan["selected_action_ids"] = canonical_selected_ids
        plan["operation_hash"] = operation_hash
        plan["status"] = "validated"
        plan["validation"] = {
            "status": "passed",
            "validated_at": _iso(self._now_fn()),
            "operation_hash": operation_hash,
            "result": result,
        }
        plan["approval"] = {"status": "not_approved"}
        await self._save(plan)
        return {
            "changeset_id": plan["id"],
            "status": "validated",
            "selected_action_ids": canonical_selected_ids,
            "operation_hash": operation_hash,
            "next_step": (
                "Review the exact diff, then explicitly approve it through the "
                "Claude or Codex tool confirmation."
            ),
            "expires_at": plan["expires_at"],
        }

    async def approve(self, changeset_id: str, operation_hash: str) -> dict[str, Any]:
        """Record host-gated human approval without exposing a reusable secret."""

        plan = await self._owned(changeset_id)
        if plan["run_mode"] != "interactive":
            raise ChangesetError("Scheduled analysis can never approve changes")
        if (
            plan.get("status") != "validated"
            or plan.get("operation_hash") != operation_hash
        ):
            raise ChangesetError(
                "Changeset is not validated for this exact operation hash"
            )
        selected = set(plan["selected_action_ids"])
        for action in plan["actions"]:
            if action["id"] in selected:
                action["approval_status"] = "approved"
        plan["status"] = "approved"
        plan["approval"] = {
            "status": "approved",
            "approved_at": _iso(self._now_fn()),
            "operation_hash": operation_hash,
            "approval_surface": "host_tool_confirmation",
            "consumed": False,
        }
        await self._save(plan)
        return {
            "changeset_id": plan["id"],
            "status": "approved",
            "selected_action_ids": plan["selected_action_ids"],
            "operation_hash": operation_hash,
            "next_step": (
                "Use the separate apply tool. The host must ask for human permission again."
            ),
        }

    async def apply(self, changeset_id: str) -> dict[str, Any]:
        """Apply a previously validated and host-approved changeset once."""

        plan = await self._owned(changeset_id)
        _assert_live_apply_enabled(plan["customer_id"])
        if plan["run_mode"] != "interactive":
            raise ChangesetError(
                "Scheduled analysis can never apply Google Ads changes"
            )
        approval = plan.get("approval") or {}
        if (
            plan.get("status") != "approved"
            or approval.get("status") != "approved"
            or approval.get("consumed")
            or approval.get("operation_hash") != plan.get("operation_hash")
        ):
            raise ChangesetError("Changeset is not approved or was already consumed")
        selected_ids = plan["selected_action_ids"]
        if _operation_hash(plan, selected_ids) != plan.get("operation_hash"):
            raise ChangesetError("Changeset contents no longer match the approved hash")
        actions = self._selected_actions(plan, selected_ids)
        current = self._gateway.read_current_values(
            plan["customer_id"], plan.get("login_customer_id"), actions
        )
        drift = [
            action["id"]
            for action in actions
            if current.get(action["id"]) != action["current_value"]
        ]
        if drift:
            plan["status"] = "drifted"
            plan["application"] = {
                "status": "blocked_by_drift",
                "checked_at": _iso(self._now_fn()),
                "drifted_action_ids": drift,
            }
            await self._save(plan)
            raise ChangesetError(
                "Current Google Ads values changed after validation for: "
                + ", ".join(drift)
            )
        self._enforce_budget_policy(plan, actions)
        await self._claim_budget_cooldowns(plan, actions)
        replay_key = hashlib.sha256(
            f"gma-apply:{plan['id']}:{plan['operation_hash']}".encode()
        ).hexdigest()
        claimed = await self._replay_guard.claim(
            replay_key,
            ttl_seconds=max(
                1,
                int(
                    (
                        _parse_datetime(plan["expires_at"]) - self._now_fn()
                    ).total_seconds()
                ),
            ),
        )
        if not claimed:
            raise ChangesetError("Approved changeset replay was rejected")

        approval["consumed"] = True
        approval["consumed_at"] = _iso(self._now_fn())
        plan["status"] = "applying"
        plan["application"] = {"status": "applying", "started_at": _iso(self._now_fn())}
        await self._save(plan)
        try:
            result = self._gateway.mutate(
                plan["customer_id"],
                plan.get("login_customer_id"),
                actions,
                validate_only=False,
            )
        except Exception as error:
            plan["status"] = "apply_failed"
            plan["application"] = {
                "status": "failed",
                "failed_at": _iso(self._now_fn()),
                "message": str(error)[:500],
                "approval_consumed": True,
            }
            await self._save(plan)
            raise
        for action in plan["actions"]:
            if action["id"] in set(selected_ids):
                action["implementation_status"] = "applied_unverified"
        plan["status"] = "applied_unverified"
        plan["application"] = {
            "status": "applied",
            "applied_at": _iso(self._now_fn()),
            "atomic": True,
            "result": result,
        }
        await self._save(plan)
        return {
            "changeset_id": plan["id"],
            "status": "applied_unverified",
            "selected_action_ids": selected_ids,
            "result": result,
            "next_step": "Call changesets_verify to read the affected resources back.",
        }

    async def verify(self, changeset_id: str) -> dict[str, Any]:
        plan = await self._owned(changeset_id)
        if plan.get("status") not in {
            "applied_unverified",
            "verified",
            "verification_failed",
        }:
            raise ChangesetError("Changeset has not been applied")
        actions = self._selected_actions(plan, plan["selected_action_ids"])
        current = self._gateway.read_current_values(
            plan["customer_id"], plan.get("login_customer_id"), actions
        )
        mismatches = [
            action["id"]
            for action in actions
            if current.get(action["id"]) != action["proposed_value"]
        ]
        events = self._gateway.recent_change_events(
            plan["customer_id"],
            plan.get("login_customer_id"),
            [action["resource_name"] for action in actions],
        )
        verified = not mismatches
        for action in plan["actions"]:
            if action["id"] in set(plan["selected_action_ids"]):
                action["implementation_status"] = "verified" if verified else "mismatch"
        plan["status"] = "verified" if verified else "verification_failed"
        plan["verification"] = {
            "status": "verified" if verified else "mismatch",
            "verified_at": _iso(self._now_fn()),
            "readback": current,
            "mismatched_action_ids": mismatches,
            "change_events": events,
        }
        await self._save(plan)
        return {
            "changeset_id": plan["id"],
            "status": plan["status"],
            "readback": current,
            "mismatched_action_ids": mismatches,
            "change_events": events,
        }


def _live_customer_allowlist() -> set[str]:
    raw = os.environ.get("GMA_LIVE_MUTATION_CUSTOMER_IDS", "")
    values = [value.strip() for value in raw.replace(";", ",").split(",")]
    return {
        _normalize_customer_id(value, "GMA_LIVE_MUTATION_CUSTOMER_IDS")
        for value in values
        if value
    }


def _assert_live_apply_enabled(customer_id: str) -> None:
    if not _enabled("GMA_ENABLE_MUTATIONS"):
        raise ChangesetError("Google Ads mutations are globally disabled")
    if not _enabled("GMA_ALLOW_LIVE_MUTATIONS"):
        raise ChangesetError("Live Google Ads mutations are disabled")
    if not _enabled("GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED"):
        raise ChangesetError(
            "Developer-token Ad creation / management permission is not confirmed"
        )
    if customer_id not in _live_customer_allowlist():
        raise ChangesetError("This customer is not allowlisted for live mutations")


_service: ChangesetService | None = None


def get_changeset_service() -> ChangesetService:
    global _service
    if _service is None:
        _service = ChangesetService()
    return _service


def reset_changeset_service_for_tests() -> None:
    global _service
    _service = None
