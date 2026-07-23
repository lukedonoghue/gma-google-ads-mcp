"""Deterministic Red-Flag Radar scoring.

The model may explain this result, but it does not choose thresholds, severity,
routes, or action IDs.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence


class RedFlagAnalysisError(ValueError):
    """Safe input or evidence failure for Red-Flag Radar."""


SEVERITY_ORDER = {
    "critical": 0,
    "warning": 1,
    "info": 2,
    "win": 3,
    "healthy": 4,
    "not_checked": 5,
}

LEAD_QUALITY_TERMS = (
    "lead",
    "call",
    "book",
    "appointment",
    "contact",
    "quote",
    "form",
    "purchase",
    "sale",
)
SUSPECT_LEAD_TERMS = (
    "page view",
    "youtube",
    "channel subscription",
    "follow-on",
    "local action",
    "directions",
    "engaged",
)


def _days(start: str, end: str) -> int:
    try:
        parsed_start = date.fromisoformat(start)
        parsed_end = date.fromisoformat(end)
    except ValueError as error:
        raise RedFlagAnalysisError("Analysis dates must be YYYY-MM-DD") from error
    days = (parsed_end - parsed_start).days + 1
    if days < 14:
        raise RedFlagAnalysisError(
            "Red-Flag Radar needs at least 14 complete days for a two-week comparison"
        )
    return days


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RedFlagAnalysisError("Metric values must be numeric")
    return float(value)


def _metric_period(value: Mapping[str, Any] | None) -> dict[str, float]:
    raw = value or {}
    return {
        "cost_micros": _number(raw.get("cost_micros")),
        "conversions": _number(raw.get("conversions")),
        "conversions_value": _number(raw.get("conversions_value")),
        "clicks": _number(raw.get("clicks")),
        "impressions": _number(raw.get("impressions")),
        "search_impression_share": _number(
            raw.get("search_impression_share")
        ),
        "search_budget_lost_impression_share": _number(
            raw.get("search_budget_lost_impression_share")
        ),
    }


def _ratio_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / abs(previous)


def _rate(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _cpa(metrics: Mapping[str, float]) -> float | None:
    return _rate(metrics["cost_micros"], metrics["conversions"])


def _roas(metrics: Mapping[str, float]) -> float | None:
    return _rate(metrics["conversions_value"], metrics["cost_micros"])


def _display_change(value: float | None) -> str:
    if value is None:
        return "not comparable"
    return f"{value * 100:+.1f}%"


def _money(micros: float, currency: str) -> str:
    return f"{currency} {micros / 1_000_000:,.2f}"


def _check(
    *,
    check_id: str,
    campaign: Mapping[str, Any],
    criterion: str,
    status: str,
    evidence: str,
    why_it_matters: str,
    decision: str,
    next_step: str,
    metrics: Mapping[str, Any] | None = None,
    source: str = "live_google_ads",
) -> dict[str, Any]:
    return {
        "id": check_id,
        "campaign_id": str(campaign["id"]),
        "campaign_name": str(campaign["name"]),
        "criterion": criterion,
        "status": status,
        "evidence": evidence,
        "why_it_matters": why_it_matters,
        "decision": decision,
        "next_step": next_step,
        "metrics": dict(metrics or {}),
        "source": source,
    }


def _candidate(
    *,
    key: str,
    campaign: Mapping[str, Any],
    priority: int,
    severity: str,
    title: str,
    reason: str,
    evidence: str,
    details: str,
    expected_impact: str,
    applyability: str = "task",
    route: str | None = None,
) -> dict[str, Any]:
    return {
        "_key": key,
        "_campaign_id": str(campaign["id"]),
        "priority": priority,
        "severity": severity,
        "evidence_label": "Calculated from live Google Ads data",
        "entity": title,
        "resource_name": "",
        "operation_type": "advisory",
        "current_value": {},
        "proposed_value": {},
        "reason": reason,
        "evidence_summary": evidence,
        "details": details,
        "expected_impact": expected_impact,
        "estimate": {"label": "directional", "route": route},
        "risk": "low",
        "reversible": True,
        "applyability": applyability,
        "source_skill": "red-flag-radar",
    }


def _recovery(
    *,
    recovery_id: str,
    priority: int,
    campaign: Mapping[str, Any],
    action_type: str,
    status: str,
    title: str,
    reason: str,
    steps: Sequence[str],
    resolves: Sequence[str],
    completion_signal: str,
    owner: str,
    follow_up_kind: str = "rerun_current_skill",
    follow_up_module_id: str | None = "red_flag_radar",
    not_before: str | None = None,
) -> dict[str, Any]:
    return {
        "id": recovery_id,
        "priority": priority,
        "type": action_type,
        "status": status,
        "title": title,
        "reason": reason,
        "steps": list(steps),
        "applies_to": [
            {
                "campaign_id": str(campaign["id"]),
                "campaign_name": str(campaign["name"]),
            }
        ],
        "resolves": list(resolves),
        "completion_signal": completion_signal,
        "owner": owner,
        "follow_up": {
            "kind": follow_up_kind,
            "module_id": follow_up_module_id,
            "not_before": not_before,
        },
        "selectable": True,
    }


def _policy_result(
    campaign: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    policy = campaign.get("policy") or {}
    if not policy.get("verified"):
        check = _check(
            check_id=f"RF-POLICY-{campaign['id']}",
            campaign=campaign,
            criterion="Active ad policy eligibility",
            status="not_checked",
            evidence="Google Ads did not return a reliable active-ad policy summary.",
            why_it_matters="A campaign cannot serve normally when its active ads are disapproved or restricted.",
            decision="Policy status remains unverified.",
            next_step="Open Ads and assets, filter to this campaign, and resolve any disapproved or limited ads.",
            source="unavailable",
        )
        recovery = _recovery(
            recovery_id=f"REC-POLICY-{campaign['id']}",
            priority=1,
            campaign=campaign,
            action_type="google_ads_review",
            status="ready",
            title=f"Verify active-ad policy status for {campaign['name']}",
            reason="The API policy check was unavailable, so serving eligibility cannot be assumed.",
            steps=[
                "Open Google Ads → Campaigns → Ads for this campaign.",
                "Filter Policy details to Disapproved and Eligible (limited).",
                "Resolve or appeal affected ads, then rerun Red-Flag Radar.",
            ],
            resolves=["Active ad policy eligibility was not checked"],
            completion_signal="Every active ad has a reviewed policy status and any disapprovals are resolved.",
            owner="google_ads_admin",
        )
        return check, None, recovery

    disapproved = int(policy.get("disapproved") or 0)
    limited = int(policy.get("limited") or 0)
    active = int(policy.get("active_ads") or 0)
    if active == 0 and str(campaign.get("status")) == "ENABLED":
        status = "critical"
        decision = "The enabled campaign has no enabled ads returned by Google Ads."
        next_step = "Create or enable at least one eligible ad before judging campaign performance."
        candidate = _candidate(
            key="no-active-ads",
            campaign=campaign,
            priority=1,
            severity="critical",
            title=f"Restore an active ad in {campaign['name']}",
            reason="An enabled campaign cannot deliver normally without an enabled, eligible ad.",
            evidence="Google Ads returned zero enabled ads for this campaign.",
            details="Open Ads for the campaign, determine whether ads are paused, removed, or missing, then create or enable an eligible ad.",
            expected_impact="Restores the campaign's ability to enter auctions once an ad is eligible.",
        )
    elif disapproved:
        status = "critical"
        decision = f"{disapproved} active ad(s) are disapproved."
        next_step = "Review the policy reasons today and replace, edit, or appeal each affected ad."
        candidate = _candidate(
            key="policy-disapproved",
            campaign=campaign,
            priority=1,
            severity="critical",
            title=f"Resolve {disapproved} disapproved ad(s) in {campaign['name']}",
            reason="Disapproved ads cannot serve and can cause an abrupt delivery loss.",
            evidence=f"{disapproved} of {active} active ads are disapproved.",
            details="Open the affected ads, inspect Policy details, then edit, replace, or appeal. Rerun the radar after Google finishes its review.",
            expected_impact="Restores eligibility where policy is the delivery blocker; no performance uplift is guaranteed.",
        )
    elif limited:
        status = "warning"
        decision = f"{limited} active ad(s) are eligible with limitations."
        next_step = "Review the limitation and decide whether a compliant replacement can remove the restriction."
        candidate = _candidate(
            key="policy-limited",
            campaign=campaign,
            priority=2,
            severity="warning",
            title=f"Review {limited} policy-limited ad(s) in {campaign['name']}",
            reason="Policy limitations can reduce reach even when ads remain eligible.",
            evidence=f"{limited} of {active} active ads are eligible with limitations.",
            details="Inspect each policy topic and create a compliant replacement when the restriction matters to the campaign.",
            expected_impact="May recover eligible reach if the restriction can be resolved.",
        )
    else:
        status = "healthy"
        decision = "No active disapproved or limited ads were found."
        next_step = "No policy action is needed."
        candidate = None
    return (
        _check(
            check_id=f"RF-POLICY-{campaign['id']}",
            campaign=campaign,
            criterion="Active ad policy eligibility",
            status=status,
            evidence=(
                f"{active} active ads checked: {disapproved} disapproved and "
                f"{limited} eligible with limitations."
            ),
            why_it_matters="Policy restrictions can stop or reduce delivery before performance metrics explain the change.",
            decision=decision,
            next_step=next_step,
            metrics={
                "active_ads": active,
                "disapproved_ads": disapproved,
                "limited_ads": limited,
            },
        ),
        candidate,
        None,
    )


def _goal_result(
    campaign: Mapping[str, Any],
    business_mode: str,
    outcome_quality_confirmed: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    actions = [str(value) for value in campaign.get("effective_conversion_actions") or []]
    verified = bool(campaign.get("goal_scope_verified")) and bool(actions)
    scope = str(campaign.get("goal_scope") or "unable_to_verify")
    if not verified:
        check = _check(
            check_id=f"RF-GOALS-{campaign['id']}",
            campaign=campaign,
            criterion="Campaign-effective conversion goals",
            status="not_checked",
            evidence="The campaign's effective primary conversion actions could not be resolved.",
            why_it_matters="Bidding and reported CPA/ROAS are not trustworthy until the campaign's actual optimisation goals are known.",
            decision="Goal scope is unverified.",
            next_step="Review the campaign's Goals settings, record the effective primary actions, and rerun.",
            source="unavailable",
        )
        recovery = _recovery(
            recovery_id=f"REC-GOALS-{campaign['id']}",
            priority=1,
            campaign=campaign,
            action_type="google_ads_review",
            status="ready",
            title=f"Verify the conversion goals used by {campaign['name']}",
            reason="The radar could not prove which conversions control bidding and reporting for this campaign.",
            steps=[
                "Open Google Ads → Campaign settings → Goals.",
                "Record whether this campaign uses account-default, campaign-specific, or custom goals.",
                "Confirm which enabled primary actions represent genuine business outcomes, then rerun.",
            ],
            resolves=["Campaign-effective conversion goals were not verified"],
            completion_signal="The effective primary conversion actions and their goal scope are confirmed.",
            owner="google_ads_admin",
        )
        return check, None, recovery

    joined = ", ".join(actions)
    lowered = joined.casefold()
    suspect = [term for term in SUSPECT_LEAD_TERMS if term in lowered]
    has_outcome_term = any(term in lowered for term in LEAD_QUALITY_TERMS)
    needs_confirmation = (
        business_mode == "lead_gen"
        and not outcome_quality_confirmed
        and (bool(suspect) or not has_outcome_term)
    )
    if needs_confirmation:
        status = "warning"
        decision = "The selected goals may include actions that are not genuine enquiries."
        next_step = "Confirm which actions represent genuine, non-duplicated leads before trusting CPA or automated bidding."
        candidate = _candidate(
            key="goal-quality",
            campaign=campaign,
            priority=1,
            severity="warning",
            title=f"Confirm what counts as a lead in {campaign['name']}",
            reason="Automated bidding can optimise toward cheap but low-value actions when the effective goal set is mixed.",
            evidence=f"{scope.replace('_', ' ')} goals: {joined}.",
            details="Classify each effective primary action as a genuine enquiry, supporting micro-conversion, or irrelevant action. Keep genuine outcomes primary; review the rest before any bid or budget change.",
            expected_impact="Creates a trustworthy CPA and prevents bidding toward the wrong result.",
        )
        recovery = _recovery(
            recovery_id=f"REC-OUTCOME-{campaign['id']}",
            priority=1,
            campaign=campaign,
            action_type="user_confirmation",
            status="needs_confirmation",
            title=f"Confirm genuine lead actions for {campaign['name']}",
            reason="The radar can name the effective actions but cannot verify backend lead quality by itself.",
            steps=[
                f"Review these effective actions: {joined}.",
                "Confirm which actions create genuine, non-duplicated enquiries or bookings.",
                "Return that confirmation to GMA so the same scope can be rerun.",
            ],
            resolves=["Backend outcome quality has not been confirmed"],
            completion_signal="The account owner confirms the genuine lead actions and rejects any micro-conversions.",
            owner="account_owner",
            follow_up_kind="review_then_rerun",
        )
    else:
        status = "healthy"
        decision = f"Effective goals were resolved at {scope.replace('_', ' ')} scope."
        next_step = (
            "No goal-scope action is needed."
            if business_mode == "ecommerce" or outcome_quality_confirmed
            else "Confirm backend outcome quality when lead disposition data becomes available."
        )
        candidate = None
        recovery = None
    return (
        _check(
            check_id=f"RF-GOALS-{campaign['id']}",
            campaign=campaign,
            criterion="Campaign-effective conversion goals",
            status=status,
            evidence=f"{scope.replace('_', ' ').title()}: {joined}.",
            why_it_matters="These are the actions Google uses to report and optimise campaign results.",
            decision=decision,
            next_step=next_step,
            metrics={"goal_scope": scope, "effective_action_count": len(actions)},
        ),
        candidate,
        recovery,
    )


def _trend_result(
    campaign: Mapping[str, Any],
    business_mode: str,
    currency: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    latest = _metric_period(campaign.get("latest_period"))
    previous = _metric_period(campaign.get("previous_period"))
    volume_ok = min(latest["clicks"], previous["clicks"]) >= 30 or min(
        latest["conversions"], previous["conversions"]
    ) >= 10
    if not volume_ok:
        check = _check(
            check_id=f"RF-TREND-{campaign['id']}",
            campaign=campaign,
            criterion="Latest complete 7 days versus previous 7 days",
            status="not_checked",
            evidence=(
                f"Smaller week: {min(latest['clicks'], previous['clicks']):.0f} clicks "
                f"and {min(latest['conversions'], previous['conversions']):.1f} conversions."
            ),
            why_it_matters="Low-volume weekly swings are too noisy to label as a reliable red flag.",
            decision="The weekly comparison did not meet the GMA evidence floor.",
            next_step="Keep monitoring until both comparison weeks have at least 30 clicks or 10 conversions.",
            metrics={"evidence_floor_met": False},
        )
        recovery = _recovery(
            recovery_id=f"REC-VOLUME-{campaign['id']}",
            priority=5,
            campaign=campaign,
            action_type="collect_more_data",
            status="waiting",
            title=f"Collect enough evidence for {campaign['name']}",
            reason="The two complete comparison weeks are below the GMA volume floor.",
            steps=[
                "Leave the campaign unchanged unless a separate policy or tracking issue requires action.",
                "Wait until both comparison weeks contain at least 30 clicks or 10 conversions.",
                "Rerun Red-Flag Radar on the same campaign scope.",
            ],
            resolves=["Weekly trend evidence floor was not met"],
            completion_signal="Both comparison weeks meet the 30-click or 10-conversion floor.",
            owner="gma",
            follow_up_kind="monitor",
        )
        return check, None, recovery

    spend_change = _ratio_change(latest["cost_micros"], previous["cost_micros"])
    conversion_change = _ratio_change(
        latest["conversions"], previous["conversions"]
    )
    value_change = _ratio_change(
        latest["conversions_value"], previous["conversions_value"]
    )
    click_change = _ratio_change(latest["clicks"], previous["clicks"])
    impression_change = _ratio_change(
        latest["impressions"], previous["impressions"]
    )
    ctr_change = _ratio_change(
        _rate(latest["clicks"], latest["impressions"]) or 0,
        _rate(previous["clicks"], previous["impressions"]) or 0,
    )
    cvr_change = _ratio_change(
        _rate(latest["conversions"], latest["clicks"]) or 0,
        _rate(previous["conversions"], previous["clicks"]) or 0,
    )
    latest_efficiency = (
        _cpa(latest) if business_mode == "lead_gen" else _roas(latest)
    )
    previous_efficiency = (
        _cpa(previous) if business_mode == "lead_gen" else _roas(previous)
    )
    efficiency_change = (
        _ratio_change(latest_efficiency, previous_efficiency)
        if latest_efficiency is not None and previous_efficiency is not None
        else None
    )
    efficiency_bad = (
        efficiency_change is not None
        and (
            efficiency_change >= 0.25
            if business_mode == "lead_gen"
            else efficiency_change <= -0.25
        )
    )
    tracking_suspect = (
        conversion_change is not None
        and conversion_change <= -0.40
        and (click_change is None or click_change > -0.15)
        and (impression_change is None or impression_change > -0.15)
    )
    triggered = []
    if spend_change is not None and abs(spend_change) >= 0.20:
        triggered.append(f"spend {_display_change(spend_change)}")
    if conversion_change is not None and abs(conversion_change) >= 0.20:
        triggered.append(f"conversions {_display_change(conversion_change)}")
    if value_change is not None and abs(value_change) >= 0.20:
        triggered.append(f"value {_display_change(value_change)}")
    if efficiency_bad:
        label = "CPA" if business_mode == "lead_gen" else "ROAS"
        triggered.append(f"{label} {_display_change(efficiency_change)}")
    if cvr_change is not None and abs(cvr_change) >= 0.15:
        triggered.append(f"conversion rate {_display_change(cvr_change)}")
    if ctr_change is not None and abs(ctr_change) >= 0.20:
        triggered.append(f"CTR {_display_change(ctr_change)}")

    recent_change = campaign.get("recent_material_change_at")
    if tracking_suspect:
        severity = "critical"
        decision = "A conversion-tracking break is plausible."
        next_step = "Verify conversion actions, tags, imports, and goal settings before changing bids or budget."
        title = f"Investigate a possible tracking break in {campaign['name']}"
        reason = "Reported conversions fell sharply without a matching traffic decline."
        expected = "Restores confidence in reported performance before optimisation decisions are made."
    elif efficiency_bad and spend_change is not None and spend_change >= 0.20:
        severity = "critical"
        decision = "Spend rose while efficiency deteriorated materially."
        next_step = "Inspect recent changes and the metric tree today; hold scaling until the cause is understood."
        title = f"Investigate rising spend and weaker efficiency in {campaign['name']}"
        reason = "The campaign spent materially more while CPA/ROAS moved in the wrong direction."
        expected = "Prevents further inefficient scaling while the cause is isolated."
    elif triggered:
        unfavorable = (
            (conversion_change is not None and conversion_change <= -0.20)
            or efficiency_bad
            or (cvr_change is not None and cvr_change <= -0.15)
            or (ctr_change is not None and ctr_change <= -0.20)
        )
        favorable = (
            (conversion_change is not None and conversion_change >= 0.20)
            and not efficiency_bad
        )
        if unfavorable:
            severity = "warning"
            decision = "A material unfavorable weekly movement needs review."
            next_step = "Inspect the metric tree and recent changes; do not assume the first visible metric is the cause."
            title = f"Review the weekly decline in {campaign['name']}"
            reason = "One or more GMA weekly alert thresholds moved unfavorably."
            expected = "Identifies the controllable cause before a campaign setting is changed."
        elif favorable:
            severity = "win"
            decision = "A favorable weekly movement cleared the GMA evidence floor."
            next_step = "Identify what changed and preserve the winning element before scaling."
            title = f"Document the weekly win in {campaign['name']}"
            reason = "Conversions improved materially without an equivalent efficiency warning."
            expected = "Makes the repeatable part of the improvement visible to the team."
        else:
            severity = "info"
            decision = "A material movement occurred but is not yet an actionable loss."
            next_step = "Monitor the next complete week and investigate if the movement persists."
            title = f"Monitor the weekly movement in {campaign['name']}"
            reason = "The campaign crossed a GMA movement threshold without a clear unfavorable efficiency signal."
            expected = "Creates a dated watch item without overreacting to one week."
    else:
        severity = "healthy"
        decision = "No GMA weekly alert threshold was crossed."
        next_step = "No trend action is needed; rerun after the next complete week."
        title = ""
        reason = ""
        expected = ""

    if recent_change and severity in {"critical", "warning"}:
        severity = "warning" if severity == "critical" else "info"
        decision += f" A material account change on {recent_change} is a plausible explanation."
        next_step = "Allow the change a judgeable window, then rerun before making another material change."

    evidence = (
        f"Latest versus previous complete week: spend {_display_change(spend_change)}, "
        f"conversions {_display_change(conversion_change)}, "
        f"CTR {_display_change(ctr_change)}, CVR {_display_change(cvr_change)}."
    )
    candidate = None
    if severity != "healthy":
        candidate = _candidate(
            key="weekly-trend",
            campaign=campaign,
            priority=1 if severity == "critical" else 2 if severity == "warning" else 4,
            severity=severity,
            title=title,
            reason=reason,
            evidence=evidence,
            details=next_step,
            expected_impact=expected,
            applyability="monitor" if severity in {"info", "win"} else "task",
        )
    metrics = {
        "evidence_floor_met": True,
        "spend_change": spend_change,
        "conversion_change": conversion_change,
        "value_change": value_change,
        "click_change": click_change,
        "impression_change": impression_change,
        "ctr_change": ctr_change,
        "cvr_change": cvr_change,
        "efficiency_change": efficiency_change,
        "latest_spend": _money(latest["cost_micros"], currency),
        "previous_spend": _money(previous["cost_micros"], currency),
    }
    return (
        _check(
            check_id=f"RF-TREND-{campaign['id']}",
            campaign=campaign,
            criterion="Latest complete 7 days versus previous 7 days",
            status=severity,
            evidence=evidence,
            why_it_matters="The radar looks for material movement, then checks whether traffic, engagement, conversion, or efficiency explains it.",
            decision=decision,
            next_step=next_step,
            metrics=metrics,
        ),
        candidate,
        None,
    )


def _budget_result(
    campaign: Mapping[str, Any],
    business_mode: str,
    target_cpa_micros: int | None,
    target_roas: float | None,
    outcome_quality_confirmed: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    lost = campaign.get("search_budget_lost_impression_share")
    cost = _number(campaign.get("cost_micros"))
    conversions = _number(campaign.get("conversions"))
    value = _number(campaign.get("conversions_value"))
    target_available = (
        target_cpa_micros is not None
        if business_mode == "lead_gen"
        else target_roas is not None
    )
    efficiency_pass = False
    actual: float | None = None
    if business_mode == "lead_gen" and conversions > 0:
        actual = cost / conversions
        efficiency_pass = (
            target_cpa_micros is not None and actual <= target_cpa_micros
        )
    elif business_mode == "ecommerce" and cost > 0:
        actual = value / cost
        efficiency_pass = target_roas is not None and actual >= target_roas
    winner = (
        target_available
        and outcome_quality_confirmed
        and efficiency_pass
        and isinstance(lost, (int, float))
        and lost > 0.10
    )
    if winner:
        check_status = "warning"
        decision = "This campaign beats the confirmed goal and is losing eligible searches to budget."
        next_step = "Run Budget Reallocator to test whether a capped increase or reallocation passes every safety gate."
        candidate = _candidate(
            key="budget-capped-winner",
            campaign=campaign,
            priority=2,
            severity="warning",
            title=f"Review budget headroom for {campaign['name']}",
            reason="A goal-beating campaign is losing more than 10% of eligible Search impressions to budget.",
            evidence=f"Search budget-lost impression share is {float(lost) * 100:.1f}%.",
            details="Route this campaign to Budget Reallocator. Red-Flag Radar detects the opportunity but does not prescribe the budget amount.",
            expected_impact="May recover profitable reach if the specialist budget gates also pass.",
            route="budget_reallocator",
        )
    elif not target_available or not outcome_quality_confirmed:
        check_status = "info"
        decision = "Budget headroom was observed but not classified as a winner without a confirmed goal and outcome quality."
        next_step = "Confirm the business target and genuine outcomes before treating lost impression share as a scaling signal."
        candidate = None
    else:
        check_status = "healthy"
        decision = "No goal-beating, budget-capped winner was detected."
        next_step = "No budget route is needed from this check."
        candidate = None
    return (
        _check(
            check_id=f"RF-BUDGET-{campaign['id']}",
            campaign=campaign,
            criterion="Budget-capped winner",
            status=check_status,
            evidence=(
                "Search budget-lost impression share "
                + (
                    f"{float(lost) * 100:.1f}%."
                    if isinstance(lost, (int, float))
                    else "is not available for this campaign type."
                )
            ),
            why_it_matters="A proven campaign losing eligible searches to budget may deserve a controlled reallocation review.",
            decision=decision,
            next_step=next_step,
            metrics={
                "target_available": target_available,
                "outcome_quality_confirmed": outcome_quality_confirmed,
                "actual_efficiency": actual,
                "search_budget_lost_impression_share": lost,
            },
        ),
        candidate,
    )


def evaluate_red_flag_radar(
    snapshot: Mapping[str, Any],
    *,
    business_mode: str,
    target_cpa_micros: int | None = None,
    target_roas: float | None = None,
    outcome_quality_confirmed: bool = False,
) -> dict[str, Any]:
    """Run the fixed GMA radar against a complete server-built snapshot."""

    if business_mode not in {"lead_gen", "ecommerce"}:
        raise RedFlagAnalysisError(
            "Red-Flag Radar V1 requires lead_gen or ecommerce mode"
        )
    _days(str(snapshot["analysis_start"]), str(snapshot["analysis_end"]))
    campaigns = list(snapshot.get("campaigns") or [])
    if not campaigns:
        raise RedFlagAnalysisError("No campaigns were returned for this scope")

    currency = str(snapshot.get("currency") or "")
    checks: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    for campaign in campaigns:
        policy_check, policy_candidate, policy_recovery = _policy_result(campaign)
        goal_check, goal_candidate, goal_recovery = _goal_result(
            campaign, business_mode, outcome_quality_confirmed
        )
        trend_check, trend_candidate, trend_recovery = _trend_result(
            campaign, business_mode, currency
        )
        budget_check, budget_candidate = _budget_result(
            campaign,
            business_mode,
            target_cpa_micros,
            target_roas,
            outcome_quality_confirmed,
        )
        checks.extend([policy_check, goal_check, trend_check, budget_check])
        candidates.extend(
            item
            for item in (
                policy_candidate,
                goal_candidate,
                trend_candidate,
                budget_candidate,
            )
            if item is not None
        )
        recoveries.extend(
            item
            for item in (policy_recovery, goal_recovery, trend_recovery)
            if item is not None
        )

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate.pop("_campaign_id"), candidate.pop("_key"))
        existing = deduped.get(key)
        if existing is None or (
            candidate["priority"],
            SEVERITY_ORDER[candidate["severity"]],
        ) < (
            existing["priority"],
            SEVERITY_ORDER[existing["severity"]],
        ):
            deduped[key] = candidate
    ordered = sorted(
        deduped.values(),
        key=lambda item: (
            item["priority"],
            SEVERITY_ORDER[item["severity"]],
            item["entity"].casefold(),
        ),
    )[:7]
    recommendations = []
    for index, item in enumerate(ordered, start=1):
        recommendations.append({"id": f"RF-{index:03d}", **item})

    recovery_by_id = {item["id"]: item for item in recoveries}
    recovery_actions = sorted(
        recovery_by_id.values(), key=lambda item: (item["priority"], item["id"])
    )[:7]
    severe = sum(
        check["status"] in {"critical", "warning"} for check in checks
    )
    wins = sum(check["status"] == "win" for check in checks)
    if severe:
        status = "findings_ready"
        conclusion = (
            f"{severe} material warning(s) need review; "
            f"{len(recommendations)} action(s) were prioritised."
        )
    elif recommendations:
        status = "monitor"
        conclusion = (
            "No urgent red flag was found; the radar created monitor or win-review actions."
        )
    else:
        status = "no_change"
        conclusion = "No material red flag crossed the GMA evidence thresholds."

    core_gaps = [
        str(item)
        for item in snapshot.get("core_coverage_gaps") or []
    ]
    limitations = [
        str(item)
        for item in snapshot.get("coverage_gaps") or []
    ]
    if core_gaps:
        status = "partial"
    return {
        "contract_version": "gma-skill-run/1.0",
        "skill": {
            "number": 2,
            "slug": "red-flag-radar",
            "name": "Red-Flag Radar",
        },
        "status": status,
        "conclusion": conclusion,
        "analysis_start": str(snapshot["analysis_start"]),
        "analysis_end": str(snapshot["analysis_end"]),
        "business_mode": business_mode,
        "campaigns_analyzed": len(campaigns),
        "checks": checks,
        "recommendations": recommendations,
        "recovery_actions": recovery_actions,
        "holds": core_gaps,
        "coverage_gaps": list(dict.fromkeys(core_gaps + limitations)),
        "assessment_details": {
            "critical_or_warning_checks": severe,
            "wins": wins,
            "criteria_checked": len(checks),
            "action_limit": 7,
        },
    }
