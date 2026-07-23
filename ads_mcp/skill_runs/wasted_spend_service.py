"""Fixed-query Google Ads adapter for GMA Skill 3."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Sequence

from ads_mcp import utils
from ads_mcp.changesets import ChangesetService, get_changeset_service
from ads_mcp.skill_runs.budget_service import GoogleAdsBudgetGateway
from ads_mcp.skill_runs.common import resolve_analysis_window
from ads_mcp.skill_runs.wasted_spend_finder import (
    WastedSpendAnalysisError,
    evaluate_wasted_spend,
)


def _enum(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _micros(value: float | int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or float(value) <= 0:
        raise WastedSpendAnalysisError(f"{field} must be greater than zero")
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _campaign_filter(campaign_ids: Sequence[str] | None) -> str:
    if not campaign_ids:
        return ""
    normalized = []
    for raw in campaign_ids:
        value = str(raw).strip()
        if not re.fullmatch(r"\d{1,20}", value):
            raise WastedSpendAnalysisError("Campaign IDs must be numeric")
        normalized.append(value)
    return " AND campaign.id IN (" + ",".join(sorted(set(normalized))) + ")"


class GoogleAdsWastedSpendGateway(GoogleAdsBudgetGateway):
    """Build one bounded, normalized search-term snapshot."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        super().__init__(now_fn=now_fn)
        self._waste_now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _term_metrics(row: Any) -> dict[str, Any]:
        return {
            "cost_micros": int(row.metrics.cost_micros),
            "conversions": float(row.metrics.conversions),
            "all_conversions": float(row.metrics.all_conversions),
            "conversions_value": float(row.metrics.conversions_value),
            "clicks": int(row.metrics.clicks),
            "impressions": int(row.metrics.impressions),
        }

    def _search_term_rows(
        self,
        service: Any,
        customer_id: str,
        *,
        start_date: str,
        end_date: str,
        campaign_ids: Sequence[str],
        order_by: str,
        metric_filter: str,
        lane: str,
        statuses: Mapping[tuple[str, str], str],
        gaps: list[str],
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT search_term_view.search_term, search_term_view.status, "
            "campaign.id, campaign.name, campaign.advertising_channel_type, "
            "ad_group.id, ad_group.name, segments.search_term_match_type, "
            "metrics.cost_micros, metrics.conversions, metrics.all_conversions, "
            "metrics.conversions_value, metrics.clicks, metrics.impressions "
            "FROM search_term_view "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.advertising_channel_type = 'SEARCH' "
            f"AND {metric_filter}"
            f"{_campaign_filter(campaign_ids)} "
            f"ORDER BY {order_by} DESC LIMIT 200"
        )
        try:
            rows = self._search(service, customer_id, query)
        except Exception:
            gaps.append(
                f"Search campaign {lane.replace('_', ' ')} search terms were unavailable"
            )
            return []
        if len(rows) == 200:
            gaps.append(
                f"Search campaign {lane.replace('_', ' ')} reached the 200-row decision cap"
            )
        result = []
        for row in rows:
            result.append(
                {
                    "search_term": row.search_term_view.search_term,
                    "status": _enum(row.search_term_view.status),
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "channel_type": _enum(
                        row.campaign.advertising_channel_type
                    ),
                    "ad_group_id": str(row.ad_group.id),
                    "ad_group_name": row.ad_group.name,
                    "match_type": _enum(row.segments.search_term_match_type),
                    "lane": lane,
                    **self._term_metrics(row),
                }
            )
        return result

    def _pmax_rows(
        self,
        service: Any,
        customer_id: str,
        *,
        start_date: str,
        end_date: str,
        campaign_ids: Sequence[str],
        order_by: str,
        metric_filter: str,
        lane: str,
        gaps: list[str],
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT campaign_search_term_view.search_term, campaign.id, "
            "campaign.name, campaign.advertising_channel_type, "
            "segments.search_term_match_type, "
            "metrics.cost_micros, metrics.conversions, metrics.all_conversions, "
            "metrics.conversions_value, metrics.clicks, metrics.impressions "
            "FROM campaign_search_term_view "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.advertising_channel_type = 'PERFORMANCE_MAX' "
            f"AND {metric_filter}"
            f"{_campaign_filter(campaign_ids)} "
            f"ORDER BY {order_by} DESC LIMIT 200"
        )
        try:
            rows = self._search(service, customer_id, query)
        except Exception:
            gaps.append(
                f"Performance Max {lane.replace('_', ' ')} search terms were unavailable"
            )
            return []
        if len(rows) == 200:
            gaps.append(
                f"Performance Max {lane.replace('_', ' ')} reached the 200-row decision cap"
            )
        result = []
        for row in rows:
            result.append(
                {
                    "search_term": row.campaign_search_term_view.search_term,
                    "status": statuses.get(
                        (
                            str(row.campaign.id),
                            row.campaign_search_term_view.search_term,
                        ),
                        "NONE",
                    ),
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "channel_type": _enum(
                        row.campaign.advertising_channel_type
                    ),
                    "ad_group_id": "",
                    "ad_group_name": "",
                    "match_type": _enum(row.segments.search_term_match_type),
                    "lane": lane,
                    **self._term_metrics(row),
                }
            )
        return result

    def _pmax_statuses(
        self,
        service: Any,
        customer_id: str,
        *,
        start_date: str,
        end_date: str,
        campaign_ids: Sequence[str],
        gaps: list[str],
    ) -> dict[tuple[str, str], str]:
        query = (
            "SELECT campaign_search_term_view.search_term, campaign.id, "
            "segments.search_term_targeting_status "
            "FROM campaign_search_term_view "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.advertising_channel_type = 'PERFORMANCE_MAX'"
            f"{_campaign_filter(campaign_ids)} LIMIT 1000"
        )
        try:
            rows = self._search(service, customer_id, query)
        except Exception:
            gaps.append(
                "Performance Max search-term exclusion status was unavailable"
            )
            return {}
        if len(rows) == 1000:
            gaps.append(
                "Performance Max search-term exclusion status reached the 1,000-row decision cap"
            )
        return {
            (
                str(row.campaign.id),
                row.campaign_search_term_view.search_term,
            ): _enum(row.segments.search_term_targeting_status)
            for row in rows
        }

    def _existing_negatives(
        self,
        service: Any,
        customer_id: str,
        campaign_ids: Sequence[str],
        gaps: list[str],
    ) -> list[dict[str, Any]]:
        existing: list[dict[str, Any]] = []
        campaign_filter = _campaign_filter(campaign_ids)
        try:
            rows = self._search(
                service,
                customer_id,
                "SELECT campaign.id, campaign.name, "
                "campaign_criterion.keyword.text, "
                "campaign_criterion.keyword.match_type "
                "FROM campaign_criterion "
                "WHERE campaign_criterion.negative = TRUE "
                "AND campaign_criterion.type = 'KEYWORD'"
                f"{campaign_filter} LIMIT 5000",
            )
            for row in rows:
                existing.append(
                    {
                        "text": row.campaign_criterion.keyword.text,
                        "match_type": _enum(
                            row.campaign_criterion.keyword.match_type
                        ),
                        "scope": "campaign",
                        "campaign_ids": [str(row.campaign.id)],
                        "ad_group_ids": [],
                        "list_name": row.campaign.name,
                    }
                )
        except Exception:
            gaps.append("Campaign-level negative keywords were unavailable")

        try:
            rows = self._search(
                service,
                customer_id,
                "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, "
                "ad_group_criterion.keyword.text, "
                "ad_group_criterion.keyword.match_type "
                "FROM ad_group_criterion "
                "WHERE ad_group_criterion.negative = TRUE "
                "AND ad_group_criterion.type = 'KEYWORD'"
                f"{campaign_filter} LIMIT 5000",
            )
            for row in rows:
                existing.append(
                    {
                        "text": row.ad_group_criterion.keyword.text,
                        "match_type": _enum(
                            row.ad_group_criterion.keyword.match_type
                        ),
                        "scope": "ad_group",
                        "campaign_ids": [str(row.campaign.id)],
                        "ad_group_ids": [str(row.ad_group.id)],
                        "list_name": row.ad_group.name,
                    }
                )
        except Exception:
            gaps.append("Ad-group negative keywords were unavailable")

        shared_names: dict[str, str] = {}
        shared_terms: dict[str, list[dict[str, Any]]] = {}
        attachments: dict[str, set[str]] = {}
        account_sets: set[str] = set()
        try:
            set_rows = self._search(
                service,
                customer_id,
                "SELECT shared_set.resource_name, shared_set.name, "
                "shared_set.status, shared_set.type FROM shared_set "
                "WHERE shared_set.type = 'NEGATIVE_KEYWORDS' LIMIT 1000",
            )
            shared_names = {
                row.shared_set.resource_name: row.shared_set.name
                for row in set_rows
                if _enum(row.shared_set.status) != "REMOVED"
            }
            criterion_rows = self._search(
                service,
                customer_id,
                "SELECT shared_criterion.shared_set, "
                "shared_criterion.keyword.text, "
                "shared_criterion.keyword.match_type FROM shared_criterion "
                "LIMIT 10000",
            )
            for row in criterion_rows:
                resource = row.shared_criterion.shared_set
                if resource not in shared_names:
                    continue
                shared_terms.setdefault(resource, []).append(
                    {
                        "text": row.shared_criterion.keyword.text,
                        "match_type": _enum(
                            row.shared_criterion.keyword.match_type
                        ),
                    }
                )
            attachment_rows = self._search(
                service,
                customer_id,
                "SELECT campaign.id, campaign_shared_set.shared_set, "
                "campaign_shared_set.status FROM campaign_shared_set "
                "WHERE campaign_shared_set.status = 'ENABLED'"
                f"{campaign_filter} LIMIT 5000",
            )
            for row in attachment_rows:
                attachments.setdefault(
                    row.campaign_shared_set.shared_set, set()
                ).add(str(row.campaign.id))
            account_rows = self._search(
                service,
                customer_id,
                "SELECT customer_negative_criterion.negative_keyword_list.shared_set "
                "FROM customer_negative_criterion "
                "WHERE customer_negative_criterion.type = 'NEGATIVE_KEYWORD_LIST' "
                "LIMIT 1000",
            )
            account_sets = {
                row.customer_negative_criterion.negative_keyword_list.shared_set
                for row in account_rows
                if row.customer_negative_criterion.negative_keyword_list.shared_set
            }
            for resource, terms in shared_terms.items():
                scope = "account" if resource in account_sets else "shared_list"
                campaign_scope = (
                    []
                    if scope == "account"
                    else sorted(attachments.get(resource, set()))
                )
                for term in terms:
                    existing.append(
                        {
                            **term,
                            "scope": scope,
                            "campaign_ids": campaign_scope,
                            "ad_group_ids": [],
                            "list_name": shared_names.get(resource) or resource,
                        }
                    )
        except Exception:
            gaps.append(
                "Shared/account negative-list membership or campaign attachments were unavailable"
            )
        return existing

    def fetch_snapshot(
        self,
        *,
        customer_id: str,
        login_customer_id: str | None,
        analysis_start: str | None,
        analysis_end: str | None,
        campaign_ids: Sequence[str] | None,
    ) -> dict[str, Any]:
        normalized_customer = utils._normalize_customer_id(
            customer_id, "customer_id"
        )
        resolved_login = utils.resolve_login_customer_id(login_customer_id)
        service = utils.get_googleads_service(
            "GoogleAdsService", login_customer_id=resolved_login
        )
        utils.enforce_customer_access_root(service, normalized_customer)
        now = self._waste_now_fn()

        account_rows = self._search(
            service,
            normalized_customer,
            "SELECT customer.id, customer.descriptive_name, "
            "customer.currency_code, customer.time_zone FROM customer LIMIT 1",
        )
        if len(account_rows) != 1:
            raise WastedSpendAnalysisError(
                "The advertiser account could not be resolved"
            )
        account = account_rows[0].customer
        time_zone = account.time_zone or "UTC"
        try:
            start_date, end_date = resolve_analysis_window(
                analysis_start=analysis_start,
                analysis_end=analysis_end,
                account_time_zone=time_zone,
                now=now,
            )
        except ValueError as error:
            raise WastedSpendAnalysisError(str(error)) from error

        campaign_query = (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, metrics.cost_micros, "
            "metrics.conversions, metrics.conversions_value, metrics.clicks, "
            "metrics.impressions FROM campaign "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.status != 'REMOVED'"
            f"{_campaign_filter(campaign_ids)} "
            "ORDER BY metrics.cost_micros DESC LIMIT 500"
        )
        campaign_rows = self._search(
            service, normalized_customer, campaign_query
        )
        if not campaign_rows:
            raise WastedSpendAnalysisError(
                "No campaigns were found in the selected window"
            )
        campaigns = [
            {
                "id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": _enum(row.campaign.status),
                "channel_type": _enum(row.campaign.advertising_channel_type),
                "cost_micros": int(row.metrics.cost_micros),
                "conversions": float(row.metrics.conversions),
                "conversions_value": float(row.metrics.conversions_value),
                "clicks": int(row.metrics.clicks),
                "impressions": int(row.metrics.impressions),
            }
            for row in campaign_rows
        ]
        resolved_campaign_ids = [item["id"] for item in campaigns]
        gaps: list[str] = []
        terms = []
        terms.extend(
            self._search_term_rows(
                service,
                normalized_customer,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                campaign_ids=resolved_campaign_ids,
                order_by="metrics.cost_micros",
                metric_filter="metrics.clicks > 0",
                lane="spend_risk",
                gaps=gaps,
            )
        )
        terms.extend(
            self._search_term_rows(
                service,
                normalized_customer,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                campaign_ids=resolved_campaign_ids,
                order_by="metrics.impressions",
                metric_filter="metrics.impressions > 0",
                lane="match_pollution",
                gaps=gaps,
            )
        )
        pmax_statuses = self._pmax_statuses(
            service,
            normalized_customer,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            campaign_ids=resolved_campaign_ids,
            gaps=gaps,
        )
        terms.extend(
            self._pmax_rows(
                service,
                normalized_customer,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                campaign_ids=resolved_campaign_ids,
                order_by="metrics.cost_micros",
                metric_filter="metrics.clicks > 0",
                lane="spend_risk",
                statuses=pmax_statuses,
                gaps=gaps,
            )
        )
        terms.extend(
            self._pmax_rows(
                service,
                normalized_customer,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                campaign_ids=resolved_campaign_ids,
                order_by="metrics.impressions",
                metric_filter="metrics.impressions > 0",
                lane="match_pollution",
                statuses=pmax_statuses,
                gaps=gaps,
            )
        )
        existing = self._existing_negatives(
            service,
            normalized_customer,
            resolved_campaign_ids,
            gaps,
        )
        goal_context = self._goal_context(
            service,
            normalized_customer,
            resolved_campaign_ids,
            gaps,
        )
        active_campaigns = [
            campaign
            for campaign in campaigns
            if campaign["channel_type"] in {"SEARCH", "PERFORMANCE_MAX"}
            and campaign["cost_micros"] > 0
        ]
        goal_scope_verified = bool(active_campaigns) and all(
            bool((goal_context.get(campaign["id"]) or {}).get("verified"))
            for campaign in active_campaigns
        )
        total_cost = sum(item["cost_micros"] for item in campaigns)
        total_conversions = sum(item["conversions"] for item in campaigns)
        total_value = sum(item["conversions_value"] for item in campaigns)
        average_cpa_micros = (
            round(total_cost / total_conversions)
            if total_conversions > 0
            else None
        )
        average_order_value = (
            total_value / total_conversions if total_conversions > 0 else None
        )
        return {
            "customer_id": normalized_customer,
            "login_customer_id": resolved_login,
            "account_name": account.descriptive_name or normalized_customer,
            "currency": account.currency_code,
            "time_zone": time_zone,
            "analysis_start": start_date.isoformat(),
            "analysis_end": end_date.isoformat(),
            "retrieved_at": now.isoformat(),
            "data_through": end_date.isoformat(),
            "source": "GMA 13 Skills Google Ads",
            "campaigns": campaigns,
            "search_terms": terms,
            "existing_negatives": existing,
            "goal_scope_verified": goal_scope_verified,
            "effective_conversion_actions": {
                campaign_id: list(context.get("actions") or [])
                for campaign_id, context in goal_context.items()
            },
            "average_cpa_micros": average_cpa_micros,
            "average_order_value": average_order_value,
            "coverage_gaps": list(dict.fromkeys(gaps)),
        }


