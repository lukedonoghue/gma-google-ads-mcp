"""Normalize configured Google Ads CPA/ROAS targets for scope confirmation."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _enum(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _nested(entity: Any, field: str, nested_field: str) -> Any:
    parent = getattr(entity, field, None)
    return getattr(parent, nested_field, None) if parent is not None else None


def normalize_portfolio_strategies(
    *,
    bidding_strategy_rows: Sequence[Any],
    accessible_strategy_rows: Sequence[Any],
) -> dict[str, dict[str, Any]]:
    """Index customer- and manager-owned portfolio strategies by strategy ID."""

    strategies: dict[str, dict[str, Any]] = {}
    for row, source in (
        *((row, "portfolio") for row in bidding_strategy_rows),
        *((row, "manager_portfolio") for row in accessible_strategy_rows),
    ):
        entity = (
            getattr(row, "bidding_strategy", None)
            if source == "portfolio"
            else getattr(row, "accessible_bidding_strategy", None)
        )
        if entity is None:
            continue
        strategy_id = str(getattr(entity, "id", "") or "")
        if not strategy_id:
            resource_name = str(getattr(entity, "resource_name", "") or "")
            strategy_id = resource_name.rsplit("/", 1)[-1]
        if not strategy_id:
            continue
        strategies[strategy_id] = {
            "entity": entity,
            "source": source,
            "name": str(getattr(entity, "name", "") or "") or None,
            "resource_name": str(getattr(entity, "resource_name", "") or "") or None,
            "strategy_type": _enum(
                getattr(entity, "type_", getattr(entity, "type", "UNSPECIFIED"))
            ),
        }
    return strategies


def _configured_target(
    entity: Any,
    strategy_type: str,
) -> tuple[str, int | None, float | None]:
    if strategy_type == "TARGET_CPA":
        target_cpa_micros = _positive_int(
            _nested(entity, "target_cpa", "target_cpa_micros")
        )
        return (
            "target_cpa" if target_cpa_micros else "none",
            target_cpa_micros,
            None,
        )
    if strategy_type == "MAXIMIZE_CONVERSIONS":
        target_cpa_micros = _positive_int(
            _nested(entity, "maximize_conversions", "target_cpa_micros")
        )
        return (
            "target_cpa" if target_cpa_micros else "none",
            target_cpa_micros,
            None,
        )
    if strategy_type == "TARGET_ROAS":
        target_roas = _positive_float(_nested(entity, "target_roas", "target_roas"))
        return ("target_roas" if target_roas else "none", None, target_roas)
    if strategy_type == "MAXIMIZE_CONVERSION_VALUE":
        target_roas = _positive_float(
            _nested(entity, "maximize_conversion_value", "target_roas")
        )
        return ("target_roas" if target_roas else "none", None, target_roas)
    return "none", None, None


def campaign_goal(
    campaign: Any,
    *,
    spend_micros: int,
    portfolio_strategies: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return one campaign's configured bidding target without judging its value."""

    strategy_type = _enum(getattr(campaign, "bidding_strategy_type", "UNSPECIFIED"))
    portfolio_resource = str(getattr(campaign, "bidding_strategy", "") or "")
    portfolio_id = portfolio_resource.rsplit("/", 1)[-1] if portfolio_resource else ""
    portfolio = portfolio_strategies.get(portfolio_id)

    entity = campaign
    source = "campaign"
    strategy_name = None
    strategy_resource_name = None
    if portfolio:
        entity = portfolio["entity"]
        source = str(portfolio["source"])
        strategy_name = portfolio.get("name")
        strategy_resource_name = portfolio.get("resource_name") or portfolio_resource
        strategy_type = str(portfolio.get("strategy_type") or strategy_type)
    elif portfolio_resource:
        source = "portfolio_unavailable"
        strategy_resource_name = portfolio_resource

    goal_type, target_cpa_micros, target_roas = _configured_target(
        entity, strategy_type
    )
    return {
        "campaign_id": str(campaign.id),
        "campaign_name": str(campaign.name),
        "status": _enum(campaign.status),
        "spend_micros": int(spend_micros),
        "bidding_strategy_type": strategy_type,
        "goal_type": goal_type,
        "target_cpa_micros": target_cpa_micros,
        "target_roas": target_roas,
        "target_roas_percent": (
            round(target_roas * 100, 2) if target_roas is not None else None
        ),
        "source": source if goal_type != "none" else (
            source if source == "portfolio_unavailable" else "not_configured"
        ),
        "strategy_name": strategy_name,
        "strategy_resource_name": strategy_resource_name,
    }


