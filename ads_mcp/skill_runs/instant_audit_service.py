"""Fixed-query adapter and run service for Skill 1 — Instant Account Audit."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Sequence

from ads_mcp.changesets import ChangesetService, get_changeset_service
from ads_mcp.skill_runs.instant_account_audit import (
    InstantAuditAnalysisError,
    evaluate_instant_account_audit,
)
from ads_mcp.skill_runs.red_flag_service import (
    GoogleAdsRedFlagGateway,
    _campaign_filter,
    _enum,
)


def _micros(value: float | int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or float(value) <= 0:
        raise InstantAuditAnalysisError(f"{field} must be greater than zero")
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _value(obj: Any, path: str, default: Any = None) -> Any:
    current = obj
    for part in path.split("."):
        current = getattr(current, part, None)
        if current is None:
            return default
    return current


def _change_history_bounds(now: datetime) -> tuple[str, str]:
    """Return a finite API-safe 30-calendar-date Change Event window."""

    start = now - timedelta(days=29)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
    )


class GoogleAdsInstantAuditGateway(GoogleAdsRedFlagGateway):
    """Build a normalized full-audit snapshot using only server-owned queries."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        super().__init__(now_fn=now_fn)

    def _optional_query(
        self,
        service: Any,
        customer_id: str,
        query: str,
        *,
        gap: str,
        gaps: list[str],
    ) -> tuple[list[Any], bool]:
        try:
            return self._search(service, customer_id, query), True
        except Exception:
            # Coverage gaps are customer-facing. Keep provider exceptions and
            # transport details out of the report while still failing closed.
            gaps.append(gap)
            return [], False

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
        snapshot = super().fetch_snapshot(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            campaign_ids=campaign_ids,
            business_mode=business_mode,
        )
        from ads_mcp import utils

        service = utils.get_googleads_service(
            "GoogleAdsService",
            login_customer_id=snapshot.get("login_customer_id"),
        )
        normalized_customer = str(snapshot["customer_id"])
        campaign_ids = [str(item["id"]) for item in snapshot["campaigns"]]
        campaign_filter = _campaign_filter(campaign_ids)
        start = str(snapshot["analysis_start"])
        end = str(snapshot["analysis_end"])
        gaps = list(snapshot.get("core_coverage_gaps") or [])
        limitations = list(snapshot.get("coverage_gaps") or [])
        evidence: dict[str, Any] = {}

        account_rows, account_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT customer.auto_tagging_enabled, "
            "customer.conversion_tracking_setting.conversion_tracking_id, "
            "customer.conversion_tracking_setting.cross_account_conversion_tracking_id "
            "FROM customer LIMIT 1",
            gap="Account tracking settings unavailable",
            gaps=gaps,
        )
        evidence["account_verified"] = account_ok and len(account_rows) == 1
        if evidence["account_verified"]:
            customer = account_rows[0].customer
            evidence["account"] = {
                "auto_tagging_enabled": bool(customer.auto_tagging_enabled),
                "conversion_tracking_id": str(
                    _value(
                        customer,
                        "conversion_tracking_setting.conversion_tracking_id",
                        "",
                    )
                    or ""
                ),
                "cross_account_conversion_tracking_id": str(
                    _value(
                        customer,
                        "conversion_tracking_setting.cross_account_conversion_tracking_id",
                        "",
                    )
                    or ""
                ),
            }

        conversion_rows, conversion_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT conversion_action.resource_name, conversion_action.name, "
            "conversion_action.category, conversion_action.origin, "
            "conversion_action.type, conversion_action.status, "
            "conversion_action.primary_for_goal, "
            "conversion_action.attribution_model_settings.attribution_model, "
            "conversion_action.value_settings.default_value, "
            "conversion_action.value_settings.always_use_default_value "
            "FROM conversion_action WHERE conversion_action.status != 'REMOVED' "
            "LIMIT 500",
            gap="Conversion-action configuration unavailable",
            gaps=gaps,
        )
        evidence["conversion_actions_verified"] = conversion_ok
        evidence["conversion_actions"] = [
            {
                "resource_name": row.conversion_action.resource_name,
                "name": row.conversion_action.name,
                "category": _enum(row.conversion_action.category),
                "origin": _enum(row.conversion_action.origin),
                "type": _enum(row.conversion_action.type),
                "status": _enum(row.conversion_action.status),
                "primary_for_goal": bool(row.conversion_action.primary_for_goal),
                "attribution_model": _enum(
                    _value(
                        row.conversion_action,
                        "attribution_model_settings.attribution_model",
                        "",
                    )
                ),
                "default_value": float(
                    _value(
                        row.conversion_action,
                        "value_settings.default_value",
                        0,
                    )
                    or 0
                ),
                "always_use_default_value": bool(
                    _value(
                        row.conversion_action,
                        "value_settings.always_use_default_value",
                        False,
                    )
                ),
            }
            for row in conversion_rows
        ]

        campaign_rows, campaign_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT campaign.id, campaign.advertising_channel_type, "
            "campaign.bidding_strategy_type, "
            "campaign.target_cpa.target_cpa_micros, "
            "campaign.maximize_conversions.target_cpa_micros, "
            "campaign.target_roas.target_roas, "
            "campaign.maximize_conversion_value.target_roas, "
            "campaign.network_settings.target_content_network, "
            "metrics.cost_micros, metrics.conversions, metrics.all_conversions, "
            "metrics.conversions_value, metrics.clicks, metrics.impressions, "
            "metrics.search_impression_share, "
            "metrics.search_budget_lost_impression_share, "
            "metrics.search_rank_lost_impression_share "
            "FROM campaign "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            "AND campaign.status != 'REMOVED'"
            f"{campaign_filter} LIMIT 500",
            gap="Campaign audit metrics unavailable",
            gaps=gaps,
        )
        supplemental: dict[str, dict[str, Any]] = {}
        if campaign_ok:
            for row in campaign_rows:
                campaign = row.campaign
                target_cpa = int(
                    _value(campaign, "target_cpa.target_cpa_micros", 0) or 0
                ) or int(
                    _value(
                        campaign,
                        "maximize_conversions.target_cpa_micros",
                        0,
                    )
                    or 0
                )
                target_roas = float(
                    _value(campaign, "target_roas.target_roas", 0) or 0
                ) or float(
                    _value(
                        campaign,
                        "maximize_conversion_value.target_roas",
                        0,
                    )
                    or 0
                )
                supplemental[str(campaign.id)] = {
                    "bidding_strategy_type": _enum(campaign.bidding_strategy_type),
                    "target_cpa_micros": target_cpa,
                    "target_roas": target_roas,
                    "target_content_network": bool(
                        _value(
                            campaign,
                            "network_settings.target_content_network",
                            False,
                        )
                    ),
                    "cost_micros": int(row.metrics.cost_micros),
                    "conversions": float(row.metrics.conversions),
                    "all_conversions": float(row.metrics.all_conversions),
                    "conversions_value": float(row.metrics.conversions_value),
                    "clicks": int(row.metrics.clicks),
                    "impressions": int(row.metrics.impressions),
                    "search_impression_share": (
                        float(row.metrics.search_impression_share)
                        if _enum(campaign.advertising_channel_type)
                        in {"SEARCH", "SHOPPING"}
                        else None
                    ),
                    "search_budget_lost_impression_share": (
                        float(row.metrics.search_budget_lost_impression_share)
                        if _enum(campaign.advertising_channel_type)
                        in {"SEARCH", "SHOPPING"}
                        else None
                    ),
                    "search_rank_lost_impression_share": (
                        float(row.metrics.search_rank_lost_impression_share)
                        if _enum(campaign.advertising_channel_type)
                        in {"SEARCH", "SHOPPING"}
                        else None
                    ),
                }
        for campaign in snapshot["campaigns"]:
            campaign.update(supplemental.get(str(campaign["id"]), {}))
        evidence["latest_totals"] = {
            key: sum(
                float(item.get("latest_period", {}).get(key) or 0)
                for item in snapshot["campaigns"]
            )
            for key in ("cost_micros", "conversions", "clicks", "impressions")
        }
        evidence["previous_totals"] = {
            key: sum(
                float(item.get("previous_period", {}).get(key) or 0)
                for item in snapshot["campaigns"]
            )
            for key in ("cost_micros", "conversions", "clicks", "impressions")
        }

        search_rows, search_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT search_term_view.search_term, search_term_view.status, "
            "campaign.id, campaign.name, metrics.cost_micros, "
            "metrics.conversions, metrics.all_conversions, metrics.clicks, "
            "metrics.impressions FROM search_term_view "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            "AND metrics.clicks > 0"
            f"{campaign_filter} ORDER BY metrics.cost_micros DESC LIMIT 200",
            gap="Search-term performance unavailable",
            gaps=gaps,
        )
        evidence["search_terms_verified"] = search_ok
        evidence["search_terms"] = [
            {
                "search_term": row.search_term_view.search_term,
                "status": _enum(row.search_term_view.status),
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "cost_micros": int(row.metrics.cost_micros),
                "conversions": float(row.metrics.conversions),
                "all_conversions": float(row.metrics.all_conversions),
                "clicks": int(row.metrics.clicks),
                "impressions": int(row.metrics.impressions),
            }
            for row in search_rows
        ]

        campaign_negative_rows, campaign_neg_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT campaign_criterion.keyword.text, campaign.id "
            "FROM campaign_criterion "
            "WHERE campaign_criterion.negative = TRUE "
            "AND campaign_criterion.type = 'KEYWORD'"
            f"{campaign_filter} LIMIT 10000",
            gap="Campaign negative keywords unavailable",
            gaps=gaps,
        )
        shared_negative_rows, shared_neg_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT shared_criterion.keyword.text, shared_set.name "
            "FROM shared_criterion "
            "WHERE shared_criterion.type = 'KEYWORD' LIMIT 10000",
            gap="Shared negative keywords unavailable",
            gaps=gaps,
        )
        evidence["negative_keywords_verified"] = campaign_neg_ok and shared_neg_ok
        evidence["negative_keyword_count"] = len(campaign_negative_rows) + len(
            shared_negative_rows
        )

        ad_rows, ads_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, "
            "ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
            "ad_group_ad.ad_strength, "
            "ad_group_ad.policy_summary.approval_status, "
            "metrics.cost_micros FROM ad_group_ad "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            "AND ad_group_ad.status != 'REMOVED'"
            f"{campaign_filter} LIMIT 5000",
            gap="Ad inventory and policy unavailable",
            gaps=gaps,
        )
        ad_group_costs = defaultdict(int)
        for row in ad_rows:
            key = (str(row.campaign.id), str(row.ad_group.id))
            ad_group_costs[key] += int(row.metrics.cost_micros)
        evidence["ads_verified"] = ads_ok
        evidence["ads"] = [
            {
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "ad_group_id": str(row.ad_group.id),
                "ad_group_name": row.ad_group.name,
                "ad_id": str(row.ad_group_ad.ad.id),
                "ad_type": _enum(row.ad_group_ad.ad.type),
                "status": _enum(row.ad_group_ad.status),
                "ad_strength": _enum(row.ad_group_ad.ad_strength),
                "approval_status": _enum(
                    row.ad_group_ad.policy_summary.approval_status
                ),
                "ad_group_cost_micros": ad_group_costs[
                    (str(row.campaign.id), str(row.ad_group.id))
                ],
            }
            for row in ad_rows
        ]

        keyword_rows, keywords_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT campaign.id, ad_group_criterion.criterion_id, "
            "ad_group_criterion.quality_info.quality_score, "
            "metrics.impressions FROM keyword_view "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            "AND ad_group_criterion.status = 'ENABLED' "
            "AND metrics.impressions > 0"
            f"{campaign_filter} ORDER BY metrics.impressions DESC LIMIT 1000",
            gap="Keyword Quality Score unavailable",
            gaps=gaps,
        )
        evidence["keywords_verified"] = keywords_ok
        evidence["keywords"] = [
            {
                "campaign_id": str(row.campaign.id),
                "criterion_id": str(row.ad_group_criterion.criterion_id),
                "quality_score": (
                    int(row.ad_group_criterion.quality_info.quality_score)
                    if int(row.ad_group_criterion.quality_info.quality_score) > 0
                    else None
                ),
                "impressions": int(row.metrics.impressions),
            }
            for row in keyword_rows
        ]

        landing_rows, landing_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT landing_page_view.unexpanded_final_url, "
            "metrics.clicks, metrics.cost_micros, metrics.conversions, "
            "metrics.all_conversions FROM landing_page_view "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            "AND metrics.clicks > 0 "
            "ORDER BY metrics.clicks DESC LIMIT 100",
            gap="Landing-page performance unavailable",
            gaps=gaps,
        )
        evidence["landing_pages_verified"] = landing_ok
        evidence["landing_pages"] = [
            {
                "url": row.landing_page_view.unexpanded_final_url,
                "clicks": int(row.metrics.clicks),
                "cost_micros": int(row.metrics.cost_micros),
                "conversions": float(row.metrics.conversions),
                "all_conversions": float(row.metrics.all_conversions),
            }
            for row in landing_rows
        ]

        change_start_text, change_end_text = _change_history_bounds(
            self._radar_now_fn()
        )
        change_rows, changes_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT change_event.change_date_time, change_event.changed_fields, "
            "campaign.name FROM change_event "
            f"WHERE change_event.change_date_time >= '{change_start_text}' "
            f"AND change_event.change_date_time <= '{change_end_text}' "
            "ORDER BY change_event.change_date_time DESC LIMIT 5000",
            gap="Thirty-day bid/target change history unavailable",
            gaps=gaps,
        )
        bid_counts = Counter()
        for row in change_rows:
            paths = " ".join(row.change_event.changed_fields.paths)
            if any(
                token in paths
                for token in ("bidding_strategy", "target_cpa", "target_roas")
            ):
                bid_counts[str(row.campaign.name or "Account-wide")] += 1
        evidence["change_history_verified"] = changes_ok
        evidence["bid_change_counts"] = dict(bid_counts)

        def segmented(
            *,
            name: str,
            segment: str,
            query_resource: str = "campaign",
            name_getter,
        ) -> None:
            rows, ok = self._optional_query(
                service,
                normalized_customer,
                f"SELECT {segment}, metrics.cost_micros, metrics.conversions, "
                f"metrics.clicks FROM {query_resource} "
                f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
                "AND metrics.clicks > 0"
                + (campaign_filter if query_resource == "campaign" else "")
                + " ORDER BY metrics.cost_micros DESC LIMIT 200",
                gap=f"{name.title()} performance unavailable",
                gaps=gaps,
            )
            evidence[f"{name}_verified"] = ok
            evidence[name] = [
                {
                    "name": name_getter(row),
                    "cost_micros": int(row.metrics.cost_micros),
                    "conversions": float(row.metrics.conversions),
                    "clicks": int(row.metrics.clicks),
                }
                for row in rows
            ]

        segmented(
            name="devices",
            segment="segments.device",
            name_getter=lambda row: _enum(row.segments.device),
        )
        segmented(
            name="schedules",
            segment="segments.day_of_week",
            name_getter=lambda row: _enum(row.segments.day_of_week),
        )
        geo_rows, geos_ok = self._optional_query(
            service,
            normalized_customer,
            "SELECT geographic_view.country_criterion_id, "
            "metrics.cost_micros, metrics.conversions, metrics.clicks "
            "FROM geographic_view "
            f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
            "AND metrics.clicks > 0 ORDER BY metrics.cost_micros DESC LIMIT 30",
            gap="Location performance unavailable",
            gaps=gaps,
        )
        evidence["geos_verified"] = geos_ok
        evidence["geos"] = [
            {
                "name": str(row.geographic_view.country_criterion_id),
                "cost_micros": int(row.metrics.cost_micros),
                "conversions": float(row.metrics.conversions),
                "clicks": int(row.metrics.clicks),
            }
            for row in geo_rows
        ]

        if business_mode == "ecommerce":
            product_rows, products_ok = self._optional_query(
                service,
                normalized_customer,
                "SELECT segments.product_item_id, segments.product_title, "
                "metrics.cost_micros, metrics.conversions, metrics.conversions_value, "
                "metrics.clicks FROM shopping_performance_view "
                f"WHERE segments.date BETWEEN '{start}' AND '{end}' "
                "AND metrics.clicks > 0 ORDER BY metrics.cost_micros DESC LIMIT 100",
                gap="Product-level Shopping performance unavailable",
                gaps=gaps,
            )
            evidence["products_verified"] = products_ok
            evidence["products"] = [
                {
                    "id": str(row.segments.product_item_id),
                    "name": str(row.segments.product_title),
                    "cost_micros": int(row.metrics.cost_micros),
                    "conversions": float(row.metrics.conversions),
                    "conversions_value": float(row.metrics.conversions_value),
                    "clicks": int(row.metrics.clicks),
                }
                for row in product_rows
            ]

        snapshot["evidence"] = evidence
        snapshot["core_coverage_gaps"] = gaps
        snapshot["coverage_gaps"] = list(dict.fromkeys(limitations + gaps))
        return snapshot


