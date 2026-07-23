"""Live Google Ads evidence adapter for deterministic Budget Reallocator runs."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Sequence
from ads_mcp import utils
from ads_mcp.changesets import ChangesetService, get_changeset_service
from ads_mcp.skill_runs.budget_reallocator import (
    BudgetAnalysisError,
    evaluate_budget_reallocation,
)
from ads_mcp.skill_runs.common import resolve_analysis_window


def _enum(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _micros(value: float | int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or float(value) <= 0:
        raise BudgetAnalysisError(f"{field} must be greater than zero")
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _campaign_filter(campaign_ids: Sequence[str] | None) -> str:
    if not campaign_ids:
        return ""
    normalized = []
    for value in campaign_ids:
        campaign_id = str(value).strip()
        if not re.fullmatch(r"\d{1,20}", campaign_id):
            raise BudgetAnalysisError("Campaign IDs must be numeric")
        normalized.append(campaign_id)
    return " AND campaign.id IN (" + ",".join(sorted(set(normalized))) + ")"


class GoogleAdsBudgetGateway:
    """Fixed-query adapter; the model never composes GAQL for this skill."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _search(service, customer_id: str, query: str):
        return list(service.search(customer_id=customer_id, query=query))

    def fetch_snapshot(
        self,
        *,
        customer_id: str,
        login_customer_id: str | None,
        analysis_start: str | None,
        analysis_end: str | None,
        campaign_ids: Sequence[str] | None,
    ) -> dict[str, Any]:
        normalized_customer = utils._normalize_customer_id(customer_id, "customer_id")
        resolved_login = utils.resolve_login_customer_id(login_customer_id)
        service = utils.get_googleads_service(
            "GoogleAdsService", login_customer_id=resolved_login
        )
        utils.enforce_customer_access_root(service, normalized_customer)

        account_rows = self._search(
            service,
            normalized_customer,
            "SELECT customer.id, customer.descriptive_name, "
            "customer.currency_code, customer.time_zone FROM customer LIMIT 1",
        )
        if len(account_rows) != 1:
            raise BudgetAnalysisError("The advertiser account could not be resolved")
        account = account_rows[0].customer
        time_zone = account.time_zone or "UTC"
        try:
            start_date, end_date = resolve_analysis_window(
                analysis_start=analysis_start,
                analysis_end=analysis_end,
                account_time_zone=time_zone,
                now=self._now_fn(),
            )
        except ValueError as error:
            raise BudgetAnalysisError(str(error)) from error

        campaign_query = (
            "SELECT campaign.id, campaign.resource_name, campaign.name, "
            "campaign.status, campaign.advertising_channel_type, "
            "campaign.bidding_strategy_type, "
            "campaign.campaign_budget, campaign_budget.resource_name, "
            "campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
            "metrics.cost_micros, metrics.conversions, metrics.conversions_value, "
            "metrics.clicks, metrics.impressions, metrics.average_cpc, "
            "metrics.search_impression_share, "
            "metrics.search_budget_lost_impression_share, "
            "metrics.search_rank_lost_impression_share "
            "FROM campaign "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.status != 'REMOVED'"
            f"{_campaign_filter(campaign_ids)} "
            "ORDER BY metrics.cost_micros DESC LIMIT 500"
        )
        campaign_rows = self._search(service, normalized_customer, campaign_query)
        if not campaign_rows:
            raise BudgetAnalysisError("No campaigns had data in the selected window")

        coverage_gaps: list[str] = []
        goal_context = self._goal_context(
            service,
            normalized_customer,
            [str(row.campaign.id) for row in campaign_rows],
            coverage_gaps,
        )
        change_context = self._change_context(
            service,
            normalized_customer,
            campaign_rows,
            coverage_gaps,
        )

        campaigns = []
        for row in campaign_rows:
            campaign = row.campaign
            budget = row.campaign_budget
            campaign_id = str(campaign.id)
            channel = _enum(campaign.advertising_channel_type)
            is_search_inventory = channel in {"SEARCH", "SHOPPING"}
            goal = goal_context.get(campaign_id) or {
                "verified": False,
                "scope": "unable_to_verify",
                "actions": [],
            }
            campaigns.append(
                {
                    "id": campaign_id,
                    "resource_name": campaign.resource_name,
                    "name": campaign.name,
                    "status": _enum(campaign.status),
                    "channel_type": channel,
                    "bidding_strategy_type": _enum(campaign.bidding_strategy_type),
                    "budget_resource_name": budget.resource_name
                    or campaign.campaign_budget,
                    "daily_budget_micros": int(budget.amount_micros),
                    "budget_explicitly_shared": bool(budget.explicitly_shared),
                    "cost_micros": int(row.metrics.cost_micros),
                    "conversions": float(row.metrics.conversions),
                    "conversions_value": float(row.metrics.conversions_value),
                    "clicks": int(row.metrics.clicks),
                    "impressions": int(row.metrics.impressions),
                    "average_cpc_micros": int(row.metrics.average_cpc),
                    "search_impression_share": (
                        float(row.metrics.search_impression_share)
                        if is_search_inventory
                        else None
                    ),
                    "search_budget_lost_impression_share": (
                        float(row.metrics.search_budget_lost_impression_share)
                        if is_search_inventory
                        else None
                    ),
                    "search_rank_lost_impression_share": (
                        float(row.metrics.search_rank_lost_impression_share)
                        if is_search_inventory
                        else None
                    ),
                    "goal_scope_verified": bool(goal["verified"]),
                    "goal_scope": goal["scope"],
                    "effective_conversion_actions": goal["actions"],
                    "change_history_verified": change_context["verified"],
                    "recent_material_change_at": change_context["campaigns"].get(
                        campaign_id
                    ),
                }
            )

        return {
            "customer_id": normalized_customer,
            "login_customer_id": resolved_login,
            "account_name": account.descriptive_name or normalized_customer,
            "currency": account.currency_code,
            "time_zone": time_zone,
            "analysis_start": start_date.isoformat(),
            "analysis_end": end_date.isoformat(),
            "retrieved_at": self._now_fn().isoformat(),
            "data_through": end_date.isoformat(),
            "source": "GMA 13 Skills Google Ads",
            "campaigns": campaigns,
            "coverage_gaps": coverage_gaps,
        }

    def _goal_context(
        self,
        service,
        customer_id: str,
        campaign_ids: Sequence[str],
        coverage_gaps: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Resolve campaign-effective goal scope and action names conservatively."""

        try:
            action_rows = self._search(
                service,
                customer_id,
                "SELECT conversion_action.resource_name, conversion_action.name, "
                "conversion_action.category, conversion_action.origin, "
                "conversion_action.status, conversion_action.primary_for_goal "
                "FROM conversion_action WHERE conversion_action.status != 'REMOVED' "
                "LIMIT 500",
            )
            actions = []
            for row in action_rows:
                action = row.conversion_action
                actions.append(
                    {
                        "resource_name": action.resource_name,
                        "name": action.name,
                        "category": _enum(action.category),
                        "origin": _enum(action.origin),
                        "enabled": _enum(action.status) == "ENABLED",
                        "primary": bool(action.primary_for_goal),
                    }
                )

            config_rows = self._search(
                service,
                customer_id,
                "SELECT conversion_goal_campaign_config.campaign, "
                "conversion_goal_campaign_config.goal_config_level, "
                "conversion_goal_campaign_config.custom_conversion_goal "
                "FROM conversion_goal_campaign_config LIMIT 5000",
            )
            configs = {
                str(row.conversion_goal_campaign_config.campaign).rsplit("/", 1)[-1]: {
                    "level": _enum(
                        row.conversion_goal_campaign_config.goal_config_level
                    ),
                    "custom": row.conversion_goal_campaign_config.custom_conversion_goal,
                }
                for row in config_rows
            }
            customer_goal_rows = self._search(
                service,
                customer_id,
                "SELECT customer_conversion_goal.category, "
                "customer_conversion_goal.origin, customer_conversion_goal.biddable "
                "FROM customer_conversion_goal LIMIT 500",
            )
            account_pairs = {
                (
                    _enum(row.customer_conversion_goal.category),
                    _enum(row.customer_conversion_goal.origin),
                )
                for row in customer_goal_rows
                if row.customer_conversion_goal.biddable
            }
            campaign_goal_rows = self._search(
                service,
                customer_id,
                "SELECT campaign_conversion_goal.campaign, "
                "campaign_conversion_goal.category, campaign_conversion_goal.origin, "
                "campaign_conversion_goal.biddable FROM campaign_conversion_goal "
                "LIMIT 5000",
            )
            campaign_pairs: dict[str, set[tuple[str, str]]] = {}
            for row in campaign_goal_rows:
                goal = row.campaign_conversion_goal
                campaign_id = str(goal.campaign).rsplit("/", 1)[-1]
                if goal.biddable:
                    campaign_pairs.setdefault(campaign_id, set()).add(
                        (_enum(goal.category), _enum(goal.origin))
                    )

            custom_names: dict[str, list[str]] = {}
            custom_resources = sorted(
                {config["custom"] for config in configs.values() if config["custom"]}
            )
            for resource_name in custom_resources:
                rows = self._search(
                    service,
                    customer_id,
                    "SELECT custom_conversion_goal.resource_name, "
                    "custom_conversion_goal.status, "
                    "custom_conversion_goal.conversion_actions "
                    "FROM custom_conversion_goal "
                    f"WHERE custom_conversion_goal.resource_name = '{resource_name}' LIMIT 1",
                )
                if (
                    not rows
                    or _enum(rows[0].custom_conversion_goal.status) != "ENABLED"
                ):
                    continue
                resources = set(rows[0].custom_conversion_goal.conversion_actions)
                custom_names[resource_name] = [
                    action["name"]
                    for action in actions
                    if action["resource_name"] in resources and action["enabled"]
                ]

            resolved: dict[str, dict[str, Any]] = {}
            for campaign_id in campaign_ids:
                config = configs.get(campaign_id)
                if not config:
                    resolved[campaign_id] = {
                        "verified": False,
                        "scope": "unable_to_verify",
                        "actions": [],
                    }
                    continue
                if config["custom"]:
                    names = custom_names.get(config["custom"], [])
                    scope = "custom_goal"
                else:
                    scope = (
                        "campaign_specific"
                        if config["level"] == "CAMPAIGN"
                        else "account_default"
                    )
                    pairs = (
                        campaign_pairs.get(campaign_id, set())
                        if scope == "campaign_specific"
                        else account_pairs
                    )
                    names = [
                        action["name"]
                        for action in actions
                        if action["enabled"]
                        and action["primary"]
                        and (action["category"], action["origin"]) in pairs
                    ]
                resolved[campaign_id] = {
                    "verified": bool(names),
                    "scope": scope,
                    "actions": sorted(set(names)),
                }
            return resolved
        except Exception as error:
            coverage_gaps.append(
                "Campaign-effective conversion goals unavailable: " + str(error)[:240]
            )
            return {}

    def _change_context(
        self,
        service,
        customer_id: str,
        campaign_rows: Sequence[Any],
        coverage_gaps: list[str],
    ) -> dict[str, Any]:
        campaign_resources = {
            row.campaign.resource_name: str(row.campaign.id) for row in campaign_rows
        }
        budget_to_campaigns: dict[str, list[str]] = {}
        for row in campaign_rows:
            budget_to_campaigns.setdefault(
                row.campaign_budget.resource_name, []
            ).append(str(row.campaign.id))
        try:
            rows = self._search(
                service,
                customer_id,
                "SELECT change_event.change_date_time, "
                "change_event.change_resource_name, change_event.changed_fields "
                "FROM change_event WHERE change_event.change_date_time "
                "DURING LAST_14_DAYS ORDER BY change_event.change_date_time DESC "
                "LIMIT 1000",
            )
            recent: dict[str, str] = {}
            material_terms = (
                "amount_micros",
                "bidding_strategy",
                "target_cpa",
                "target_roas",
            )
            for row in rows:
                event = row.change_event
                fields = " ".join(event.changed_fields.paths)
                if not any(term in fields for term in material_terms):
                    continue
                affected = []
                if event.change_resource_name in campaign_resources:
                    affected.append(campaign_resources[event.change_resource_name])
                affected.extend(budget_to_campaigns.get(event.change_resource_name, []))
                for campaign_id in affected:
                    recent.setdefault(campaign_id, event.change_date_time)
            return {"verified": True, "campaigns": recent}
        except Exception as error:
            coverage_gaps.append(
                "Recent bid/budget change history unavailable: " + str(error)[:240]
            )
            return {"verified": False, "campaigns": {}}


class BudgetReallocatorRunService:
    def __init__(
        self,
        *,
        gateway: GoogleAdsBudgetGateway | None = None,
        changesets: ChangesetService | None = None,
    ) -> None:
        self._gateway = gateway or GoogleAdsBudgetGateway()
        self._changesets = changesets or get_changeset_service()

    async def run(
        self,
        *,
        customer_id: str,
        business_mode: str,
        target_cpa: float | None = None,
        target_roas: float | None = None,
        outcome_quality_confirmed: bool = False,
        analysis_start: str | None = None,
        analysis_end: str | None = None,
        campaign_ids: Sequence[str] | None = None,
        monthly_budget: float | None = None,
        allow_net_increase: bool = False,
        login_customer_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._gateway.fetch_snapshot(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            campaign_ids=campaign_ids,
        )
        result = evaluate_budget_reallocation(
            snapshot,
            business_mode=business_mode,
            target_cpa_micros=_micros(target_cpa, "target_cpa"),
            target_roas=target_roas,
            outcome_quality_confirmed=outcome_quality_confirmed,
            monthly_budget_micros=_micros(monthly_budget, "monthly_budget"),
            allow_net_increase=allow_net_increase,
        )
        plan = await self._changesets.create(
            {
                "customer_id": snapshot["customer_id"],
                "login_customer_id": snapshot.get("login_customer_id"),
                "account_name": snapshot["account_name"],
                "currency": snapshot["currency"],
                "time_zone": snapshot["time_zone"],
                "analysis_start": snapshot["analysis_start"],
                "analysis_end": snapshot["analysis_end"],
                "analysis_label": (
                    "Last 30 days"
                    if result["analysis_days"] == 30
                    else f"{result['analysis_days']} days"
                ),
                "campaign_scope": "selected" if campaign_ids else "all_eligible",
                "campaigns": [
                    {"id": campaign["id"], "name": campaign["name"]}
                    for campaign in snapshot["campaigns"]
                ],
                "skills": ["budget-reallocator"],
                "run_mode": "interactive",
                "coverage_gaps": snapshot["coverage_gaps"],
                "data_quality_holds": result["holds"],
                "actions": result["recommendations"],
                "recovery_actions": result["recovery_actions"],
            }
        )
        result["account"] = {
            "customer_id": snapshot["customer_id"],
            "name": snapshot["account_name"],
            "currency": snapshot["currency"],
            "time_zone": snapshot["time_zone"],
        }
        result["data_receipt"] = {
            "source": snapshot["source"],
            "retrieved_at": snapshot["retrieved_at"],
            "data_through": snapshot["data_through"],
            "analysis_start": snapshot["analysis_start"],
            "analysis_end": snapshot["analysis_end"],
        }
        result["change_plan"] = {
            "id": plan["id"],
            "status": plan["status"],
            "review_url": plan["review_url"],
            "applyable_action_ids": result["applyable_action_ids"],
        }
        return result
