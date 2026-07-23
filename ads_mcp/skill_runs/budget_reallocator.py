"""Deterministic recommendation engine for GMA Skill 12.

The model may explain this result, but it must not invent or recalculate the
eligibility gates, dollar moves, action IDs, or applyability state.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
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
    routes: list[str] = []
    if status != "ENABLED":
        holds.append(
            f"Campaign is {status.replace('_', ' ').lower()}; only enabled campaigns "
            "can donate or receive budget"
        )
    if days < 14:
        holds.append("Fewer than 14 days of evidence")
    if not outcome_quality_confirmed:
        holds.append("Outcome quality has not been confirmed")
    if not goal_verified:
        holds.append("Campaign-effective conversion goals could not be verified")
    if not bool(campaign.get("change_history_verified", True)):
        holds.append("Recent bid and budget change history could not be verified")
    if efficiency["status"] == "target_missing":
        holds.append("Business CPA or ROAS target is missing")
    if recent_change:
        holds.append(f"Recent material change on {recent_change}")
    if shared:
        holds.append("Campaign uses a shared budget; assess the pool before editing")
    if clicks_per_day < 1:
        holds.append("Campaign averages fewer than 1 click per day")
    if channel_type == "PERFORMANCE_MAX":
        holds.append(
            "Performance Max needs product or lead-quality, inventory, and brand-mix "
            "evidence before a budget move"
        )
        routes.append("Instant Account Audit")
    elif channel_type not in {"SEARCH", "SHOPPING"}:
        holds.append(
            f"{channel_type.replace('_', ' ').title()} needs its own channel-specific "
            "budget review"
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
        holds.append(
            f"Only {conversions:g} conversions in the window; "
            f"{scaling_volume_floor} required for this scaling decision"
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
        "routes": sorted(set(routes)),
        "clicks_per_day": round(clicks_per_day, 2),
        "conversion_volume": conversions,
        "scaling_volume_floor": scaling_volume_floor,
        "bidding_strategy_type": bidding_strategy,
        "search_impression_share": search_is,
        "search_budget_lost_impression_share": lost_budget,
        "search_rank_lost_impression_share": lost_rank,
    }


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
