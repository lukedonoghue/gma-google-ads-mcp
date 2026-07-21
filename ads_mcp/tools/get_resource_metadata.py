# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools for fetching metadata for Google Ads resources."""

import re
from typing import Any, Dict
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils

metadata_mcp = FastMCP("metadata", mask_error_details=True)


@metadata_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_resource_metadata(resource_name: str) -> Dict[str, Any]:
    """Retrieves the selectable, filterable, and sortable fields for a specific Google Ads resource,
    including compatible metrics and segments.

    Use this tool to find out which fields you can select, filter by, or sort by
    when querying a specific resource (e.g., 'campaign', 'ad_group').
    This tool also returns metrics and segments that can be selected with the resource.
    Their names start with 'metrics.' and 'segments.' respectively.

    Do not guess fields, you MUST use this tool to discover them before constructing a query for the
    `search` tool.

    The responses of this tool should be cached, as they don't change frequently.

    Args:
        resource_name: The name of the Google Ads resource (e.g., 'campaign', 'ad_group').
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
        raise ValueError("resource_name must be a valid Google Ads resource name")

    ga_service = utils.get_googleads_service("GoogleAdsFieldService")
    request = utils.get_googleads_type("SearchGoogleAdsFieldsRequest")

    selectable = set()
    filterable = set()
    sortable = set()

    def add_fields(fields) -> None:
        for field in fields:
            if field.selectable:
                selectable.add(field.name)
            if field.filterable:
                filterable.add(field.name)
            if field.sortable:
                sortable.add(field.name)

    # First discover attributed resources. Their fields can be selected from
    # the requested FROM resource without segmenting metrics, so omitting them
    # would make the catalog materially incomplete.
    request.query = (
        "SELECT name, attribute_resources " f"WHERE name = '{resource_name}'"
    )
    try:
        resource_response = ga_service.search_google_ads_fields(request=request)
        resource = next(iter(resource_response), None)
    except Exception as error:
        raise RuntimeError(
            f"API call to search_google_ads_fields failed: {error}"
        ) from error

    if resource is None:
        raise ValueError(f"Unknown Google Ads resource: {resource_name}")

    attribute_resources = sorted(set(resource.attribute_resources))
    for attribute_resource in [resource_name, *attribute_resources]:
        request.query = (
            "SELECT name, selectable, filterable, sortable "
            f"WHERE name LIKE '{attribute_resource}.%' "
            "AND category = 'ATTRIBUTE'"
        )
        try:
            attributes_response = ga_service.search_google_ads_fields(request=request)
            add_fields(attributes_response)
        except Exception as error:
            utils.logger.warning("Failed attributes query: %s", error)
            request.query = (
                "SELECT name, selectable, filterable, sortable "
                f"WHERE name LIKE '{attribute_resource}.%'"
            )
            try:
                attributes_response = ga_service.search_google_ads_fields(
                    request=request
                )
                add_fields(
                    field
                    for field in attributes_response
                    if field.name.startswith(f"{attribute_resource}.")
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    "API call to search_google_ads_fields failed: " f"{fallback_error}"
                ) from fallback_error

    # Query 2: Get selectable metrics and segments
    metrics_segments_query = f"SELECT name, selectable, filterable, sortable WHERE selectable_with CONTAINS ANY('{resource_name}')"
    request.query = metrics_segments_query
    try:
        metrics_segments_response = ga_service.search_google_ads_fields(request=request)
        add_fields(metrics_segments_response)
    except Exception as error:
        utils.logger.warning("Failed metrics/segments query: %s", error)

    return {
        "resource": resource_name,
        "attribute_resources": attribute_resources,
        "selectable": sorted(list(selectable)),
        "filterable": sorted(list(filterable)),
        "sortable": sorted(list(sortable)),
    }
