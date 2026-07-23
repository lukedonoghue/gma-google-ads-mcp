"""Deterministic scoring for GMA Skill 1 — Instant Account Audit.

The model may explain this result. It does not choose the checks, thresholds,
grade, recommendation order, recovery tasks, or action IDs.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any, Mapping, Sequence


class InstantAuditAnalysisError(ValueError):
    """Safe input or evidence failure for Instant Account Audit."""


SECTIONS = {
    "A": ("Tracking & measurement", 20, tuple(f"A{i}" for i in range(1, 10))),
    "B": ("Destination & landing pages", 12, tuple(f"B{i}" for i in range(1, 5))),
    "C": ("Goals, bidding & feasibility", 14, tuple(f"C{i}" for i in range(1, 7))),
    "D": (
        "Budget allocation & impression share",
        12,
        tuple(f"D{i}" for i in range(1, 5)),
    ),
    "E": ("Structure & traffic control", 14, tuple(f"E{i}" for i in range(1, 9))),
    "F": ("Search terms & waste", 10, tuple(f"F{i}" for i in range(1, 4))),
    "G": ("Ads, creative & Quality Score", 10, tuple(f"G{i}" for i in range(1, 5))),
    "H": ("Device, geo & schedule", 8, tuple(f"H{i}" for i in range(1, 4))),
}

CRITERIA = {
    "A1": "Auto-tagging is enabled",
    "A2": "Conversion tracking is live",
    "A3": "A genuine business outcome is biddable",
    "A4": "Primary conversions are not double-counted",
    "A5": "Micro-actions are not primary goals",
    "A6": "Primary conversions use one attribution model",
    "A7": "Conversion values are suitable for the bidding method",
    "A8": "Primary and all-conversion totals are coherent",
    "A9": "No evidence of a current tracking break",
    "B1": "The main paid landing page loads",
    "B2": "The main page matches intent and has a visible action",
    "B3": "Paid traffic lands on the right page type",
    "B4": "No high-traffic landing page has zero reported outcomes",
    "C1": "Bidding strategy matches the business goal",
    "C2": "Automated bidding has enough conversion volume",
    "C3": "Bid targets are realistic",
    "C4": "The stated CPA or ROAS goal is mathematically possible",
    "C5": "Enough data is being used by automated bidding",
    "C6": "Bid strategy is not being changed too frequently",
    "D1": "Strong campaigns are not being limited by budget",
    "D2": "Weak campaigns are not over-funded",
    "D3": "Shared budgets group compatible campaigns",
    "D4": "Ad-rank losses are not being mistaken for budget problems",
    "E1": "Brand and non-brand traffic are separated",
    "E2": "Search campaigns are not expanded into Display",
    "E3": "The core campaign type receives the largest spend share",
    "E4": "Cold-audience channels are controlled",
    "E5": "The same search demand is not spread across too many campaigns",
    "E6": "A negative-keyword system exists",
    "E7": "Losing ecommerce products are isolated",
    "E8": "Lead-generation Performance Max has a quality signal",
    "F1": "Search-term waste is below the GMA threshold",
    "F2": "Most top-spend search terms produce outcomes",
    "F3": "Proven search terms are not waiting to be promoted",
    "G1": "No active ad is blocked by policy",
    "G2": "Every spending Search ad group has an active responsive ad",
    "G3": "Top ad groups are not relying on poor-strength ads",
    "G4": "Weighted keyword Quality Score is healthy",
    "H1": "No major device has a severe efficiency gap",
    "H2": "No major location is spending without outcomes",
    "H3": "No major day or time block has a severe efficiency gap",
}

PRIORITY = {
    "A": 1,
    "B": 1,
    "G1": 2,
    "F": 3,
    "C": 4,
    "D": 5,
    "E": 6,
    "G": 7,
    "H": 7,
}

MICRO_TERMS = (
    "page view",
    "page_view",
    "scroll",
    "button",
    "add to cart",
    "begin checkout",
    "engaged",
    "directions",
    "youtube",
)
LEAD_CATEGORIES = {
    "LEAD",
    "SUBMIT_LEAD_FORM",
    "PHONE_CALL_LEAD",
    "BOOK_APPOINTMENT",
    "REQUEST_QUOTE",
    "SIGNUP",
    "CONTACT",
}
SMART_CPA = {"TARGET_CPA", "MAXIMIZE_CONVERSIONS"}
SMART_ROAS = {"TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"}
COLD_CHANNELS = {"DISPLAY", "VIDEO", "DEMAND_GEN", "DISCOVERY"}


def _days(start: str, end: str) -> int:
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except ValueError as error:
        raise InstantAuditAnalysisError("Analysis dates must be YYYY-MM-DD") from error
    value = (last - first).days + 1
    if value < 14:
        raise InstantAuditAnalysisError(
            "Instant Account Audit needs at least 14 complete days"
        )
    return value


def _money(micros: float, currency: str) -> str:
    return f"{currency} {micros / 1_000_000:,.2f}"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _term_root(value: Any) -> str:
    return " ".join(
        part
        for part in "".join(
            character if str(character).isalnum() else " "
            for character in str(value).casefold()
        ).split()
        if part
    )


def _shares_converting_root(
    candidate: Mapping[str, Any],
    converting_roots: Sequence[str],
) -> bool:
    root = _term_root(candidate.get("search_term"))
    return any(
        converted == root or converted in root or root in converted
        for converted in converting_roots
        if converted and root
    )


def _campaign_refs(campaigns: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {"campaign_id": str(item["id"]), "campaign_name": str(item["name"])}
        for item in campaigns
    ]


def _check(
    check_id: str,
    *,
    status: str,
    scope: str,
    evidence: str,
    standard: str,
    effect: str,
    next_step: str,
    window: str,
    evidence_label: str = "Observed in live Google Ads data",
    recovery_group: str | None = None,
) -> dict[str, Any]:
    section_id = check_id[0]
    return {
        "id": check_id,
        "section_id": section_id,
        "section_name": SECTIONS[section_id][0],
        "criterion": CRITERIA[check_id],
        "status": status,
        "scope": scope,
        "evidence": evidence,
        "gma_standard": standard,
        "decision_effect": effect,
        "next_step": next_step,
        "source_window": window,
        "evidence_label": evidence_label,
        "recovery_group": recovery_group,
    }


def _recovery(
    *,
    recovery_id: str,
    priority: int,
    title: str,
    reason: str,
    steps: Sequence[str],
    resolves: Sequence[str],
    campaigns: Sequence[Mapping[str, Any]],
    completion_signal: str,
    owner: str,
    status: str = "ready",
    follow_up_kind: str = "rerun_current_skill",
    follow_up_module_id: str | None = "instant_account_audit",
) -> dict[str, Any]:
    return {
        "id": recovery_id,
        "priority": priority,
        "type": "audit_recovery",
        "status": status,
        "title": title,
        "reason": reason,
        "steps": list(steps),
        "applies_to": _campaign_refs(campaigns),
        "resolves": list(resolves),
        "completion_signal": completion_signal,
        "owner": owner,
        "follow_up": {
            "kind": follow_up_kind,
            "module_id": follow_up_module_id,
            "not_before": None,
        },
        "selectable": True,
    }


def _recommendation(
    check: Mapping[str, Any],
    *,
    action_id: str,
    priority: int,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "priority": priority,
        "severity": "critical" if priority <= 2 else "warning",
        "evidence_label": check["evidence_label"],
        "entity": check["criterion"],
        "resource_name": "",
        "operation_type": "advisory",
        "current_value": {},
        "proposed_value": {},
        "reason": check["decision_effect"],
        "evidence_summary": f"{check['evidence']} ({check['source_window']}).",
        "details": check["next_step"],
        "expected_impact": (
            "Removes a confirmed audit risk or creates the evidence needed for a "
            "safe optimisation decision; no performance uplift is guaranteed."
        ),
        "estimate": {"label": "directional", "check_id": check["id"]},
        "risk": "low",
        "reversible": True,
        "applyability": "task",
        "source_skill": "instant-account-audit",
    }


def _grade(checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sections = []
    earned_total = 0.0
    achievable_total = 0.0
    for section_id, (name, weight, ids) in SECTIONS.items():
        rows = [row for row in checks if row["id"] in ids]
        passes = sum(row["status"] == "pass" for row in rows)
        failures = sum(row["status"] == "fail" for row in rows)
        denominator = passes + failures
        achievable = float(weight) if denominator else 0.0
        earned = float(weight) * passes / denominator if denominator else 0.0
        earned_total += earned
        achievable_total += achievable
        sections.append(
            {
                "id": section_id,
                "name": name,
                "weight": weight,
                "pass": passes,
                "fail": failures,
                "unavailable": sum(row["status"] == "unavailable" for row in rows),
                "not_applicable": sum(
                    row["status"] == "not_applicable" for row in rows
                ),
                "earned": round(earned, 2),
                "achievable": achievable,
            }
        )
    percentage = round(
        earned_total * 100 / achievable_total if achievable_total else 0.0, 1
    )
    if percentage >= 93:
        band = "A+"
    elif percentage >= 89:
        band = "A"
    elif percentage >= 85:
        band = "A-"
    elif percentage >= 80:
        band = "B+"
    elif percentage >= 75:
        band = "B"
    elif percentage >= 70:
        band = "B-"
    elif percentage >= 65:
        band = "C+"
    elif percentage >= 60:
        band = "C"
    elif percentage >= 55:
        band = "C-"
    elif percentage >= 40:
        band = "D"
    else:
        band = "F"
    return {
        "percentage": percentage,
        "band": band,
        "earned_points": round(earned_total, 2),
        "achievable_points": round(achievable_total, 2),
        "sections": sections,
    }


def evaluate_instant_account_audit(
    snapshot: Mapping[str, Any],
    *,
    business_mode: str,
    target_cpa_micros: int | None = None,
    target_roas: float | None = None,
    outcome_quality_confirmed: bool = False,
    brand_terms: Sequence[str] | None = None,
    available_module_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return all 41 deterministic audit checks and bounded next actions."""

    if business_mode not in {"lead_gen", "ecommerce"}:
        raise InstantAuditAnalysisError(
            "Instant Account Audit V1 supports lead_gen or ecommerce scopes"
        )
    analysis_start = str(snapshot["analysis_start"])
    analysis_end = str(snapshot["analysis_end"])
    days = _days(analysis_start, analysis_end)
    window = f"{analysis_start} to {analysis_end}"
    currency = str(snapshot.get("currency") or "")
    campaigns = [dict(item) for item in snapshot.get("campaigns") or []]
    if not campaigns:
        raise InstantAuditAnalysisError("No campaigns were available in the scope")
    enabled = [item for item in campaigns if item.get("status") == "ENABLED"]
    spend_total = sum(int(item.get("cost_micros") or 0) for item in campaigns)
    conversions_total = sum(float(item.get("conversions") or 0) for item in campaigns)
    all_conversions_total = sum(
        float(item.get("all_conversions") or item.get("conversions") or 0)
        for item in campaigns
    )
    value_total = sum(float(item.get("conversions_value") or 0) for item in campaigns)
    reference_cpa = _ratio(spend_total, conversions_total)
    reference_roas = _ratio(value_total, spend_total / 1_000_000)
    evidence = dict(snapshot.get("evidence") or {})
    performance_trusted = bool(outcome_quality_confirmed)
    account = dict(evidence.get("account") or {})
    conversion_actions = [
        dict(item) for item in evidence.get("conversion_actions") or []
    ]
    checks: list[dict[str, Any]] = []
    available_modules = set(
        (
            "instant_account_audit",
            "red_flag_radar",
            "budget_reallocator",
        )
        if available_module_ids is None
        else available_module_ids
    )

    def add(check_id: str, **kwargs: Any) -> None:
        checks.append(_check(check_id, window=window, **kwargs))

    def specialist_or_manual(
        *,
        module_id: str,
        number: int,
        name: str,
        specialist_step: str,
        manual_step: str,
    ) -> str:
        if module_id in available_modules:
            return specialist_step
        return (
            f"Do this now: {manual_step} "
            f"Skill {number} — {name} is still being added to the hosted runtime, "
            "so this manual route keeps the review moving."
        )

    # A — tracking and measurement
    if evidence.get("account_verified"):
        auto = bool(account.get("auto_tagging_enabled"))
        add(
            "A1",
            status="pass" if auto else "fail",
            scope="Account",
            evidence=f"Auto-tagging is {'enabled' if auto else 'disabled'}.",
            standard="Auto-tagging must be enabled so paid visits can be attributed.",
            effect="Visit and conversion attribution is reliable only when ad clicks retain their Google click identifier.",
            next_step=(
                "No action is needed."
                if auto
                else "Open Google Ads → Admin → Account settings → Auto-tagging, enable it, save, and rerun Skill 1."
            ),
        )
        tracking_id = account.get("conversion_tracking_id") or account.get(
            "cross_account_conversion_tracking_id"
        )
        enabled_actions = [
            item for item in conversion_actions if item.get("status") == "ENABLED"
        ]
        live = bool(tracking_id) and bool(enabled_actions)
        add(
            "A2",
            status="pass" if live else "fail",
            scope="Account",
            evidence=(
                f"Tracking ID {'present' if tracking_id else 'missing'}; "
                f"{len(enabled_actions)} enabled conversion actions."
            ),
            standard="A conversion tracking ID and at least one enabled conversion action are required.",
            effect="Without live conversion tracking, Google Ads cannot report or optimise genuine outcomes.",
            next_step=(
                "No action is needed."
                if live
                else "Open Goals → Conversions → Summary, restore or create the genuine outcome action, verify its tag/import status, then rerun Skill 1."
            ),
        )
    else:
        for check_id in ("A1", "A2"):
            add(
                check_id,
                status="unavailable",
                scope="Account",
                evidence="Account tracking settings were not returned.",
                standard="GMA must read the current account tracking setting before scoring it.",
                effect="This check is excluded from the grade until the live setting is available.",
                next_step="Reconnect Google Ads with account-read access, then rerun Skill 1.",
                evidence_label="Needed evidence",
                recovery_group="account_settings",
            )

    goal_verified = bool(campaigns) and all(
        bool(item.get("goal_scope_verified")) for item in campaigns
    )
    primary_enabled = [
        item
        for item in conversion_actions
        if item.get("status") == "ENABLED" and item.get("primary_for_goal")
    ]
    wanted_categories = (
        {"PURCHASE"} if business_mode == "ecommerce" else LEAD_CATEGORIES
    )
    macro_actions = [
        item for item in primary_enabled if item.get("category") in wanted_categories
    ]
    if not evidence.get("conversion_actions_verified") or not goal_verified:
        add(
            "A3",
            status="unavailable",
            scope="Selected campaigns",
            evidence="Campaign-effective goal scope or conversion actions could not be fully resolved.",
            standard="The exact biddable business outcome must be verified per campaign.",
            effect="The audit cannot safely judge bidding or CPA/ROAS until the effective goal is known.",
            next_step="Open each selected campaign → Settings → Goals, record its goal scope and genuine primary actions, then rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="goal_scope",
        )
    elif not outcome_quality_confirmed:
        add(
            "A3",
            status="unavailable",
            scope="Selected campaigns",
            evidence=(
                f"{len(macro_actions)} configured primary lead/purchase action(s) "
                "look like business outcomes, but Google Ads cannot prove they are "
                "genuine, non-duplicated customers."
            ),
            standard="At least one verified genuine purchase or lead action must control campaign bidding.",
            effect="Configured goal labels alone cannot prove that automated bidding is optimising toward real business results.",
            next_step="Compare the named action totals with bookings, CRM leads, or purchases; classify the genuine outcomes; then rerun Skill 1.",
            evidence_label="Business confirmation needed",
            recovery_group="outcome_target",
        )
    else:
        add(
            "A3",
            status="pass" if macro_actions else "fail",
            scope="Selected campaigns",
            evidence=f"{len(macro_actions)} enabled biddable macro action(s) match the {business_mode.replace('_', ' ')} playbook.",
            standard="At least one genuine purchase or lead action must control campaign bidding.",
            effect="Automated bidding is useful only when it optimises toward a real business result.",
            next_step=(
                "Confirm backend quality and keep the current goal scope."
                if macro_actions
                else "Open Goals → Conversions → Summary, make the genuine purchase/lead action primary, then verify each campaign's Goals setting and rerun."
            ),
        )

    if evidence.get("conversion_actions_verified"):
        duplicate_keys = Counter(
            (
                str(item.get("category")),
                (
                    "PURCHASE_EVENT"
                    if item.get("category") == "PURCHASE"
                    else str(item.get("type"))
                ),
            )
            for item in primary_enabled
        )
        duplicate_pairs = [pair for pair, count in duplicate_keys.items() if count >= 2]
        add(
            "A4",
            status="fail" if duplicate_pairs else "pass",
            scope="Account",
            evidence=(
                "Potential duplicate primary category/type pairs: "
                + ", ".join("/".join(pair) for pair in duplicate_pairs)
                if duplicate_pairs
                else "No duplicate primary category/type pair was found."
            ),
            standard="Two primary actions must not count the same business event.",
            effect="Double-counting makes CPA, ROAS, and automated bidding misleading.",
            next_step=(
                "Open Goals → Conversions → Summary, compare the named pair against tag/import sources, keep one genuine action primary, and move the duplicate to secondary."
                if duplicate_pairs
                else "No action is needed."
            ),
        )
        micros = [
            item["name"]
            for item in primary_enabled
            if item.get("category") in {"DEFAULT", "PAGE_VIEW"}
            or any(term in str(item.get("name", "")).casefold() for term in MICRO_TERMS)
        ]
        add(
            "A5",
            status="fail" if micros else "pass",
            scope="Account",
            evidence=(
                "Primary micro-actions: " + ", ".join(micros)
                if micros
                else "No obvious page-view, scroll, click, cart, or checkout action is primary."
            ),
            standard="Micro and supporting actions belong in Secondary, not in the Conversions bidding column.",
            effect="Primary micro-actions train Google to optimise activity rather than customers.",
            next_step=(
                "Open Goals → Conversions → Summary, select each named micro-action, change Action optimisation to Secondary, and rerun."
                if micros
                else "No action is needed."
            ),
        )
        models = sorted(
            {
                str(item.get("attribution_model"))
                for item in primary_enabled
                if item.get("attribution_model")
            }
        )
        add(
            "A6",
            status="fail" if len(models) > 1 else "pass",
            scope="Account",
            evidence=f"Primary attribution models: {', '.join(models) or 'none returned'}.",
            standard="Comparable primary actions should use one attribution model.",
            effect="Mixed attribution models make campaign comparisons inconsistent.",
            next_step=(
                "Open each primary action in Goals → Conversions and align attribution after confirming the business reporting requirement."
                if len(models) > 1
                else "No urgent action is needed; review a uniform last-click setup for a future data-driven migration."
            ),
        )
        values = [float(item.get("default_value") or 0) for item in primary_enabled]
        static_purchase = any(
            item.get("category") == "PURCHASE" and item.get("always_use_default_value")
            for item in primary_enabled
        )
        value_bidding = any(
            item.get("bidding_strategy_type") in SMART_ROAS for item in enabled
        )
        uniform_leads = (
            business_mode == "lead_gen"
            and bool(values)
            and len(set(values)) <= 1
            and value_bidding
        )
        value_fail = static_purchase if business_mode == "ecommerce" else uniform_leads
        add(
            "A7",
            status="fail" if value_fail else "pass",
            scope="Account",
            evidence=(
                "Purchase conversion uses a forced default value."
                if static_purchase
                else (
                    "All primary leads carry the same value while value-based bidding is active."
                    if uniform_leads
                    else "No value-setting conflict with the current bidding mode was confirmed."
                )
            ),
            standard="Purchase revenue must be dynamic; lead values must represent quality before value bidding is used.",
            effect="Untrustworthy values direct budget toward the wrong outcomes.",
            next_step=(
                "Open Goals → Conversions, correct the purchase value/tag or import qualified-lead values before using value-based bidding."
                if value_fail
                else "No action is needed."
            ),
        )
    else:
        for check_id in ("A4", "A5", "A6", "A7"):
            add(
                check_id,
                status="unavailable",
                scope="Account",
                evidence="Conversion-action configuration was not returned.",
                standard="The primary actions, type, attribution, and value settings must be read before scoring.",
                effect="This check is excluded from the grade until the live goal configuration is available.",
                next_step="Reconnect Google Ads with conversion-action read access and rerun Skill 1.",
                evidence_label="Needed evidence",
                recovery_group="conversion_actions",
            )

    coherence_fail = conversions_total == 0 and all_conversions_total > 10
    add(
        "A8",
        status="fail" if coherence_fail else "pass",
        scope="Account",
        evidence=f"{conversions_total:g} primary conversions and {all_conversions_total:g} all conversions.",
        standard="A large all-conversions total with zero primary conversions signals goal hygiene trouble.",
        effect="The main Conversions column may not represent the intended business outcome.",
        next_step=(
            "Open Goals → Conversions → Summary, compare Primary versus Secondary action counts, repair the genuine outcome's optimisation setting, and rerun."
            if coherence_fail
            else "No action is needed."
        ),
    )
    latest = dict(evidence.get("latest_totals") or {})
    previous = dict(evidence.get("previous_totals") or {})
    trend_available = bool(latest) and bool(previous)
    tracking_suspect = False
    if trend_available:
        latest_conv = float(latest.get("conversions") or 0)
        prev_conv = float(previous.get("conversions") or 0)
        latest_clicks = float(latest.get("clicks") or 0)
        prev_clicks = float(previous.get("clicks") or 0)
        conv_change = (latest_conv - prev_conv) / prev_conv if prev_conv else None
        click_change = (
            (latest_clicks - prev_clicks) / prev_clicks if prev_clicks else None
        )
        tracking_suspect = (
            conv_change is not None
            and conv_change < -0.40
            and (click_change is None or click_change > -0.15)
        )
        add(
            "A9",
            status="fail" if tracking_suspect else "pass",
            scope="Account",
            evidence=(
                f"Latest complete week: {latest_conv:g} conversions from {latest_clicks:g} clicks; "
                f"previous week: {prev_conv:g} from {prev_clicks:g}."
            ),
            standard="A conversion fall over 40% without a similar traffic fall is treated as a possible tracking break.",
            effect="Bid, budget, and structure changes are held when measurement may be broken.",
            next_step=(
                "Open Goals → Conversions → Summary, compare action-level counts and diagnostics for both weeks, test the form/call/import path, fix any break, then rerun before changing bids or budgets."
                if tracking_suspect
                else "No tracking-break action is needed."
            ),
        )
    else:
        add(
            "A9",
            status="unavailable",
            scope="Account",
            evidence="The two complete comparison weeks were not both available.",
            standard="GMA requires two complete weeks to screen for a current tracking break.",
            effect="This check is excluded from the grade, and bid/budget conclusions remain conservative.",
            next_step="Pull the latest and previous complete seven-day totals, then rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="trend_data",
        )

    # B — server cannot crawl the page; retain every promised criterion.
    for check_id in ("B1", "B2", "B3"):
        add(
            check_id,
            status="unavailable",
            scope="Top-spend landing page",
            evidence="This Google Ads-only run identified destinations but did not open page content.",
            standard="GMA reviews page availability, intent match, visible action, and page type before campaign settings.",
            effect="Landing-page conclusions are excluded rather than guessed.",
            next_step="Open the top-spend final URL on desktop and mobile, verify it loads, matches the paid intent, and exposes the real form/call/purchase action above the fold; then rerun or attach the page review.",
            evidence_label="Manual verification needed",
            recovery_group="landing_page_review",
        )
    landing_pages = [dict(item) for item in evidence.get("landing_pages") or []]
    if not performance_trusted:
        add(
            "B4",
            status="unavailable",
            scope="Landing pages",
            evidence="Per-page clicks are available, but the reported outcomes have not been confirmed as genuine customers.",
            standard="Landing-page efficiency requires a trusted business outcome.",
            effect="The audit will not label a page healthy or wasteful from unverified conversion counts.",
            next_step="Confirm which conversion actions represent genuine customers, then rerun Skill 1.",
            evidence_label="Business confirmation needed",
            recovery_group="outcome_target",
        )
    elif evidence.get("landing_pages_verified"):
        bad_pages = [
            item
            for item in landing_pages
            if int(item.get("clicks") or 0) >= 100
            and float(item.get("conversions") or 0) == 0
            and float(item.get("all_conversions") or 0) == 0
        ]
        qualifying = [
            item for item in landing_pages if int(item.get("clicks") or 0) >= 100
        ]
        add(
            "B4",
            status=(
                "fail" if bad_pages else ("pass" if qualifying else "not_applicable")
            ),
            scope="Landing pages",
            evidence=(
                "; ".join(
                    f"{item['url']} — {item['clicks']} clicks, 0 outcomes"
                    for item in bad_pages[:3]
                )
                or (
                    f"{len(qualifying)} page(s) met the 100-click evidence floor without a zero-outcome failure."
                    if qualifying
                    else "No landing page reached 100 clicks in the selected window."
                )
            ),
            standard="A page with at least 100 clicks and no primary or secondary outcome is a confirmed destination risk.",
            effect="High-traffic zero-outcome pages can waste spend even when campaign targeting is sound.",
            next_step=(
                "Open each named page, test its form/call/purchase path, compare intent and mobile experience, repair the page or stop routing paid traffic there, then rerun."
                if bad_pages
                else "No action is needed at the current evidence level."
            ),
        )
    else:
        add(
            "B4",
            status="unavailable",
            scope="Landing pages",
            evidence="Per-URL click and outcome totals were unavailable.",
            standard="GMA needs per-URL traffic and outcomes to judge high-spend dead pages.",
            effect="This check is excluded from the grade until destination performance is readable.",
            next_step="Restore landing-page report access and rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="landing_page_data",
        )

    # C — goals, bidding, feasibility
    spend_share = {
        str(item["id"]): (
            int(item.get("cost_micros") or 0) / spend_total if spend_total else 0
        )
        for item in campaigns
    }
    strategy_mismatches = []
    for item in enabled:
        if spend_share[str(item["id"])] < 0.20:
            continue
        strategy = str(item.get("bidding_strategy_type") or "")
        if business_mode == "ecommerce" and strategy in SMART_CPA:
            strategy_mismatches.append(item["name"])
        if business_mode == "lead_gen" and strategy in SMART_ROAS:
            strategy_mismatches.append(item["name"])
    add(
        "C1",
        status="fail" if strategy_mismatches else "pass",
        scope="Campaigns with at least 20% of spend",
        evidence=(
            "Goal/strategy mismatch: " + ", ".join(strategy_mismatches)
            if strategy_mismatches
            else "No major-spend campaign used the opposite business-mode bidding lane."
        ),
        standard="Ecommerce normally bids to trusted value; lead generation normally bids to trusted lead volume or quality.",
        effect="The wrong strategy can optimise the account toward a KPI that does not represent its economics.",
        next_step=(
            specialist_or_manual(
                module_id="bid_strategy_check",
                number=8,
                name="Bid Strategy Check",
                specialist_step="Run Skill 8 — Bid Strategy Check on the named campaigns before changing strategy.",
                manual_step="Open each named campaign → Settings → Bidding; record the current strategy, goal actions, target, last-30-day conversions, and actual CPA/ROAS. Keep the strategy unchanged until those facts have been reviewed.",
            )
            if strategy_mismatches
            else "No action is needed."
        ),
    )
    volume_violations = []
    for item in enabled:
        if spend_share[str(item["id"])] < 0.20:
            continue
        strategy = str(item.get("bidding_strategy_type") or "")
        conversions = float(item.get("conversions") or 0)
        floor = (
            30
            if strategy == "TARGET_CPA"
            else (50 if strategy == "TARGET_ROAS" else None)
        )
        if floor and conversions < floor:
            volume_violations.append(f"{item['name']} ({conversions:g}/{floor})")
    smart_targets = any(
        str(item.get("bidding_strategy_type") or "") in {"TARGET_CPA", "TARGET_ROAS"}
        for item in enabled
    )
    add(
        "C2",
        status=(
            "unavailable"
            if smart_targets and not performance_trusted
            else (
                "fail"
                if volume_violations
                else ("pass" if smart_targets else "not_applicable")
            )
        ),
        scope="Major-spend automated-bidding campaigns",
        evidence=(
            "Configured target bidding exists, but reported outcomes have not been confirmed as genuine customers."
            if smart_targets and not performance_trusted
            else "Below GMA volume floor: " + ", ".join(volume_violations)
            if volume_violations
            else (
                "Every relevant target-bidding campaign met its conversion-volume floor."
                if smart_targets
                else "No tCPA or tROAS target was active."
            )
        ),
        standard="GMA requires 30 conversions for a tCPA decision and 50 for tROAS.",
        effect="Targets become unstable when a campaign lacks enough recent outcomes.",
        next_step=(
            "Confirm the genuine outcomes, then rerun the conversion-volume check."
            if smart_targets and not performance_trusted
            else specialist_or_manual(
                module_id="bid_strategy_check",
                number=8,
                name="Bid Strategy Check",
                specialist_step="Run Skill 8, then consolidate or loosen the target only after reviewing the campaign's live goal and learning state.",
                manual_step="Keep the current target unchanged, confirm the campaign's genuine 30-day outcomes, and wait until it has at least 30 conversions for tCPA or 50 for tROAS before reassessing.",
            )
            if volume_violations
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if smart_targets and not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group=(
            "outcome_target" if smart_targets and not performance_trusted else None
        ),
    )
    unrealistic = []
    for item in enabled:
        if spend_share[str(item["id"])] < 0.10:
            continue
        conversions = float(item.get("conversions") or 0)
        cost = int(item.get("cost_micros") or 0)
        actual_cpa = _ratio(cost, conversions)
        actual_roas = _ratio(
            float(item.get("conversions_value") or 0), cost / 1_000_000
        )
        target_cpa = int(item.get("target_cpa_micros") or 0)
        target_value = float(item.get("target_roas") or 0)
        if target_cpa and actual_cpa and target_cpa < actual_cpa * 0.75:
            unrealistic.append(item["name"])
        if target_value and actual_roas and target_value > actual_roas * 1.33:
            unrealistic.append(item["name"])
    explicit_targets = any(
        int(item.get("target_cpa_micros") or 0) or float(item.get("target_roas") or 0)
        for item in enabled
    )
    add(
        "C3",
        status=(
            "unavailable"
            if explicit_targets and not performance_trusted
            else (
                "fail"
                if unrealistic
                else ("pass" if explicit_targets else "not_applicable")
            )
        ),
        scope="Campaigns with at least 10% of spend",
        evidence=(
            "A configured target exists, but its achieved CPA/ROAS is not trustworthy until the genuine outcomes are confirmed."
            if explicit_targets and not performance_trusted
            else "Targets over 25% more aggressive than achieved performance: "
            + ", ".join(sorted(set(unrealistic)))
            if unrealistic
            else (
                "No explicit target breached the GMA realism threshold."
                if explicit_targets
                else "No explicit CPA or ROAS target was active."
            )
        ),
        standard="A target should not be more than 25% tighter than the campaign's recent actual result.",
        effect="An unrealistic target can suppress traffic without reaching the goal.",
        next_step=(
            "Confirm the genuine outcomes, then compare the configured target with achieved CPA/ROAS."
            if explicit_targets and not performance_trusted
            else specialist_or_manual(
                module_id="bid_strategy_check",
                number=8,
                name="Bid Strategy Check",
                specialist_step="Run Skill 8 to calculate a staged target from current achieved performance; do not tighten it further meanwhile.",
                manual_step="Keep the target from becoming any tighter. Compare it with the campaign's genuine last-30-day CPA/ROAS and prepare a separate staged target review based on achieved performance.",
            )
            if unrealistic
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if explicit_targets and not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group=(
            "outcome_target" if explicit_targets and not performance_trusted else None
        ),
    )
    avg_cpc = _ratio(
        spend_total, sum(int(item.get("clicks") or 0) for item in campaigns)
    )
    account_cvr = _ratio(
        conversions_total, sum(int(item.get("clicks") or 0) for item in campaigns)
    )
    implied_cpa = _ratio(avg_cpc or 0, account_cvr or 0)
    if not performance_trusted:
        impossible = False
        evidence_text = "A business target was supplied, but reported outcomes have not been confirmed as genuine customers."
    elif business_mode == "lead_gen" and target_cpa_micros:
        impossible = implied_cpa is not None and implied_cpa > target_cpa_micros * 1.25
        evidence_text = (
            f"Current click price and conversion rate imply {_money(implied_cpa or 0, currency)} CPA versus "
            f"the {_money(target_cpa_micros, currency)} goal."
        )
    elif business_mode == "ecommerce" and target_roas:
        impossible = reference_roas is not None and reference_roas < target_roas * 0.75
        evidence_text = f"Current account ROAS is {reference_roas:.2f} versus the {target_roas:.2f} goal."
    else:
        impossible = False
        evidence_text = "No confirmed business target was supplied."
    add(
        "C4",
        status=(
            "unavailable"
            if not performance_trusted
            else (
                "fail"
                if impossible
                else (
                    "pass"
                    if (
                        target_cpa_micros
                        if business_mode == "lead_gen"
                        else target_roas
                    )
                    else "not_applicable"
                )
            )
        ),
        scope="Account",
        evidence=evidence_text,
        standard="Current click cost and conversion rate must make the stated goal achievable within 25%.",
        effect="Impossible goal math must be fixed before tactical optimisations are prioritised.",
        next_step=(
            "Confirm which reported actions are genuine customers, then rerun the feasibility calculation."
            if not performance_trusted
            else "Use the Goal Benchmark Report to decide whether CPC, conversion rate, offer economics, or the target itself must change."
            if impossible
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group="outcome_target" if not performance_trusted else None,
    )
    manual_spend = sum(
        int(item.get("cost_micros") or 0)
        for item in enabled
        if item.get("bidding_strategy_type") in {"MANUAL_CPC", "MAXIMIZE_CLICKS"}
    )
    manual_share = _ratio(manual_spend, spend_total) or 0
    manual_candidate = manual_share > 0.20
    manual_fail = (
        manual_candidate and performance_trusted and conversions_total >= 20
    )
    add(
        "C5",
        status=(
            "unavailable"
            if manual_candidate and not performance_trusted
            else ("fail" if manual_fail else "pass")
        ),
        scope="Account",
        evidence=f"{_percent(manual_share)} of spend used Manual CPC or Maximize Clicks; {conversions_total:g} conversions reported.",
        standard="When at least 20 recent conversions exist, manual/click bidding should not carry over 20% of spend without a documented exception.",
        effect="The account may be ignoring enough outcome data to use automated bidding safely.",
        next_step=(
            "Confirm genuine outcome volume before deciding whether manual/click bidding should change."
            if manual_candidate and not performance_trusted
            else specialist_or_manual(
                module_id="bid_strategy_check",
                number=8,
                name="Bid Strategy Check",
                specialist_step="Run Skill 8 on the affected campaigns and document the exception or prepare a staged strategy change for separate approval.",
                manual_step="List the affected campaigns, confirm their genuine outcome volume and current learning state, and document why manual/click bidding remains necessary. Keep bidding unchanged until that review is complete.",
            )
            if manual_fail
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if manual_candidate and not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group=(
            "outcome_target" if manual_candidate and not performance_trusted else None
        ),
    )
    if evidence.get("change_history_verified"):
        thrash = [
            name
            for name, count in dict(evidence.get("bid_change_counts") or {}).items()
            if int(count) >= 3
        ]
        add(
            "C6",
            status="fail" if thrash else "pass",
            scope="Selected campaigns",
            evidence=(
                "Three or more bid/target changes: " + ", ".join(thrash)
                if thrash
                else "No selected campaign had three bid/target changes in the available 30-day history."
            ),
            standard="Three or more bid/target changes inside 30 days prevents a stable learning period.",
            effect="Frequent changes make it impossible to tell whether bidding is working.",
            next_step=(
                specialist_or_manual(
                    module_id="bid_strategy_check",
                    number=8,
                    name="Bid Strategy Check",
                    specialist_step="Stop bid/target edits, record the last change date, wait through the learning window, then rerun Skill 8.",
                    manual_step="Stop bid and target edits, record the most recent change date for each named campaign, and wait through a full learning window before reassessing.",
                )
                if thrash
                else "No action is needed."
            ),
        )
    else:
        add(
            "C6",
            status="unavailable",
            scope="Selected campaigns",
            evidence="Bid and target change history was unavailable.",
            standard="GMA checks 30 days of strategy and target changes before judging bidding.",
            effect="This check is excluded and current performance conclusions remain conservative.",
            next_step="Restore Change history read access and rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="change_history",
        )

    # D — budget and impression share
    target = target_cpa_micros if business_mode == "lead_gen" else target_roas
    if not target or (business_mode == "lead_gen" and not outcome_quality_confirmed):
        add(
            "D1",
            status="unavailable",
            scope="Search and Shopping campaigns",
            evidence="A trusted target/outcome baseline was not confirmed.",
            standard="A campaign is a scaling winner only when its result beats a trusted target.",
            effect="Budget increases are held until the result being optimised is trusted.",
            next_step="Confirm the genuine lead actions and CPA goal, or confirm the ecommerce ROAS goal, then rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="outcome_target",
        )
    else:
        winners = []
        for item in enabled:
            cost = int(item.get("cost_micros") or 0)
            conversions = float(item.get("conversions") or 0)
            actual_cpa = _ratio(cost, conversions)
            actual_roas = _ratio(
                float(item.get("conversions_value") or 0), cost / 1_000_000
            )
            beats = (
                actual_cpa is not None and actual_cpa <= target_cpa_micros
                if business_mode == "lead_gen"
                else actual_roas is not None and actual_roas >= target_roas
            )
            if (
                beats
                and float(item.get("search_budget_lost_impression_share") or 0) > 0.10
            ):
                winners.append(item["name"])
        add(
            "D1",
            status="fail" if winners else "pass",
            scope="Search and Shopping campaigns",
            evidence=(
                "Target-beating campaigns losing over 10% to budget: "
                + ", ".join(winners)
                if winners
                else "No target-beating campaign crossed the 10% budget-loss threshold."
            ),
            standard="A proven campaign losing over 10% of eligible impressions to budget is a scaling candidate.",
            effect="Qualified demand may be missed while weaker areas retain spend.",
            next_step=(
                "Run Skill 12 — Budget Reallocator; validate rank loss, shared budgets, and current values before preparing any move."
                if winners
                else "No action is needed."
            ),
        )
    losers = []
    if performance_trusted and (reference_cpa or reference_roas):
        for item in campaigns:
            if spend_share[str(item["id"])] < 0.10 or int(item.get("clicks") or 0) < 30:
                continue
            cost = int(item.get("cost_micros") or 0)
            conversions = float(item.get("conversions") or 0)
            actual_cpa = _ratio(cost, conversions)
            actual_roas = _ratio(
                float(item.get("conversions_value") or 0), cost / 1_000_000
            )
            bad = (
                actual_cpa is None or actual_cpa >= float(reference_cpa) * 2
                if business_mode == "lead_gen"
                else actual_roas is None or actual_roas <= float(reference_roas) * 0.5
            )
            if bad:
                losers.append(item["name"])
    add(
        "D2",
        status=(
            "unavailable"
            if not performance_trusted
            else (
                "fail"
                if losers
                else (
                    "pass"
                    if (reference_cpa or reference_roas)
                    else "not_applicable"
                )
            )
        ),
        scope="Campaigns with at least 10% of spend",
        evidence=(
            "Reported outcomes have not been confirmed as genuine customers."
            if not performance_trusted
            else "Campaigns at twice reference CPA / half reference ROAS: "
            + ", ".join(losers)
            if losers
            else (
                "No material-spend campaign crossed the loser threshold."
                if (reference_cpa or reference_roas)
                else "The account did not have a usable reference CPA/ROAS."
            )
        ),
        standard="Material-spend campaigns at twice reference CPA or half reference ROAS should not remain over-funded.",
        effect="Budget can remain trapped in a proven weak lane.",
        next_step=(
            "Confirm the genuine outcome and CPA/ROAS target, then rerun Skill 1 before labeling a campaign as a donor."
            if not performance_trusted
            else "Run Skill 12 to evaluate safe donor eligibility; do not cut budget while tracking or learning is unresolved."
            if losers
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group="outcome_target" if not performance_trusted else None,
    )
    shared = defaultdict(list)
    for item in campaigns:
        if item.get("budget_explicitly_shared"):
            shared[str(item.get("budget_resource_name"))].append(item)
    mixed_pools = []
    for pool, items in shared.items():
        winner = False
        loser = False
        for item in items:
            cost = int(item.get("cost_micros") or 0)
            conversions = float(item.get("conversions") or 0)
            actual_cpa = _ratio(cost, conversions)
            actual_roas = _ratio(
                float(item.get("conversions_value") or 0),
                cost / 1_000_000,
            )
            if business_mode == "lead_gen" and reference_cpa:
                winner = winner or (
                    actual_cpa is not None and actual_cpa <= reference_cpa
                )
                loser = loser or (actual_cpa is None or actual_cpa >= reference_cpa * 2)
            elif business_mode == "ecommerce" and reference_roas:
                winner = winner or (
                    actual_roas is not None and actual_roas >= reference_roas
                )
                loser = loser or (
                    actual_roas is None or actual_roas <= reference_roas * 0.5
                )
        if winner and loser:
            mixed_pools.append(pool)
    add(
        "D3",
        status=(
            "unavailable"
            if shared and not performance_trusted
            else ("fail" if mixed_pools else ("pass" if shared else "not_applicable"))
        ),
        scope="Shared budgets",
        evidence=(
            "Shared budgets exist, but winner/loser compatibility cannot be judged until outcomes are confirmed."
            if shared and not performance_trusted
            else f"{len(mixed_pools)} shared budget pool(s) require compatibility review."
            if mixed_pools
            else (
                "Shared budgets found without a mixed-pool flag."
                if shared
                else "No shared budget was used."
            )
        ),
        standard="A shared pool must not mix proven winners and material losers.",
        effect="A weak campaign can consume a strong campaign's budget invisibly.",
        next_step=(
            "Confirm the genuine outcomes and business target, then rerun the shared-budget compatibility check."
            if shared and not performance_trusted
            else "Run Skill 12 and review every campaign in each named pool before separating or reallocating it."
            if mixed_pools
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if shared and not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group=(
            "outcome_target" if shared and not performance_trusted else None
        ),
    )
    rank_losers = [
        item["name"]
        for item in enabled
        if spend_share[str(item["id"])] >= 0.10
        and float(item.get("search_rank_lost_impression_share") or 0) > 0.50
    ]
    search_exists = any(
        item.get("channel_type") in {"SEARCH", "SHOPPING"} for item in campaigns
    )
    add(
        "D4",
        status=(
            "fail" if rank_losers else ("pass" if search_exists else "not_applicable")
        ),
        scope="Search and Shopping campaigns",
        evidence=(
            "Over 50% lost to ad rank: " + ", ".join(rank_losers)
            if rank_losers
            else (
                "No major campaign crossed the 50% rank-loss threshold."
                if search_exists
                else "No Search/Shopping campaign was in scope."
            )
        ),
        standard="Rank-constrained campaigns need relevance, quality, or bid work before more budget.",
        effect="Raising budget does not solve auctions lost because the ad is not competitive enough.",
        next_step=(
            specialist_or_manual(
                module_id="quality_score_booster",
                number=6,
                name="Quality Score Booster",
                specialist_step="Run Skill 6 — Quality Score Booster and Skill 8 — Bid Strategy Check before considering budget.",
                manual_step="Review the affected campaign's keyword Quality Score components, ad relevance, landing-page experience, and current bid target. Do not add budget until the rank constraint has been diagnosed.",
            )
            if rank_losers
            else "No action is needed."
        ),
    )

    # E — structure and traffic control
    normalized_brands = [
        term.casefold().strip() for term in brand_terms or [] if term.strip()
    ]
    search_terms = [dict(item) for item in evidence.get("search_terms") or []]
    if not normalized_brands or not evidence.get("search_terms_verified"):
        add(
            "E1",
            status="unavailable",
            scope="Search campaigns",
            evidence="A confirmed brand list and searchable term rows were not both available.",
            standard="Brand terms must be confirmed before judging separation or negatives.",
            effect="Brand leakage is excluded rather than inferred from campaign names.",
            next_step="Confirm the company/brand names and rerun Skill 1 with search-term access.",
            evidence_label="Needed evidence",
            recovery_group="brand_terms",
        )
    else:
        brand_clicks_by_campaign = defaultdict(int)
        clicks_by_campaign = defaultdict(int)
        for row in search_terms:
            cid = str(row.get("campaign_id"))
            clicks = int(row.get("clicks") or 0)
            clicks_by_campaign[cid] += clicks
            if any(
                term in str(row.get("search_term", "")).casefold()
                for term in normalized_brands
            ):
                brand_clicks_by_campaign[cid] += clicks
        leakage = [
            item["name"]
            for item in campaigns
            if clicks_by_campaign[str(item["id"])]
            and brand_clicks_by_campaign[str(item["id"])]
            / clicks_by_campaign[str(item["id"])]
            >= 0.10
            and "brand" not in str(item["name"]).casefold()
        ]
        add(
            "E1",
            status="fail" if leakage else "pass",
            scope="Search campaigns",
            evidence=(
                "At least 10% brand clicks inside non-brand campaigns: "
                + ", ".join(leakage)
                if leakage
                else "No non-brand campaign crossed the 10% brand-click threshold."
            ),
            standard="Brand demand should be isolated so it does not inflate non-brand performance.",
            effect="Mixed brand traffic hides the real cost of acquiring new demand.",
            next_step=(
                specialist_or_manual(
                    module_id="structure_fixer",
                    number=7,
                    name="Structure Fixer",
                    specialist_step="Run Skill 7 — Structure Fixer to prepare brand separation and protected negatives for review.",
                    manual_step="Export the leaking brand queries, confirm the protected brand list, and draft a separate brand/non-brand routing plan with protected negatives for review before changing structure.",
                )
                if leakage
                else "No action is needed."
            ),
        )
    display_expansion = [
        item["name"]
        for item in enabled
        if item.get("channel_type") == "SEARCH"
        and bool(item.get("target_content_network"))
    ]
    add(
        "E2",
        status="fail" if display_expansion else "pass",
        scope="Search campaigns",
        evidence=(
            "Display Expansion enabled: " + ", ".join(display_expansion)
            if display_expansion
            else "No Search campaign had Display Expansion enabled."
        ),
        standard="Search and Display traffic should not be mixed in one campaign.",
        effect="Mixed inventory can hide lower-intent spend inside Search reporting.",
        next_step=(
            "Open each named campaign → Settings → Networks, turn off Display Network only after reviewing its contribution, then prepare the exact change for approval."
            if display_expansion
            else "No action is needed."
        ),
    )
    spend_by_channel = defaultdict(int)
    for item in campaigns:
        spend_by_channel[str(item.get("channel_type"))] += int(
            item.get("cost_micros") or 0
        )
    largest_channel = (
        max(spend_by_channel, key=spend_by_channel.get) if spend_by_channel else ""
    )
    lane_fail = (
        business_mode == "lead_gen"
        and largest_channel != "SEARCH"
        or business_mode == "ecommerce"
        and spend_by_channel["SEARCH"]
        > spend_by_channel["PERFORMANCE_MAX"] + spend_by_channel["SHOPPING"]
    )
    add(
        "E3",
        status="fail" if lane_fail else "pass",
        scope="Account",
        evidence=f"Largest spend lane: {largest_channel.replace('_', ' ').title() or 'none'}.",
        standard="Lead generation normally centres on Search; ecommerce normally centres on Shopping/PMax.",
        effect="The account may be funding a lower-control or lower-intent lane before its core engine.",
        next_step=(
            specialist_or_manual(
                module_id="structure_fixer",
                number=7,
                name="Structure Fixer",
                specialist_step="Run Skill 7 to review the documented exception and design the correct campaign hierarchy before moving budget.",
                manual_step="Document why the current largest spend channel should lead this account, compare it with the expected core channel, and draft the intended campaign hierarchy before moving budget.",
            )
            if lane_fail
            else "No action is needed."
        ),
    )
    cold_bad = []
    for item in enabled:
        if (
            item.get("channel_type") not in COLD_CHANNELS
            or spend_share[str(item["id"])] < 0.15
        ):
            continue
        cost = int(item.get("cost_micros") or 0)
        conversions = float(item.get("conversions") or 0)
        actual_cpa = _ratio(cost, conversions)
        if conversions == 0 or (
            business_mode == "lead_gen"
            and reference_cpa
            and actual_cpa
            and actual_cpa > reference_cpa * 1.5
        ):
            cold_bad.append(item["name"])
    cold_exists = any(item.get("channel_type") in COLD_CHANNELS for item in campaigns)
    add(
        "E4",
        status=(
            "unavailable"
            if cold_exists and not performance_trusted
            else ("fail" if cold_bad else ("pass" if cold_exists else "not_applicable"))
        ),
        scope="Display, Video and Demand Gen",
        evidence=(
            "Cold-audience campaigns exist, but their reported outcomes have not been confirmed as genuine customers."
            if cold_exists and not performance_trusted
            else "Cold campaigns carrying material weak spend: " + ", ".join(cold_bad)
            if cold_bad
            else (
                "No cold campaign breached the spend/efficiency gate."
                if cold_exists
                else "No cold-audience campaign was in scope."
            )
        ),
        standard="Cold channels should not carry at least 15% of spend while the core lane is weak.",
        effect="Expansion spend can distract from higher-intent demand.",
        next_step=(
            "Confirm genuine outcomes, then compare the cold campaigns with the core lane."
            if cold_exists and not performance_trusted
            else specialist_or_manual(
                module_id="structure_fixer",
                number=7,
                name="Structure Fixer",
                specialist_step="Run Skill 7 and Skill 12; validate the core lane before preparing any cold-channel reduction.",
                manual_step="List the cold campaigns, their spend share and genuine result, then compare them with the core Search/Shopping lane. Keep budgets unchanged until Skill 12 confirms a safe donor/receiver plan.",
            )
            if cold_bad
            else "No action is needed."
        ),
        evidence_label=(
            "Business confirmation needed"
            if cold_exists and not performance_trusted
            else "Observed in live Google Ads data"
        ),
        recovery_group=(
            "outcome_target" if cold_exists and not performance_trusted else None
        ),
    )
    if evidence.get("search_terms_verified"):
        term_campaigns = defaultdict(set)
        for row in search_terms:
            if int(row.get("clicks") or 0) > 0:
                term_campaigns[str(row.get("search_term", "")).casefold()].add(
                    str(row.get("campaign_id"))
                )
        overlap = [
            term for term, ids in term_campaigns.items() if term and len(ids) >= 3
        ]
        add(
            "E5",
            status="fail" if overlap else "pass",
            scope="Search campaigns",
            evidence=(
                f"{len(overlap)} exact search term(s) appeared with clicks in three or more campaigns."
                if overlap
                else "No exact search term appeared with clicks in three or more campaigns."
            ),
            standard="The same demand spread across at least three campaigns is likely cannibalisation.",
            effect="Overlap weakens traffic control and makes campaign results harder to interpret.",
            next_step=(
                specialist_or_manual(
                    module_id="structure_fixer",
                    number=7,
                    name="Structure Fixer",
                    specialist_step="Run Skill 7 and confirm the overlap in the Search terms report before changing keywords or negatives.",
                    manual_step="Export the overlapping queries with campaign, match type, spend, and outcomes; decide which campaign should own each intent before changing keywords or negatives.",
                )
                if overlap
                else "No action is needed."
            ),
        )
    else:
        add(
            "E5",
            status="unavailable",
            scope="Search campaigns",
            evidence="Search-term rows were unavailable.",
            standard="GMA compares exact search terms across campaigns to detect likely overlap.",
            effect="This check is excluded rather than guessed.",
            next_step="Restore Search terms report access and rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="search_terms",
        )
    if evidence.get("negative_keywords_verified"):
        negative_count = int(evidence.get("negative_keyword_count") or 0)
        search_spend = sum(
            int(item.get("cost_micros") or 0)
            for item in campaigns
            if item.get("channel_type") == "SEARCH"
        )
        negative_fail = search_spend >= 1_000_000_000 and negative_count == 0
        add(
            "E6",
            status="fail" if negative_fail else "pass",
            scope="Search campaigns",
            evidence=f"{negative_count} campaign/shared negative keywords were returned; {_money(search_spend, currency)} Search spend.",
            standard="A non-brand Search account spending at least 1,000 per month should not have zero negatives.",
            effect="Without a negative system, irrelevant queries can continue consuming budget.",
            next_step=(
                specialist_or_manual(
                    module_id="wasted_spend_finder",
                    number=3,
                    name="Wasted-Spend Finder",
                    specialist_step="Run Skill 3 — Wasted-Spend Finder; review protected brand and converting roots before preparing negatives.",
                    manual_step="Export the Search terms report, protect confirmed brand and converting roots, classify irrelevant demand, and prepare a reviewed negative-keyword list without applying it.",
                )
                if negative_fail
                else "No urgent action is needed."
            ),
        )
    else:
        add(
            "E6",
            status="unavailable",
            scope="Search campaigns",
            evidence="Negative-keyword inventory was unavailable.",
            standard="GMA checks campaign, ad-group, and shared negative controls.",
            effect="This check is excluded until the current negative system is readable.",
            next_step="Restore keyword-criterion read access and rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="negative_keywords",
        )
    if business_mode != "ecommerce":
        add(
            "E7",
            status="not_applicable",
            scope="Ecommerce feed",
            evidence="The confirmed business mode is Lead Generation.",
            standard="Product isolation applies only to ecommerce feed campaigns.",
            effect="This criterion is excluded from the grade.",
            next_step="No action is needed.",
        )
    elif not performance_trusted:
        add(
            "E7",
            status="unavailable",
            scope="Shopping/PMax products",
            evidence="Product rows are available, but their reported outcomes have not been confirmed as genuine purchases.",
            standard="Product isolation requires trusted purchase and value evidence.",
            effect="The audit will not label products winners or losers from unverified outcomes.",
            next_step="Confirm genuine purchase tracking and values, then rerun Skill 1.",
            evidence_label="Business confirmation needed",
            recovery_group="outcome_target",
        )
    elif evidence.get("products_verified"):
        products = [dict(item) for item in evidence.get("products") or []]
        losers_products = [
            item
            for item in products
            if reference_cpa
            and int(item.get("cost_micros") or 0) >= reference_cpa * 3
            and float(item.get("conversions") or 0) == 0
        ]
        add(
            "E7",
            status="fail" if losers_products else "pass",
            scope="Shopping/PMax products",
            evidence=f"{len(losers_products)} product(s) spent at least three reference CPAs with zero outcomes.",
            standard="Proven product losers should be isolated from the core winner lane.",
            effect="Repeated product-level waste can dilute profitable Shopping/PMax spend.",
            next_step=(
                "Review the named products' inventory, margin, destination, and feed; route confirmed losers to the secondary lane."
                if losers_products
                else "No action is needed."
            ),
        )
    else:
        add(
            "E7",
            status="unavailable",
            scope="Shopping/PMax products",
            evidence="Product-level performance was unavailable.",
            standard="Ecommerce product losers are judged from product-level spend and outcomes.",
            effect="This check is excluded until product evidence is available.",
            next_step="Restore Shopping performance access and rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="product_data",
        )
    if business_mode != "lead_gen" or not any(
        item.get("channel_type") == "PERFORMANCE_MAX" for item in campaigns
    ):
        add(
            "E8",
            status="not_applicable",
            scope="Lead-generation Performance Max",
            evidence="No lead-generation Performance Max campaign was in scope.",
            standard="This risk applies only when lead-generation PMax is active.",
            effect="This criterion is excluded from the grade.",
            next_step="No action is needed.",
        )
    else:
        offline = any(
            item.get("status") == "ENABLED"
            and str(item.get("origin")) in {"UPLOAD", "SALESFORCE", "STORE"}
            for item in conversion_actions
        )
        pmax_share = sum(
            spend_share[str(item["id"])]
            for item in campaigns
            if item.get("channel_type") == "PERFORMANCE_MAX"
        )
        pmax_fail = pmax_share >= 0.20 and performance_trusted and not offline
        add(
            "E8",
            status=(
                "not_applicable"
                if pmax_share == 0
                else (
                    "unavailable"
                    if not performance_trusted
                    else ("fail" if pmax_fail else "pass")
                )
            ),
            scope="Lead-generation Performance Max",
            evidence=(
                "PMax carried no spend in the selected window."
                if pmax_share == 0
                else f"PMax carried {_percent(pmax_share)} of spend; "
                + (
                    "an offline/imported action is configured, but its customer quality is not confirmed."
                    if not performance_trusted and offline
                    else (
                        "reported outcomes have not been confirmed as genuine leads."
                        if not performance_trusted
                        else (
                            "an offline quality action exists."
                            if offline
                            else "no offline/CRM quality action was found."
                        )
                    )
                )
            ),
            standard="Lead-gen PMax should not scale on shallow form fills without a downstream quality signal.",
            effect="Google may optimise for the cheapest leads rather than qualified enquiries.",
            next_step=(
                "No action is needed while PMax has no spend."
                if pmax_share == 0
                else "Confirm which imported/offline actions are genuine qualified or won leads, then rerun before scaling PMax."
                if not performance_trusted
                else "Connect qualified/won lead imports, make the correct downstream action usable for bidding, then rerun before scaling PMax."
                if pmax_fail
                else "No action is needed."
            ),
            evidence_label=(
                "Business confirmation needed"
                if pmax_share > 0 and not performance_trusted
                else "Observed in live Google Ads data"
            ),
            recovery_group=(
                "outcome_target"
                if pmax_share > 0 and not performance_trusted
                else None
            ),
        )

    # F — search terms and waste
    search_spend = sum(
        int(item.get("cost_micros") or 0)
        for item in campaigns
        if item.get("channel_type") == "SEARCH"
    )
    if not performance_trusted:
        for check_id in ("F1", "F2", "F3"):
            add(
                check_id,
                status="unavailable",
                scope="Search terms",
                evidence="Search-term rows are available, but their reported outcomes have not been confirmed as genuine customers.",
                standard="Waste and promotion decisions require a trusted business outcome.",
                effect="The audit will not add negatives or promote queries from unverified conversion counts.",
                next_step="Confirm which conversion actions represent genuine customers, then rerun Skill 1.",
                evidence_label="Business confirmation needed",
                recovery_group="outcome_target",
            )
    elif not evidence.get("search_terms_verified"):
        for check_id in ("F1", "F2", "F3"):
            add(
                check_id,
                status="unavailable",
                scope="Search terms",
                evidence="Search-term performance was unavailable.",
                standard="The full search-term decision requires query-level clicks, spend, outcomes, and current status.",
                effect="The check is excluded rather than turning missing rows into a healthy result.",
                next_step="Restore Search terms report access and rerun Skill 1.",
                evidence_label="Needed evidence",
                recovery_group="search_terms",
            )
    else:
        converting_roots = [
            _term_root(row.get("search_term"))
            for row in search_terms
            if float(row.get("conversions") or 0) > 0
            or float(row.get("all_conversions") or 0) > 0
        ]
        waste = [
            row
            for row in search_terms
            if int(row.get("clicks") or 0) >= 5
            and reference_cpa
            and int(row.get("cost_micros") or 0) >= reference_cpa
            and float(row.get("conversions") or 0) == 0
            and float(row.get("all_conversions") or 0) == 0
            and not _shares_converting_root(row, converting_roots)
        ]
        waste_cost = sum(int(row.get("cost_micros") or 0) for row in waste)
        waste_share = _ratio(waste_cost, search_spend) or 0
        f1_na = not reference_cpa or search_spend < 500_000_000
        add(
            "F1",
            status=(
                "not_applicable"
                if f1_na
                else ("fail" if waste_share >= 0.10 else "pass")
            ),
            scope="Search terms",
            evidence=(
                f"{_money(waste_cost, currency)} qualifying waste, {_percent(waste_share)} of Search spend."
                if not f1_na
                else "Search spend or reference CPA did not meet the scoring floor."
            ),
            standard="Qualifying zero-outcome terms should remain below 10% of Search spend.",
            effect="Confirmed irrelevant or non-converting demand can consume material budget.",
            next_step=(
                specialist_or_manual(
                    module_id="wasted_spend_finder",
                    number=3,
                    name="Wasted-Spend Finder",
                    specialist_step="Run Skill 3; protect brand and converting roots, then prepare only the reviewed negative actions.",
                    manual_step="Review every flagged query against protected brand terms and converting roots, classify its intent, and prepare only confirmed irrelevant terms as a draft negative list.",
                )
                if not f1_na and waste_share >= 0.10
                else "No action is needed."
            ),
        )
        top = sorted(
            search_terms, key=lambda row: int(row.get("cost_micros") or 0), reverse=True
        )[:10]
        top_converters = sum(
            float(row.get("conversions") or 0) > 0
            or float(row.get("all_conversions") or 0) > 0
            for row in top
        )
        add(
            "F2",
            status=(
                "not_applicable"
                if len(top) < 10
                else ("pass" if top_converters >= 5 else "fail")
            ),
            scope="Top 10 spend search terms",
            evidence=f"{top_converters} of {len(top)} top-spend terms produced a primary or secondary outcome.",
            standard="At least five of the top 10 spend terms should produce an outcome.",
            effect="When most expensive queries do not convert, intent control is weak.",
            next_step=(
                specialist_or_manual(
                    module_id="wasted_spend_finder",
                    number=3,
                    name="Wasted-Spend Finder",
                    specialist_step="Run Skill 3 to classify the non-converting terms and correct negatives, match control, or landing-page fit.",
                    manual_step="Review the top 10 terms one by one, classify each as relevant, irrelevant, or landing-page mismatch, and draft the corresponding negative, match-control, or page task.",
                )
                if len(top) >= 10 and top_converters < 5
                else "No action is needed."
            ),
        )
        backlog = [
            row
            for row in search_terms
            if float(row.get("conversions") or 0) >= 2
            and str(row.get("status")) == "NONE"
        ]
        add(
            "F3",
            status="fail" if backlog else "pass",
            scope="Search terms",
            evidence=f"{len(backlog)} term(s) had at least two conversions but were not added or excluded.",
            standard="Proven converting demand should be reviewed for controlled keyword coverage.",
            effect="The account may be leaving a proven query without explicit control.",
            next_step=(
                specialist_or_manual(
                    module_id="winning_keyword_promoter",
                    number=4,
                    name="Winning-Keyword Promoter",
                    specialist_step="Run Skill 4 — Winning-Keyword Promoter to check close variants, landing-page fit, and promotion eligibility.",
                    manual_step="Export the eight proven terms, check whether each already has close keyword coverage, confirm landing-page fit and brand safety, then prepare eligible additions as a draft list.",
                )
                if backlog
                else "No action is needed."
            ),
        )

    # G — ads and quality
    ads = [dict(item) for item in evidence.get("ads") or []]
    has_spending_search = any(
        item.get("channel_type") == "SEARCH" and int(item.get("cost_micros") or 0) > 0
        for item in campaigns
    )
    if evidence.get("ads_verified") and (ads or not has_spending_search):
        policy_bad = [
            row
            for row in ads
            if row.get("status") == "ENABLED"
            and row.get("approval_status") != "APPROVED"
            and int(row.get("ad_group_cost_micros") or 0) > 0
        ]
        add(
            "G1",
            status="fail" if policy_bad else "pass",
            scope="Active ads in spending ad groups",
            evidence=f"{len(policy_bad)} active ad(s) in spending ad groups are not fully approved.",
            standard="A spending ad group should not rely on a disapproved or limited active ad.",
            effect="Policy restrictions can stop or reduce delivery immediately.",
            next_step=(
                "Open Ads → Policy details for each affected ad, then edit, replace, or appeal it today and rerun."
                if policy_bad
                else "No action is needed."
            ),
        )
        spending_groups = {
            (str(row.get("campaign_id")), str(row.get("ad_group_id")))
            for row in ads
            if int(row.get("ad_group_cost_micros") or 0) > 0
        }
        rsa_groups = {
            (str(row.get("campaign_id")), str(row.get("ad_group_id")))
            for row in ads
            if row.get("status") == "ENABLED"
            and row.get("ad_type") == "RESPONSIVE_SEARCH_AD"
        }
        missing_rsa = spending_groups.difference(rsa_groups)
        search_exists = any(item.get("channel_type") == "SEARCH" for item in campaigns)
        add(
            "G2",
            status=(
                "fail"
                if missing_rsa
                else ("pass" if search_exists else "not_applicable")
            ),
            scope="Spending Search ad groups",
            evidence=f"{len(missing_rsa)} spending ad group(s) have no enabled responsive search ad.",
            standard="Every spending Search ad group needs at least one live responsive search ad.",
            effect="A missing live ad can prevent or weaken normal Search delivery.",
            next_step=(
                specialist_or_manual(
                    module_id="ad_copy_analyzer",
                    number=10,
                    name="Ad-Copy Analyzer",
                    specialist_step="Run Skill 10 — Ad-Copy Analyzer and prepare a compliant RSA for each named ad group.",
                    manual_step="Open each affected ad group, confirm its keyword intent and landing page, then draft one compliant responsive search ad without publishing it.",
                )
                if missing_rsa
                else "No action is needed."
            ),
        )
        poor_groups = {
            (str(row.get("campaign_id")), str(row.get("ad_group_id")))
            for row in ads
            if row.get("status") == "ENABLED"
            and row.get("ad_type") == "RESPONSIVE_SEARCH_AD"
            and row.get("ad_strength") == "POOR"
            and int(row.get("ad_group_cost_micros") or 0) > 0
        }
        strength_available = any(row.get("ad_strength") for row in ads)
        add(
            "G3",
            status=(
                "fail"
                if poor_groups
                else ("pass" if strength_available else "not_applicable")
            ),
            scope="Top spending Search ad groups",
            evidence=f"{len(poor_groups)} spending ad group(s) rely on an enabled Poor-strength RSA.",
            standard="Poor ad strength is a refinement flag, not proof of performance failure.",
            effect="The ad may lack message breadth or relevance, but performance evidence still outranks the label.",
            next_step=(
                specialist_or_manual(
                    module_id="ad_copy_analyzer",
                    number=10,
                    name="Ad-Copy Analyzer",
                    specialist_step="Run Skill 10; compare the offer, keyword intent, landing page, and existing asset performance before editing copy.",
                    manual_step="Compare each poor-strength ad with its keywords, offer, landing page, and asset performance; draft missing message themes, but do not replace a winning ad based on strength alone.",
                )
                if poor_groups
                else "No action is needed."
            ),
        )
    else:
        for check_id in ("G1", "G2", "G3"):
            add(
                check_id,
                status="unavailable",
                scope="Search ads",
                evidence="Ad inventory, policy, or strength data was unavailable.",
                standard="GMA must read active ad and ad-group evidence before scoring.",
                effect="This check is excluded from the grade and policy health is not assumed.",
                next_step="Restore ad and policy report access, then rerun Skill 1.",
                evidence_label="Needed evidence",
                recovery_group="ad_data",
            )
    if evidence.get("keywords_verified"):
        keywords = [dict(item) for item in evidence.get("keywords") or []]
        qs_rows = [row for row in keywords if row.get("quality_score") is not None]
        total_impressions = sum(int(row.get("impressions") or 0) for row in keywords)
        qs_impressions = sum(int(row.get("impressions") or 0) for row in qs_rows)
        coverage = _ratio(qs_impressions, total_impressions) or 0
        weighted = _ratio(
            sum(
                float(row["quality_score"]) * int(row.get("impressions") or 0)
                for row in qs_rows
            ),
            qs_impressions,
        )
        add(
            "G4",
            status=(
                "not_applicable"
                if coverage < 0.50 or weighted is None
                else ("pass" if weighted >= 6 else "fail")
            ),
            scope="Search keywords",
            evidence=(
                f"Impression-weighted Quality Score: {weighted:.1f}; score coverage {_percent(coverage)}."
                if weighted is not None
                else "No scorable Quality Score coverage."
            ),
            standard="Weighted Quality Score should be at least 6, with values covering at least half of Search impressions.",
            effect="Low relevance or landing-page experience can reduce auction competitiveness.",
            next_step=(
                specialist_or_manual(
                    module_id="quality_score_booster",
                    number=6,
                    name="Quality Score Booster",
                    specialist_step="Run Skill 6 — Quality Score Booster to separate expected CTR, ad relevance, and landing-page causes.",
                    manual_step="Export keyword Quality Score with expected CTR, ad relevance, landing-page experience, impressions, and spend; group the weak keywords by component before proposing a fix.",
                )
                if weighted is not None and coverage >= 0.50 and weighted < 6
                else "No action is needed."
            ),
        )
    else:
        add(
            "G4",
            status="unavailable",
            scope="Search keywords",
            evidence="Keyword Quality Score evidence was unavailable.",
            standard="GMA requires score components and impression weights.",
            effect="Quality health is excluded rather than guessed.",
            next_step="Restore keyword report access and rerun Skill 1.",
            evidence_label="Needed evidence",
            recovery_group="keyword_data",
        )

    # H — slices
    for check_id, key, threshold_share, label in (
        ("H1", "devices", 0.20, "device"),
        ("H2", "geos", 0.0, "location"),
        ("H3", "schedules", 0.15, "schedule"),
    ):
        verified = bool(evidence.get(f"{key}_verified"))
        rows = [dict(item) for item in evidence.get(key) or []]
        if not performance_trusted:
            add(
                check_id,
                status="unavailable",
                scope=label.title(),
                evidence=f"{label.title()} performance is available, but the reported outcomes have not been confirmed as genuine customers.",
                standard=f"GMA needs trusted outcomes by {label} before judging an efficiency gap.",
                effect="The audit will not recommend exclusions from unverified conversion counts.",
                next_step="Confirm which conversion actions represent genuine customers, then rerun Skill 1.",
                evidence_label="Business confirmation needed",
                recovery_group="outcome_target",
            )
            continue
        if not verified:
            add(
                check_id,
                status="unavailable",
                scope=label.title(),
                evidence=f"{label.title()} performance was unavailable.",
                standard=f"GMA needs spend, clicks, and outcomes by {label} before judging a skew.",
                effect="This check is excluded rather than turning missing slices into health.",
                next_step=f"Restore {label} performance access and rerun Skill 1.",
                evidence_label="Needed evidence",
                recovery_group="segmentation",
            )
            continue
        row_spend = sum(int(row.get("cost_micros") or 0) for row in rows)
        bad = []
        if check_id == "H1":
            eligible = [
                row
                for row in rows
                if int(row.get("clicks") or 0) >= 30
                and (_ratio(int(row.get("cost_micros") or 0), row_spend) or 0)
                >= threshold_share
                and float(row.get("conversions") or 0) > 0
            ]
            cpas = [
                _ratio(
                    int(row.get("cost_micros") or 0), float(row.get("conversions") or 0)
                )
                for row in eligible
            ]
            best = min((value for value in cpas if value is not None), default=None)
            if best:
                bad = [
                    str(row.get("name"))
                    for row in eligible
                    if (
                        _ratio(
                            int(row.get("cost_micros") or 0),
                            float(row.get("conversions") or 0),
                        )
                        or 0
                    )
                    >= best * 2
                ]
        elif check_id == "H2" and reference_cpa:
            bad = [
                str(row.get("name"))
                for row in rows[:30]
                if int(row.get("cost_micros") or 0) >= reference_cpa * 2
                and float(row.get("conversions") or 0) == 0
            ]
        elif check_id == "H3":
            bad = [
                str(row.get("name"))
                for row in rows
                if int(row.get("clicks") or 0) >= 30
                and (_ratio(int(row.get("cost_micros") or 0), row_spend) or 0)
                >= threshold_share
                and reference_cpa
                and (
                    _ratio(
                        int(row.get("cost_micros") or 0),
                        float(row.get("conversions") or 0),
                    )
                    or float("inf")
                )
                >= reference_cpa * 2
            ]
        enough = bool(rows) and (check_id != "H2" or bool(reference_cpa))
        add(
            check_id,
            status=("fail" if bad else ("pass" if enough else "not_applicable")),
            scope=label.title(),
            evidence=(
                f"Severe {label} skew: " + ", ".join(bad[:5])
                if bad
                else (
                    f"No {label} crossed the GMA evidence and efficiency thresholds."
                    if enough
                    else f"No {label} met the scoring floor."
                )
            ),
            standard=(
                "A material slice with enough traffic should not run at twice the best/reference CPA."
            ),
            effect=f"A severe {label} gap can reveal landing-page, targeting, staffing, or service-area waste.",
            next_step=(
                f"Open the {label} report for the named rows, verify lead quality and intent, then correct the underlying page/targeting/schedule cause before preparing an exclusion."
                if bad
                else "No action is needed."
            ),
        )

    if len(checks) != 41 or {row["id"] for row in checks} != set(CRITERIA):
        raise InstantAuditAnalysisError(
            "Instant Account Audit did not produce all 41 checks"
        )

    grade = _grade(checks)
    failures = [row for row in checks if row["status"] == "fail"]
    unavailable = [row for row in checks if row["status"] == "unavailable"]
    not_applicable = [row for row in checks if row["status"] == "not_applicable"]
    sorted_failures = sorted(
        failures,
        key=lambda row: (
            PRIORITY.get(row["id"], PRIORITY.get(row["section_id"], 9)),
            row["id"],
        ),
    )
    if tracking_suspect:
        recommendation_rows = [
            row for row in sorted_failures if row["section_id"] == "A"
        ]
    else:
        recommendation_rows = sorted_failures
    recommendations = [
        _recommendation(
            row,
            action_id=f"IA-{index:03d}",
            priority=PRIORITY.get(row["id"], PRIORITY.get(row["section_id"], 9)),
        )
        for index, row in enumerate(recommendation_rows[:7], start=1)
    ]

    recovery_actions: list[dict[str, Any]] = []
    grouped = defaultdict(list)
    for row in unavailable:
        grouped[str(row.get("recovery_group") or row["id"])].append(row["id"])
    recovery_specs = {
        "account_settings": (
            "REC-IA-ACCOUNT",
            1,
            "Restore account tracking-setting access",
            "The audit could not read the current auto-tagging or conversion tracking setting.",
            [
                "Reconnect GMA 13 Skills Google Ads.",
                "Confirm the selected advertiser account.",
                "Rerun Skill 1.",
            ],
            "Account tracking settings are returned in the new data receipt.",
            "google_ads_admin",
        ),
        "goal_scope": (
            "REC-IA-GOALS",
            1,
            "Verify campaign-effective conversion goals",
            "The audit could not prove which primary actions control bidding for every selected campaign.",
            [
                "Open each selected campaign → Settings → Goals.",
                "Record Account default, Campaign-specific, or Custom.",
                "Confirm the genuine primary action names and rerun Skill 1.",
            ],
            "Every selected campaign has a verified goal scope and named effective actions.",
            "google_ads_admin",
        ),
        "conversion_actions": (
            "REC-IA-CONVERSIONS",
            1,
            "Restore conversion-action configuration evidence",
            "Primary/secondary, attribution, type, and value settings were unavailable.",
            [
                "Reconnect Google Ads with conversion-action read access.",
                "Open Goals → Conversions → Summary and confirm the genuine primary actions.",
                "Rerun Skill 1.",
            ],
            "The audit can read status, primary setting, attribution, type, and value for every active action.",
            "google_ads_admin",
        ),
        "trend_data": (
            "REC-IA-TRENDS",
            1,
            "Collect the two complete tracking comparison weeks",
            "A current tracking-break screen requires two complete seven-day periods.",
            [
                "Keep bid and budget settings unchanged meanwhile.",
                "Wait until two complete weeks are available.",
                "Rerun Skill 1.",
            ],
            "Both weeks contain traffic and conversion totals.",
            "gma",
        ),
        "landing_page_review": (
            "REC-IA-PAGE-REVIEW",
            1,
            "Complete the destination and conversion-path review",
            "The Google Ads connector can identify URLs but cannot verify visible page content or form/checkout operation.",
            [
                "Open the top-spend final URL on desktop and mobile.",
                "Confirm the headline matches paid intent and the main action is visible above the fold.",
                "Complete a test form, call, or purchase path and record the result.",
                "Attach the result and rerun Skill 1.",
            ],
            "The top page load, intent match, action visibility, and page type are confirmed.",
            "account_owner",
        ),
        "landing_page_data": (
            "REC-IA-PAGE-DATA",
            3,
            "Restore landing-page performance data",
            "The audit could not compare clicks and outcomes by URL.",
            [
                "Reconnect Google Ads.",
                "Confirm landing-page report access.",
                "Rerun Skill 1.",
            ],
            "Per-URL clicks, spend, and outcomes are returned.",
            "google_ads_admin",
        ),
        "change_history": (
            "REC-IA-CHANGES",
            4,
            "Restore bid and target change history",
            "The audit could not verify whether campaigns were repeatedly reset.",
            [
                "Open Google Ads → Change history.",
                "Filter to Bidding and Target changes for the last 30 days.",
                "Restore connector access or attach the export, then rerun Skill 1.",
            ],
            "Each selected campaign has a 30-day bid/target change count.",
            "google_ads_admin",
        ),
        "outcome_target": (
            "REC-IA-OUTCOME",
            1,
            "Confirm the genuine outcome and business target",
            "A budget-scaling verdict requires a trustworthy lead/purchase outcome and confirmed CPA/ROAS target.",
            [
                "Compare Google Ads action totals with bookings, CRM leads, or purchase records.",
                "Confirm which actions are genuine and non-duplicated.",
                "Confirm the CPA or ROAS goal, then rerun Skill 1.",
            ],
            "Outcome quality and the business target are confirmed for the selected scope.",
            "account_owner",
        ),
        "brand_terms": (
            "REC-IA-BRAND",
            6,
            "Confirm protected brand terms",
            "Brand separation cannot be judged safely without the advertiser's protected names.",
            [
                "Confirm the company, product, and common misspelling list.",
                "Add the confirmed list to the Account Profile.",
                "Rerun Skill 1.",
            ],
            "A user-confirmed protected brand list is saved for this account.",
            "account_owner",
        ),
        "search_terms": (
            "REC-IA-SEARCH-TERMS",
            3,
            "Restore search-term evidence",
            "Waste, overlap, top-term conversion, and promotion checks require query-level rows.",
            [
                "Reconnect Google Ads.",
                "Confirm Search terms report access for the selected campaigns.",
                "Rerun Skill 1.",
            ],
            "Search term, campaign, status, clicks, spend, and outcome rows are returned.",
            "google_ads_admin",
        ),
        "negative_keywords": (
            "REC-IA-NEGATIVES",
            6,
            "Restore negative-keyword inventory",
            "The audit could not verify campaign, ad-group, or shared negative controls.",
            [
                "Reconnect Google Ads.",
                "Confirm keyword-criterion report access.",
                "Rerun Skill 1.",
            ],
            "Current campaign/ad-group/shared negative counts are returned.",
            "google_ads_admin",
        ),
        "product_data": (
            "REC-IA-PRODUCTS",
            3,
            "Restore ecommerce product performance",
            "Product-level spend and outcomes are required to identify proven losers.",
            [
                "Confirm the selected campaigns use a Merchant Center feed.",
                "Restore Shopping performance report access.",
                "Rerun Skill 1.",
            ],
            "Product item, spend, and outcome rows are returned.",
            "google_ads_admin",
        ),
        "ad_data": (
            "REC-IA-ADS",
            2,
            "Restore ad inventory and policy evidence",
            "The audit could not verify live RSAs or current policy eligibility.",
            [
                "Reconnect Google Ads.",
                "Confirm Ads and policy report access.",
                "Open Ads and resolve any visible disapprovals while access is restored.",
                "Rerun Skill 1.",
            ],
            "Active ad type, status, strength, policy, and spending ad-group rows are returned.",
            "google_ads_admin",
        ),
        "keyword_data": (
            "REC-IA-KEYWORDS",
            7,
            "Restore keyword Quality Score evidence",
            "The audit could not calculate an impression-weighted Quality Score.",
            [
                "Reconnect Google Ads.",
                "Confirm keyword report access.",
                "Rerun Skill 1.",
            ],
            "Keyword Quality Score and impressions are returned.",
            "google_ads_admin",
        ),
        "segmentation": (
            "REC-IA-SLICES",
            7,
            "Restore device, location, and schedule evidence",
            "The audit could not verify whether one major segment is wasting spend.",
            [
                "Reconnect Google Ads.",
                "Confirm device, location, and day-of-week report access.",
                "Rerun Skill 1.",
            ],
            "All three segmentation reports return spend, clicks, and outcomes.",
            "google_ads_admin",
        ),
    }
    for group, resolved_ids in grouped.items():
        spec = recovery_specs.get(group)
        if spec is None:
            raise InstantAuditAnalysisError(
                f"No deterministic recovery is defined for {group}"
            )
        recovery_actions.append(
            _recovery(
                recovery_id=spec[0],
                priority=spec[1],
                title=spec[2],
                reason=spec[3],
                steps=spec[4],
                resolves=resolved_ids,
                campaigns=campaigns,
                completion_signal=spec[5],
                owner=spec[6],
                status=(
                    "waiting"
                    if group == "trend_data"
                    else (
                        "needs_confirmation" if group == "outcome_target" else "ready"
                    )
                ),
                follow_up_kind=(
                    "monitor"
                    if group == "trend_data"
                    else (
                        "review_then_rerun"
                        if group == "outcome_target"
                        else "rerun_current_skill"
                    )
                ),
            )
        )

    if (
        business_mode == "lead_gen"
        and not outcome_quality_confirmed
        and "outcome_target" not in grouped
    ):
        recovery_actions.append(
            _recovery(
                recovery_id="REC-IA-OUTCOME",
                priority=1,
                title="Confirm which reported conversions are genuine leads",
                reason="Google Ads can show configured actions, but it cannot prove whether they became genuine, non-duplicated enquiries.",
                steps=[
                    "Open Goals → Conversions → Summary and export the selected-window action counts.",
                    "Compare them with bookings, call records, or CRM leads.",
                    "Classify each action as genuine lead, supporting action, or irrelevant action.",
                    "Return the classification to GMA and rerun Skill 1.",
                ],
                resolves=["Lead outcome quality confirmation"],
                campaigns=campaigns,
                completion_signal="Every effective action is classified and the account owner confirms the genuine lead actions.",
                owner="account_owner",
                status="needs_confirmation",
                follow_up_kind="review_then_rerun",
            )
        )

    if tracking_suspect:
        status = "hold"
        holds = [
            "Possible tracking break: bid, budget, target, and structure recommendations are suppressed until measurement is verified."
        ]
        conclusion = (
            f"Grade {grade['band']} ({grade['percentage']:.1f}% of "
            f"{grade['achievable_points']:.0f} achievable points), with a possible "
            "tracking break that must be checked first."
        )
    elif not performance_trusted:
        status = "partial"
        holds = [
            "Performance-based bid, budget, search-term, and audience decisions are held until genuine customer outcomes are confirmed. Conversion setup fixes and evidence-recovery tasks can proceed now."
        ]
        conclusion = (
            f"Partial audit: grade {grade['band']} ({grade['percentage']:.1f}% of "
            f"{grade['achievable_points']:.0f} currently achievable points). "
            f"{len(failures)} configuration checks need action now; "
            f"{len(unavailable)} checks have a defined evidence-recovery path."
        )
    elif unavailable:
        status = "partial"
        holds = []
        conclusion = (
            f"Partial audit: grade {grade['band']} ({grade['percentage']:.1f}% of "
            f"{grade['achievable_points']:.0f} currently achievable points). "
            f"{len(unavailable)} of 41 checks need more evidence."
        )
    elif failures:
        status = "findings_ready"
        holds = []
        conclusion = (
            f"Grade {grade['band']} ({grade['percentage']:.1f}%). "
            f"{len(failures)} of 41 checks need action."
        )
    else:
        status = "no_material_findings"
        holds = []
        conclusion = f"Grade {grade['band']} ({grade['percentage']:.1f}%); no scored criterion failed."

    return {
        "status": status,
        "analysis_days": days,
        "campaigns_analyzed": len(campaigns),
        "checks": checks,
        "grade": grade,
        "assessment_details": {
            "total_criteria": 41,
            "assessed": sum(row["status"] in {"pass", "fail"} for row in checks),
            "passed": sum(row["status"] == "pass" for row in checks),
            "failed": len(failures),
            "unavailable": len(unavailable),
            "not_applicable": len(not_applicable),
            "tracking_circuit_breaker": tracking_suspect,
            "outcome_quality_circuit_breaker": not performance_trusted,
            "business_mode": business_mode,
        },
        "conclusion": conclusion,
        "holds": holds,
        "recommendations": recommendations,
        "recovery_actions": sorted(
            recovery_actions, key=lambda item: (item["priority"], item["id"])
        ),
        "coverage_gaps": list(snapshot.get("coverage_gaps") or []),
    }
