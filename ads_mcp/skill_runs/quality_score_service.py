"""Fixed-query Google Ads adapter for GMA Skill 6."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from ads_mcp import utils
from ads_mcp.changesets import ChangesetService, get_changeset_service
from ads_mcp.skill_runs.budget_service import GoogleAdsBudgetGateway
from ads_mcp.skill_runs.common import resolve_analysis_window
from ads_mcp.skill_runs.quality_score_booster import (
    QualityScoreAnalysisError,
    evaluate_quality_score,
)


def _enum(value: Any) -> str:
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value).rsplit(".", 1)[-1]


def _campaign_filter(campaign_ids: Sequence[str] | None) -> str:
    if not campaign_ids:
        return ""
    normalized = []
    for raw in campaign_ids:
        value = str(raw).strip()
        if not re.fullmatch(r"\d{1,20}", value):
            raise QualityScoreAnalysisError("Campaign IDs must be numeric")
        normalized.append(value)
    return " AND campaign.id IN (" + ",".join(sorted(set(normalized))) + ")"


def _asset_texts(values: Any) -> list[str]:
    return [
        str(getattr(value, "text", "") or "")
        for value in values or []
        if str(getattr(value, "text", "") or "").strip()
    ]


class GoogleAdsQualityScoreGateway(GoogleAdsBudgetGateway):
    """Build one bounded keyword Quality Score snapshot."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        super().__init__(now_fn=now_fn)
        self._quality_now_fn = now_fn or (
            lambda: datetime.now(timezone.utc)
        )

    def _keyword_rows(
        self,
        service: Any,
        customer_id: str,
        *,
        start_date: str,
        end_date: str,
        campaign_ids: Sequence[str],
        window: str,
        gaps: list[str],
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, ad_group.id, ad_group.name, "
            "ad_group.status, ad_group_criterion.criterion_id, "
            "ad_group_criterion.status, ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, "
            "ad_group_criterion.quality_info.quality_score, "
            "ad_group_criterion.quality_info.creative_quality_score, "
            "ad_group_criterion.quality_info.search_predicted_ctr, "
            "ad_group_criterion.quality_info.post_click_quality_score, "
            "metrics.impressions, metrics.clicks, metrics.conversions, "
            "metrics.all_conversions, metrics.cost_micros, metrics.average_cpc "
            "FROM keyword_view "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.advertising_channel_type = 'SEARCH' "
            "AND campaign.status = 'ENABLED' "
            "AND ad_group.status = 'ENABLED' "
            "AND ad_group_criterion.status = 'ENABLED' "
            "AND metrics.impressions > 0"
            f"{_campaign_filter(campaign_ids)} "
            "ORDER BY metrics.impressions DESC LIMIT 100"
        )
        try:
            rows = self._search(service, customer_id, query)
        except Exception:
            gaps.append(
                f"Search keyword Quality Score rows were unavailable for the {window.replace('_', ' ')} window"
            )
            return []
        if len(rows) == 100:
            gaps.append(
                f"Search keyword Quality Score reached the 100-row decision cap for the {window.replace('_', ' ')} window"
            )
        result = []
        for row in rows:
            quality = row.ad_group_criterion.quality_info
            impressions = int(row.metrics.impressions)
            clicks = int(row.metrics.clicks)
            result.append(
                {
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "channel_type": _enum(
                        row.campaign.advertising_channel_type
                    ),
                    "ad_group_id": str(row.ad_group.id),
                    "ad_group_name": row.ad_group.name,
                    "criterion_id": str(
                        row.ad_group_criterion.criterion_id
                    ),
                    "keyword_text": row.ad_group_criterion.keyword.text,
                    "match_type": _enum(
                        row.ad_group_criterion.keyword.match_type
                    ),
                    "quality_score": (
                        int(quality.quality_score)
                        if int(quality.quality_score or 0) > 0
                        else None
                    ),
                    "ad_relevance": _enum(quality.creative_quality_score),
                    "expected_ctr": _enum(quality.search_predicted_ctr),
                    "landing_page_experience": _enum(
                        quality.post_click_quality_score
                    ),
                    "impressions": impressions,
                    "clicks": clicks,
                    "ctr": clicks / impressions if impressions else 0.0,
                    "conversions": float(row.metrics.conversions),
                    "all_conversions": float(row.metrics.all_conversions),
                    "cost_micros": int(row.metrics.cost_micros),
                    "average_cpc_micros": round(
                        float(row.metrics.average_cpc or 0)
                    ),
                    "window": window,
                }
            )
        return result

    def _rsa_context(
        self,
        service: Any,
        customer_id: str,
        *,
        campaign_ids: Sequence[str],
        gaps: list[str],
    ) -> dict[str, dict[str, Any]]:
        query = (
            "SELECT campaign.id, ad_group.id, ad_group.name, "
            "ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
            "ad_group_ad.ad_strength, "
            "ad_group_ad.ad.responsive_search_ad.headlines, "
            "ad_group_ad.ad.responsive_search_ad.descriptions, "
            "ad_group_ad.ad.final_urls FROM ad_group_ad "
            "WHERE campaign.advertising_channel_type = 'SEARCH' "
            "AND campaign.status = 'ENABLED' "
            "AND ad_group.status = 'ENABLED' "
            "AND ad_group_ad.status = 'ENABLED' "
            "AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'"
            f"{_campaign_filter(campaign_ids)} LIMIT 500"
        )
        try:
            rows = self._search(service, customer_id, query)
        except Exception:
            gaps.append(
                "Responsive Search Ad evidence was unavailable; ad-related findings route to Ad-Copy Analyzer for recovery"
            )
            return {}
        if len(rows) == 500:
            gaps.append(
                "Responsive Search Ad evidence reached the 500-row context cap"
            )
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            ad_group_id = str(row.ad_group.id)
            context = result.setdefault(
                ad_group_id,
                {
                    "rsa_count": 0,
                    "headlines": [],
                    "descriptions": [],
                    "final_urls": [],
                    "ad_strengths": [],
                },
            )
            rsa = row.ad_group_ad.ad.responsive_search_ad
            context["rsa_count"] += 1
            context["headlines"].extend(_asset_texts(rsa.headlines))
            context["descriptions"].extend(_asset_texts(rsa.descriptions))
            context["final_urls"].extend(
                str(value) for value in row.ad_group_ad.ad.final_urls or []
            )
            context["ad_strengths"].append(
                _enum(row.ad_group_ad.ad_strength)
            )
        return result

    @staticmethod
    def _attach_rsa_context(
        keywords: list[dict[str, Any]],
        context: Mapping[str, Mapping[str, Any]],
    ) -> None:
        for keyword in keywords:
            ad_group = context.get(str(keyword["ad_group_id"])) or {}
            headlines = " ".join(ad_group.get("headlines") or []).lower()
            tokens = [
                token
                for token in re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    str(keyword["keyword_text"]).lower(),
                ).split()
                if len(token) > 2
            ]
            keyword["rsa_count"] = int(ad_group.get("rsa_count") or 0)
            keyword["keyword_theme_in_headline"] = (
                any(token in headlines for token in tokens)
                if headlines and tokens
                else None
            )

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
        now = self._quality_now_fn()

        account_rows = self._search(
            service,
            normalized_customer,
            "SELECT customer.id, customer.descriptive_name, "
            "customer.currency_code, customer.time_zone FROM customer LIMIT 1",
        )
        if len(account_rows) != 1:
            raise QualityScoreAnalysisError(
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
            raise QualityScoreAnalysisError(str(error)) from error

        campaign_query = (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, metrics.cost_micros, "
            "metrics.conversions, metrics.all_conversions, metrics.clicks, "
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
            raise QualityScoreAnalysisError(
                "No campaigns were found in the selected window"
            )
        campaigns = [
            {
                "id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": _enum(row.campaign.status),
                "channel_type": _enum(
                    row.campaign.advertising_channel_type
                ),
                "cost_micros": int(row.metrics.cost_micros),
                "conversions": float(row.metrics.conversions),
                "all_conversions": float(row.metrics.all_conversions),
                "clicks": int(row.metrics.clicks),
                "impressions": int(row.metrics.impressions),
            }
            for row in campaign_rows
        ]
        resolved_campaign_ids = [item["id"] for item in campaigns]
        gaps: list[str] = []
        keywords = self._keyword_rows(
            service,
            normalized_customer,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            campaign_ids=resolved_campaign_ids,
            window="selected",
            gaps=gaps,
        )
        secondary_keywords: list[dict[str, Any]] = []
        analysis_days = (end_date - start_date).days + 1
        if (
            analysis_days < 90
            and sum(
                int(item.get("impressions") or 0) >= 100
                for item in keywords
            )
            < 20
        ):
            secondary_keywords = self._keyword_rows(
                service,
                normalized_customer,
                start_date=(end_date - timedelta(days=89)).isoformat(),
                end_date=end_date.isoformat(),
                campaign_ids=resolved_campaign_ids,
                window="secondary_90_day",
                gaps=gaps,
            )
        rsa_context = self._rsa_context(
            service,
            normalized_customer,
            campaign_ids=resolved_campaign_ids,
            gaps=gaps,
        )
        self._attach_rsa_context(keywords, rsa_context)
        self._attach_rsa_context(secondary_keywords, rsa_context)
        goal_context = self._goal_context(
            service,
            normalized_customer,
            [
                item["id"]
                for item in campaigns
                if item["channel_type"] == "SEARCH"
                and item["status"] == "ENABLED"
            ],
            gaps,
        )
        active_search = [
            item
            for item in campaigns
            if item["channel_type"] == "SEARCH"
            and item["status"] == "ENABLED"
        ]
        goal_scope_verified = bool(active_search) and all(
            bool((goal_context.get(item["id"]) or {}).get("verified"))
            for item in active_search
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
            "keywords": keywords,
            "secondary_keywords": secondary_keywords,
            "goal_scope_verified": goal_scope_verified,
            "effective_conversion_actions": {
                campaign_id: list(context.get("actions") or [])
                for campaign_id, context in goal_context.items()
            },
            "coverage_gaps": list(dict.fromkeys(gaps)),
        }


class QualityScoreBoosterRunService:
    def __init__(
        self,
        *,
        gateway: GoogleAdsQualityScoreGateway | None = None,
        changesets: ChangesetService | None = None,
    ) -> None:
        self._gateway = gateway or GoogleAdsQualityScoreGateway()
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
        outcome_quality_confirmed: bool = False,
    ) -> dict[str, Any]:
        snapshot = self._gateway.fetch_snapshot(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            campaign_ids=campaign_ids,
        )
        result = evaluate_quality_score(
            snapshot,
            business_mode=business_mode,
            brand_terms=brand_terms,
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
                "skills": ["quality-score-booster"],
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
