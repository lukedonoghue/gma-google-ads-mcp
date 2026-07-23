"""Deterministic recommendation engine for GMA Skill 12.

The model may explain this result, but it must not invent or recalculate the
eligibility gates, dollar moves, action IDs, or applyability state.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence


class BudgetAnalysisError(ValueError):
    """A safe user-facing error in the normalized analysis input."""


def _money_micros(value: int) -> str:
    return f"{Decimal(value) / Decimal(1_000_000):,.2f}"


def _percent(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value * 100:.1f}%"


def _round_micros(value: Decimal) -> int:
    return int(
        (value / Decimal("10000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        * Decimal("10000")
    )


def _days(start: str, end: str) -> int:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as error:
        raise BudgetAnalysisError("Analysis dates must be YYYY-MM-DD") from error
    if end_date < start_date:
        raise BudgetAnalysisError("Analysis end cannot be before start")
    return (end_date - start_date).days + 1


def _efficiency(
    campaign: Mapping[str, Any],
    business_mode: str,
    target_cpa_micros: int | None,
    target_roas: float | None,
) -> dict[str, Any]:
    cost = int(campaign.get("cost_micros") or 0)
    conversions = float(campaign.get("conversions") or 0)
    value = float(campaign.get("conversions_value") or 0)
    if business_mode == "lead_gen":
        if not target_cpa_micros:
            return {"status": "target_missing", "actual": None, "target": None}
        actual = (cost / conversions) if conversions > 0 else None
        profitable = actual is not None and actual <= target_cpa_micros
        return {
            "status": "profitable" if profitable else "unprofitable",
            "actual": actual,
            "target": target_cpa_micros,
            "ratio": (actual / target_cpa_micros) if actual is not None else None,
        }
    if business_mode == "ecommerce":
        if not target_roas:
            return {"status": "target_missing", "actual": None, "target": None}
        actual = (value * 1_000_000 / cost) if cost > 0 else None
        profitable = actual is not None and actual >= target_roas
        return {
            "status": "profitable" if profitable else "unprofitable",
            "actual": actual,
            "target": target_roas,
            "ratio": (target_roas / actual) if actual else None,
        }
    return {"status": "target_missing", "actual": None, "target": None}


def _campaign_gate(
    campaign: Mapping[str, Any],
    *,
    days: int,
    business_mode: str,
    target_cpa_micros: int | None,
    target_roas: float | None,
    outcome_quality_confirmed: bool,
) -> dict[str, Any]:
    efficiency = _efficiency(campaign, business_mode, target_cpa_micros, target_roas)
    search_is = campaign.get("search_impression_share")
    lost_budget = campaign.get("search_budget_lost_impression_share")
    lost_rank = campaign.get("search_rank_lost_impression_share")
    shared = bool(campaign.get("budget_explicitly_shared"))
    recent_change = campaign.get("recent_material_change_at")
    goal_verified = bool(campaign.get("goal_scope_verified"))
    clicks_per_day = float(campaign.get("clicks") or 0) / max(days, 1)
    conversions = float(campaign.get("conversions") or 0)
    status = str(campaign.get("status") or "UNKNOWN")
    bidding_strategy = str(campaign.get("bidding_strategy_type") or "UNKNOWN")
    channel_type = str(campaign.get("channel_type") or "UNKNOWN")
    if bidding_strategy == "TARGET_ROAS":
        scaling_volume_floor = 50
    elif bidding_strategy == "TARGET_CPA":
        scaling_volume_floor = 30
    elif bidding_strategy == "MAXIMIZE_CONVERSIONS":
        scaling_volume_floor = 15
    elif bidding_strategy == "MAXIMIZE_CONVERSION_VALUE":
        scaling_volume_floor = 30
    else:
        scaling_volume_floor = 15

    holds: list[str] = []
    hold_codes: list[str] = []
    routes: list[str] = []

    def hold(code: str, message: str) -> None:
        hold_codes.append(code)
        holds.append(message)

    if status != "ENABLED":
        hold(
            "CAMPAIGN_NOT_ENABLED",
            f"Campaign is {status.replace('_', ' ').lower()}; only enabled campaigns "
            "can donate or receive budget",
        )
    if days < 14:
        hold("INSUFFICIENT_ANALYSIS_WINDOW", "Fewer than 14 days of evidence")
    if not outcome_quality_confirmed:
        hold(
            "OUTCOME_QUALITY_UNCONFIRMED",
            "Outcome quality has not been confirmed",
        )
    if not goal_verified:
        hold(
            "GOAL_SCOPE_UNVERIFIED",
            "Campaign-effective conversion goals could not be verified",
        )
    if not bool(campaign.get("change_history_verified", True)):
        hold(
            "CHANGE_HISTORY_UNVERIFIED",
            "Recent bid and budget change history could not be verified",
        )
    if efficiency["status"] == "target_missing":
        hold("BUSINESS_TARGET_MISSING", "Business CPA or ROAS target is missing")
    if recent_change:
        hold("RECENT_MATERIAL_CHANGE", f"Recent material change on {recent_change}")
    if shared:
        hold(
            "SHARED_BUDGET",
            "Campaign uses a shared budget; assess the pool before editing",
        )
    if clicks_per_day < 1:
        hold(
            "LOW_CLICK_VOLUME",
            "Campaign averages fewer than 1 click per day",
        )
    if channel_type == "PERFORMANCE_MAX":
        hold(
            "PMAX_EVIDENCE_REQUIRED",
            "Performance Max needs product or lead-quality, inventory, and brand-mix "
            "evidence before a budget move",
        )
        routes.append("Instant Account Audit")
    elif channel_type not in {"SEARCH", "SHOPPING"}:
        hold(
            "CHANNEL_SPECIFIC_REVIEW_REQUIRED",
            f"{channel_type.replace('_', ' ').title()} needs its own channel-specific "
            "budget review",
        )

    constraint = "not_available"
    if search_is is not None and search_is >= 0.90:
        constraint = "demand_ceiling"
        routes.append("Keyword Gap Finder")
    elif lost_budget is not None and lost_rank is not None:
        if lost_budget >= 0.10 and lost_rank >= 0.10:
            constraint = "mixed_budget_and_rank"
            routes.extend(["Quality Score Booster", "Bid Strategy Check"])
        elif lost_budget >= 0.10:
            constraint = "budget_limited"
        elif lost_rank >= 0.10:
            constraint = "rank_limited"
            routes.extend(["Quality Score Booster", "Bid Strategy Check"])
        else:
            constraint = "not_materially_limited"

    if (
        efficiency["status"] == "profitable"
        and constraint == "budget_limited"
        and conversions < scaling_volume_floor
    ):
        hold(
            "INSUFFICIENT_CONVERSION_VOLUME",
            f"Only {conversions:g} conversions in the window; "
            f"{scaling_volume_floor} required for this scaling decision",
        )

    recipient = (
        not holds
        and efficiency["status"] == "profitable"
        and constraint == "budget_limited"
        and search_is is not None
        and search_is < 0.90
    )
    donor = (
        not holds
        and days >= 30
        and efficiency["status"] == "unprofitable"
        and int(campaign.get("cost_micros") or 0) > 0
    )
    return {
        "campaign_id": str(campaign["id"]),
        "campaign_name": campaign["name"],
        "status": status,
        "channel_type": campaign.get("channel_type", "UNKNOWN"),
        "efficiency": efficiency,
        "constraint": constraint,
        "recipient_eligible": recipient,
        "donor_eligible": donor,
        "holds": holds,
        "hold_codes": hold_codes,
        "routes": sorted(set(routes)),
        "goal_scope": str(campaign.get("goal_scope") or "unable_to_verify"),
        "effective_conversion_actions": sorted(
            {
                str(value)
                for value in campaign.get("effective_conversion_actions") or []
                if str(value).strip()
            }
        ),
        "clicks_per_day": round(clicks_per_day, 2),
        "conversion_volume": conversions,
        "scaling_volume_floor": scaling_volume_floor,
        "bidding_strategy_type": bidding_strategy,
        "search_impression_share": search_is,
        "search_budget_lost_impression_share": lost_budget,
        "search_rank_lost_impression_share": lost_rank,
    }


def _recovery_action(
    *,
    action_id: str,
    priority: int,
    action_type: str,
    status: str,
    title: str,
    reason: str,
    steps: Sequence[str],
    affected: Sequence[Mapping[str, Any]],
    resolves: Sequence[str],
    completion_signal: str,
    follow_up_kind: str,
    follow_up_module_id: str | None = None,
    not_before: str | None = None,
    owner: str = "account_owner",
) -> dict[str, Any]:
    return {
        "id": action_id,
        "priority": priority,
        "type": action_type,
        "status": status,
        "title": title,
        "reason": reason,
        "steps": list(steps),
        "applies_to": [
            {
                "campaign_id": str(item["campaign_id"]),
                "campaign_name": str(item["campaign_name"]),
            }
            for item in affected
        ],
        "resolves": sorted(set(str(item) for item in resolves)),
        "completion_signal": completion_signal,
        "owner": owner,
        "follow_up": {
            "kind": follow_up_kind,
            "module_id": follow_up_module_id,
            "not_before": not_before,
        },
        "selectable": True,
    }


def _recovery_actions(
    gates: Sequence[Mapping[str, Any]],
    *,
    analysis_start: str,
    analysis_end: str,
    business_mode: str,
    include_monitor_fallback: bool,
) -> list[dict[str, Any]]:
    """Turn every blocking gate into a concrete, selectable recovery route."""

    by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for gate in gates:
        for code in gate.get("hold_codes") or []:
            by_code[str(code)].append(gate)

    actions: list[dict[str, Any]] = []

    def holds_for(code: str) -> list[str]:
        messages: list[str] = []
        for gate in by_code.get(code, []):
            codes = [str(item) for item in gate.get("hold_codes") or []]
            holds = [str(item) for item in gate.get("holds") or []]
            messages.extend(
                hold
                for hold_code, hold in zip(codes, holds, strict=False)
                if hold_code == code
            )
        return list(dict.fromkeys(messages))

    if by_code["OUTCOME_QUALITY_UNCONFIRMED"]:
        affected = by_code["OUTCOME_QUALITY_UNCONFIRMED"]
        action_names = sorted(
            {
                name
                for gate in affected
                for name in gate.get("effective_conversion_actions") or []
            }
        )
        action_detail = (
            "The campaign-effective actions currently reported are: "
            + "; ".join(action_names)
            + "."
            if action_names
            else (
                "First review the campaign-effective conversion actions because "
                "their names were not available in this run."
            )
        )
        outcome_word = "purchases" if business_mode == "ecommerce" else "leads"
        actions.append(
            _recovery_action(
                action_id="REC-OUTCOME-QUALITY",
                priority=1,
                action_type="user_confirmation",
                status="needs_confirmation",
                title=f"Confirm which reported conversions are genuine {outcome_word}",
                reason=(
                    "GMA will not move budget toward a campaign until the outcomes "
                    "used to judge it are confirmed as genuine and non-duplicated."
                ),
                steps=[
                    action_detail,
                    (
                        f"Confirm which actions represent genuine {outcome_word}, "
                        "which are duplicates, and which should not guide bidding."
                    ),
                    (
                        "Return that confirmation to GMA. It will be recorded as "
                        "user-confirmed business evidence, then this skill will rerun "
                        "with a fresh Google Ads pull."
                    ),
                ],
                affected=affected,
                resolves=holds_for("OUTCOME_QUALITY_UNCONFIRMED"),
                completion_signal=(
                    "The account owner confirms the genuine, non-duplicated "
                    "campaign-effective conversion actions for this scope."
                ),
                follow_up_kind="review_then_rerun",
                follow_up_module_id="budget_reallocator",
            )
        )

    if by_code["GOAL_SCOPE_UNVERIFIED"]:
        affected = by_code["GOAL_SCOPE_UNVERIFIED"]
        actions.append(
            _recovery_action(
                action_id="REC-CONVERSION-GOALS",
                priority=1,
                action_type="google_ads_review",
                status="ready",
                title="Verify the conversion goals used by each blocked campaign",
                reason=(
                    "A budget decision is unsafe when GMA cannot prove whether a "
                    "campaign uses account-default, campaign-specific, or custom goals."
                ),
                steps=[
                    (
                        "In Google Ads, open each affected campaign, then Settings → "
                        "Goals, and record whether it uses account-default or "
                        "campaign-specific goals."
                    ),
                    (
                        "Open Goals → Conversions → Summary and confirm the enabled "
                        "primary actions are the outcomes this campaign should optimise."
                    ),
                    (
                        "Fix any incorrect goal assignment in Google Ads, or reconnect "
                        "and retry if the configuration is correct but could not be read."
                    ),
                ],
                affected=affected,
                resolves=holds_for("GOAL_SCOPE_UNVERIFIED"),
                completion_signal=(
                    "The runtime can resolve a non-empty set of campaign-effective "
                    "primary conversion actions."
                ),
                follow_up_kind="review_then_rerun",
                follow_up_module_id="budget_reallocator",
                owner="google_ads_admin",
            )
        )

    if by_code["BUSINESS_TARGET_MISSING"]:
        affected = by_code["BUSINESS_TARGET_MISSING"]
        target_name = "ROAS" if business_mode == "ecommerce" else "CPA"
        actions.append(
            _recovery_action(
                action_id="REC-CONFIRM-TARGET",
                priority=1,
                action_type="user_confirmation",
                status="needs_confirmation",
                title=f"Confirm the business {target_name} limit",
                reason=(
                    f"Campaign performance cannot be called efficient or inefficient "
                    f"without a confirmed {target_name} goal."
                ),
                steps=[
                    (
                        f"Review the configured Google Ads {target_name}, the last-30-day "
                        f"reported {target_name}, and the GMA Goal Benchmark reference."
                    ),
                    (
                        f"Confirm the maximum acceptable {target_name} for this scope; "
                        "the industry benchmark remains advisory."
                    ),
                    "Rerun Budget Reallocator with the confirmed business goal.",
                ],
                affected=affected,
                resolves=holds_for("BUSINESS_TARGET_MISSING"),
                completion_signal=f"The account owner confirms a positive {target_name} goal.",
                follow_up_kind="review_then_rerun",
                follow_up_module_id="budget_reallocator",
            )
        )

    if by_code["RECENT_MATERIAL_CHANGE"]:
        affected = by_code["RECENT_MATERIAL_CHANGE"]
        dates = []
        for gate in affected:
            for value in gate.get("holds") or []:
                if value.startswith("Recent material change on "):
                    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
                    if not match:
                        continue
                    try:
                        dates.append(date.fromisoformat(match.group(0)))
                    except ValueError:
                        pass
        not_before = (
            (max(dates) + timedelta(days=14)).isoformat() if dates else None
        )
        actions.append(
            _recovery_action(
                action_id="REC-WAIT-FOR-LEARNING",
                priority=2,
                action_type="wait_and_rerun",
                status="waiting",
                title="Let the recent change finish its observation period",
                reason=(
                    "Moving budget again now would mix the effect of two changes and "
                    "make the result harder to judge."
                ),
                steps=[
                    (
                        f"Keep the affected campaign budget and bidding unchanged until "
                        f"{not_before}."
                        if not_before
                        else "Keep the affected campaign budget and bidding unchanged for 14 days."
                    ),
                    "Watch for tracking or policy breakages, but do not react to ordinary daily noise.",
                    "Rerun Budget Reallocator with a fresh completed window after the hold date.",
                ],
                affected=affected,
                resolves=holds_for("RECENT_MATERIAL_CHANGE"),
                completion_signal=(
                    "Fourteen complete days have elapsed since the latest material "
                    "bid, target, or budget change."
                ),
                follow_up_kind="rerun_current_skill",
                follow_up_module_id="budget_reallocator",
                not_before=not_before,
                owner="gma",
            )
        )

    if by_code["CHANGE_HISTORY_UNVERIFIED"]:
        affected = by_code["CHANGE_HISTORY_UNVERIFIED"]
        actions.append(
            _recovery_action(
                action_id="REC-VERIFY-CHANGE-HISTORY",
                priority=2,
                action_type="google_ads_review",
                status="ready",
                title="Recover the recent change-history check",
                reason=(
                    "GMA must know whether bidding, targets, or budgets changed "
                    "recently before judging the current performance window."
                ),
                steps=[
                    "Retry the run once to rule out a temporary Google Ads API failure.",
                    (
                        "If it still fails, open Change history in Google Ads and check "
                        "the last 14 days for budget, bid strategy, target CPA, or target ROAS edits."
                    ),
                    "Record the latest material change date and rerun this skill.",
                ],
                affected=affected,
                resolves=holds_for("CHANGE_HISTORY_UNVERIFIED"),
                completion_signal=(
                    "The runtime reads change history successfully, or an account owner "
                    "provides the latest material change date for the affected campaigns."
                ),
                follow_up_kind="review_then_rerun",
                follow_up_module_id="budget_reallocator",
                owner="google_ads_admin",
            )
        )

    if by_code["SHARED_BUDGET"]:
        affected = by_code["SHARED_BUDGET"]
        actions.append(
            _recovery_action(
                action_id="REC-REVIEW-SHARED-BUDGET",
                priority=3,
                action_type="google_ads_review",
                status="ready",
                title="Review the shared budget as one pool",
                reason=(
                    "Changing a shared budget affects every campaign attached to it, "
                    "so it cannot be treated as a campaign-only dollar move."
                ),
                steps=[
                    "Open Tools → Budgets and identify every campaign using the shared budget.",
                    "Review the pool's combined spend, outcomes, and delivery constraint.",
                    "Keep it shared and assess the pool, or separate it deliberately before rerunning.",
                ],
                affected=affected,
                resolves=holds_for("SHARED_BUDGET"),
                completion_signal=(
                    "The complete shared-budget pool is in scope, or each affected "
                    "campaign has its own independently editable budget."
                ),
                follow_up_kind="review_then_rerun",
                follow_up_module_id="budget_reallocator",
                owner="google_ads_admin",
            )
        )

    if by_code["PMAX_EVIDENCE_REQUIRED"]:
        affected = by_code["PMAX_EVIDENCE_REQUIRED"]
        actions.append(
            _recovery_action(
                action_id="REC-PMAX-EVIDENCE",
                priority=3,
                action_type="manual_review",
                status="ready",
                title="Complete the Performance Max quality check",
                reason=(
                    "Performance Max can blend brand demand, prospecting, products, "
                    "and low-quality leads; campaign-level CPA or ROAS alone is not enough."
                ),
                steps=[
                    (
                        "For ecommerce, review product-level value, feed/inventory health, "
                        "brand mix, and new-customer value. For lead generation, review "
                        "qualified-lead rate by campaign."
                    ),
                    "Separate genuine incremental outcomes from brand or low-quality volume.",
                    "Return the quality evidence and rerun the budget decision.",
                ],
                affected=affected,
                resolves=holds_for("PMAX_EVIDENCE_REQUIRED"),
                completion_signal=(
                    "Product or qualified-lead evidence, inventory context, and brand "
                    "mix have been reviewed for the affected Performance Max campaigns."
                ),
                follow_up_kind="run_named_skill",
                follow_up_module_id="instant_account_audit",
            )
        )

    if by_code["CAMPAIGN_NOT_ENABLED"]:
        affected = by_code["CAMPAIGN_NOT_ENABLED"]
        actions.append(
            _recovery_action(
                action_id="REC-RESOLVE-CAMPAIGN-STATUS",
                priority=4,
                action_type="scope_decision",
                status="needs_confirmation",
                title="Exclude paused campaigns or confirm a deliberate reactivation review",
                reason=(
                    "Paused campaigns cannot donate or receive live budget. Historical "
                    "performance can still inform the account, but not an immediate move."
                ),
                steps=[
                    "Confirm whether each paused campaign should remain paused.",
                    "Exclude campaigns that are intentionally inactive from the next budget run.",
                    (
                        "If reactivation is intended, review strategy, ads, landing page, "
                        "tracking, and budget separately before enabling it."
                    ),
                ],
                affected=affected,
                resolves=holds_for("CAMPAIGN_NOT_ENABLED"),
                completion_signal=(
                    "The next run excludes intentionally paused campaigns, or a separate "
                    "reactivation review confirms an affected campaign is safe to enable."
                ),
                follow_up_kind="review_then_rerun",
                follow_up_module_id="budget_reallocator",
            )
        )

    volume_affected = (
        by_code["LOW_CLICK_VOLUME"]
        + by_code["INSUFFICIENT_CONVERSION_VOLUME"]
        + by_code["INSUFFICIENT_ANALYSIS_WINDOW"]
    )
    if volume_affected:
        unique = {
            str(item["campaign_id"]): item for item in volume_affected
        }
        affected = list(unique.values())
        actions.append(
            _recovery_action(
                action_id="REC-COLLECT-MORE-EVIDENCE",
                priority=5,
                action_type="collect_more_data",
                status="waiting",
                title="Collect enough evidence for a defensible budget decision",
                reason=(
                    "The current window is too short or too low-volume to separate a "
                    "real opportunity from ordinary account noise."
                ),
                steps=[
                    (
                        f"Keep the affected campaigns stable and extend the completed "
                        f"window beyond {analysis_start} to {analysis_end}."
                    ),
                    (
                        "For scaling, reach the conversion floor shown in each campaign "
                        "check; for general eligibility, average at least one click per day."
                    ),
                    "Rerun when the evidence floor is met.",
                ],
                affected=affected,
                resolves=(
                    holds_for("LOW_CLICK_VOLUME")
                    + holds_for("INSUFFICIENT_CONVERSION_VOLUME")
                    + holds_for("INSUFFICIENT_ANALYSIS_WINDOW")
                ),
                completion_signal=(
                    "The selected window is at least 14 complete days and each affected "
                    "campaign meets its displayed click/conversion evidence floor."
                ),
                follow_up_kind="rerun_current_skill",
                follow_up_module_id="budget_reallocator",
                owner="gma",
            )
        )

    route_modules = {
        "Quality Score Booster": "quality_score_booster",
        "Bid Strategy Check": "bid_strategy_check",
        "Keyword Gap Finder": "keyword_gap_finder",
        "Instant Account Audit": "instant_account_audit",
    }
    route_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for gate in gates:
        for route in gate.get("routes") or []:
            route_groups[str(route)].append(gate)
    for route, affected in sorted(route_groups.items()):
        if route == "Instant Account Audit" and by_code["PMAX_EVIDENCE_REQUIRED"]:
            continue
        module_id = route_modules.get(route)
        slug = re.sub(r"[^A-Z0-9]+", "-", route.upper()).strip("-")
        actions.append(
            _recovery_action(
                action_id=f"REC-RUN-{slug}",
                priority=4,
                action_type="run_specialist",
                status="ready",
                title=f"Run {route} before moving budget",
                reason=(
                    "The delivery pattern points to a different root cause. More budget "
                    "would not solve it until that specialist check is complete."
                ),
                steps=[
                    f"Run {route} for the affected campaigns and the same date window.",
                    "Review its checks and add any supported repair tasks to the Action list.",
                    "Rerun Budget Reallocator after the root cause is addressed.",
                ],
                affected=affected,
                resolves=[],
                completion_signal=(
                    f"{route} completes and its blocking recommendation is resolved or "
                    "explicitly cleared with evidence."
                ),
                follow_up_kind="run_named_skill",
                follow_up_module_id=module_id,
            )
        )

    if not actions and include_monitor_fallback:
        actions.append(
            _recovery_action(
                action_id="REC-MONITOR-BUDGET-FIT",
                priority=5,
                action_type="monitor",
                status="waiting",
                title="Keep budgets unchanged and schedule a fresh review",
                reason=(
                    "No campaign currently qualifies as a safe donor or receiver, and "
                    "there is no unresolved safety gate that justifies an immediate fix."
                ),
                steps=[
                    "Leave the current budgets unchanged.",
                    "Monitor CPA or ROAS, conversion volume, and budget-vs-rank loss.",
                    "Rerun after 14 complete days or sooner if tracking or policy breaks.",
                ],
                affected=gates,
                resolves=[],
                completion_signal=(
                    "A new completed evidence window is available, or a material "
                    "tracking, policy, target, or delivery change occurs."
                ),
                follow_up_kind="monitor",
                follow_up_module_id="budget_reallocator",
                owner="gma",
            )
        )

    return sorted(actions, key=lambda item: (item["priority"], item["id"]))[:7]


def _action(
    *,
    action_id: str,
    priority: int,
    entity: str,
    resource_name: str,
    current_micros: int,
    proposed_micros: int,
    reason: str,
    evidence: str,
    details: str,
    applyability: str,
    expected_impact: str,
    estimate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "priority": priority,
        "severity": "high" if priority == 1 else "medium",
        "evidence_label": "Calculated from live Google Ads data",
        "entity": entity,
        "resource_name": resource_name if applyability == "applyable" else "",
        "operation_type": (
            "set_campaign_budget_amount" if applyability == "applyable" else "advisory"
        ),
        "current_value": {"amount_micros": current_micros},
        "proposed_value": {"amount_micros": proposed_micros},
        "reason": reason,
        "evidence_summary": evidence,
        "details": details,
        "expected_impact": expected_impact,
        "estimate": dict(estimate or {}),
        "risk": "medium",
        "reversible": True,
        "applyability": applyability,
        "source_skill": "budget-reallocator",
    }


def evaluate_budget_reallocation(
    snapshot: Mapping[str, Any],
    *,
    business_mode: str,
    target_cpa_micros: int | None = None,
    target_roas: float | None = None,
    outcome_quality_confirmed: bool = False,
    monthly_budget_micros: int | None = None,
    allow_net_increase: bool = False,
) -> dict[str, Any]:
    """Score every campaign and produce a bounded, deterministic move plan."""

    if business_mode not in {"lead_gen", "ecommerce"}:
        raise BudgetAnalysisError(
            "Budget Reallocator V1 requires lead_gen or ecommerce mode"
        )
    if target_cpa_micros is not None and (
        isinstance(target_cpa_micros, bool) or target_cpa_micros <= 0
    ):
        raise BudgetAnalysisError("target_cpa_micros must be a positive integer")
    if target_roas is not None and target_roas <= 0:
        raise BudgetAnalysisError("target_roas must be positive")

    analysis_start = str(snapshot["analysis_start"])
    analysis_end = str(snapshot["analysis_end"])
    days = _days(analysis_start, analysis_end)
    campaigns = list(snapshot.get("campaigns") or [])
    if not campaigns:
        raise BudgetAnalysisError("No eligible campaigns were returned")

    gates = [
        _campaign_gate(
            campaign,
            days=days,
            business_mode=business_mode,
            target_cpa_micros=target_cpa_micros,
            target_roas=target_roas,
            outcome_quality_confirmed=outcome_quality_confirmed,
        )
        for campaign in campaigns
    ]
    campaign_by_id = {str(campaign["id"]): campaign for campaign in campaigns}
    gate_by_id = {gate["campaign_id"]: gate for gate in gates}

    recipients = sorted(
        (gate for gate in gates if gate["recipient_eligible"]),
        key=lambda item: (
            item["efficiency"].get("ratio") or 999,
            -(item["search_budget_lost_impression_share"] or 0),
        ),
    )
    donors = sorted(
        (gate for gate in gates if gate["donor_eligible"]),
        key=lambda item: -(item["efficiency"].get("ratio") or 0),
    )

    recipient_capacity: dict[str, int] = {}
    for gate in recipients:
        campaign = campaign_by_id[gate["campaign_id"]]
        current = int(campaign["daily_budget_micros"])
        recipient_capacity[gate["campaign_id"]] = _round_micros(
            Decimal(current) * Decimal("0.20")
        )

    donor_allocations: dict[str, int] = defaultdict(int)
    recipient_allocations: dict[str, int] = defaultdict(int)
    for donor in donors:
        campaign = campaign_by_id[donor["campaign_id"]]
        available = _round_micros(
            Decimal(int(campaign["daily_budget_micros"])) * Decimal("0.20")
        )
        for recipient in recipients:
            recipient_id = recipient["campaign_id"]
            remaining = (
                recipient_capacity[recipient_id] - recipient_allocations[recipient_id]
            )
            if remaining <= 0:
                continue
            moved = min(available, remaining)
            donor_allocations[donor["campaign_id"]] += moved
            recipient_allocations[recipient_id] += moved
            available -= moved
            if available <= 0:
                break

    actions: list[dict[str, Any]] = []
    sequence = 1
    window = f"{analysis_start} to {analysis_end}"
    for campaign_id, reduction in donor_allocations.items():
        if reduction <= 0:
            continue
        campaign = campaign_by_id[campaign_id]
        gate = gate_by_id[campaign_id]
        current = int(campaign["daily_budget_micros"])
        proposed = current - reduction
        efficiency = gate["efficiency"]
        metric = (
            f"CPA ${_money_micros(int(efficiency['actual']))} vs "
            f"${_money_micros(int(efficiency['target']))} target"
            if business_mode == "lead_gen" and efficiency["actual"] is not None
            else f"ROAS {efficiency['actual']:.2f} vs {efficiency['target']:.2f} target"
        )
        actions.append(
            _action(
                action_id=f"BR-{sequence:03d}",
                priority=1,
                entity=f"{campaign['name']} daily budget",
                resource_name=campaign["budget_resource_name"],
                current_micros=current,
                proposed_micros=proposed,
                reason="This campaign is below the confirmed efficiency target and is the safest donor.",
                evidence=(
                    f"{metric}; ${_money_micros(int(campaign['cost_micros']))} "
                    f"spent during {window}."
                ),
                details=(
                    f"Reduce by ${_money_micros(reduction)}/day (20%). The paired "
                    "recipient increase keeps the planned account total approximately flat."
                ),
                applyability="applyable",
                expected_impact=(
                    f"Frees ${_money_micros(reduction)}/day. Estimate: about "
                    f"{(float(campaign.get('conversions') or 0) / days) * (reduction / current):.2f} "
                    "reported conversions/day may be forgone at the window average."
                ),
                estimate={
                    "label": "estimate",
                    "budget_freed_micros_per_day": reduction,
                    "reported_conversions_forgone_per_day": round(
                        (float(campaign.get("conversions") or 0) / days)
                        * (reduction / current),
                        4,
                    ),
                    "basis": f"{window} average",
                },
            )
        )
        sequence += 1

    for campaign_id, increase in recipient_allocations.items():
        if increase <= 0:
            continue
        campaign = campaign_by_id[campaign_id]
        gate = gate_by_id[campaign_id]
        current = int(campaign["daily_budget_micros"])
        proposed = current + increase
        efficiency = gate["efficiency"]
        average_cpc = int(campaign.get("average_cpc_micros") or 0)
        clicks = float(campaign.get("clicks") or 0)
        conversions = float(campaign.get("conversions") or 0)
        search_is = gate["search_impression_share"]
        lost_budget = gate["search_budget_lost_impression_share"]
        estimate: dict[str, Any] = {
            "label": "estimate",
            "budget_added_micros_per_day": increase,
            "basis": f"{window} average",
        }
        if (
            average_cpc > 0
            and clicks > 0
            and search_is is not None
            and search_is > 0.0999
            and lost_budget is not None
            and lost_budget < 0.9001
        ):
            capturable_clicks = (clicks / days) * (lost_budget / search_is)
            funded_clicks = increase / average_cpc
            added_clicks = min(capturable_clicks, funded_clicks)
            cvr = conversions / clicks
            added_conversions = added_clicks * cvr
            estimate.update(
                {
                    "added_clicks_per_day": round(added_clicks, 2),
                    "added_reported_conversions_per_day": round(added_conversions, 3),
                    "estimated_cpa_micros": (
                        round(average_cpc / cvr) if cvr > 0 else None
                    ),
                }
            )
            expected_impact = (
                f"Estimate: up to {added_clicks:.1f} additional clicks/day and "
                f"{added_conversions:.2f} reported conversions/day at the {window} "
                "average. New auctions can perform worse; recheck after 14 days."
            )
        else:
            expected_impact = (
                "Directional only: impression-share bounds or CPC/CVR evidence are "
                "insufficient for a defensible volume estimate. Recheck after 14 days."
            )
        metric = (
            f"CPA ${_money_micros(int(efficiency['actual']))} vs "
            f"${_money_micros(int(efficiency['target']))} target"
            if business_mode == "lead_gen" and efficiency["actual"] is not None
            else f"ROAS {efficiency['actual']:.2f} vs {efficiency['target']:.2f} target"
        )
        actions.append(
            _action(
                action_id=f"BR-{sequence:03d}",
                priority=1,
                entity=f"{campaign['name']} daily budget",
                resource_name=campaign["budget_resource_name"],
                current_micros=current,
                proposed_micros=proposed,
                reason="This campaign beats the confirmed target and is losing eligible searches to budget.",
                evidence=(
                    f"{metric}; {_percent(gate['search_budget_lost_impression_share'])} "
                    f"lost to budget and {_percent(gate['search_rank_lost_impression_share'])} "
                    f"lost to rank during {window}."
                ),
                details=(
                    f"Increase by ${_money_micros(increase)}/day, capped at 20%. "
                    "Recheck efficiency after 14 days before another increase."
                ),
                applyability="applyable",
                expected_impact=expected_impact,
                estimate=estimate,
            )
        )
        sequence += 1

    if recipients and not donor_allocations:
        total_daily_budget = sum(
            {
                campaign["budget_resource_name"]: int(campaign["daily_budget_micros"])
                for campaign in campaigns
            }.values()
        )
        confirmed_daily_headroom = None
        if monthly_budget_micros is not None:
            confirmed_daily_headroom = max(
                0,
                int(Decimal(monthly_budget_micros) / Decimal("30.4"))
                - total_daily_budget,
            )
        for recipient in recipients[:3]:
            campaign = campaign_by_id[recipient["campaign_id"]]
            current = int(campaign["daily_budget_micros"])
            increase = recipient_capacity[recipient["campaign_id"]]
            if confirmed_daily_headroom is not None:
                increase = min(increase, confirmed_daily_headroom)
            applyable = (
                "applyable" if allow_net_increase and increase > 0 else "advisory"
            )
            if confirmed_daily_headroom is not None:
                confirmed_daily_headroom -= increase
            if increase > 0:
                net_increase_reason = (
                    "No safe donor exists, but this profitable campaign has verified "
                    "budget headroom."
                )
                net_increase_detail = (
                    f"Net account increase of ${_money_micros(increase)}/day. "
                    "This remains advisory until the user explicitly confirms new spend."
                )
                net_increase_impact = (
                    "Directional scaling opportunity; rerun after explicit new-spend "
                    "confirmation to create an applyable step and 14-day guardrail."
                )
            else:
                net_increase_reason = (
                    "This campaign has performance headroom, but the confirmed monthly "
                    "budget leaves no room for an increase."
                )
                net_increase_detail = (
                    "The confirmed monthly budget has no remaining headroom, so no "
                    "budget increase is selectable."
                )
                net_increase_impact = (
                    "No Google Ads change is proposed. Reallocate from a safe donor or "
                    "confirm a higher monthly budget first."
                )
            actions.append(
                _action(
                    action_id=f"BR-{sequence:03d}",
                    priority=2,
                    entity=f"{campaign['name']} daily budget",
                    resource_name=campaign["budget_resource_name"],
                    current_micros=current,
                    proposed_micros=current + increase,
                    reason=net_increase_reason,
                    evidence=(
                        f"{_percent(recipient['search_budget_lost_impression_share'])} "
                        f"of eligible searches lost to budget during {window}."
                    ),
                    details=(net_increase_detail),
                    applyability=applyable,
                    expected_impact=net_increase_impact,
                    estimate={
                        "label": "estimate",
                        "net_budget_increase_micros_per_day": increase,
                        "basis": f"{window} budget-headroom evidence",
                    },
                )
            )
            sequence += 1

    spend = sum(int(campaign.get("cost_micros") or 0) for campaign in campaigns)
    total_daily_budget = sum(
        {
            campaign["budget_resource_name"]: int(campaign["daily_budget_micros"])
            for campaign in campaigns
        }.values()
    )
    projected_monthly = total_daily_budget * Decimal("30.4")
    structural = {
        "current_daily_budget_micros": total_daily_budget,
        "monthly_cap_micros": int(projected_monthly),
        "selected_window_spend_micros": spend,
        "confirmed_monthly_budget_micros": monthly_budget_micros,
        "within_confirmed_monthly_budget": (
            None
            if monthly_budget_micros is None
            else int(projected_monthly) <= monthly_budget_micros
        ),
    }

    holds = sorted({hold for gate in gates for hold in gate["holds"]})
    applyable_count = sum(action["applyability"] == "applyable" for action in actions)
    recovery_actions = _recovery_actions(
        gates,
        analysis_start=analysis_start,
        analysis_end=analysis_end,
        business_mode=business_mode,
        include_monitor_fallback=not actions,
    )
    if applyable_count:
        status = "recommendations_ready"
        conclusion = (
            f"{applyable_count} exact budget changes passed the GMA eligibility gates."
        )
    elif holds:
        status = "hold"
        conclusion = "No budget change is safe yet because one or more required gates remain unresolved."
    else:
        status = "no_change"
        conclusion = "No campaign pair currently supports a safe budget move."

    return {
        "contract_version": "gma-skill-run/1.0",
        "skill": {
            "number": 12,
            "slug": "budget-reallocator",
            "name": "Budget Reallocator",
        },
        "status": status,
        "conclusion": conclusion,
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
        "analysis_days": days,
        "business_mode": business_mode,
        "campaigns_analyzed": len(campaigns),
        "campaign_results": gates,
        "recommendations": actions,
        "recovery_actions": recovery_actions,
        "applyable_action_ids": [
            action["id"] for action in actions if action["applyability"] == "applyable"
        ],
        "holds": holds,
        "coverage_gaps": list(snapshot.get("coverage_gaps") or []),
        "structural_budget_check": structural,
        "calculation_policy": {
            "recipient_increase_cap": "20% per apply",
            "donor_reduction": "20% in this plan",
            "recheck_after_days": 14,
            "daily_to_monthly_multiplier": 30.4,
        },
    }
