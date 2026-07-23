"""Deterministic, source-labelled CPA/ROAS goal benchmark reports."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from statistics import median
from typing import Any, Mapping

from ads_mcp.industry_benchmarks import get_benchmark_profile

GOAL_REPORT_VERSION = "gma-goal-report/1.0"


class GoalReportError(ValueError):
    """Safe input or evidence failure while building a goal report."""


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise GoalReportError(f"{label} must be a positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise GoalReportError(f"{label} must be a positive number") from error
    if result <= 0:
        raise GoalReportError(f"{label} must be a positive number")
    return result


def _optional_percent(
    economics: Mapping[str, Any],
    field: str,
) -> float | None:
    value = economics.get(field)
    if value is None:
        return None
    result = _positive_number(value, field)
    if result > 100:
        raise GoalReportError(f"{field} must not exceed 100")
    return result


def _account_reference(
    scope: Mapping[str, Any],
    business_mode: str,
) -> dict[str, Any]:
    goal_context = scope.get("goal_context")
    if not isinstance(goal_context, Mapping):
        raise GoalReportError(
            "Prepared scope does not contain live goal evidence; prepare it again"
        )
    campaign_goals = goal_context.get("campaign_goals")
    if not isinstance(campaign_goals, list):
        raise GoalReportError(
            "Prepared scope does not contain campaign goal evidence"
        )
    enabled = [
        item
        for item in campaign_goals
        if isinstance(item, Mapping)
        and item.get("status") == "ENABLED"
        and int(item.get("spend_micros") or 0) > 0
    ]
    total_spend_micros = sum(int(item["spend_micros"]) for item in enabled)
    total_conversions = sum(
        max(float(item.get("reported_conversions") or 0), 0) for item in enabled
    )
    total_conversion_value = sum(
        max(float(item.get("reported_conversion_value") or 0), 0)
        for item in enabled
    )
    campaign_count = len(enabled)
    coverage_gaps = list(goal_context.get("coverage_gaps") or [])
    common = {
        "source": "live_google_ads_reported_performance",
        "currency": str(scope.get("currency") or ""),
        "analysis_start": str(scope.get("campaign_spend_window_start") or ""),
        "analysis_end": str(scope.get("campaign_spend_window_end") or ""),
        "campaign_count": campaign_count,
        "campaign_ids": [str(item["campaign_id"]) for item in enabled],
        "spend_micros": total_spend_micros,
        "spend": round(total_spend_micros / 1_000_000, 2),
        "reported_conversions": round(total_conversions, 4),
        "reported_conversion_value": round(total_conversion_value, 2),
        "coverage_gaps": coverage_gaps,
        "qualified_outcomes_confirmed": False,
        "caveat": (
            "Google Ads reported outcomes are not automatically verified as "
            "genuine leads, sales, or profitable customers."
        ),
    }
    if total_spend_micros <= 0:
        return {
            **common,
            "status": "unavailable",
            "metric": (
                "reported_cpa"
                if business_mode == "lead_gen"
                else "reported_roas"
            ),
            "value": None,
            "reason": "No enabled campaign had spend in the comparison window.",
        }
    if business_mode == "lead_gen":
        if total_conversions <= 0:
            return {
                **common,
                "status": "unavailable",
                "metric": "reported_cpa",
                "value": None,
                "reason": "No reported conversions were found.",
            }
        value = total_spend_micros / 1_000_000 / total_conversions
        return {
            **common,
            "status": "available",
            "metric": "reported_cpa",
            "value": round(value, 2),
            "reason": None,
        }
    if total_conversion_value <= 0:
        return {
            **common,
            "status": "unavailable",
            "metric": "reported_roas",
            "value": None,
            "reason": "No reported conversion value was found.",
        }
    value = total_conversion_value / (total_spend_micros / 1_000_000)
    return {
        **common,
        "status": "available",
        "metric": "reported_roas",
        "value": round(value, 4),
        "reason": None,
    }


def _industry_reference(
    profile_id: str,
    business_mode: str,
) -> dict[str, Any]:
    profile = get_benchmark_profile(profile_id, business_mode)
    if not profile:
        raise GoalReportError(
            "Unknown or incompatible industry benchmark profile"
        )
    observations = profile["observations"]
    low = min(float(item["low"]) for item in observations)
    high = max(float(item["high"]) for item in observations)
    centers = [
        (float(item["low"]) + float(item["high"])) / 2 for item in observations
    ]
    return {
        **profile,
        "range_low": round(low, 2),
        "range_high": round(high, 2),
        "reference_value": round(float(median(centers)), 2),
        "advisory_only": True,
        "limitation": (
            "This is a directional comparison, not a target recommendation. "
            "Industry averages do not prove lead quality, margin, or profitability."
        ),
    }


def _economics_reference(
    business_mode: str,
    economics: Mapping[str, Any] | None,
) -> dict[str, Any]:
    values = dict(economics or {})
    if business_mode == "lead_gen":
        required = [
            "average_customer_value",
            "gross_margin_percent",
            "lead_to_sale_rate_percent",
        ]
        missing = [field for field in required if values.get(field) is None]
        if missing:
            return {
                "status": "not_calculated",
                "metric": "break_even_cpa",
                "value": None,
                "missing_inputs": missing,
                "formula": (
                    "average customer value × gross margin % × lead-to-sale rate %"
                ),
            }
        customer_value = _positive_number(
            values["average_customer_value"], "average_customer_value"
        )
        margin = _optional_percent(values, "gross_margin_percent")
        close_rate = _optional_percent(values, "lead_to_sale_rate_percent")
        assert margin is not None and close_rate is not None
        break_even = customer_value * (margin / 100) * (close_rate / 100)
        return {
            "status": "calculated",
            "metric": "break_even_cpa",
            "value": round(break_even, 2),
            "currency": values.get("currency"),
            "inputs": {
                "average_customer_value": customer_value,
                "gross_margin_percent": margin,
                "lead_to_sale_rate_percent": close_rate,
            },
            "missing_inputs": [],
            "formula": (
                "average customer value × gross margin % × lead-to-sale rate %"
            ),
            "caveat": (
                "Break-even CPA excludes overhead, repeat purchase, refunds, "
                "sales capacity, and cash-flow constraints unless included in inputs."
            ),
        }
    required = ["contribution_margin_percent"]
    missing = [field for field in required if values.get(field) is None]
    if missing:
        return {
            "status": "not_calculated",
            "metric": "break_even_roas",
            "value": None,
            "missing_inputs": missing,
            "formula": "1 ÷ contribution margin rate",
        }
    margin = _optional_percent(values, "contribution_margin_percent")
    assert margin is not None
    return {
        "status": "calculated",
        "metric": "break_even_roas",
        "value": round(1 / (margin / 100), 4),
        "inputs": {"contribution_margin_percent": margin},
        "missing_inputs": [],
        "formula": "1 ÷ contribution margin rate",
        "caveat": (
            "Break-even ROAS excludes lifetime value, returns, fixed overhead, "
            "and attribution differences unless included in the margin input."
        ),
    }


def _position(
    value: float,
    low: float,
    high: float,
    *,
    lower_is_better: bool,
) -> str:
    if low <= value <= high:
        return "within_directional_range"
    if lower_is_better:
        return (
            "better_than_directional_range"
            if value < low
            else "worse_than_directional_range"
        )
    return (
        "worse_than_directional_range"
        if value < low
        else "better_than_directional_range"
    )


def build_goal_report(
    scope: Mapping[str, Any],
    *,
    industry_profile_id: str,
    confirmed_target_cpa: Any = None,
    confirmed_target_roas: Any = None,
    economics: Mapping[str, Any] | None = None,
    now_fn=None,
) -> dict[str, Any]:
    """Build one immutable comparison without changing the advertiser account."""

    business_mode = str(scope.get("business_mode") or "")
    if business_mode not in {"lead_gen", "ecommerce"}:
        raise GoalReportError(
            "Goal Benchmark Report requires a lead_gen or ecommerce scope"
        )
    if business_mode == "lead_gen":
        if confirmed_target_roas is not None:
            raise GoalReportError("Lead-generation scopes require a CPA target")
        target = _positive_number(confirmed_target_cpa, "confirmed_target_cpa")
        metric = "target_cpa"
        lower_is_better = True
    else:
        if confirmed_target_cpa is not None:
            raise GoalReportError("Ecommerce scopes require a ROAS target")
        target = _positive_number(
            confirmed_target_roas, "confirmed_target_roas"
        )
        metric = "target_roas"
        lower_is_better = False

    account = _account_reference(scope, business_mode)
    industry = _industry_reference(industry_profile_id, business_mode)
    economics_reference = _economics_reference(business_mode, economics)
    actual = account["value"]
    meets_target = (
        None
        if actual is None
        else (actual <= target if lower_is_better else actual >= target)
    )
    target_position = _position(
        target,
        industry["range_low"],
        industry["range_high"],
        lower_is_better=lower_is_better,
    )
    actual_position = (
        None
        if actual is None
        else _position(
            float(actual),
            industry["range_low"],
            industry["range_high"],
            lower_is_better=lower_is_better,
        )
    )
    if actual is None:
        conclusion = (
            "The confirmed business goal is recorded, but live account "
            "performance is unavailable for a reliable comparison."
        )
    elif meets_target:
        conclusion = (
            "Google Ads is reporting performance inside the confirmed business "
            "goal. Confirm outcome quality before treating this as proven success."
        )
    else:
        conclusion = (
            "Google Ads is reporting performance outside the confirmed business "
            "goal. Diagnose the cause before changing bids or budgets."
        )

    stable_input = {
        "scope_hash_material": {
            "customer_id": scope.get("customer_id"),
            "analysis_start": account["analysis_start"],
            "analysis_end": account["analysis_end"],
            "campaign_ids": account["campaign_ids"],
        },
        "business_mode": business_mode,
        "industry_profile_id": industry_profile_id,
        "metric": metric,
        "confirmed_target": target,
        "economics": dict(economics or {}),
    }
    stable_hash = hashlib.sha256(
        json.dumps(stable_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    generated_at = (now_fn or (lambda: datetime.now(timezone.utc)))()
    report: dict[str, Any] = {
        "contract_version": GOAL_REPORT_VERSION,
        "report_id": f"goal_{stable_hash[:24]}",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "scope": {
            "customer_id": str(scope.get("customer_id") or ""),
            "account_name": str(scope.get("account_name") or ""),
            "currency": str(scope.get("currency") or ""),
            "business_mode": business_mode,
            "campaign_count": account["campaign_count"],
            "campaign_ids": account["campaign_ids"],
            "analysis_start": account["analysis_start"],
            "analysis_end": account["analysis_end"],
        },
        "confirmed_goal": {
            "metric": metric,
            "value": round(target, 4),
            "source": "user_confirmed",
            "authoritative_for_recommendations": True,
            "persisted_to_google_ads": False,
        },
        "account_reference": account,
        "industry_reference": industry,
        "economics_reference": economics_reference,
        "comparisons": {
            "account_meets_confirmed_goal": meets_target,
            "confirmed_goal_vs_industry": target_position,
            "account_vs_industry": actual_position,
        },
        "assessment": {
            "conclusion": conclusion,
            "outcome_quality_confirmation_required": True,
            "industry_average_controls_recommendations": False,
            "next_step": (
                "Confirm that reported conversions represent genuine, qualified "
                "outcomes; then use the confirmed business goal in the specialist."
            ),
        },
        "core_signature": "",
    }
    report["core_signature"] = hashlib.sha256(
        json.dumps(
            {**report, "generated_at": "", "core_signature": ""},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return report