def _weighted_median(
    candidates: Sequence[tuple[int | float, int]],
) -> int | float:
    ordered = sorted(candidates, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    midpoint = total_weight / 2
    accumulated = 0
    for value, weight in ordered:
        accumulated += weight
        if accumulated >= midpoint:
            return value
    return ordered[-1][0]


def _unweighted_median_low(values: Sequence[int | float]) -> int | float:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def build_goal_suggestion(
    campaign_goals: Sequence[Mapping[str, Any]],
    *,
    business_mode: str,
    currency: str,
) -> dict[str, Any]:
    """Suggest a starting goal from enabled campaigns; always require confirmation."""

    if business_mode not in {"lead_gen", "ecommerce"}:
        raise ValueError("business_mode must be lead_gen or ecommerce")
    goal_type = "target_cpa" if business_mode == "lead_gen" else "target_roas"
    value_field = (
        "target_cpa_micros" if goal_type == "target_cpa" else "target_roas"
    )
    enabled = [item for item in campaign_goals if item["status"] == "ENABLED"]
    candidates = [
        item
        for item in enabled
        if item["goal_type"] == goal_type and item.get(value_field) is not None
    ]
    total_enabled_spend = sum(int(item["spend_micros"]) for item in enabled)
    covered_spend = sum(int(item["spend_micros"]) for item in candidates)
    spend_coverage_percent = (
        round(covered_spend * 100 / total_enabled_spend, 1)
        if total_enabled_spend > 0
        else None
    )
    common = {
        "status": "unavailable" if not candidates else "suggested",
        "goal_type": goal_type,
        "source": "google_ads_configured_target",
        "confirmation_required": True,
        "business_goal_verified": False,
        "campaign_count": len(candidates),
        "campaign_ids": [str(item["campaign_id"]) for item in candidates],
        "distinct_target_count": len(
            {item[value_field] for item in candidates}
        ),
        "spend_coverage_percent": spend_coverage_percent,
        "target_cpa_micros": None,
        "target_roas": None,
        "target_roas_percent": None,
        "display_value": None,
        "basis": "no_enabled_campaign_has_this_configured_target",
    }
    if not candidates:
        return common

    weighted = [
        (item[value_field], int(item["spend_micros"]))
        for item in candidates
        if int(item["spend_micros"]) > 0
    ]
    if weighted:
        suggestion = _weighted_median(weighted)
        basis = (
            "common_configured_target"
            if common["distinct_target_count"] == 1
            else "spend_weighted_median_of_configured_targets"
        )
    else:
        suggestion = _unweighted_median_low(
            [item[value_field] for item in candidates]
        )
        basis = (
            "common_configured_target_no_recent_spend"
            if common["distinct_target_count"] == 1
            else "median_configured_target_no_recent_spend"
        )

    if goal_type == "target_cpa":
        target_cpa_micros = int(suggestion)
        common["target_cpa_micros"] = target_cpa_micros
        common["display_value"] = (
            f"{currency} {target_cpa_micros / 1_000_000:,.2f}"
        )
    else:
        target_roas = float(suggestion)
        common["target_roas"] = target_roas
        common["target_roas_percent"] = round(target_roas * 100, 2)
        common["display_value"] = (
            f"{target_roas:.2f}× ({round(target_roas * 100, 2):g}%)"
        )
    common["basis"] = basis
    return common


def build_goal_context(
    campaign_rows: Sequence[Any],
    *,
    spend_by_campaign: Mapping[str, int],
    bidding_strategy_rows: Sequence[Any],
    accessible_strategy_rows: Sequence[Any],
    business_mode: str,
    inferred_business_mode: str,
    currency: str,
    coverage_gaps: Sequence[str],
) -> dict[str, Any]:
    """Build campaign details plus lead-gen and ecommerce confirmation defaults."""

    strategies = normalize_portfolio_strategies(
        bidding_strategy_rows=bidding_strategy_rows,
        accessible_strategy_rows=accessible_strategy_rows,
    )
    campaign_goals = [
        campaign_goal(
            row.campaign,
            spend_micros=spend_by_campaign.get(str(row.campaign.id), 0),
            portfolio_strategies=strategies,
        )
        for row in campaign_rows
    ]
    suggestions = {
        "lead_gen": build_goal_suggestion(
            campaign_goals, business_mode="lead_gen", currency=currency
        ),
        "ecommerce": build_goal_suggestion(
            campaign_goals, business_mode="ecommerce", currency=currency
        ),
    }
    selected_mode = (
        business_mode
        if business_mode in {"lead_gen", "ecommerce"}
        else (
            inferred_business_mode
            if inferred_business_mode in {"lead_gen", "ecommerce"}
            else None
        )
    )
    return {
        "campaign_goals": campaign_goals,
        "suggestions": suggestions,
        "selected_business_mode": selected_mode,
        "selected_suggestion": suggestions.get(selected_mode),
        "coverage_gaps": list(coverage_gaps),
    }
