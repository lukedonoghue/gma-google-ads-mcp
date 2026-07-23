"""Deterministic judgment engine for GMA Skill 3 — Wasted-Spend Finder.

The model may explain the canonical result, but it never classifies search
terms, chooses negative match types, calculates waste, or creates action IDs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any, Mapping, Sequence


class WastedSpendAnalysisError(ValueError):
    """Safe input or evidence failure for Wasted-Spend Finder."""


THEME_PATTERNS: dict[str, tuple[str, ...]] = {
    "jobs": ("job", "jobs", "career", "careers", "hiring", "salary", "resume"),
    "informational": (
        "how to",
        "what is",
        "definition",
        "tutorial",
        "diy",
        "manual",
        "pdf",
        "wiki",
    ),
    "free": ("free", "gratis", "open source", "pro bono", "volunteer"),
    "education": (
        "course",
        "courses",
        "training",
        "certification",
        "degree",
        "exam",
        "student",
    ),
    "support": (
        "support",
        "repair",
        "repairs",
        "warranty",
        "refund",
        "cancel",
        "troubleshoot",
    ),
    "secondhand": (
        "used",
        "refurbished",
        "secondhand",
        "second hand",
        "vintage",
    ),
    "rental": ("rent", "rental", "lease", "borrow"),
}
CONQUEST_PATTERNS = (
    "alternative",
    "alternatives",
    "vs",
    "versus",
    "similar",
    "like",
    "comparable",
    "dupe",
    "knock off",
    "cheaper than",
)
REVIEW_PATTERNS = ("review", "reviews", "best")
CLASSIFICATION_ORDER = {
    "EXCLUDE": 0,
    "FLAG": 1,
    "EDGE_CASE": 2,
    "MONITOR": 3,
    "ALREADY_COVERED": 4,
    "KEEP": 5,
}
THEME_LISTS = {
    "jobs": "Jobs & Careers",
    "informational": "Irrelevant keywords",
    "free": "Irrelevant keywords",
    "education": "Irrelevant keywords",
    "support": "Support & Repair",
    "secondhand": "Irrelevant keywords",
    "rental": "Irrelevant keywords",
    "performance": "Non-converting roots",
}


def _normalise(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", text))


def _days(start: str, end: str) -> int:
    try:
        parsed_start = date.fromisoformat(start)
        parsed_end = date.fromisoformat(end)
    except ValueError as error:
        raise WastedSpendAnalysisError(
            "Analysis dates must be YYYY-MM-DD"
        ) from error
    days = (parsed_end - parsed_start).days + 1
    if days < 7:
        raise WastedSpendAnalysisError(
            "Wasted-Spend Finder needs at least 7 complete days"
        )
    return days


def _money(micros: int, currency: str) -> str:
    return f"{currency} {micros / 1_000_000:,.2f}"


def _applicable_existing_negative(
    term: str,
    campaign_id: str,
    ad_group_id: str,
    existing: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for item in existing:
        negative = _normalise(item.get("text"))
        match_type = str(item.get("match_type") or "EXACT").upper()
        if not negative:
            continue
        if match_type == "EXACT":
            matches = negative == term
        elif match_type == "PHRASE":
            matches = _contains(term, negative)
        elif match_type == "BROAD":
            matches = set(negative.split()).issubset(set(term.split()))
        else:
            matches = False
        if not matches:
            continue
        scope = str(item.get("scope") or "")
        campaign_ids = {str(value) for value in item.get("campaign_ids") or []}
        ad_group_ids = {str(value) for value in item.get("ad_group_ids") or []}
        if scope == "account":
            return item
        if scope == "ad_group" and (
            campaign_id in campaign_ids and ad_group_id in ad_group_ids
        ):
            return item
        if scope in {"campaign", "shared_list"} and campaign_id in campaign_ids:
            return item
    return None


def _theme_for_term(term: str) -> tuple[str | None, str | None]:
    for theme, patterns in THEME_PATTERNS.items():
        for pattern in patterns:
            if _contains(term, pattern):
                return theme, pattern
    return None, None


def _match_type(negative: str, *, performance_only: bool) -> str:
    if performance_only:
        return "EXACT"
    words = negative.split()
    if len(words) == 1:
        return "BROAD"
    return "PHRASE"


def _display_negative(text: str, match_type: str) -> str:
    if match_type == "EXACT":
        return f"[{text}]"
    if match_type == "PHRASE":
        return f'"{text}"'
    return text


def _check(
    *,
    index: int,
    term: Mapping[str, Any],
    classification: str,
    evidence: str,
    why: str,
    decision: str,
    next_step: str,
    metrics: Mapping[str, Any],
    source: str = "calculated",
) -> dict[str, Any]:
    return {
        "id": f"WS-TERM-{index:03d}",
        "campaign_id": str(term["campaign_id"]),
        "campaign_name": str(term["campaign_name"]),
        "criterion": str(term["search_term"]),
        "status": classification.lower(),
        "evidence": evidence,
        "why_it_matters": why,
        "decision": decision,
        "next_step": next_step,
        "metrics": dict(metrics),
        "source": source,
    }


def _recommendation(
    *,
    action_id: str,
    priority: int,
    term: Mapping[str, Any],
    negative: str,
    match_type: str,
    destination: str,
    reason: str,
    evidence: str,
    window: str,
) -> dict[str, Any]:
    rendered = _display_negative(negative, match_type)
    return {
        "id": action_id,
        "priority": priority,
        "severity": "warning",
        "evidence_label": "Calculated from live Google Ads search-term data",
        "entity": f"Add {rendered} to {destination}",
        "resource_name": "",
        "operation_type": "advisory",
        "current_value": {"negative_keyword": None},
        "proposed_value": {
            "negative_keyword": negative,
            "match_type": match_type,
            "scope": "shared_negative_list",
            "destination": destination,
        },
        "reason": reason,
        "evidence_summary": evidence,
        "details": (
            f"In Google Ads, open Tools → Shared library → Exclusion lists, add "
            f"{rendered} to “{destination}”, and link that list only to the "
            "campaigns where this intent is genuinely unwanted. Review the "
            "affected campaign after 14 complete days."
        ),
        "expected_impact": (
            f"Stops future matching queries covered by {rendered}; historical "
            f"spend from {window} is not recoverable and future budget may "
            "redistribute to other searches."
        ),
        "estimate": {
            "label": "directional",
            "basis": window,
            "historical_cost_micros": int(term.get("cost_micros") or 0),
        },
        "risk": "medium",
        "reversible": True,
        "applyability": "task",
        "source_skill": "wasted-spend-finder",
    }


def _recovery(
    *,
    recovery_id: str,
    priority: int,
    title: str,
    reason: str,
    steps: Sequence[str],
    affected: Sequence[Mapping[str, Any]],
    resolves: Sequence[str],
    completion_signal: str,
    status: str = "needs_confirmation",
    owner: str = "account_owner",
) -> dict[str, Any]:
    unique = {
        str(item["campaign_id"]): {
            "campaign_id": str(item["campaign_id"]),
            "campaign_name": str(item["campaign_name"]),
        }
        for item in affected
    }
    return {
        "id": recovery_id,
        "priority": priority,
        "type": "search_term_recovery",
        "status": status,
        "title": title,
        "reason": reason,
        "steps": list(steps),
        "applies_to": list(unique.values()),
        "resolves": list(dict.fromkeys(str(value) for value in resolves)),
        "completion_signal": completion_signal,
        "owner": owner,
        "follow_up": {
            "kind": "review_then_rerun",
            "module_id": "wasted_spend_finder",
            "not_before": None,
        },
        "selectable": True,
    }


def evaluate_wasted_spend(
    snapshot: Mapping[str, Any],
    *,
    business_mode: str,
    brand_terms: Sequence[str] | None,
    confirmed_irrelevant_themes: Sequence[str] | None = None,
    protected_intent_themes: Sequence[str] | None = None,
    competitor_policy: str = "review",
    target_cpa_micros: int | None = None,
    target_roas: float | None = None,
    outcome_quality_confirmed: bool = False,
) -> dict[str, Any]:
    """Classify visible search terms and build a safe negative-keyword plan."""

    if business_mode not in {"lead_gen", "ecommerce"}:
        raise WastedSpendAnalysisError(
            "Wasted-Spend Finder requires lead_gen or ecommerce mode"
        )
    analysis_start = str(snapshot["analysis_start"])
    analysis_end = str(snapshot["analysis_end"])
    analysis_days = _days(analysis_start, analysis_end)
    currency = str(snapshot.get("currency") or "")
    window = f"{analysis_start} to {analysis_end}"
    campaigns = list(snapshot.get("campaigns") or [])
    terms = list(snapshot.get("search_terms") or [])
    if not campaigns:
        raise WastedSpendAnalysisError(
            "No campaigns were found in the selected scope"
        )

    confirmed_themes = {
        _normalise(value) for value in confirmed_irrelevant_themes or []
    }
    protected_themes = {
        _normalise(value) for value in protected_intent_themes or []
    }
    unknown_themes = sorted(
        (confirmed_themes | protected_themes).difference(THEME_PATTERNS)
    )
    if unknown_themes:
        raise WastedSpendAnalysisError(
            "Unknown intent themes: " + ", ".join(unknown_themes)
        )
    brands = sorted(
        {_normalise(value) for value in brand_terms or [] if _normalise(value)},
        key=len,
        reverse=True,
    )
    existing = list(snapshot.get("existing_negatives") or [])
    goal_scope_verified = bool(snapshot.get("goal_scope_verified"))
    average_cpa_micros = snapshot.get("average_cpa_micros")
    average_order_value = snapshot.get("average_order_value")

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in terms:
        text = _normalise(raw.get("search_term"))
        campaign_id = str(raw.get("campaign_id") or "")
        ad_group_id = str(raw.get("ad_group_id") or "")
        if not text or not re.fullmatch(r"\d{1,20}", campaign_id):
            continue
        key = (campaign_id, ad_group_id, text)
        item = merged.setdefault(
            key,
            {
                **dict(raw),
                "search_term": text,
                "cost_micros": 0,
                "conversions": 0.0,
                "all_conversions": 0.0,
                "conversions_value": 0.0,
                "clicks": 0,
                "impressions": 0,
                "lanes": set(),
            },
        )
        for field in (
            "cost_micros",
            "conversions",
            "all_conversions",
            "conversions_value",
            "clicks",
            "impressions",
        ):
            item[field] = max(item[field], raw.get(field) or 0)
        item["lanes"].add(str(raw.get("lane") or "spend_risk"))
        statuses = set(item.get("statuses") or [])
        statuses.add(str(raw.get("status") or "NONE"))
        item["statuses"] = sorted(statuses)

    ordered_terms = sorted(
        merged.values(),
        key=lambda item: (
            -int(item.get("cost_micros") or 0),
            -int(item.get("impressions") or 0),
            str(item["search_term"]),
        ),
    )

    root_has_conversion: dict[tuple[str, str], bool] = defaultdict(bool)
    for item in ordered_terms:
        theme, root = _theme_for_term(str(item["search_term"]))
        if theme and root:
            root_has_conversion[(theme, root)] = root_has_conversion[
                (theme, root)
            ] or bool(
                float(item.get("conversions") or 0) > 0
                or float(item.get("all_conversions") or 0) > 0
            )

    checks: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    ambiguous_terms: list[dict[str, Any]] = []
    performance_held: list[dict[str, Any]] = []
    identified_waste_micros = 0
    excluded_impressions = 0
    already_covered = 0

    for index, item in enumerate(ordered_terms, start=1):
        term = str(item["search_term"])
        campaign_id = str(item["campaign_id"])
        cost = int(item.get("cost_micros") or 0)
        clicks = int(item.get("clicks") or 0)
        impressions = int(item.get("impressions") or 0)
        conversions = float(item.get("conversions") or 0)
        all_conversions = float(item.get("all_conversions") or 0)
        metrics = {
            "cost_micros": cost,
            "clicks": clicks,
            "impressions": impressions,
            "conversions": conversions,
            "all_conversions": all_conversions,
            "channel_type": item.get("channel_type"),
            "lane": sorted(item.get("lanes") or []),
        }
        evidence = (
            f"{_money(cost, currency)} spent, {clicks} clicks, "
            f"{impressions} impressions, {conversions:g} primary conversions "
            f"during {window}."
        )
        existing_match = _applicable_existing_negative(
            term,
            campaign_id,
            str(item.get("ad_group_id") or ""),
            existing,
        )
        statuses = {str(value) for value in item.get("statuses") or []}
        if existing_match or statuses.intersection(
            {"EXCLUDED", "ADDED_EXCLUDED"}
        ):
            already_covered += 1
            coverage_name = (
                str(
                    existing_match.get("list_name")
                    or existing_match.get("scope")
                )
                if existing_match
                else "Google Ads"
            )
            checks.append(
                _check(
                    index=index,
                    term=item,
                    classification="ALREADY_COVERED",
                    evidence=evidence,
                    why="Duplicate negatives create clutter without adding protection.",
                    decision=f"Already covered by {coverage_name}.",
                    next_step="Keep the existing exclusion; do not add a duplicate.",
                    metrics=metrics,
                )
            )
            continue

        if any(_contains(term, brand) for brand in brands):
            checks.append(
                _check(
                    index=index,
                    term=item,
                    classification="KEEP",
                    evidence=evidence,
                    why="Brand and close brand intent must be protected from accidental blocking.",
                    decision="Keep this search term.",
                    next_step="Route brand traffic deliberately; never negate the brand itself.",
                    metrics=metrics,
                )
            )
            continue

        if conversions > 0 or all_conversions > 0:
            checks.append(
                _check(
                    index=index,
                    term=item,
                    classification="FLAG",
                    evidence=evidence,
                    why="A converting search term or root cannot be safely auto-excluded.",
                    decision="Human review required; no negative is prepared.",
                    next_step="Review lead or purchase quality before deciding whether to keep or isolate it.",
                    metrics=metrics,
                )
            )
            continue

        if any(_contains(term, pattern) for pattern in CONQUEST_PATTERNS):
            classification = (
                "EDGE_CASE" if competitor_policy == "exclude" else "KEEP"
            )
            checks.append(
                _check(
                    index=index,
                    term=item,
                    classification=classification,
                    evidence=evidence,
                    why="Comparison and alternative searches often represent active buyers.",
                    decision=(
                        "Needs an explicit conquest decision."
                        if classification == "EDGE_CASE"
                        else "Keep protected comparison intent."
                    ),
                    next_step=(
                        "Confirm whether the business wants conquest traffic before excluding it."
                        if classification == "EDGE_CASE"
                        else "Keep it unless lead or purchase quality later proves poor."
                    ),
                    metrics=metrics,
                )
            )
            if classification == "EDGE_CASE":
                ambiguous_terms.append(item)
            continue

        theme, root = _theme_for_term(term)
        if theme and root:
            if theme in protected_themes:
                classification = "KEEP"
                decision = (
                    f"Keep: {theme} intent is part of the confirmed offer."
                )
                next_step = "No negative action."
            elif root_has_conversion[(theme, root)]:
                classification = "FLAG"
                decision = "This root has conversion evidence elsewhere and is protected."
                next_step = "Review the converted examples before considering a narrower exact negative."
            elif theme in confirmed_themes:
                classification = "EXCLUDE"
                decision = f"Exclude the confirmed irrelevant {theme} intent."
                next_step = (
                    "Add the returned negative to the named shared list."
                )
            else:
                classification = "EDGE_CASE"
                decision = f"Possible {theme} mismatch; business confirmation is required."
                next_step = (
                    f"Confirm whether the business offers or serves {theme} intent. "
                    "GMA will rerun and prepare only the safe exclusions."
                )
                ambiguous_terms.append(item)

            checks.append(
                _check(
                    index=index,
                    term=item,
                    classification=classification,
                    evidence=evidence,
                    why=(
                        "Intent mismatch can consume budget or pollute matching, but "
                        "catalog terms are never excluded without checking the real offer."
                    ),
                    decision=decision,
                    next_step=next_step,
                    metrics=metrics,
                )
            )
            if classification == "EXCLUDE":
                match_type = _match_type(root, performance_only=False)
                candidates.append(
                    {
                        "term": item,
                        "negative": root,
                        "match_type": match_type,
                        "destination": THEME_LISTS[theme],
                        "priority": 1 if clicks else 2,
                        "reason": (
                            f"The account owner confirmed {theme} intent is not "
                            "part of the offer, and this visible search term had "
                            "no conversions."
                        ),
                    }
                )
                if clicks > 0:
                    identified_waste_micros += cost
                else:
                    excluded_impressions += impressions
            continue

        performance_floor = (
            clicks >= 5
            and average_cpa_micros is not None
            and cost >= int(average_cpa_micros)
        )
        if performance_floor:
            if outcome_quality_confirmed and goal_scope_verified:
                checks.append(
                    _check(
                        index=index,
                        term=item,
                        classification="EXCLUDE",
                        evidence=evidence,
                        why=(
                            "The term crossed the ≥5-click and ≥1× account-average-CPA "
                            "floor with no primary or all-conversion evidence."
                        ),
                        decision="Prepare an exact negative for this full query.",
                        next_step="Add the exact negative and review the campaign after 14 complete days.",
                        metrics=metrics,
                    )
                )
                candidates.append(
                    {
                        "term": item,
                        "negative": term,
                        "match_type": "EXACT",
                        "destination": THEME_LISTS["performance"],
                        "priority": 2,
                        "reason": (
                            "This full query crossed the GMA performance floor with "
                            "zero primary and all conversions; exact match avoids "
                            "blocking broader valid intent."
                        ),
                    }
                )
                identified_waste_micros += cost
            else:
                performance_held.append(item)
                checks.append(
                    _check(
                        index=index,
                        term=item,
                        classification="FLAG",
                        evidence=evidence,
                        why=(
                            "The term crossed the volume floor, but outcome quality "
                            "or campaign-effective goals are not confirmed."
                        ),
                        decision="Performance exclusion held for safety.",
                        next_step="Confirm genuine outcomes and rerun before excluding this query.",
                        metrics=metrics,
                    )
                )
            continue

        if any(_contains(term, pattern) for pattern in REVIEW_PATTERNS):
            classification = "KEEP"
            decision = "Keep protected review and research intent."
            next_step = "Reassess only after meaningful conversion evidence accumulates."
        elif clicks == 0 and impressions >= 25:
            classification = "MONITOR"
            decision = (
                "Visible match pollution, but intent is not proven irrelevant."
            )
            next_step = "Review again when the term recurs or its business fit is confirmed."
        elif conversions == 0:
            classification = "MONITOR"
            decision = "Below the ≥5-click / ≥1× average-CPA performance floor."
            next_step = (
                "Collect more evidence; do not exclude this search term yet."
            )
        else:
            classification = "KEEP"
            decision = "No supported waste finding."
            next_step = "Keep and monitor."
        checks.append(
            _check(
                index=index,
                term=item,
                classification=classification,
                evidence=evidence,
                why="Low-volume or ambiguous terms are more dangerous to block than to observe.",
                decision=decision,
                next_step=next_step,
                metrics=metrics,
            )
        )

    holds: list[str] = []
    recovery_actions: list[dict[str, Any]] = []
    all_affected = [
        {
            "campaign_id": str(campaign["id"]),
            "campaign_name": str(campaign["name"]),
        }
        for campaign in campaigns
    ]
    if not brands:
        holds.append("Brand terms have not been confirmed")
        recovery_actions.append(
            _recovery(
                recovery_id="REC-WS-CONFIRM-BRAND",
                priority=1,
                title="Confirm the brand names this account must never block",
                reason=(
                    "Negative-keyword work is unsafe until the business name, domain "
                    "name, abbreviations, and common variants are protected."
                ),
                steps=[
                    "Review the account name, website domain, and brand campaign names shown by GMA.",
                    "Confirm the brand, product-brand combinations, abbreviations, and common misspellings.",
                    "Return the confirmed list and rerun Wasted-Spend Finder.",
                ],
                affected=all_affected,
                resolves=["Brand terms have not been confirmed"],
                completion_signal="The account owner confirms a non-empty protected brand-term list.",
            )
        )

    if ambiguous_terms:
        unresolved_themes = sorted(
            {
                theme
                for item in ambiguous_terms
                for theme, root in [_theme_for_term(str(item["search_term"]))]
                if theme and root
            }
        )
        resolves = [
            "Business relevance is unconfirmed for: "
            + ", ".join(unresolved_themes or ["comparison/competitor intent"])
        ]
        recovery_actions.append(
            _recovery(
                recovery_id="REC-WS-CONFIRM-INTENT",
                priority=2,
                title="Confirm which questionable search intents the business does not serve",
                reason=(
                    "GMA found possible mismatches, but a generic catalog cannot know "
                    "whether this business offers training, repairs, rentals, free "
                    "services, or similar edge cases."
                ),
                steps=[
                    "Review the EDGE CASE search terms in this report.",
                    "Mark each returned theme as genuinely offered/protected or confirmed irrelevant.",
                    "Rerun; GMA will turn only confirmed irrelevant themes into negative-keyword tasks.",
                ],
                affected=ambiguous_terms,
                resolves=resolves,
                completion_signal=(
                    "Every returned intent theme is marked protected or confirmed irrelevant by the account owner."
                ),
            )
        )

    if performance_held:
        hold = (
            "Performance-based negatives are held until genuine outcomes and "
            "campaign-effective goals are confirmed"
        )
        holds.append(hold)
        recovery_actions.append(
            _recovery(
                recovery_id="REC-WS-CONFIRM-OUTCOMES",
                priority=1,
                title="Confirm the outcomes used to judge non-converting search terms",
                reason=(
                    "A query cannot be called waste from zero reported conversions "
                    "until the account's genuine lead or purchase actions are known."
                ),
                steps=[
                    "Review the campaign-effective conversion actions returned by GMA.",
                    "Confirm which actions are genuine, non-duplicated leads or purchases.",
                    "Rerun with outcome quality confirmed; clear relevance-based exclusions can remain separate.",
                ],
                affected=performance_held,
                resolves=[hold],
                completion_signal=(
                    "The account owner confirms genuine outcomes and the runtime verifies campaign-effective goals."
                ),
            )
        )

    if not goal_scope_verified and not performance_held:
        hold = "Campaign-effective conversion goals could not be verified"
        holds.append(hold)
        recovery_actions.append(
            _recovery(
                recovery_id="REC-WS-VERIFY-GOALS",
                priority=1,
                title="Verify the conversion goals used by the reviewed campaigns",
                reason=(
                    "Performance-based negatives need the actual campaign bidding "
                    "goals, not the account-wide count of primary conversions."
                ),
                steps=[
                    "Open each affected campaign → Settings → Goals.",
                    "Confirm whether it uses account-default, campaign-specific, or custom goals.",
                    "Correct any wrong primary goal assignment, then rerun Wasted-Spend Finder.",
                ],
                affected=all_affected,
                resolves=[hold],
                completion_signal="The runtime resolves a non-empty campaign-effective goal set.",
                owner="google_ads_admin",
            )
        )

    deduped_candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            str(candidate["negative"]),
            str(candidate["match_type"]),
            str(candidate["destination"]),
        )
        existing_candidate = deduped_candidates.get(key)
        if existing_candidate is None or int(
            candidate["term"].get("cost_micros") or 0
        ) > int(existing_candidate["term"].get("cost_micros") or 0):
            deduped_candidates[key] = candidate

    recommendations: list[dict[str, Any]] = []
    if brands:
        for sequence, candidate in enumerate(
            sorted(
                deduped_candidates.values(),
                key=lambda item: (
                    int(item["priority"]),
                    -int(item["term"].get("cost_micros") or 0),
                    str(item["negative"]),
                ),
            )[:7],
            start=1,
        ):
            term = candidate["term"]
            evidence = (
                f"{_money(int(term.get('cost_micros') or 0), currency)} spent, "
                f"{int(term.get('clicks') or 0)} clicks, "
                f"{int(term.get('impressions') or 0)} impressions, and 0 primary/"
                f"all conversions during {window}."
            )
            recommendations.append(
                _recommendation(
                    action_id=f"WS-{sequence:03d}",
                    priority=int(candidate["priority"]),
                    term=term,
                    negative=str(candidate["negative"]),
                    match_type=str(candidate["match_type"]),
                    destination=str(candidate["destination"]),
                    reason=str(candidate["reason"]),
                    evidence=evidence,
                    window=window,
                )
            )

    if not recommendations and not recovery_actions:
        recovery_actions.append(
            _recovery(
                recovery_id="REC-WS-MONITOR",
                priority=5,
                title="Keep the current negatives and review a fresh search-term window",
                reason=(
                    "No visible term crossed the relevance or evidence threshold for "
                    "a safe exclusion in this run."
                ),
                steps=[
                    "Leave current negative keywords unchanged.",
                    "For new campaigns, review search terms daily during week one; otherwise review monthly.",
                    "Rerun after at least 7 new complete days or when a questionable term repeats.",
                ],
                affected=all_affected,
                resolves=["No search term crossed a safe exclusion threshold"],
                completion_signal="At least 7 new complete days are available, or a questionable term repeats.",
                status="waiting",
                owner="gma",
            )
        )

    gaps = list(snapshot.get("coverage_gaps") or [])
    if not terms:
        gaps.append(
            "No visible Search or Performance Max query rows were returned for the selected window"
        )
    if not brands:
        status = "hold"
        conclusion = (
            "Search-term data was reviewed, but no negative list is safe until "
            "the protected brand terms are confirmed."
        )
    elif gaps or holds or ambiguous_terms:
        status = "partial"
        conclusion = (
            f"{len(recommendations)} safe negative-keyword task"
            f"{'' if len(recommendations) == 1 else 's'} prepared; unresolved "
            "business or measurement questions remain in the Recovery plan."
        )
    elif recommendations:
        status = "findings_ready"
        conclusion = (
            f"{len(recommendations)} negative-keyword task"
            f"{'' if len(recommendations) == 1 else 's'} passed the GMA safety rules."
        )
    else:
        status = "no_change"
        conclusion = (
            "No visible search term currently supports a safe new negative."
        )

    classification_counts = {
        key: sum(check["status"] == key.lower() for check in checks)
        for key in CLASSIFICATION_ORDER
    }
    return {
        "contract_version": "gma-skill-run/1.0",
        "skill": {
            "number": 3,
            "slug": "wasted-spend-finder",
            "name": "Wasted-Spend Finder",
        },
        "status": status,
        "conclusion": conclusion,
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
        "analysis_days": analysis_days,
        "business_mode": business_mode,
        "campaigns_analyzed": len(campaigns),
        "checks": checks
        or [
            _check(
                index=1,
                term={
                    "campaign_id": campaigns[0]["id"],
                    "campaign_name": campaigns[0]["name"],
                    "search_term": "No visible search terms",
                },
                classification="MONITOR",
                evidence=f"Google Ads returned no visible query rows during {window}.",
                why="Privacy thresholds and low volume can hide query-level evidence.",
                decision="No negative-keyword decision is possible from an empty query set.",
                next_step="Collect a fresh window and rerun.",
                metrics={},
                source="unavailable",
            )
        ],
        "recommendations": recommendations,
        "recovery_actions": recovery_actions,
        "holds": list(dict.fromkeys(holds)),
        "coverage_gaps": list(dict.fromkeys(gaps)),
        "assessment_details": {
            "visible_search_terms_reviewed": len(ordered_terms),
            "classification_counts": classification_counts,
            "identified_spend_on_irrelevant_queries_micros": identified_waste_micros,
            "match_pollution_impressions": excluded_impressions,
            "already_covered_count": already_covered,
            "recommendation_cap": 7,
            "brand_terms_confirmed": bool(brands),
            "goal_scope_verified": goal_scope_verified,
            "outcome_quality_confirmed": outcome_quality_confirmed,
            "average_cpa_micros": average_cpa_micros,
            "average_order_value": average_order_value,
            "target_cpa_micros": target_cpa_micros,
            "target_roas": target_roas,
            "waste_claim_window": window,
        },
    }