class InstantAccountAuditRunService:
    def __init__(
        self,
        *,
        gateway: GoogleAdsInstantAuditGateway | None = None,
        changesets: ChangesetService | None = None,
    ) -> None:
        self._gateway = gateway or GoogleAdsInstantAuditGateway()
        self._changesets = changesets or get_changeset_service()

    async def run(
        self,
        *,
        customer_id: str,
        business_mode: str,
        target_cpa: float | None = None,
        target_roas: float | None = None,
        outcome_quality_confirmed: bool = False,
        brand_terms: Sequence[str] | None = None,
        available_module_ids: Sequence[str] | None = None,
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
        result = evaluate_instant_account_audit(
            snapshot,
            business_mode=business_mode,
            target_cpa_micros=_micros(target_cpa, "target_cpa"),
            target_roas=target_roas,
            outcome_quality_confirmed=outcome_quality_confirmed,
            brand_terms=brand_terms,
            available_module_ids=available_module_ids,
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
                "analysis_label": "Instant Account Audit",
                "campaign_scope": "selected" if campaign_ids else "all_eligible",
                "campaigns": [
                    {"id": campaign["id"], "name": campaign["name"]}
                    for campaign in snapshot["campaigns"]
                ],
                "skills": ["instant-account-audit"],
                "run_mode": "interactive",
                "coverage_gaps": result["coverage_gaps"],
                "data_quality_holds": result["holds"],
                "actions": result["recommendations"],
                "recovery_actions": result["recovery_actions"],
            }
        )
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
