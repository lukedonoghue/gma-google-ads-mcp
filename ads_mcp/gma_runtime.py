"""Thin typed runtime for the GMA specialist products."""

from __future__ import annotations

import hashlib
import html
import json
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from ads_mcp import utils
from ads_mcp.changeset_store import AsyncChangesetStore, get_changeset_store
from ads_mcp.changesets import current_identity_owner_id, get_changeset_service
from ads_mcp.goal_report import build_goal_report as build_goal_report_result
from ads_mcp.goal_targets import build_goal_context
from ads_mcp.industry_benchmarks import list_benchmark_profiles
from ads_mcp.skill_runs.budget_service import BudgetReallocatorRunService
from ads_mcp.skill_runs.common import (
    resolve_analysis_window,
    trailing_complete_days,
)
from ads_mcp.skill_runs.red_flag_service import RedFlagRadarRunService

RUNTIME_VERSION = "1.0.0-alpha.10"
METHODOLOGY_VERSIONS = {
    "red_flag_radar": "gma-red-flag-v1.0.2",
    "budget_reallocator": "gma-budget-v1.0.0",
}
EXPECTED_PLUGIN_VERSION = "0.6.2"
SCOPE_TTL_SECONDS = 24 * 60 * 60
RUN_TTL_SECONDS = 90 * 24 * 60 * 60
WORKSPACE_TTL_SECONDS = 180 * 24 * 60 * 60
MODULES = {
    "instant_account_audit": (1, "Instant Account Audit"),
    "red_flag_radar": (2, "Red-Flag Radar"),
    "wasted_spend_finder": (3, "Wasted-Spend Finder"),
    "winning_keyword_promoter": (4, "Winning-Keyword Promoter"),
    "keyword_gap_finder": (5, "Keyword Gap Finder"),
    "quality_score_booster": (6, "Quality Score Booster"),
    "structure_fixer": (7, "Structure Fixer"),
    "bid_strategy_check": (8, "Bid Strategy Check"),
    "competitor_spy": (9, "Competitor Spy"),
    "ad_copy_analyzer": (10, "Ad-Copy Analyzer"),
    "landing_page_cro_audit": (11, "Landing-Page CRO Audit"),
    "budget_reallocator": (12, "Budget Reallocator"),
    "weekly_digest": (13, "Weekly Digest"),
}
RESULT_STATUSES = {"complete", "partial", "blocked", "failed"}
RECOMMENDATION_TYPES = {
    "applyable",
    "advisory",
    "task",
    "monitor",
    "hold",
    "blocked",
}


class GmaRuntimeError(ValueError):
    """Typed, safe failure from the GMA runtime boundary."""


def _store_or_default(store: AsyncChangesetStore | None) -> AsyncChangesetStore:
    return store or get_changeset_store()


def _owner_or_default(owner_resolver=None) -> str:
    return (owner_resolver or current_identity_owner_id)()


def _public_state(value: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(value))
    result.pop("owner_id", None)
    return result


