"""Deterministic judgment engine for GMA Skill 6 — Quality Score Booster.

The model may explain the canonical result, but it never calculates weighted
Quality Score, assigns priority, chooses the failing component, or invents a
repair route.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from typing import Any, Mapping, Sequence


class QualityScoreAnalysisError(ValueError):
    """Safe input or evidence failure for Quality Score Booster."""


COMPONENTS = (
    ("ad_relevance", "Ad relevance"),
    ("expected_ctr", "Expected click-through rate"),
    ("landing_page_experience", "Landing-page experience"),
)
COMPONENT_VALUES = {
    "ABOVE_AVERAGE",
    "AVERAGE",
    "BELOW_AVERAGE",
    "UNKNOWN",
    "UNSPECIFIED",
}
ROUTES = {
    "ad_relevance": {
        "module_id": "ad_copy_analyzer",
        "module_name": "Skill 10 — Ad-Copy Analyzer",
        "recheck_days": 14,
        "task": "Check whether the ad actually matches this keyword theme",
        "reason": (
            "Google rates ad relevance Below Average. Ad-Copy Analyzer must "
            "inspect the live responsive-search-ad assets before any copy or "
            "structure change is proposed."
        ),
    },
    "expected_ctr": {
        "module_id": "ad_copy_analyzer",
        "module_name": "Skill 10 — Ad-Copy Analyzer",
        "recheck_days": 21,
        "task": "Diagnose why this keyword is expected to attract fewer clicks",
        "reason": (
            "Google rates expected click-through rate Below Average. "
            "Ad-Copy Analyzer must assess the offer, message, and live ad "
            "assets; Quality Score Booster does not guess replacement copy."
        ),
    },
    "landing_page_experience": {
        "module_id": "landing_page_cro_audit",
        "module_name": "Skill 11 — Landing-Page CRO Audit",
        "recheck_days": 30,
        "task": "Inspect the landing page for this keyword theme",
        "reason": (
            "Google rates landing-page experience Below Average. A page "
            "inspection is required before naming the cause or proposing a "
            "page change."
        ),
    },
    "manual_review": {
        "module_id": None,
        "module_name": "Google Ads specialist review",
        "recheck_days": 14,
        "task": "Review the unexplained low Quality Score",
        "reason": (
            "The visible Quality Score is low but none of Google's three "
            "components is Below Average. A human should check account "
            "maturity, keyword history, matching, and recent changes without "
            "inventing a component failure."
        ),
    },
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "best",
    "for",
    "in",
    "me",
    "near",
    "of",
    "the",
    "to",
    "with",
}


def _normalise(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _days(start: str, end: str) -> int:
    try:
        parsed_start = date.fromisoformat(start)
        parsed_end = date.fromisoformat(end)
    except ValueError as error:
        raise QualityScoreAnalysisError(
            "Analysis dates must be YYYY-MM-DD"
        ) from error
    days = (parsed_end - parsed_start).days + 1
    if days < 7:
        raise QualityScoreAnalysisError(
            "Quality Score Booster needs at least 7 complete days"
        )
    return days


def _is_brand(keyword: str, brands: Sequence[str]) -> bool:
    normalized = _normalise(keyword)
    return any(
        brand
        and (
            normalized == brand
            or normalized.startswith(brand + " ")
            or normalized.endswith(" " + brand)
            or f" {brand} " in f" {normalized} "
        )
        for brand in brands
    )


def _keyword_theme(keyword: str) -> str:
    tokens = [
        token
        for token in _normalise(keyword).split()
        if token not in STOPWORDS
    ]
    return " ".join(tokens[:3]) or _normalise(keyword)


def _component(value: Any) -> str:
    normalized = str(value or "UNKNOWN").upper()
    return normalized if normalized in COMPONENT_VALUES else "UNKNOWN"


def _recheck_date(analysis_end: str, days: int) -> str:
    return (date.fromisoformat(analysis_end) + timedelta(days=days)).isoformat()


def _check(
    *,
    index: int,
    keyword: Mapping[str, Any],
    status: str,
    evidence: str,
    why: str,
    decision: str,
    next_step: str,
    route: str,
    recheck_date: str | None,
) -> dict[str, Any]:
    return {
        "id": f"QS-KW-{index:03d}",
        "campaign_id": str(keyword["campaign_id"]),
        "campaign_name": str(keyword["campaign_name"]),
        "ad_group_id": str(keyword["ad_group_id"]),
        "ad_group_name": str(keyword["ad_group_name"]),
        "criterion": str(keyword["keyword_text"]),
        "status": status,
        "evidence": evidence,
        "why_it_matters": why,
        "decision": decision,
        "next_step": next_step,
        "metrics": {
            "quality_score": keyword.get("quality_score"),
            "ad_relevance": _component(keyword.get("ad_relevance")),
            "expected_ctr": _component(keyword.get("expected_ctr")),
            "landing_page_experience": _component(
                keyword.get("landing_page_experience")
            ),
            "impressions": int(keyword.get("impressions") or 0),
            "clicks": int(keyword.get("clicks") or 0),
            "ctr": float(keyword.get("ctr") or 0),
            "conversions": float(keyword.get("conversions") or 0),
            "all_conversions": float(keyword.get("all_conversions") or 0),
            "cost_micros": int(keyword.get("cost_micros") or 0),
            "average_cpc_micros": int(
                keyword.get("average_cpc_micros") or 0
            ),
            "priority_score": (
                (10 - int(keyword["quality_score"]))
                * int(keyword.get("impressions") or 0)
                if keyword.get("quality_score") is not None
                else None
            ),
            "window": str(keyword.get("window") or "selected"),
            "rsa_count": int(keyword.get("rsa_count") or 0),
            "keyword_theme_in_headline": keyword.get(
                "keyword_theme_in_headline"
            ),
        },
        "source": "calculated",
        "route": route,
        "recheck_date": recheck_date,
    }


def _recommendation(
    *,
    action_id: str,
    priority: int,
    keyword: Mapping[str, Any],
    route: str,
    analysis_end: str,
) -> dict[str, Any]:
    route_data = ROUTES[route]
    quality_score = int(keyword["quality_score"])
    impressions = int(keyword.get("impressions") or 0)
    priority_score = (10 - quality_score) * impressions
    component_label = next(
        (
            label
            for component_id, label in COMPONENTS
            if component_id == route
        ),
        "Quality Score",
    )
    destination = route_data["module_name"]
    entity = (
        f"{route_data['task']} — “{keyword['keyword_text']}”"
        f" ({keyword['ad_group_name']})"
    )
    details = (
        f"Run {destination} for campaign “{keyword['campaign_name']}”, "
        f"ad group “{keyword['ad_group_name']}”, and keyword theme "
        f"“{_keyword_theme(str(keyword['keyword_text']))}”. That specialist "
        "must inspect its own required evidence and return a separate proposed "
        "change, if one is supported."
    )
    if route == "manual_review":
        details = (
            f"Have a Google Ads specialist review “{keyword['keyword_text']}” "
            f"in “{keyword['ad_group_name']}”. Check keyword history, recent "
            "changes, matching, and account maturity. Do not rewrite ads, "
            "restructure, pause, or negate the keyword from this result alone."
        )
    return {
        "id": action_id,
        "priority": priority,
        "severity": "warning" if quality_score > 4 else "critical",
        "evidence_label": (
            "Calculated from live Google Ads keyword Quality Score data"
        ),
        "entity": entity,
        "resource_name": "",
        "operation_type": "advisory",
        "current_value": {
            "quality_score": quality_score,
            "ad_relevance": _component(keyword.get("ad_relevance")),
            "expected_ctr": _component(keyword.get("expected_ctr")),
            "landing_page_experience": _component(
                keyword.get("landing_page_experience")
            ),
        },
        "proposed_value": {
            "task": route_data["task"],
            "route_module_id": route_data["module_id"],
            "route_name": destination,
            "recheck_date": _recheck_date(
                analysis_end, int(route_data["recheck_days"])
            ),
        },
        "reason": route_data["reason"],
        "evidence_summary": (
            f"Quality Score {quality_score}/10 across {impressions:,} "
            f"impressions; {component_label} is "
            f"{_component(keyword.get(route)).replace('_', ' ').title()}."
        ),
        "details": details,
        "expected_impact": (
            "Clarifies the evidence-backed repair before any Google Ads "
            "change. If the routed specialist supports a change, recheck the "
            "same keyword after the stated evidence window."
        ),
        "estimate": {
            "label": "GMA priority score",
            "priority_score": priority_score,
            "impressions": impressions,
            "recheck_date": _recheck_date(
                analysis_end, int(route_data["recheck_days"])
            ),
        },
        "risk": "low",
        "reversible": True,
        "applyability": "task",
        "source_skill": "quality-score-booster",
    }


def _recovery(
    *,
    recovery_id: str,
    priority: int,
    title: str,
    reason: str,
    steps: Sequence[str],
    campaigns: Sequence[Mapping[str, Any]],
    resolves: Sequence[str],
    completion_signal: str,
    owner: str = "account_owner",
    kind: str = "review_then_rerun",
    module_id: str | None = "quality_score_booster",
    not_before: str | None = None,
) -> dict[str, Any]:
    affected = [
        {
            "campaign_id": str(item["id"]),
            "campaign_name": str(item["name"]),
        }
        for item in campaigns
    ]
    return {
        "id": recovery_id,
        "priority": priority,
        "type": "quality_score_recovery",
        "status": (
            "needs_confirmation"
            if owner == "account_owner"
            else "ready"
        ),
        "title": title,
        "reason": reason,
        "steps": list(steps),
        "applies_to": affected,
        "resolves": list(dict.fromkeys(str(value) for value in resolves)),
        "completion_signal": completion_signal,
        "owner": owner,
        "follow_up": {
            "kind": kind,
            "module_id": module_id,
            "not_before": not_before,
        },
        "selectable": True,
    }


def evaluate_quality_score(
    snapshot: Mapping[str, Any],
    *,
    business_mode: str,
    brand_terms: Sequence[str] | None,
    outcome_quality_confirmed: bool = False,
) -> dict[str, Any]:
    """Evaluate keyword Quality Score using one fixed decision sequence."""

    if business_mode not in {"lead_gen", "ecommerce"}:
        raise QualityScoreAnalysisError(
            "Quality Score Booster requires lead_gen or ecommerce mode"
        )
    analysis_start = str(snapshot["analysis_start"])
    analysis_end = str(snapshot["analysis_end"])
    analysis_days = _days(analysis_start, analysis_end)
    campaigns = list(snapshot.get("campaigns") or [])
    if not campaigns:
        raise QualityScoreAnalysisError(
            "No campaigns were found in the selected scope"
        )
    search_campaigns = [
        item for item in campaigns if item.get("channel_type") == "SEARCH"
    ]
    brands = sorted(
        {_normalise(value) for value in brand_terms or [] if _normalise(value)},
        key=len,
        reverse=True,
    )
    rows = list(snapshot.get("keywords") or [])
    secondary_rows = list(snapshot.get("secondary_keywords") or [])
    high_volume_count = sum(
        int(item.get("impressions") or 0) >= 100 for item in rows
    )
    used_secondary_window = (
        high_volume_count < 20 and bool(secondary_rows)
    )
    selected_rows = secondary_rows if used_secondary_window else rows
    ordered = sorted(
        selected_rows,
        key=lambda item: (
            -(
                (10 - int(item["quality_score"]))
                * int(item.get("impressions") or 0)
                if item.get("quality_score") is not None
                else -1
            ),
            -int(item.get("impressions") or 0),
            str(item.get("keyword_text") or ""),
        ),
    )
    if high_volume_count < 20:
        ordered = ordered[:20]

    holds: list[str] = []
    recovery_actions: list[dict[str, Any]] = []
    if not brands:
        hold = "Brand terms have not been confirmed"
        holds.append(hold)
        recovery_actions.append(
            _recovery(
                recovery_id="REC-QS-CONFIRM-BRAND",
                priority=1,
                title="Confirm the brand keywords before acting on Quality Score",
                reason=(
                    "Brand keywords normally score differently from non-brand "
                    "keywords. Without the protected brand list, GMA cannot "
                    "safely apply the brand rules or rank action items."
                ),
                steps=[
                    "Review the account name, website domain, brand campaign names, and product brands shown by GMA.",
                    "Confirm the business name, abbreviations, common misspellings, and important product-brand combinations.",
                    "Return the confirmed list and rerun Quality Score Booster.",
                ],
                campaigns=search_campaigns or campaigns,
                resolves=[hold],
                completion_signal=(
                    "The account owner confirms a non-empty protected brand list."
                ),
            )
        )

    goal_scope_verified = bool(snapshot.get("goal_scope_verified"))
    if not outcome_quality_confirmed or not goal_scope_verified:
        if not outcome_quality_confirmed:
            hold = "Genuine conversion outcomes have not been confirmed"
            holds.append(hold)
            action_names = sorted(
                {
                    str(action)
                    for actions in (
                        snapshot.get("effective_conversion_actions") or {}
                    ).values()
                    for action in actions
                    if str(action).strip()
                }
            )
            actions_text = (
                ", ".join(action_names)
                if action_names
                else "the campaign-effective conversion actions shown by GMA"
            )
            recovery_actions.append(
                _recovery(
                    recovery_id="REC-QS-CONFIRM-OUTCOMES",
                    priority=1,
                    title="Confirm what Google is counting as a genuine result",
                    reason=(
                        "The strong-performance protection uses conversion "
                        "evidence. GMA can diagnose the Quality Score components "
                        "now, but it will not route changes until genuine "
                        "outcomes are confirmed."
                    ),
                    steps=[
                        f"Review {actions_text} in Google Ads → Goals → Conversions → Summary.",
                        "Confirm which actions represent genuine, non-duplicated leads or purchases for the selected campaigns.",
                        "Give that confirmation to GMA; it will rerun this skill and protect real performers before creating the action queue.",
                    ],
                    campaigns=search_campaigns or campaigns,
                    resolves=[hold],
                    completion_signal=(
                        "The account owner confirms which reported actions are "
                        "genuine business outcomes."
                    ),
                )
            )
        if not goal_scope_verified:
            hold = "Campaign-effective conversion goals could not be verified"
            holds.append(hold)
            recovery_actions.append(
                _recovery(
                    recovery_id="REC-QS-VERIFY-GOALS",
                    priority=1,
                    title="Verify the conversion goals used by each campaign",
                    reason=(
                        "An account-wide conversion list does not prove which "
                        "goals each campaign is optimizing toward."
                    ),
                    steps=[
                        "Open each selected campaign in Google Ads and review Settings → Goals.",
                        "Confirm whether it uses account-default goals, campaign-specific goals, or a custom goal.",
                        "Repair access or goal configuration if needed, then refresh the GMA connection and rerun.",
                    ],
                    campaigns=search_campaigns or campaigns,
                    resolves=[hold],
                    completion_signal=(
                        "GMA can read a verified effective goal set for every "
                        "active Search campaign."
                    ),
                    owner="google_ads_admin",
                )
            )

    gaps = list(dict.fromkeys(snapshot.get("coverage_gaps") or []))
    if not search_campaigns:
        keyword = {
            "campaign_id": str(campaigns[0]["id"]),
            "campaign_name": str(campaigns[0]["name"]),
            "ad_group_id": "0",
            "ad_group_name": "Not applicable",
            "keyword_text": "Search keyword Quality Score availability",
            "quality_score": None,
            "impressions": 0,
        }
        checks = [
            _check(
                index=1,
                keyword=keyword,
                status="not_applicable",
                evidence=(
                    "The selected scope contains no Search campaigns. Google "
                    "does not publish keyword Quality Score for Performance Max, "
                    "Shopping, Display, or Video."
                ),
                why=(
                    "Quality Score work cannot affect the selected campaign mix."
                ),
                decision="Do not run a Quality Score repair project here.",
                next_step=(
                    "Use Bid Strategy Check for bidding concerns or Wasted-Spend "
                    "Finder for visible query hygiene."
                ),
                route="none",
                recheck_date=None,
            )
        ]
        return {
            "status": "complete",
            "conclusion": (
                "Quality Score is not applicable to this selected campaign mix. "
                "Use a specialist that matches the campaign types instead."
            ),
            "campaigns_analyzed": 0,
            "checks": checks,
            "recommendations": [],
            "recovery_actions": recovery_actions,
            "holds": holds,
            "coverage_gaps": gaps,
            "assessment_details": {
                "keywords_reviewed": 0,
                "keywords_with_quality_score": 0,
                "null_quality_score_keywords": 0,
                "weighted_quality_score": None,
                "non_brand_weighted_quality_score": None,
                "band_counts": {},
                "component_failure_counts": {},
                "search_spend_micros": 0,
                "non_search_spend_micros": sum(
                    int(item.get("cost_micros") or 0) for item in campaigns
                ),
                "used_secondary_90_day_window": False,
                "action_limit": 7,
            },
            "analysis_days": analysis_days,
        }

    if not ordered:
        hold = "No Search keyword had enough data for a Quality Score review"
        holds.append(hold)
        recovery_actions.append(
            _recovery(
                recovery_id="REC-QS-EXPAND-WINDOW",
                priority=2,
                title="Collect a larger keyword evidence window",
                reason=(
                    "The selected Search campaigns returned no keyword rows "
                    "with impressions and usable Quality Score evidence."
                ),
                steps=[
                    "Confirm that the selected Search campaigns and keywords are enabled.",
                    "Rerun with the last 90 complete days.",
                    "If the result remains empty, use Bid Strategy Check or Wasted-Spend Finder instead of forcing a Quality Score diagnosis.",
                ],
                campaigns=search_campaigns,
                resolves=[hold],
                completion_signal=(
                    "At least one Search keyword has impressions and a visible "
                    "Quality Score."
                ),
            )
        )
        ordered = [
            {
                "campaign_id": str(search_campaigns[0]["id"]),
                "campaign_name": str(search_campaigns[0]["name"]),
                "ad_group_id": "0",
                "ad_group_name": "No eligible keyword",
                "keyword_text": "Keyword Quality Score evidence",
                "quality_score": None,
                "impressions": 0,
            }
        ]

    checks: list[dict[str, Any]] = []
    candidates: list[tuple[dict[str, Any], str]] = []
    weighted_numerator = 0
    weighted_denominator = 0
    non_brand_numerator = 0
    non_brand_denominator = 0
    null_count = 0
    bands: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    prerequisites_ready = not holds

    for index, item in enumerate(ordered, start=1):
        keyword = dict(item)
        quality_score = keyword.get("quality_score")
        impressions = int(keyword.get("impressions") or 0)
        clicks = int(keyword.get("clicks") or 0)
        ctr = clicks / impressions if impressions else 0.0
        keyword["ctr"] = ctr
        is_brand = _is_brand(str(keyword.get("keyword_text")), brands)
        evidence_window = (
            "the labelled 90-day secondary window"
            if keyword.get("window") == "secondary_90_day"
            else f"{analysis_start} to {analysis_end}"
        )
        component_text = (
            f"Ad relevance {_component(keyword.get('ad_relevance')).replace('_', ' ').title()}, "
            f"Expected CTR {_component(keyword.get('expected_ctr')).replace('_', ' ').title()}, "
            f"Landing page {_component(keyword.get('landing_page_experience')).replace('_', ' ').title()}."
        )
        if quality_score is None:
            null_count += 1
            checks.append(
                _check(
                    index=index,
                    keyword=keyword,
                    status="not_applicable",
                    evidence=(
                        f"Google has not assigned a visible Quality Score after "
                        f"{impressions:,} impressions in {evidence_window}."
                    ),
                    why=(
                        "A blank Quality Score means insufficient or unavailable "
                        "Google evidence; it is not a failure to repair."
                    ),
                    decision="No Quality Score action.",
                    next_step=(
                        "Keep collecting data; do not restructure or rewrite "
                        "because the score is blank."
                    ),
                    route="none",
                    recheck_date=None,
                )
            )
            continue
        quality_score = int(quality_score)
        if quality_score < 1 or quality_score > 10:
            raise QualityScoreAnalysisError(
                "Quality Score must be between 1 and 10"
            )
        weighted_numerator += quality_score * impressions
        weighted_denominator += impressions
        if not is_brand:
            non_brand_numerator += quality_score * impressions
            non_brand_denominator += impressions
        if quality_score <= 4:
            bands["1-4"] += 1
        elif quality_score <= 6:
            bands["5-6"] += 1
        else:
            bands["7-10"] += 1
        for component_id, _label in COMPONENTS:
            if _component(keyword.get(component_id)) == "BELOW_AVERAGE":
                failures[component_id] += 1

        priority_score = (10 - quality_score) * impressions
        evidence = (
            f"Quality Score {quality_score}/10 from {impressions:,} "
            f"impressions in {evidence_window}; priority score "
            f"{priority_score:,}. {component_text}"
        )
        if is_brand and quality_score >= 8:
            checks.append(
                _check(
                    index=index,
                    keyword=keyword,
                    status="healthy",
                    evidence=evidence,
                    why="Brand Quality Score of 8–10 is the healthy GMA range.",
                    decision=(
                        "The brand Quality Score story is healthy; do not force "
                        "a repair."
                    ),
                    next_step=(
                        "Keep this keyword protected. Investigate CPC or rank "
                        "concerns with Bid Strategy Check."
                    ),
                    route="bid_strategy_check",
                    recheck_date=None,
                )
            )
            continue
        trustworthy_conversion = (
            outcome_quality_confirmed and goal_scope_verified
        )
        strong_performance = (
            trustworthy_conversion
            and (
                float(keyword.get("conversions") or 0) > 0
                or float(keyword.get("all_conversions") or 0) > 0
            )
        ) or (impressions >= 100 and ctr >= 0.13)
        if strong_performance:
            checks.append(
                _check(
                    index=index,
                    keyword=keyword,
                    status="watch",
                    evidence=evidence,
                    why=(
                        "Strong real performance overrides a weak diagnostic "
                        "label and protects the keyword from forced repair."
                    ),
                    decision="Leave this keyword alone.",
                    next_step=(
                        "Watch it; never pause, negate, or restructure it from "
                        "Quality Score alone."
                    ),
                    route="none",
                    recheck_date=None,
                )
            )
            continue
        if impressions < 100:
            checks.append(
                _check(
                    index=index,
                    keyword=keyword,
                    status="watch",
                    evidence=evidence,
                    why=(
                        "The keyword is below the 100-impression action floor."
                    ),
                    decision="Do not act on low-volume Quality Score noise.",
                    next_step=(
                        "Keep collecting evidence and rerun after it crosses "
                        "100 impressions."
                    ),
                    route="none",
                    recheck_date=None,
                )
            )
            continue
        if quality_score >= 7:
            checks.append(
                _check(
                    index=index,
                    keyword=keyword,
                    status="healthy",
                    evidence=evidence,
                    why="A non-brand Quality Score of 7+ is genuinely healthy.",
                    decision="No Quality Score repair is justified.",
                    next_step=(
                        "If reach or CPC remains the concern, run Bid Strategy "
                        "Check instead."
                    ),
                    route="bid_strategy_check",
                    recheck_date=None,
                )
            )
            continue

        route = next(
            (
                component_id
                for component_id, _label in COMPONENTS
                if _component(keyword.get(component_id)) == "BELOW_AVERAGE"
            ),
            "manual_review",
        )
        route_data = ROUTES[route]
        recheck = _recheck_date(
            analysis_end, int(route_data["recheck_days"])
        )
        if not prerequisites_ready:
            status = "held"
            decision = (
                "Diagnosis recorded, but the repair route is held until the "
                "recovery tasks above are completed."
            )
            next_step = (
                "Resolve the highest-priority recovery task, then rerun; GMA "
                f"will route this item to {route_data['module_name']} if it "
                "still qualifies."
            )
        else:
            status = "needs_attention"
            decision = (
                f"The first failed component routes to "
                f"{route_data['module_name']}."
            )
            next_step = (
                f"{route_data['task']}. Recheck this keyword on {recheck}."
            )
            candidates.append((keyword, route))
        checks.append(
            _check(
                index=index,
                keyword=keyword,
                status=status,
                evidence=evidence,
                why=route_data["reason"],
                decision=decision,
                next_step=next_step,
                route=route,
                recheck_date=recheck,
            )
        )

    candidates.sort(
        key=lambda entry: (
            -(
                (10 - int(entry[0]["quality_score"]))
                * int(entry[0].get("impressions") or 0)
            ),
            str(entry[0].get("keyword_text") or ""),
        )
    )
    recommendations = [
        _recommendation(
            action_id=f"QS-{index:03d}",
            priority=index,
            keyword=keyword,
            route=route,
            analysis_end=analysis_end,
        )
        for index, (keyword, route) in enumerate(candidates[:7], start=1)
    ]
    if not recommendations and not recovery_actions:
        recovery_actions.append(
            _recovery(
                recovery_id="REC-QS-MONITOR",
                priority=3,
                title="Keep the current Quality Score setup and recheck later",
                reason=(
                    "No keyword crossed the evidence and safety thresholds for "
                    "a repair route in this run."
                ),
                steps=[
                    "Keep the current keyword, ad, and landing-page setup unchanged.",
                    "Save this run as the baseline for future drop detection.",
                    "Rerun Quality Score Booster after the next 30 complete days or after a material site/ad change.",
                ],
                campaigns=search_campaigns,
                resolves=["No keyword crossed the action threshold"],
                completion_signal=(
                    "A fresh run is available after the monitoring window."
                ),
                owner="gma",
                kind="monitor",
                not_before=_recheck_date(analysis_end, 30),
            )
        )

    search_spend = sum(
        int(item.get("cost_micros") or 0) for item in search_campaigns
    )
    non_search_spend = sum(
        int(item.get("cost_micros") or 0)
        for item in campaigns
        if item.get("channel_type") != "SEARCH"
    )
    status = "partial" if holds or gaps else (
        "recommendations_ready" if recommendations else "complete"
    )
    if holds:
        conclusion = (
            f"GMA completed the component diagnosis for {len(checks)} "
            "keywords, but held the repair queue. Complete the Recovery plan "
            "below and rerun; the diagnosed keywords will then be routed to "
            "the right specialist instead of being abandoned."
        )
    elif recommendations:
        conclusion = (
            f"{len(recommendations)} specialist follow-up"
            f"{'s' if len(recommendations) != 1 else ''} passed the Quality "
            "Score evidence gates. No Google Ads setting was changed."
        )
    else:
        conclusion = (
            "No Quality Score repair is justified right now. Keep the current "
            "setup and use the monitoring task below."
        )
    return {
        "status": status,
        "conclusion": conclusion,
        "campaigns_analyzed": len(search_campaigns),
        "checks": checks,
        "recommendations": recommendations,
        "recovery_actions": recovery_actions,
        "holds": holds,
        "coverage_gaps": gaps,
        "assessment_details": {
            "keywords_reviewed": len(checks),
            "keywords_with_quality_score": sum(
                check["metrics"]["quality_score"] is not None
                for check in checks
            ),
            "null_quality_score_keywords": null_count,
            "weighted_quality_score": (
                round(weighted_numerator / weighted_denominator, 2)
                if weighted_denominator
                else None
            ),
            "non_brand_weighted_quality_score": (
                round(non_brand_numerator / non_brand_denominator, 2)
                if non_brand_denominator and brands
                else None
            ),
            "band_counts": dict(sorted(bands.items())),
            "component_failure_counts": dict(sorted(failures.items())),
            "search_spend_micros": search_spend,
            "non_search_spend_micros": non_search_spend,
            "used_secondary_90_day_window": used_secondary_window,
            "action_limit": 7,
        },
        "analysis_days": analysis_days,
    }
