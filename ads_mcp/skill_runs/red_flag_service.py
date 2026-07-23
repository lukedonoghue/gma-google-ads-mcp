"""Fixed-query Google Ads adapter and service for Red-Flag Radar."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Sequence

from ads_mcp import utils
from ads_mcp.changesets import ChangesetService, get_changeset_service
from ads_mcp.skill_runs.budget_service import GoogleAdsBudgetGateway
from ads_mcp.skill_runs.common import resolve_analysis_window
from ads_mcp.skill_runs.red_flag_radar import (
    RedFlagAnalysisError,
    evaluate_red_flag_radar,
)


def _enum(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _micros(value: float | int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or float(value) <= 0:
        raise RedFlagAnalysisError(f"{field} must be greater than zero")
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
            raise RedFlagAnalysisError("Campaign IDs must be numeric")
        normalized.append(campaign_id)
    return " AND campaign.id IN (" + ",".join(sorted(set(normalized))) + ")"


def _empty_metrics() -> dict[str, Any]:
    return {
        "cost_micros": 0,
        "conversions": 0.0,
        "conversions_value": 0.0,
        "clicks": 0,
        "impressions": 0,
        "average_cpc_micros": 0,
        "search_impression_share": None,
        "search_budget_lost_impression_share": None,
        "search_rank_lost_impression_share": None,
    }


class GoogleAdsRedFlagGateway(GoogleAdsBudgetGateway):
    """Build the complete radar snapshot with fixed, server-owned GAQL."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        super().__init__(now_fn=now_fn)
        self._radar_now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _metrics_from_row(row: Any) -> dict[str, Any]:
        metrics = row.metrics
        channel = _enum(row.campaign.advertising_channel_type)
        search_inventory = channel in {"SEARCH", "SHOPPING"}
        return {
            "cost_micros": int(metrics.cost_micros),
            "conversions": float(metrics.conversions),
            "conversions_value": float(metrics.conversions_value),
            "clicks": int(metrics.clicks),
            "impressions": int(metrics.impressions),
            "average_cpc_micros": int(metrics.average_cpc),
            "search_impression_share": (
                float(metrics.search_impression_share)
                if search_inventory
                else None
            ),
            "search_budget_lost_impression_share": (
                float(metrics.search_budget_lost_impression_share)
                if search_inventory
                else None
            ),
            "search_rank_lost_impression_share": (
                float(metrics.search_rank_lost_impression_share)
                if search_inventory
                else None
            ),
        }

    def _metric_window(
        self,
        service: Any,
        customer_id: str,
        *,
        start_date: str,
        end_date: str,
        campaign_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        query = (
            "SELECT campaign.id, campaign.advertising_channel_type, "
            "metrics.cost_micros, metrics.conversions, metrics.conversions_value, "
            "metrics.clicks, metrics.impressions, metrics.average_cpc, "
            "metrics.search_impression_share, "
            "metrics.search_budget_lost_impression_share, "
            "metrics.search_rank_lost_impression_share "
            "FROM campaign "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.status != 'REMOVED'"
            f"{_campaign_filter(campaign_ids)} "
            "LIMIT 500"
        )
        return {
            str(row.campaign.id): self._metrics_from_row(row)
            for row in self._search(service, customer_id, query)
        }

    def _policy_context(
        self,
        service: Any,
        customer_id: str,
        campaign_ids: Sequence[str],
        core_gaps: list[str],
    ) -> dict[str, dict[str, Any]]:
        context = {
            campaign_id: {
                "verified": True,
                "active_ads": 0,
                "disapproved": 0,
                "limited": 0,
            }
            for campaign_id in campaign_ids
        }
        try:
            query = (
                "SELECT campaign.id, ad_group_ad.status, "
                "ad_group_ad.policy_summary.approval_status "
                "FROM ad_group_ad "
                "WHERE ad_group_ad.status != 'REMOVED'"
                f"{_campaign_filter(campaign_ids)} "
                "LIMIT 5000"
            )
            for row in self._search(service, customer_id, query):
                campaign_id = str(row.campaign.id)
                item = context.setdefault(
                    campaign_id,
                    {
                        "verified": True,
                        "active_ads": 0,
                        "disapproved": 0,
                        "limited": 0,
                    },
                )
                if _enum(row.ad_group_ad.status) != "ENABLED":
                    continue
                item["active_ads"] += 1
                approval = _enum(
                    row.ad_group_ad.policy_summary.approval_status
                )
                if approval == "DISAPPROVED":
                    item["disapproved"] += 1
                elif approval in {"APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"}:
                    item["limited"] += 1
            return context
        except Exception as error:
            core_gaps.append(
                "Active-ad policy summaries unavailable: " + str(error)[:240]
            )
            return {
                campaign_id: {
                    "verified": False,
                    "active_ads": 0,
                    "disapproved": 0,
                    "limited": 0,
                }
                for campaign_id in campaign_ids
            }

    def fetch_snapshot(
        self,
        *,
        customer_id: str,
        login_customer_id: str | None,
        analysis_start: str | None,
        analysis_end: str | None,
        campaign_ids: Sequence[str] | None,
        business_mode: str,
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
            raise RedFlagAnalysisError("The advertiser account could not be resolved")
        account = account_rows[0].customer
        time_zone = account.time_zone or "UTC"
        try:
            start_date, end_date = resolve_analysis_window(
                analysis_start=analysis_start,
                analysis_end=analysis_end,
                account_time_zone=time_zone,
                now=self._radar_now_fn(),
            )
        except ValueError as error:
            raise RedFlagAnalysisError(str(error)) from error
        if (end_date - start_date).days + 1 < 14:
            raise RedFlagAnalysisError(
                "Red-Flag Radar needs at least 14 complete days"
            )

        campaign_query = (
            "SELECT campaign.id, campaign.resource_name, campaign.name, "
            "campaign.status, campaign.advertising_channel_type, "
            "campaign.bidding_strategy_type, campaign.campaign_budget, "
            "campaign_budget.resource_name, campaign_budget.amount_micros, "
            "campaign_budget.explicitly_shared "
            "FROM campaign WHERE campaign.status != 'REMOVED'"
            f"{_campaign_filter(campaign_ids)} "
            "ORDER BY campaign.name LIMIT 500"
        )
        campaign_rows = self._search(service, normalized_customer, campaign_query)
        if not campaign_rows:
            raise RedFlagAnalysisError("No campaigns were found in the selected scope")
        resolved_campaign_ids = [str(row.campaign.id) for row in campaign_rows]

        current_metrics = self._metric_window(
            service,
            normalized_customer,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            campaign_ids=resolved_campaign_ids,
        )
        latest_start = end_date - timedelta(days=6)
        previous_end = latest_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=6)
        latest_metrics = self._metric_window(
            service,
            normalized_customer,
            start_date=latest_start.isoformat(),
            end_date=end_date.isoformat(),
            campaign_ids=resolved_campaign_ids,
        )
        previous_metrics = self._metric_window(
            service,
            normalized_customer,
            start_date=previous_start.isoformat(),
            end_date=previous_end.isoformat(),
            campaign_ids=resolved_campaign_ids,
        )

        core_gaps: list[str] = []
        limitations: list[str] = []
        goal_context = self._goal_context(
            service,
            normalized_customer,
            resolved_campaign_ids,
            core_gaps,
        )
        change_context = self._change_context(
            service,
            normalized_customer,
            campaign_rows,
            core_gaps,
        )
        policy_context = self._policy_context(
            service,
            normalized_customer,
            resolved_campaign_ids,
            core_gaps,
        )
        if business_mode == "lead_gen":
            limitations.append(
                "Backend lead quality and duplicate/junk disposition were not available from Google Ads; genuine outcomes require account-owner confirmation."
            )
        else:
            limitations.append(
                "Merchant Center feed and product disapprovals were not checked because this connector currently has Google Ads, not Merchant Center, access."
            )
        limitations.append(
            "Landing-page HTML, forms, and checkout availability were not crawled in this Google Ads-only run."
        )

        campaigns = []
        for row in campaign_rows:
            campaign = row.campaign
            campaign_id = str(campaign.id)
            budget = row.campaign_budget
            goal = goal_context.get(campaign_id) or {
                "verified": False,
                "scope": "unable_to_verify",
                "actions": [],
            }
            metrics = {
                **_empty_metrics(),
                **current_metrics.get(campaign_id, {}),
            }
            campaigns.append(
                {
                    "id": campaign_id,
                    "resource_name": campaign.resource_name,
                    "name": campaign.name,
                    "status": _enum(campaign.status),
                    "channel_type": _enum(campaign.advertising_channel_type),
                    "bidding_strategy_type": _enum(
                        campaign.bidding_strategy_type
                    ),
                    "budget_resource_name": budget.resource_name
                    or campaign.campaign_budget,
                    "daily_budget_micros": int(budget.amount_micros),
                    "budget_explicitly_shared": bool(budget.explicitly_shared),
                    **metrics,
                    "goal_scope_verified": bool(goal["verified"]),
                    "goal_scope": goal["scope"],
                    "effective_conversion_actions": goal["actions"],
                    "change_history_verified": change_context["verified"],
                    "recent_material_change_at": change_context["campaigns"].get(
                        campaign_id
                    ),
                    "policy": policy_context.get(campaign_id),
                    "latest_period": {
                        **_empty_metrics(),
                        **latest_metrics.get(campaign_id, {}),
                    },
                    "previous_period": {
                        **_empty_metrics(),
                        **previous_metrics.get(campaign_id, {}),
                    },
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
            "retrieved_at": self._radar_now_fn().isoformat(),
            "data_through": end_date.isoformat(),
            "source": "GMA 13 Skills Google Ads",
            "campaigns": campaigns,
            "core_coverage_gaps": core_gaps,
            "coverage_gaps": limitations,
        }


class RedFlagRadarRunService:
    def __init__(
        self,
        *,
        gateway: GoogleAdsRedFlagGateway | None = None,
        changesets: ChangesetService | None = None,
    ) -> None:
        self._gateway = gateway or GoogleAdsRedFlagGateway()
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
        login_customer_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._gateway.fetch_snapshot(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            campaign_ids=campaign_ids,
            business_mode=business_mode,
        )
        result = evaluate_red_flag_radar(
            snapshot,
            business_mode=business_mode,
            target_cpa_micros=_micros(target_cpa, "target_cpa"),
            target_roas=target_roas,
            outcome_quality_confirmed=outcome_quality_confirmed,
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
                "analysis_label": "Red-Flag Radar comparison",
                "campaign_scope": "selected" if campaign_ids else "all_eligible",
                "campaigns": [
                    {"id": campaign["id"], "name": campaign["name"]}
                    for campaign in snapshot["campaigns"]
                ],
                "skills": ["red-flag-radar"],
                "run_mode": "interactive",
                "coverage_gaps": result["coverage_gaps"],
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
            "applyable_action_ids": [],
        }
        return result
