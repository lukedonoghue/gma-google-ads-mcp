"""Curated, source-labelled industry references for GMA goal reports."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CATALOG_VERSION = "gma-industry-benchmarks/1.0"
CATALOG_ACCESSED_ON = "2026-07-23"

SOURCES = {
    "wordstream_search_2026": {
        "title": "Google Ads Benchmarks 2026: Competitive Data & Insights",
        "publisher": "WordStream / LocaliQ",
        "url": "https://www.wordstream.com/blog/2026-google-ads-benchmarks",
        "source_type": "cross-account_dataset",
        "data_period": "April 2025 to March 2026",
        "sample": "More than 13,000 US search advertising campaigns",
        "confidence": "high",
        "limitations": (
            "Google and Microsoft search campaigns are combined; cost per lead "
            "does not prove lead quality, booking rate, or profitability."
        ),
    },
    "triple_whale_google_roas_2025": {
        "title": "Turbocharging Sales and Growth with PPC",
        "publisher": "BigCommerce, citing Triple Whale 2025 ecommerce data",
        "url": (
            "https://assets.ctfassets.net/wowgx05xsdrr/"
            "2gnl2szLNJNHWKJmr5hOBu/"
            "a87e4b3cc237cccab5b44a15065198e0/BigCommerce-en-AU-ppc.pdf"
        ),
        "source_type": "cross-account_dataset_summary",
        "data_period": "2025",
        "sample": "Thousands of ecommerce brands",
        "confidence": "medium",
        "limitations": (
            "A cross-category Google Ads median; it does not account for gross "
            "margin, returns, attribution settings, or new-customer mix."
        ),
    },
    "groas_ecommerce_2026": {
        "title": "Google Ads ROAS Benchmarks by Industry",
        "publisher": "Groas",
        "url": "https://www.groas.com/post/google-ads-roas-benchmarks-by-industry",
        "source_type": "vendor_guidance",
        "data_period": "2026",
        "sample": "Published industry guidance; underlying sample not disclosed",
        "confidence": "low",
        "limitations": (
            "Directional vendor ranges, not an independently audited dataset. "
            "Use only as a sanity check behind contribution-margin economics."
        ),
    },
}

LEAD_CPL = {
    "animals_pets": ("Animals & Pets", 31.50),
    "apparel_fashion_jewelry": ("Apparel / Fashion & Jewelry", 97.51),
    "arts_entertainment": ("Arts & Entertainment", 26.84),
    "attorneys_legal": ("Attorneys & Legal Services", 131.63),
    "automotive_sales": ("Automotive — For Sale", 44.26),
    "automotive_service": ("Automotive — Repair, Service & Parts", 29.96),
    "beauty_personal_care": ("Beauty & Personal Care", 39.25),
    "business_services": ("Business Services", 93.69),
    "career_employment": ("Career & Employment", 67.36),
    "dentists_dental": ("Dentists & Dental Services", 72.97),
    "education_instruction": ("Education & Instruction", 77.48),
    "finance_insurance": ("Finance & Insurance", 74.44),
    "furniture": ("Furniture", 106.70),
    "health_fitness": ("Health & Fitness", 67.36),
    "home_improvement": ("Home & Home Improvement", 90.92),
    "industrial_commercial": ("Industrial & Commercial", 75.19),
    "personal_services": ("Personal Services", 54.60),
    "physicians_surgeons": ("Physicians & Surgeons", 40.04),
    "real_estate": ("Real Estate", 102.51),
    "restaurants_food": ("Restaurants & Food", 30.57),
    "shopping_collectibles_gifts": ("Shopping, Collectibles & Gifts", 49.40),
    "sports_recreation": ("Sports & Recreation", 44.26),
    "travel": ("Travel", 44.70),
}

LEAD_COMPOSITES = {
    "spa_wellness": {
        "display_name": "Spa, massage, wellness & personal care",
        "categories": [
            "beauty_personal_care",
            "personal_services",
            "health_fitness",
        ],
        "rationale": (
            "Uses the closest broad search-ad categories because no audited "
            "massage-spa-only Google Ads dataset is available."
        ),
    }
}

ECOMMERCE_PROFILES = {
    "ecommerce_general": {
        "display_name": "Ecommerce — all categories",
        "observations": [
            {
                "label": "Cross-category median",
                "low": 3.68,
                "high": 3.68,
                "source_id": "triple_whale_google_roas_2025",
            }
        ],
        "rationale": "Use only until a product-category and margin profile is confirmed.",
    },
    "ecommerce_fashion_apparel": {
        "display_name": "Ecommerce — fashion & apparel",
        "observations": [
            {
                "label": "Published directional range",
                "low": 3.0,
                "high": 6.0,
                "source_id": "groas_ecommerce_2026",
            }
        ],
        "rationale": "Returns and discounting can materially reduce effective ROAS.",
    },
    "ecommerce_home_goods": {
        "display_name": "Ecommerce — home goods & furniture",
        "observations": [
            {
                "label": "Published directional range",
                "low": 4.0,
                "high": 8.0,
                "source_id": "groas_ecommerce_2026",
            }
        ],
        "rationale": "Higher order values can support higher reported ROAS.",
    },
    "ecommerce_electronics": {
        "display_name": "Ecommerce — electronics",
        "observations": [
            {
                "label": "Published directional range",
                "low": 3.0,
                "high": 5.0,
                "source_id": "groas_ecommerce_2026",
            }
        ],
        "rationale": "Thin margins make contribution-margin ROAS more important.",
    },
    "ecommerce_beauty_personal_care": {
        "display_name": "Ecommerce — beauty & personal care",
        "observations": [
            {
                "label": "Published directional range",
                "low": 4.0,
                "high": 7.0,
                "source_id": "groas_ecommerce_2026",
            }
        ],
        "rationale": "Repeat purchase can make first-order ROAS understate lifetime value.",
    },
    "ecommerce_food_beverage": {
        "display_name": "Ecommerce — food & beverage",
        "observations": [
            {
                "label": "Published directional range",
                "low": 2.0,
                "high": 5.0,
                "source_id": "groas_ecommerce_2026",
            }
        ],
        "rationale": "Subscription and repeat purchase strongly affect acceptable ROAS.",
    },
}


def _lead_profile(profile_id: str) -> dict[str, Any] | None:
    if profile_id in LEAD_CPL:
        label, value = LEAD_CPL[profile_id]
        categories = [profile_id]
        display_name = label
        rationale = "Closest available 2026 search-ad industry category."
    else:
        composite = LEAD_COMPOSITES.get(profile_id)
        if not composite:
            return None
        categories = composite["categories"]
        display_name = composite["display_name"]
        rationale = composite["rationale"]
    observations = [
        {
            "label": LEAD_CPL[category][0],
            "low": LEAD_CPL[category][1],
            "high": LEAD_CPL[category][1],
            "source_id": "wordstream_search_2026",
        }
        for category in categories
    ]
    return {
        "id": profile_id,
        "display_name": display_name,
        "business_mode": "lead_gen",
        "metric": "cost_per_lead",
        "unit": "account_currency",
        "observations": observations,
        "rationale": rationale,
    }


def get_benchmark_profile(
    profile_id: str,
    business_mode: str,
) -> dict[str, Any] | None:
    """Return one immutable, source-labelled benchmark profile."""

    if business_mode == "lead_gen":
        profile = _lead_profile(profile_id)
    elif business_mode == "ecommerce":
        raw = ECOMMERCE_PROFILES.get(profile_id)
        profile = (
            {
                "id": profile_id,
                "display_name": raw["display_name"],
                "business_mode": "ecommerce",
                "metric": "return_on_ad_spend",
                "unit": "ratio",
                "observations": deepcopy(raw["observations"]),
                "rationale": raw["rationale"],
            }
            if raw
            else None
        )
    else:
        return None
    if not profile:
        return None
    source_ids = sorted({item["source_id"] for item in profile["observations"]})
    profile["sources"] = [
        {"id": source_id, **deepcopy(SOURCES[source_id])}
        for source_id in source_ids
    ]
    profile["catalog_version"] = CATALOG_VERSION
    profile["accessed_on"] = CATALOG_ACCESSED_ON
    return profile


def list_benchmark_profiles(business_mode: str) -> dict[str, Any]:
    """List selectable categories without exposing unsupported inference."""

    if business_mode == "lead_gen":
        ids = [*LEAD_COMPOSITES, *LEAD_CPL]
    elif business_mode == "ecommerce":
        ids = list(ECOMMERCE_PROFILES)
    else:
        raise ValueError("business_mode must be lead_gen or ecommerce")
    profiles = [
        get_benchmark_profile(profile_id, business_mode) for profile_id in ids
    ]
    return {
        "contract_version": "gma-goal-benchmark-catalog/1.0",
        "business_mode": business_mode,
        "catalog_version": CATALOG_VERSION,
        "accessed_on": CATALOG_ACCESSED_ON,
        "profiles": [
            {
                "id": profile["id"],
                "display_name": profile["display_name"],
                "metric": profile["metric"],
                "unit": profile["unit"],
                "rationale": profile["rationale"],
            }
            for profile in profiles
            if profile
        ],
    }