async def save_prepared_scope(
    prepared: Mapping[str, Any],
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Persist the exact server-prepared scope passed to a later skill run."""

    scope_id = str(prepared.get("scope_id") or "")
    if not re.fullmatch(r"scope_[a-f0-9]{24}", scope_id):
        raise GmaRuntimeError("Prepared scope has an invalid ID")
    payload = deepcopy(dict(prepared))
    payload["owner_id"] = _owner_or_default(owner_resolver)
    await _store_or_default(store).put(scope_id, payload, ttl=SCOPE_TTL_SECONDS)
    return _public_state(payload)


async def get_prepared_scope(
    scope_id: str,
    confirmed_scope_hash: str,
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Load an owner-bound scope and verify the user's confirmed scope hash."""

    if not re.fullmatch(r"scope_[a-f0-9]{24}", scope_id):
        raise GmaRuntimeError("Scope ID is invalid")
    value = await _store_or_default(store).get(scope_id)
    if not value or value.get("owner_id") != _owner_or_default(owner_resolver):
        raise GmaRuntimeError("Prepared scope was not found or has expired")
    if not re.fullmatch(r"[a-f0-9]{64}", confirmed_scope_hash or ""):
        raise GmaRuntimeError("A confirmed scope hash is required")
    if value.get("scope_hash") != confirmed_scope_hash:
        raise GmaRuntimeError(
            "The confirmed scope does not match the prepared scope"
        )
    return deepcopy(dict(value["scope"]))


async def _save_run_result(
    result: Mapping[str, Any],
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> None:
    payload = deepcopy(dict(result))
    payload["owner_id"] = _owner_or_default(owner_resolver)
    await _store_or_default(store).put(
        str(result["run_id"]), payload, ttl=RUN_TTL_SECONDS
    )


async def get_run(
    run_id: str,
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Return an owner-bound canonical run receipt."""

    if not re.fullmatch(r"run_[a-f0-9]{32}", run_id):
        raise GmaRuntimeError("Run ID is invalid")
    value = await _store_or_default(store).get(run_id)
    if not value or value.get("owner_id") != _owner_or_default(owner_resolver):
        raise GmaRuntimeError("GMA run was not found or has expired")
    result = _public_state(value)
    validate_run_result(result)
    return result


def _workspace_id(owner_id: str, scope_id: str) -> str:
    digest = hashlib.sha256(f"{owner_id}:{scope_id}".encode()).hexdigest()
    return f"ws_{digest[:32]}"


def _workspace_scope_signature(scope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "customer_id": str(scope["customer_id"]),
        "login_customer_id": scope.get("login_customer_id"),
        "analysis_start": str(scope["analysis_start"]),
        "analysis_end": str(scope["analysis_end"]),
        "business_mode": str(scope["business_mode"]),
        "campaign_ids": sorted(str(item["id"]) for item in scope["campaigns"]),
    }


async def _hydrate_workspace(
    payload: Mapping[str, Any],
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    run_summaries: list[dict[str, Any]] = []
    action_index: dict[str, dict[str, Any]] = {}
    for run_id in payload.get("run_ids") or []:
        try:
            result = await get_run(
                str(run_id),
                store=store,
                owner_resolver=owner_resolver,
            )
        except GmaRuntimeError:
            run_summaries.append(
                {"run_id": str(run_id), "status": "expired"}
            )
            continue
        run_summaries.append(
            {
                "run_id": result["run_id"],
                "module": result["module"],
                "status": result["status"],
                "assessment": result["assessment"],
                "data_receipt": result["data_receipt"],
                "coverage": result["coverage"],
                "recommendation_count": len(result["recommendations"]),
                "recovery_action_count": len(result["recovery_actions"]),
                "core_signature": result["core_signature"],
            }
        )
        for recommendation in result["recommendations"]:
            action_index[recommendation["id"]] = {
                "id": recommendation["id"],
                "kind": (
                    "google_ads_change"
                    if recommendation["applyability"] == "applyable"
                    else "advisory"
                ),
                "title": recommendation["entity"],
                "reason": recommendation["reason"],
                "status": recommendation["applyability"],
                "source_run_id": result["run_id"],
                "source_module": result["module"],
                "change_plan_id": result["change_plan"]["id"],
            }
        for recovery in result["recovery_actions"]:
            action_index[recovery["id"]] = {
                "id": recovery["id"],
                "kind": "recovery_task",
                "title": recovery["title"],
                "reason": recovery["reason"],
                "status": recovery["status"],
                "owner": recovery["owner"],
                "completion_signal": recovery["completion_signal"],
                "follow_up": recovery["follow_up"],
                "source_run_id": result["run_id"],
                "source_module": result["module"],
            }
    selected_ids = list(payload.get("selected_action_ids") or [])
    action_list = []
    for action_id in selected_ids:
        item = action_index.get(str(action_id))
        if item:
            action_list.append(deepcopy(item))
    return {
        "contract_version": "gma-workspace/1.0",
        "workspace_id": payload["workspace_id"],
        "scope_id": payload["scope_id"],
        "scope_hash": payload["scope_hash"],
        "scope": deepcopy(payload["scope"]),
        "created_at": payload["created_at"],
        "updated_at": payload["updated_at"],
        "runs": run_summaries,
        "available_action_count": len(action_index),
        "selected_action_ids": selected_ids,
        "action_list": action_list,
    }


async def create_workspace(
    *,
    scope_id: str,
    confirmed_scope_hash: str,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Create or reopen the owner-bound dashboard workspace for one scope."""

    selected_store = _store_or_default(store)
    owner_id = _owner_or_default(owner_resolver)
    scope = await get_prepared_scope(
        scope_id,
        confirmed_scope_hash,
        store=selected_store,
        owner_resolver=owner_resolver,
    )
    workspace_id = _workspace_id(owner_id, scope_id)
    existing = await selected_store.get(workspace_id)
    if existing:
        if (
            existing.get("owner_id") != owner_id
            or existing.get("scope_hash") != confirmed_scope_hash
        ):
            raise GmaRuntimeError(
                "The existing workspace does not match this confirmed scope"
            )
        return await _hydrate_workspace(
            existing,
            store=selected_store,
            owner_resolver=owner_resolver,
        )
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "scope_id": scope_id,
        "scope_hash": confirmed_scope_hash,
        "scope": deepcopy(scope),
        "scope_signature": _workspace_scope_signature(scope),
        "run_ids": [],
        "selected_action_ids": [],
        "created_at": now,
        "updated_at": now,
    }
    await selected_store.put(
        workspace_id,
        payload,
        ttl=WORKSPACE_TTL_SECONDS,
    )
    return await _hydrate_workspace(
        payload,
        store=selected_store,
        owner_resolver=owner_resolver,
    )


async def get_workspace(
    workspace_id: str,
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Return the durable run list and shared Action list for one workspace."""

    if not re.fullmatch(r"ws_[a-f0-9]{32}", workspace_id):
        raise GmaRuntimeError("Workspace ID is invalid")
    selected_store = _store_or_default(store)
    owner_id = _owner_or_default(owner_resolver)
    payload = await selected_store.get(workspace_id)
    if not payload or payload.get("owner_id") != owner_id:
        raise GmaRuntimeError("GMA workspace was not found or has expired")
    return await _hydrate_workspace(
        payload,
        store=selected_store,
        owner_resolver=owner_resolver,
    )


async def attach_run_to_workspace(
    workspace_id: str,
    run_id: str,
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Attach one canonical run to its matching workspace, idempotently."""

    selected_store = _store_or_default(store)
    owner_id = _owner_or_default(owner_resolver)
    payload = await selected_store.get(workspace_id)
    if not payload or payload.get("owner_id") != owner_id:
        raise GmaRuntimeError("GMA workspace was not found or has expired")
    result = await get_run(
        run_id,
        store=selected_store,
        owner_resolver=owner_resolver,
    )
    if _workspace_scope_signature(result["scope"]) != payload.get(
        "scope_signature"
    ):
        raise GmaRuntimeError(
            "This run belongs to a different account, date, campaign, or business scope"
        )
    run_ids = list(payload.get("run_ids") or [])
    if run_id not in run_ids:
        run_ids.append(run_id)
    payload = deepcopy(dict(payload))
    payload["run_ids"] = run_ids
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    await selected_store.put(
        workspace_id,
        payload,
        ttl=WORKSPACE_TTL_SECONDS,
    )
    return await _hydrate_workspace(
        payload,
        store=selected_store,
        owner_resolver=owner_resolver,
    )


async def update_workspace_actions(
    workspace_id: str,
    selected_action_ids: Sequence[str],
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Save exact dashboard selections without validating or changing Google Ads."""

    selected_store = _store_or_default(store)
    owner_id = _owner_or_default(owner_resolver)
    payload = await selected_store.get(workspace_id)
    if not payload or payload.get("owner_id") != owner_id:
        raise GmaRuntimeError("GMA workspace was not found or has expired")
    available_ids: set[str] = set()
    for run_id in payload.get("run_ids") or []:
        try:
            result = await get_run(
                str(run_id),
                store=selected_store,
                owner_resolver=owner_resolver,
            )
        except GmaRuntimeError:
            continue
        available_ids.update(item["id"] for item in result["recommendations"])
        available_ids.update(item["id"] for item in result["recovery_actions"])
    normalized_ids = list(dict.fromkeys(str(value) for value in selected_action_ids))
    unknown = sorted(set(normalized_ids).difference(available_ids))
    if unknown:
        raise GmaRuntimeError(
            "Action IDs are not available in this workspace: " + ", ".join(unknown)
        )
    payload = deepcopy(dict(payload))
    payload["selected_action_ids"] = normalized_ids
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    await selected_store.put(
        workspace_id,
        payload,
        ttl=WORKSPACE_TTL_SECONDS,
    )
    return await _hydrate_workspace(
        payload,
        store=selected_store,
        owner_resolver=owner_resolver,
    )


def _enum(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _core_signature(result: Mapping[str, Any]) -> str:
    core = {
        key: value for key, value in result.items() if key != "core_signature"
    }
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text(value: Any) -> str:
    return html.escape(str(value or ""), quote=False).replace("\n", " ")


def _money_from_value(value: Any, currency: str) -> str:
    if not isinstance(value, Mapping) or "amount_micros" not in value:
        return _text(value)
    return f"{currency} {int(value['amount_micros']) / 1_000_000:,.2f}/day"


def render_run_result(result: Mapping[str, Any]) -> str:
    """Render the canonical result as plain-English Markdown without recalculation."""

    module = result["module"]
    scope = result["scope"]
    receipt = result["data_receipt"]
    assessment = result["assessment"]
    currency = str(scope.get("currency") or "")
    lines = [
        f"# Skill {module['number']} — {_text(module['name'])}",
        "",
        f"**Account:** {_text(scope['account_name'])}  ",
        (
            f"**Campaigns:** {len(scope['campaigns'])}  "
            f"**Period:** {_text(scope['analysis_start'])} to {_text(scope['analysis_end'])}  "
        ),
        f"**Mode:** {_text(scope['business_mode']).replace('_', ' ').title()}",
        "",
        "## Live data receipt",
        "",
        (
            f"Data pulled from **{_text(receipt.get('source'))}** at "
            f"{_text(receipt.get('retrieved_at'))}; data through "
            f"{_text(receipt.get('data_through'))}."
        ),
        (
            "**Coverage:** "
            + (
                f"{result['coverage']['spend_coverage_percent']:.1f}% of scoped spend"
                if result["coverage"].get("spend_coverage_percent") is not None
                else "Spend coverage unavailable"
            )
            + f" · {result['coverage']['campaigns_analyzed']} of "
            f"{result['coverage']['campaigns_requested']} campaigns analyzed"
        ),
        "",
        "## Assessment",
        "",
        f"**{_text(assessment.get('state')).replace('_', ' ').title()}:** "
        f"{_text(assessment.get('conclusion'))}",
        "",
        "## What the skill checked",
        "",
        "| Campaign | Check | Result | What to do next |",
        "|---|---|---|---|",
    ]
    for check in result["checks"]:
        if module["id"] == "red_flag_radar":
            lines.append(
                f"| {_text(check.get('campaign_name'))} | "
                f"{_text(check.get('criterion'))} | "
                f"{_text(check.get('status')).replace('_', ' ').title()}: "
                f"{_text(check.get('decision'))} | "
                f"{_text(check.get('next_step'))} |"
            )
        else:
            holds = check.get("holds") or []
            routes = check.get("routes") or []
            if holds:
                decision = "Hold"
                why = "; ".join(_text(item) for item in holds)
            elif check.get("recipient_eligible"):
                decision = "Eligible to receive budget"
                why = _text(check.get("constraint"))
            elif check.get("donor_eligible"):
                decision = "Eligible budget donor"
                why = _text(check.get("constraint"))
            else:
                decision = "No budget move"
                why = _text(check.get("constraint"))
            if routes:
                why += "; review with " + ", ".join(
                    _text(item) for item in routes
                )
            lines.append(
                f"| {_text(check.get('campaign_name'))} | "
                f"Budget eligibility | {decision} | {why} |"
            )

    recovery_actions = result.get("recovery_actions") or []
    lines.extend(["", "## Recovery plan", ""])
    if not recovery_actions:
        lines.append(
            "No prerequisite or recovery task is required for this run."
        )
    for action in recovery_actions:
        affected = ", ".join(
            _text(item.get("campaign_name"))
            for item in action.get("applies_to") or []
        )
        lines.extend(
            [
                f"### {_text(action['id'])} — {_text(action['title'])}",
                "",
                (
                    f"**Status:** {_text(action.get('status')).replace('_', ' ').title()} "
                    f"· **Owner:** {_text(action.get('owner')).replace('_', ' ').title()}"
                ),
                "",
                f"**Why:** {_text(action.get('reason'))}",
                "",
            ]
        )
        if affected:
            lines.extend([f"**Applies to:** {affected}", ""])
        lines.append("**What to do:**")
        lines.extend(
            f"{index}. {_text(step)}"
            for index, step in enumerate(action.get("steps") or [], start=1)
        )
        lines.extend(
            [
                "",
                f"**Done when:** {_text(action.get('completion_signal'))}",
                "",
            ]
        )

    lines.extend(["## Recommended actions", ""])
    recommendations = result["recommendations"]
    if not recommendations:
        lines.append(
            "No action crossed this skill's evidence threshold. "
            "Use the Recovery plan above if a required check was unavailable."
        )
    for recommendation in recommendations:
        lines.extend(
            [
                (
                    f"### {_text(recommendation['id'])} — "
                    f"{_text(recommendation.get('entity'))}"
                ),
                "",
                f"**Status:** {_text(recommendation.get('applyability')).title()}",
                "",
                f"**Why:** {_text(recommendation.get('reason'))}",
                "",
                f"**Evidence:** {_text(recommendation.get('evidence_summary'))}",
                "",
                f"**What to do:** {_text(recommendation.get('details'))}",
                "",
                f"**Expected impact:** {_text(recommendation.get('expected_impact'))}",
                "",
            ]
        )
        if recommendation.get("applyability") == "applyable":
            lines[-1:-1] = [
                (
                    f"**Exact change:** "
                    f"{_money_from_value(recommendation.get('current_value'), currency)} "
                    f"→ {_money_from_value(recommendation.get('proposed_value'), currency)}"
                ),
                "",
            ]

    gaps = result["coverage"].get("gaps") or []
    lines.extend(["## What was not checked", ""])
    if gaps:
        lines.extend(f"- {_text(gap)}" for gap in gaps)
    else:
        lines.append(
            "No API coverage gaps were recorded for this selected scope."
        )
    change_plan = result.get("change_plan") or {}
    lines.extend(["", "## Next step", ""])
    applyable_ids = change_plan.get("applyable_action_ids") or []
    if applyable_ids:
        lines.append(
            "Google Ads has **not** been changed. Select exact change IDs "
            "to validate, or recovery-task IDs to add to the Action list."
        )
    elif recommendations:
        lines.append(
            "Google Ads has **not** been changed. Select advisory or task IDs to "
            "add them to the shared Action list; these items cannot enter the "
            "Google Ads apply path."
        )
    elif recovery_actions:
        lines.append(
            "Google Ads has **not** been changed. Select recovery-task IDs to add "
            "to the Action list, or give GMA the confirmation requested by the "
            "highest-priority task so it can rerun this skill."
        )
    else:
        lines.append(
            "Google Ads has **not** been changed. Keep the current settings and "
            "rerun when a fresh evidence window is available."
        )
    if change_plan.get("review_url"):
        lines.append(f"Private Change Plan: {_text(change_plan['review_url'])}")
    return "\n".join(lines)


def validate_run_result(result: Mapping[str, Any]) -> None:
    """Reject malformed authoritative results before a model can explain them."""

    required = {
        "contract_version",
        "run_id",
        "runtime_version",
        "methodology_version",
        "module",
        "status",
        "scope",
        "data_receipt",
        "coverage",
        "checks",
        "assessment",
        "recommendations",
        "recovery_actions",
        "unavailable_evidence",
        "change_plan",
        "renderers",
        "core_signature",
    }
    missing = sorted(required.difference(result))
    if missing:
        raise GmaRuntimeError(
            "Runtime result is missing: " + ", ".join(missing)
        )
    unknown = sorted(set(result).difference(required))
    if unknown:
        raise GmaRuntimeError(
            "Runtime result has unknown fields: " + ", ".join(unknown)
        )
    if result["contract_version"] != "gma-run-result/1.1":
        raise GmaRuntimeError("Runtime result has an invalid contract version")
    if not re.fullmatch(r"run_[a-f0-9]{32}", str(result["run_id"])):
        raise GmaRuntimeError("Runtime result has an invalid run ID")
    for field in ("runtime_version", "methodology_version"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise GmaRuntimeError(f"Runtime result has an invalid {field}")
    if result["status"] not in RESULT_STATUSES:
        raise GmaRuntimeError("Runtime result has an invalid status")
    module = result["module"]
    if (
        not isinstance(module, Mapping)
        or set(module) != {"id", "number", "name"}
        or module.get("id") not in MODULES
    ):
        raise GmaRuntimeError("Runtime result has an invalid module")
    expected_module = MODULES[str(module["id"])]
    if (
        module.get("number") != expected_module[0]
        or module.get("name") != expected_module[1]
    ):
        raise GmaRuntimeError(
            "Runtime result module metadata does not match the registry"
        )

    scope = result["scope"]
    scope_fields = {
        "customer_id",
        "login_customer_id",
        "account_name",
        "currency",
        "time_zone",
        "analysis_start",
        "analysis_end",
        "campaigns",
        "business_mode",
    }
    if not isinstance(scope, Mapping) or set(scope) != scope_fields:
        raise GmaRuntimeError("Runtime result has an invalid scope")
    if not re.fullmatch(r"\d{10}", str(scope["customer_id"])):
        raise GmaRuntimeError("Runtime scope has an invalid customer ID")
    login_customer_id = scope.get("login_customer_id")
    if login_customer_id is not None and not re.fullmatch(
        r"\d{10}", str(login_customer_id)
    ):
        raise GmaRuntimeError("Runtime scope has an invalid login customer ID")
    if scope.get("business_mode") not in {"lead_gen", "ecommerce"}:
        raise GmaRuntimeError("Runtime scope has an invalid business mode")
    try:
        start = date.fromisoformat(str(scope["analysis_start"]))
        end = date.fromisoformat(str(scope["analysis_end"]))
    except ValueError as error:
        raise GmaRuntimeError(
            "Runtime scope has invalid analysis dates"
        ) from error
    if end < start:
        raise GmaRuntimeError("Runtime scope analysis dates are reversed")
    if not isinstance(scope["campaigns"], list) or not scope["campaigns"]:
        raise GmaRuntimeError("Runtime scope must contain campaigns")
    campaign_allowed = {"id", "name", "type", "status", "spend_micros"}
    for campaign in scope["campaigns"]:
        if (
            not isinstance(campaign, Mapping)
            or not {"id", "name"}.issubset(campaign)
            or not set(campaign).issubset(campaign_allowed)
            or not re.fullmatch(r"\d{1,20}", str(campaign["id"]))
            or not isinstance(campaign["name"], str)
        ):
            raise GmaRuntimeError("Runtime scope contains an invalid campaign")

    receipt = result["data_receipt"]
    receipt_fields = {
        "source",
        "retrieved_at",
        "data_through",
        "analysis_start",
        "analysis_end",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != receipt_fields:
        raise GmaRuntimeError("Runtime result has an invalid data receipt")
    if receipt.get("source") != "GMA 13 Skills Google Ads":
        raise GmaRuntimeError("Runtime result has an invalid data source")

    coverage = result["coverage"]
    if not isinstance(coverage, Mapping) or set(coverage) != {
        "campaigns_requested",
        "campaigns_analyzed",
        "spend_coverage_percent",
        "gaps",
    }:
        raise GmaRuntimeError("Runtime result has invalid coverage")
    if not isinstance(coverage["gaps"], list):
        raise GmaRuntimeError("Runtime coverage gaps must be a list")
    spend_coverage = coverage["spend_coverage_percent"]
    if spend_coverage is not None and (
        isinstance(spend_coverage, bool)
        or not isinstance(spend_coverage, (int, float))
        or spend_coverage < 0
        or spend_coverage > 100
    ):
        raise GmaRuntimeError(
            "Runtime spend coverage must be between 0 and 100"
        )

    if not isinstance(result["checks"], list) or not result["checks"]:
        raise GmaRuntimeError("Runtime result must contain checks")
    if module["id"] == "red_flag_radar":
        check_fields = {
            "id",
            "campaign_id",
            "campaign_name",
            "criterion",
            "status",
            "evidence",
            "why_it_matters",
            "decision",
            "next_step",
            "metrics",
            "source",
        }
    else:
        check_fields = {
            "campaign_id",
            "campaign_name",
            "status",
            "channel_type",
            "efficiency",
            "constraint",
            "recipient_eligible",
            "donor_eligible",
            "holds",
            "hold_codes",
            "routes",
            "goal_scope",
            "effective_conversion_actions",
            "clicks_per_day",
            "conversion_volume",
            "scaling_volume_floor",
            "bidding_strategy_type",
            "search_impression_share",
            "search_budget_lost_impression_share",
            "search_rank_lost_impression_share",
        }
    for check in result["checks"]:
        if not isinstance(check, Mapping) or set(check) != check_fields:
            raise GmaRuntimeError("Runtime result contains an invalid check")
        if not re.fullmatch(r"\d{1,20}", str(check["campaign_id"])):
            raise GmaRuntimeError("Runtime check has an invalid campaign ID")
        if module["id"] == "red_flag_radar":
            if (
                not re.fullmatch(r"RF-[A-Z]+-\d{1,20}", str(check["id"]))
                or check["status"]
                not in {
                    "critical",
                    "warning",
                    "info",
                    "win",
                    "healthy",
                    "not_checked",
                }
                or check["source"]
                not in {"live_google_ads", "calculated", "unavailable"}
                or not isinstance(check["metrics"], Mapping)
            ):
                raise GmaRuntimeError(
                    "Runtime Red-Flag Radar check is invalid"
                )
        else:
            if not isinstance(check["holds"], list) or not isinstance(
                check["routes"], list
            ):
                raise GmaRuntimeError(
                    "Runtime check holds, hold codes, routes, and conversion actions "
                    "must be lists"
                )
            if not isinstance(check["hold_codes"], list) or not isinstance(
                check["effective_conversion_actions"], list
            ):
                raise GmaRuntimeError(
                    "Runtime check holds, hold codes, routes, and conversion actions "
                    "must be lists"
                )

    assessment = result["assessment"]
    assessment_detail_field = (
        "radar_summary"
        if module["id"] == "red_flag_radar"
        else "structural_budget_check"
    )
    if not isinstance(assessment, Mapping) or set(assessment) != {
        "state",
        "conclusion",
        "holds",
        assessment_detail_field,
    }:
        raise GmaRuntimeError("Runtime result has an invalid assessment")
    if not isinstance(result["recommendations"], list):
        raise GmaRuntimeError("Runtime recommendations must be a list")
    recommendation_fields = {
        "id",
        "priority",
        "severity",
        "evidence_label",
        "entity",
        "resource_name",
        "operation_type",
        "current_value",
        "proposed_value",
        "reason",
        "evidence_summary",
        "details",
        "expected_impact",
        "estimate",
        "risk",
        "reversible",
        "applyability",
        "source_skill",
    }
    action_ids: list[str] = []
    for recommendation in result["recommendations"]:
        if (
            not isinstance(recommendation, Mapping)
            or set(recommendation) != recommendation_fields
        ):
            raise GmaRuntimeError(
                "Runtime result contains an invalid recommendation"
            )
        action_id = str(recommendation.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", action_id):
            raise GmaRuntimeError(
                "Runtime recommendation has an invalid action ID"
            )
        if recommendation.get("applyability") not in RECOMMENDATION_TYPES:
            raise GmaRuntimeError(
                f"Runtime recommendation {action_id} has invalid applyability"
            )
        for text_field in (
            "entity",
            "reason",
            "evidence_summary",
            "details",
            "expected_impact",
        ):
            if (
                not isinstance(recommendation.get(text_field), str)
                or not recommendation[text_field].strip()
            ):
                raise GmaRuntimeError(
                    f"Runtime recommendation {action_id} has no actionable {text_field}"
                )
        for value_field in ("current_value", "proposed_value"):
            value = recommendation[value_field]
            if not isinstance(value, Mapping):
                raise GmaRuntimeError(
                    f"Runtime recommendation {action_id} has an invalid {value_field}"
                )
            if module["id"] == "budget_reallocator" and (
                set(value) != {"amount_micros"}
                or isinstance(value["amount_micros"], bool)
                or not isinstance(value["amount_micros"], int)
                or value["amount_micros"] <= 0
            ):
                raise GmaRuntimeError(
                    f"Runtime recommendation {action_id} has an invalid {value_field}"
                )
        if recommendation["applyability"] == "applyable":
            if recommendation["operation_type"] != "set_campaign_budget_amount":
                raise GmaRuntimeError(
                    f"Runtime recommendation {action_id} has an invalid operation"
                )
            if not re.fullmatch(
                r"customers/\d{10}/campaignBudgets/\d+",
                str(recommendation["resource_name"]),
            ):
                raise GmaRuntimeError(
                    f"Runtime recommendation {action_id} has an invalid resource"
                )
        elif recommendation["operation_type"] != "advisory":
            raise GmaRuntimeError(
                f"Runtime recommendation {action_id} must use an advisory operation"
            )
        expected_source_skill = str(module["id"]).replace("_", "-")
        if recommendation["source_skill"] != expected_source_skill:
            raise GmaRuntimeError(
                f"Runtime recommendation {action_id} has an invalid source skill"
            )
        action_ids.append(action_id)
    if len(action_ids) != len(set(action_ids)):
        raise GmaRuntimeError("Runtime recommendation IDs must be unique")
    if not isinstance(result["recovery_actions"], list):
        raise GmaRuntimeError("Runtime recovery actions must be a list")
    recovery_fields = {
        "id",
        "priority",
        "type",
        "status",
        "title",
        "reason",
        "steps",
        "applies_to",
        "resolves",
        "completion_signal",
        "owner",
        "follow_up",
        "selectable",
    }
    recovery_ids: list[str] = []
    for recovery in result["recovery_actions"]:
        if not isinstance(recovery, Mapping) or set(recovery) != recovery_fields:
            raise GmaRuntimeError(
                "Runtime result contains an invalid recovery action"
            )
        recovery_id = str(recovery.get("id", ""))
        if not re.fullmatch(r"REC-[A-Z0-9-]{1,60}", recovery_id):
            raise GmaRuntimeError(
                "Runtime recovery action has an invalid action ID"
            )
        if recovery.get("status") not in {
            "ready",
            "waiting",
            "needs_confirmation",
        }:
            raise GmaRuntimeError(
                f"Runtime recovery action {recovery_id} has an invalid status"
            )
        if recovery.get("owner") not in {
            "account_owner",
            "google_ads_admin",
            "gma",
        }:
            raise GmaRuntimeError(
                f"Runtime recovery action {recovery_id} has an invalid owner"
            )
        if (
            not isinstance(recovery.get("title"), str)
            or not recovery["title"].strip()
            or not isinstance(recovery.get("reason"), str)
            or not recovery["reason"].strip()
            or not isinstance(recovery.get("steps"), list)
            or not recovery["steps"]
            or not all(isinstance(step, str) and step.strip() for step in recovery["steps"])
            or not isinstance(recovery.get("applies_to"), list)
            or not recovery["applies_to"]
            or not isinstance(recovery.get("resolves"), list)
            or not recovery["resolves"]
            or not isinstance(recovery.get("completion_signal"), str)
            or not recovery["completion_signal"].strip()
            or recovery.get("selectable") is not True
        ):
            raise GmaRuntimeError(
                f"Runtime recovery action {recovery_id} is incomplete"
            )
        for entity in recovery["applies_to"]:
            if (
                not isinstance(entity, Mapping)
                or set(entity) != {"campaign_id", "campaign_name"}
                or not re.fullmatch(r"\d{1,20}", str(entity["campaign_id"]))
            ):
                raise GmaRuntimeError(
                    f"Runtime recovery action {recovery_id} has invalid scope"
                )
        follow_up = recovery.get("follow_up")
        if not isinstance(follow_up, Mapping) or set(follow_up) != {
            "kind",
            "module_id",
            "not_before",
        }:
            raise GmaRuntimeError(
                f"Runtime recovery action {recovery_id} has invalid follow-up"
            )
        if follow_up["kind"] not in {
            "rerun_current_skill",
            "run_named_skill",
            "review_then_rerun",
            "monitor",
        }:
            raise GmaRuntimeError(
                f"Runtime recovery action {recovery_id} has invalid follow-up kind"
            )
        module_id = follow_up.get("module_id")
        if module_id is not None and module_id not in MODULES:
            raise GmaRuntimeError(
                f"Runtime recovery action {recovery_id} has an unknown module"
            )
        not_before = follow_up.get("not_before")
        if not_before is not None:
            try:
                date.fromisoformat(str(not_before))
            except ValueError as error:
                raise GmaRuntimeError(
                    f"Runtime recovery action {recovery_id} has an invalid hold date"
                ) from error
        recovery_ids.append(recovery_id)
    if len(recovery_ids) != len(set(recovery_ids)):
        raise GmaRuntimeError("Runtime recovery action IDs must be unique")
    if (
        result["status"] in {"blocked", "partial"}
        or bool(assessment.get("holds"))
    ) and not result["recovery_actions"]:
        raise GmaRuntimeError(
            "A blocked or partial runtime result must contain a recovery plan"
        )
    recovery_campaign_ids = {
        str(entity["campaign_id"])
        for recovery in result["recovery_actions"]
        for entity in recovery["applies_to"]
    }
    for check in result["checks"]:
        needs_recovery = (
            check.get("source") == "unavailable"
            if module["id"] == "red_flag_radar"
            else bool(check.get("holds"))
        )
        if needs_recovery and str(check["campaign_id"]) not in recovery_campaign_ids:
            raise GmaRuntimeError(
                "Every unavailable or held campaign check must have an actionable recovery task"
            )
    if not isinstance(result["unavailable_evidence"], list):
        raise GmaRuntimeError("Runtime unavailable evidence must be a list")
    if result["unavailable_evidence"] != coverage["gaps"]:
        raise GmaRuntimeError("Runtime evidence gaps do not match coverage")
    change_plan = result["change_plan"]
    if not isinstance(change_plan, Mapping) or set(change_plan) != {
        "id",
        "status",
        "review_url",
        "applyable_action_ids",
    }:
        raise GmaRuntimeError("Runtime result has an invalid change plan")
    if change_plan["applyable_action_ids"] != [
        item["id"]
        for item in result["recommendations"]
        if item["applyability"] == "applyable"
    ]:
        raise GmaRuntimeError(
            "Runtime change plan action IDs do not match recommendations"
        )
    if result["renderers"] != {"chat": "gma_render_run", "app": "gma_get_run"}:
        raise GmaRuntimeError("Runtime result has invalid renderers")
    expected_signature = _core_signature({**result, "core_signature": ""})
    if result["core_signature"] != expected_signature:
        raise GmaRuntimeError("Runtime result signature is invalid")


class ScopeGateway:
    """Prepare one explicit advertiser/campaign scope with fixed queries."""

    def __init__(self, now_fn=None) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _search(service, customer_id: str, query: str):
        return list(service.search(customer_id=customer_id, query=query))

    def prepare(
        self,
        *,
        customer_id: str,
        login_customer_id: str | None = None,
        analysis_start: str | None = None,
        analysis_end: str | None = None,
        campaign_ids: Sequence[str] | None = None,
        business_mode: str | None = None,
    ) -> dict[str, Any]:
        customer = utils._normalize_customer_id(customer_id, "customer_id")
        login = utils.resolve_login_customer_id(login_customer_id)
        service = utils.get_googleads_service(
            "GoogleAdsService", login_customer_id=login
        )
        utils.enforce_customer_access_root(service, customer)
        rows = self._search(
            service,
            customer,
            "SELECT customer.id, customer.descriptive_name, customer.manager, "
            "customer.currency_code, customer.time_zone FROM customer LIMIT 1",
        )
        if len(rows) != 1:
            raise GmaRuntimeError(
                "The advertiser account could not be resolved"
            )
        account = rows[0].customer
        if account.manager:
            raise GmaRuntimeError(
                "This is a manager account. Choose a non-manager advertiser account."
            )

        time_zone = account.time_zone or "UTC"
        now = self._now_fn()
        try:
            start, end = resolve_analysis_window(
                analysis_start=analysis_start,
                analysis_end=analysis_end,
                account_time_zone=time_zone,
                now=now,
            )
        except ValueError as error:
            raise GmaRuntimeError(str(error)) from error

        selected_ids: list[str] = []
        if campaign_ids:
            for value in campaign_ids:
                campaign_id = str(value).strip()
                if not re.fullmatch(r"\d{1,20}", campaign_id):
                    raise GmaRuntimeError("Campaign IDs must be numeric")
                selected_ids.append(campaign_id)
        campaign_filter = (
            " AND campaign.id IN (" + ",".join(sorted(set(selected_ids))) + ")"
            if selected_ids
            else ""
        )
        inventory_rows = self._search(
            service,
            customer,
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, campaign.bidding_strategy, "
            "campaign.bidding_strategy_type, "
            "campaign.target_cpa.target_cpa_micros, "
            "campaign.maximize_conversions.target_cpa_micros, "
            "campaign.target_roas.target_roas, "
            "campaign.maximize_conversion_value.target_roas "
            "FROM campaign WHERE campaign.status != 'REMOVED'"
            f"{campaign_filter} ORDER BY campaign.name LIMIT 500",
        )
        if not inventory_rows:
            raise GmaRuntimeError(
                "No campaigns were found in the selected scope"
            )
        inventory_ids = {str(row.campaign.id) for row in inventory_rows}
        missing_selected = sorted(set(selected_ids).difference(inventory_ids))
        if missing_selected:
            raise GmaRuntimeError(
                "Selected campaigns were not found in this advertiser: "
                + ", ".join(missing_selected)
            )
        selector_start, selector_end = trailing_complete_days(
            account_time_zone=time_zone,
            now=now,
            days=30,
        )
        spend_rows = self._search(
            service,
            customer,
            "SELECT campaign.id, metrics.conversions, metrics.conversions_value, "
            "metrics.cost_micros FROM campaign "
            f"WHERE segments.date BETWEEN '{selector_start}' AND '{selector_end}' "
            "AND campaign.status != 'REMOVED'"
            f"{campaign_filter} LIMIT 500",
        )
        spend_by_campaign = {
            str(row.campaign.id): int(row.metrics.cost_micros)
            for row in spend_rows
        }
        performance_by_campaign = {
            str(row.campaign.id): {
                "conversions": float(row.metrics.conversions),
                "conversion_value": float(row.metrics.conversions_value),
            }
            for row in spend_rows
        }
        campaign_rows = sorted(
            inventory_rows,
            key=lambda row: (
                -spend_by_campaign.get(str(row.campaign.id), 0),
                0 if _enum(row.campaign.status) == "ENABLED" else 1,
                str(row.campaign.name).casefold(),
            ),
        )

        conversion_rows = self._search(
            service,
            customer,
            "SELECT conversion_action.category, conversion_action.status, "
            "conversion_action.primary_for_goal FROM conversion_action "
            "WHERE conversion_action.status != 'REMOVED' LIMIT 500",
        )
        primary_categories = {
            _enum(row.conversion_action.category)
            for row in conversion_rows
            if _enum(row.conversion_action.status) == "ENABLED"
            and row.conversion_action.primary_for_goal
        }
        inferred = "unknown"
        has_purchase = "PURCHASE" in primary_categories
        has_lead = bool(
            primary_categories.intersection(
                {"SUBMIT_LEAD_FORM", "PHONE_CALL_LEAD", "CONTACT", "SIGNUP"}
            )
        )
        if has_purchase and has_lead:
            inferred = "hybrid"
        elif has_purchase:
            inferred = "ecommerce"
        elif has_lead:
            inferred = "lead_gen"

        if business_mode and business_mode not in {
            "ecommerce",
            "lead_gen",
            "hybrid",
        }:
            raise GmaRuntimeError(
                "business_mode must be ecommerce, lead_gen, or hybrid"
            )

        goal_coverage_gaps: list[str] = []
        try:
            bidding_strategy_rows = self._search(
                service,
                customer,
                "SELECT bidding_strategy.id, bidding_strategy.resource_name, "
                "bidding_strategy.name, bidding_strategy.type, "
                "bidding_strategy.target_cpa.target_cpa_micros, "
                "bidding_strategy.maximize_conversions.target_cpa_micros, "
                "bidding_strategy.target_roas.target_roas, "
                "bidding_strategy.maximize_conversion_value.target_roas "
                "FROM bidding_strategy LIMIT 500",
            )
        except Exception as error:
            bidding_strategy_rows = []
            goal_coverage_gaps.append(
                "Customer-owned portfolio bidding targets unavailable: "
                + str(error)[:240]
            )
        try:
            accessible_strategy_rows = self._search(
                service,
                customer,
                "SELECT accessible_bidding_strategy.id, "
                "accessible_bidding_strategy.resource_name, "
                "accessible_bidding_strategy.name, "
                "accessible_bidding_strategy.type, "
                "accessible_bidding_strategy.target_cpa.target_cpa_micros, "
                "accessible_bidding_strategy.maximize_conversions.target_cpa_micros, "
                "accessible_bidding_strategy.target_roas.target_roas, "
                "accessible_bidding_strategy.maximize_conversion_value.target_roas "
                "FROM accessible_bidding_strategy LIMIT 500",
            )
        except Exception as error:
            accessible_strategy_rows = []
            goal_coverage_gaps.append(
                "Manager-owned portfolio bidding targets unavailable: "
                + str(error)[:240]
            )

        selected_business_mode = business_mode or inferred
        goal_context = build_goal_context(
            campaign_rows,
            spend_by_campaign=spend_by_campaign,
            performance_by_campaign=performance_by_campaign,
            bidding_strategy_rows=bidding_strategy_rows,
            accessible_strategy_rows=accessible_strategy_rows,
            business_mode=selected_business_mode,
            inferred_business_mode=inferred,
            currency=account.currency_code,
            coverage_gaps=goal_coverage_gaps,
        )
        scope = {
            "customer_id": customer,
            "login_customer_id": login,
            "account_name": account.descriptive_name or customer,
            "currency": account.currency_code,
            "time_zone": time_zone,
            "analysis_start": start.isoformat(),
            "analysis_end": end.isoformat(),
            "analysis_label": (
                "Last 30 days" if (end - start).days == 29 else "Custom"
            ),
            "campaign_scope": "selected" if selected_ids else "all_eligible",
            "campaigns": [
                {
                    "id": str(row.campaign.id),
                    "name": row.campaign.name,
                    "type": _enum(row.campaign.advertising_channel_type),
                    "status": _enum(row.campaign.status),
                    "spend_micros": spend_by_campaign.get(
                        str(row.campaign.id), 0
                    ),
                }
                for row in campaign_rows
            ],
            "business_mode": selected_business_mode,
            "business_mode_inferred": inferred,
            "business_mode_needs_confirmation": (
                not business_mode or business_mode != inferred
            ),
            "goal_context": goal_context,
            "campaign_spend_window_start": selector_start.isoformat(),
            "campaign_spend_window_end": selector_end.isoformat(),
            "run_mode": "interactive_read_only",
        }
        scope_hash = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "contract_version": "gma-scope/1.0",
            "scope_id": f"scope_{scope_hash[:24]}",
            "scope_hash": scope_hash,
            "scope": scope,
            "confirmation_required": True,
        }


class AccountGateway:
    """Return non-manager advertiser accounts within the connector boundary."""

    @staticmethod
    def _search(service, customer_id: str, query: str):
        return list(service.search(customer_id=customer_id, query=query))

    def list_advertisers(self) -> dict[str, Any]:
        access_root = utils.get_access_root_customer_id()
        enforced_login = utils.get_enforced_login_customer_id()
        if access_root:
            root_ids = [access_root]
        elif enforced_login:
            root_ids = [enforced_login]
        else:
            customer_service = utils.get_googleads_service("CustomerService")
            response = customer_service.list_accessible_customers()
            root_ids = [
                value.removeprefix("customers/")
                for value in response.resource_names
            ]
        if not root_ids:
            raise GmaRuntimeError(
                "The signed-in Google account has no accessible Google Ads accounts"
            )

        accounts: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        for root_id in root_ids:
            try:
                service = utils.get_googleads_service(
                    "GoogleAdsService",
                    login_customer_id=(
                        root_id
                        if root_id in {access_root, enforced_login}
                        else None
                    ),
                )
                rows = self._search(
                    service,
                    root_id,
                    "SELECT customer.id, customer.descriptive_name, customer.manager, "
                    "customer.currency_code, customer.time_zone FROM customer LIMIT 1",
                )
                if len(rows) != 1:
                    failures.append(root_id)
                    continue
                customer = rows[0].customer
                if not customer.manager:
                    accounts[str(customer.id)] = {
                        "customer_id": str(customer.id),
                        "name": customer.descriptive_name or str(customer.id),
                        "currency": customer.currency_code,
                        "time_zone": customer.time_zone,
                        "login_customer_id": None,
                    }
                    continue
                child_rows = self._search(
                    service,
                    root_id,
                    "SELECT customer_client.id, customer_client.descriptive_name, "
                    "customer_client.manager, customer_client.status, "
                    "customer_client.currency_code, customer_client.time_zone "
                    "FROM customer_client WHERE customer_client.status = 'ENABLED' "
                    "ORDER BY customer_client.descriptive_name LIMIT 500",
                )
                for row in child_rows:
                    child = row.customer_client
                    if child.manager:
                        continue
                    child_id = str(child.id)
                    accounts[child_id] = {
                        "customer_id": child_id,
                        "name": child.descriptive_name or child_id,
                        "currency": child.currency_code,
                        "time_zone": child.time_zone,
                        "login_customer_id": root_id,
                    }
            except Exception:
                failures.append(root_id)

        if not accounts:
            raise GmaRuntimeError(
                "No non-manager advertiser accounts could be read within the connector boundary"
            )
        ordered = sorted(
            accounts.values(),
            key=lambda item: (item["name"].casefold(), item["customer_id"]),
        )
        return {
            "contract_version": "gma-accounts/1.0",
            "accounts": ordered,
            "selection_required": len(ordered) != 1,
            "unreadable_root_ids": sorted(set(failures)),
        }


def preflight(host_package_version: str | None = None) -> dict[str, Any]:
    """Return connector identity, versions, boundary, and safe capabilities."""

    changes = get_changeset_service().capabilities()
    return {
        "contract_version": "gma-preflight/1.0",
        "status": "ready_for_read_only",
        "connector": {
            "name": "GMA 13 Skills — Google Ads",
            "runtime_version": RUNTIME_VERSION,
            "expected_plugin_version": EXPECTED_PLUGIN_VERSION,
            "host_package_version": host_package_version,
            "package_match": (
                None
                if host_package_version is None
                else host_package_version == EXPECTED_PLUGIN_VERSION
            ),
        },
        "account_boundary": {
            "enforced_login_customer_id": utils.get_enforced_login_customer_id(),
            "access_root_customer_id": utils.get_access_root_customer_id(),
        },
        "modules": [
            {
                "id": module_id,
                "number": data[0],
                "name": data[1],
                "runtime_status": (
                    "available"
                    if module_id in MODULE_HANDLERS
                    else "not_yet_ported"
                ),
            }
            for module_id, data in MODULES.items()
        ],
        "controlled_changes": changes,
        "live_apply_status": (
            "available_for_allowlisted_pilot"
            if changes["live_mutations_enabled"]
            and changes["developer_token_ad_management_confirmed"]
            else "disabled"
        ),
    }


async def _run_budget_reallocator(
    scope: Mapping[str, Any], inputs: Mapping[str, Any]
) -> dict[str, Any]:
    business_mode = str(scope["business_mode"])
    if business_mode == "hybrid":
        raise GmaRuntimeError(
            "Budget Reallocator V1 requires separate ecommerce and lead-gen campaign scopes"
        )
    return await BudgetReallocatorRunService().run(
        customer_id=str(scope["customer_id"]),
        login_customer_id=scope.get("login_customer_id"),
        business_mode=business_mode,
        target_cpa=inputs.get("target_cpa"),
        target_roas=inputs.get("target_roas"),
        outcome_quality_confirmed=bool(
            inputs.get("outcome_quality_confirmed", False)
        ),
        analysis_start=str(scope["analysis_start"]),
        analysis_end=str(scope["analysis_end"]),
        campaign_ids=[str(item["id"]) for item in scope["campaigns"]],
        monthly_budget=inputs.get("monthly_budget"),
        allow_net_increase=bool(inputs.get("allow_net_increase", False)),
    )


async def _run_red_flag_radar(
    scope: Mapping[str, Any], inputs: Mapping[str, Any]
) -> dict[str, Any]:
    business_mode = str(scope["business_mode"])
    if business_mode == "hybrid":
        raise GmaRuntimeError(
            "Red-Flag Radar V1 requires separate ecommerce and lead-gen campaign scopes"
        )
    return await RedFlagRadarRunService().run(
        customer_id=str(scope["customer_id"]),
        login_customer_id=scope.get("login_customer_id"),
        business_mode=business_mode,
        target_cpa=inputs.get("target_cpa"),
        target_roas=inputs.get("target_roas"),
        outcome_quality_confirmed=bool(
            inputs.get("outcome_quality_confirmed", False)
        ),
        analysis_start=str(scope["analysis_start"]),
        analysis_end=str(scope["analysis_end"]),
        campaign_ids=[str(item["id"]) for item in scope["campaigns"]],
    )


def list_goal_benchmarks(business_mode: str) -> dict[str, Any]:
    """Return source-labelled benchmark categories for one business mode."""

    try:
        return list_benchmark_profiles(business_mode)
    except ValueError as error:
        raise GmaRuntimeError(str(error)) from error


async def build_goal_report(
    *,
    scope_id: str,
    confirmed_scope_hash: str,
    industry_profile_id: str,
    confirmed_target_cpa: Any = None,
    confirmed_target_roas: Any = None,
    economics: Mapping[str, Any] | None = None,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Build an owner-bound, read-only Goal Benchmark Report."""

    scope = await get_prepared_scope(
        scope_id,
        confirmed_scope_hash,
        store=store,
        owner_resolver=owner_resolver,
    )
    try:
        return build_goal_report_result(
            scope,
            industry_profile_id=industry_profile_id,
            confirmed_target_cpa=confirmed_target_cpa,
            confirmed_target_roas=confirmed_target_roas,
            economics=economics,
        )
    except ValueError as error:
        raise GmaRuntimeError(str(error)) from error


MODULE_HANDLERS = {
    "red_flag_radar": _run_red_flag_radar,
    "budget_reallocator": _run_budget_reallocator,
}


async def run_skill(
    *,
    module_id: str,
    scope_id: str,
    confirmed_scope_hash: str,
    business_inputs: Mapping[str, Any] | None = None,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Execute one registered deterministic module and validate its result."""

    definition = MODULES.get(module_id)
    if not definition:
        raise GmaRuntimeError("Unknown GMA module")
    handler = MODULE_HANDLERS.get(module_id)
    if handler is None:
        raise GmaRuntimeError(
            f"Skill {definition[0]} — {definition[1]} is not yet ported to the V1 runtime"
        )
    scope = await get_prepared_scope(
        scope_id,
        confirmed_scope_hash,
        store=store,
        owner_resolver=owner_resolver,
    )
    required_scope = {
        "customer_id",
        "account_name",
        "currency",
        "time_zone",
        "analysis_start",
        "analysis_end",
        "campaigns",
        "business_mode",
    }
    missing = sorted(required_scope.difference(scope))
    if missing:
        raise GmaRuntimeError(
            "Prepared scope is missing: " + ", ".join(missing)
        )
    inputs = dict(business_inputs or {})
    business_mode = str(scope["business_mode"])
    raw = await handler(scope, inputs)

    gaps = list(raw.get("coverage_gaps") or [])
    if raw["status"] == "partial":
        status = "partial"
    elif raw["status"] == "hold":
        status = "blocked"
    else:
        status = "complete"
    recommendations = [dict(action) for action in raw["recommendations"]]
    checks = [
        dict(item)
        for item in raw.get("checks", raw.get("campaign_results", []))
    ]
    analyzed_ids = {
        str(item["campaign_id"]) for item in checks
    }
    requested_spend = sum(
        int(item.get("spend_micros") or 0) for item in scope["campaigns"]
    )
    analyzed_spend = sum(
        int(item.get("spend_micros") or 0)
        for item in scope["campaigns"]
        if str(item["id"]) in analyzed_ids
    )
    spend_coverage_percent = (
        round(analyzed_spend * 100 / requested_spend, 1)
        if requested_spend > 0
        else (100.0 if len(analyzed_ids) == len(scope["campaigns"]) else None)
    )
    result: dict[str, Any] = {
        "contract_version": "gma-run-result/1.1",
        "run_id": raw["change_plan"]["id"].replace("gma_", "run_", 1),
        "runtime_version": RUNTIME_VERSION,
        "methodology_version": METHODOLOGY_VERSIONS[module_id],
        "module": {
            "id": module_id,
            "number": definition[0],
            "name": definition[1],
        },
        "status": status,
        "scope": {
            "customer_id": scope["customer_id"],
            "login_customer_id": scope.get("login_customer_id"),
            "account_name": scope["account_name"],
            "currency": scope["currency"],
            "time_zone": scope["time_zone"],
            "analysis_start": scope["analysis_start"],
            "analysis_end": scope["analysis_end"],
            "campaigns": scope["campaigns"],
            "business_mode": business_mode,
        },
        "data_receipt": raw["data_receipt"],
        "coverage": {
            "campaigns_requested": len(scope["campaigns"]),
            "campaigns_analyzed": raw["campaigns_analyzed"],
            "spend_coverage_percent": spend_coverage_percent,
            "gaps": gaps,
        },
        "checks": checks,
        "assessment": {
            "state": raw["status"],
            "conclusion": raw["conclusion"],
            "holds": raw["holds"],
            (
                "radar_summary"
                if module_id == "red_flag_radar"
                else "structural_budget_check"
            ): (
                raw["assessment_details"]
                if module_id == "red_flag_radar"
                else raw["structural_budget_check"]
            ),
        },
        "recommendations": recommendations,
        "recovery_actions": [dict(action) for action in raw["recovery_actions"]],
        "unavailable_evidence": gaps,
        "change_plan": raw["change_plan"],
        "renderers": {
            "chat": "gma_render_run",
            "app": "gma_get_run",
        },
        "core_signature": "",
    }
    result["core_signature"] = _core_signature(result)
    validate_run_result(result)
    await _save_run_result(
        result,
        store=store,
        owner_resolver=owner_resolver,
    )
    return result


async def render_run(
    run_id: str,
    *,
    store: AsyncChangesetStore | None = None,
    owner_resolver=None,
) -> dict[str, Any]:
    """Render a persisted canonical run for a host chat or app surface."""

    result = await get_run(
        run_id,
        store=store,
        owner_resolver=owner_resolver,
    )
    return {
        "contract_version": "gma-rendered-run/1.0",
        "run_id": run_id,
        "module": result["module"],
        "status": result["status"],
        "core_signature": result["core_signature"],
        "content_type": "text/markdown",
        "content": render_run_result(result),
        "change_plan": result["change_plan"],
    }