class WastedSpendFinderRunService:
    def __init__(
        self,
        *,
        gateway: GoogleAdsWastedSpendGateway | None = None,
        changesets: ChangesetService | None = None,
    ) -> None:
        self._gateway = gateway or GoogleAdsWastedSpendGateway()
        self._changesets = changesets or get_changeset_service()

    async def run(
        self,
        *,
        customer_id: str,
        login_customer_id: str | None,
        business_mode: str,
        analysis_start: str | None,
        analysis_end: str | None,
        campaign_ids: Sequence[str] | None,
        brand_terms: Sequence[str] | None,
        confirmed_irrelevant_themes: Sequence[str] | None = None,
        protected_intent_themes: Sequence[str] | None = None,
        competitor_policy: str = "review",
        target_cpa: float | None = None,
        target_roas: float | None = None,
        outcome_quality_confirmed: bool = False,
    ) -> dict[str, Any]:
        snapshot = self._gateway.fetch_snapshot(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            campaign_ids=campaign_ids,
        )
        result = evaluate_wasted_spend(
            snapshot,
            business_mode=business_mode,
            brand_terms=brand_terms,
            confirmed_irrelevant_themes=confirmed_irrelevant_themes,
            protected_intent_themes=protected_intent_themes,
            competitor_policy=competitor_policy,
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
                "analysis_label": (
                    "Last 30 days"
                    if result["analysis_days"] == 30
                    else f"{result['analysis_days']} days"
                ),
                "campaign_scope": (
                    "selected" if campaign_ids else "all_eligible"
                ),
                "campaigns": [
                    {"id": campaign["id"], "name": campaign["name"]}
                    for campaign in snapshot["campaigns"]
                ],
                "skills": ["wasted-spend-finder"],
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
