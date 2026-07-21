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

"""Test cases for the get_resource_metadata tool."""

import unittest
from unittest.mock import MagicMock, patch

from ads_mcp.tools import get_resource_metadata


class TestGetResourceMetadata(unittest.TestCase):
    """Test cases for the get_resource_metadata tool."""

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.get_googleads_type")
    def test_get_resource_metadata_success(self, mock_get_type, mock_get_service):
        """Metadata includes primary, attributed, metric, and segment fields."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        mock_request = MagicMock()
        mock_get_type.return_value = mock_request

        resource = MagicMock()
        resource.name = "campaign"
        resource.attribute_resources = ["customer", "campaign_budget"]

        # Mocking fields for primary and attributed resources
        f1 = MagicMock(selectable=True, filterable=True, sortable=True)
        f1.name = "campaign.id"
        f2 = MagicMock(selectable=True, filterable=False, sortable=True)
        f2.name = "campaign.name"
        f3 = MagicMock(selectable=True, filterable=True, sortable=True)
        f3.name = "customer.id"
        f4 = MagicMock(selectable=True, filterable=True, sortable=True)
        f4.name = "campaign_budget.id"

        # Mocking fields for metrics/segments
        f5 = MagicMock(selectable=True, filterable=True, sortable=False)
        f5.name = "metrics.clicks"
        f6 = MagicMock(selectable=True, filterable=True, sortable=False)
        f6.name = "segments.date"

        mock_service.search_google_ads_fields.side_effect = [
            [resource],
            [f1, f2],
            [f3],
            [f4],
            [f5, f6],
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        # Verify results
        self.assertEqual(result["resource"], "campaign")
        self.assertIn("campaign.id", result["selectable"])
        self.assertIn("campaign.name", result["selectable"])
        self.assertIn("customer.id", result["selectable"])
        self.assertIn("campaign_budget.id", result["selectable"])
        self.assertIn("metrics.clicks", result["selectable"])
        self.assertIn("segments.date", result["selectable"])
        self.assertEqual(
            result["attribute_resources"],
            ["campaign_budget", "customer"],
        )

        self.assertIn("campaign.id", result["filterable"])
        self.assertNotIn("campaign.name", result["filterable"])
        self.assertIn("metrics.clicks", result["filterable"])
        self.assertIn("segments.date", result["filterable"])

        self.assertIn("campaign.id", result["sortable"])
        self.assertIn("campaign.name", result["sortable"])
        self.assertNotIn("metrics.clicks", result["sortable"])

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.get_googleads_type")
    def test_get_resource_metadata_attributes_fallback(
        self, mock_get_type, mock_get_service
    ):
        """Tests fallback for attributes query when the first one fails."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        mock_request = MagicMock()
        mock_get_type.return_value = mock_request

        resource = MagicMock()
        resource.name = "campaign"
        resource.attribute_resources = []

        f1 = MagicMock(selectable=True, filterable=True, sortable=True)
        f1.name = "campaign.id"

        f2 = MagicMock(selectable=True, filterable=True, sortable=False)
        f2.name = "metrics.clicks"

        # Resource lookup succeeds, attribute category query falls back, then
        # compatible metrics/segments are returned.
        mock_service.search_google_ads_fields.side_effect = [
            [resource],
            Exception("API Error"),
            [f1],
            [f2],
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        self.assertIn("campaign.id", result["selectable"])
        self.assertIn("metrics.clicks", result["selectable"])

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.get_googleads_type")
    def test_get_resource_metadata_metrics_failure_still_returns_attributes(
        self, mock_get_type, mock_get_service
    ):
        """Tests that if metrics query fails, we still return attributes."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        mock_request = MagicMock()
        mock_get_type.return_value = mock_request

        resource = MagicMock()
        resource.name = "campaign"
        resource.attribute_resources = []

        f1 = MagicMock(selectable=True, filterable=True, sortable=True)
        f1.name = "campaign.id"

        # 1st succeeds, 2nd fails
        mock_service.search_google_ads_fields.side_effect = [
            [resource],
            [f1],
            Exception("Metrics Fail"),
        ]

        result = get_resource_metadata.get_resource_metadata("campaign")

        self.assertIn("campaign.id", result["selectable"])

    @patch("ads_mcp.utils.get_googleads_service")
    @patch("ads_mcp.utils.get_googleads_type")
    def test_get_resource_metadata_api_failure(self, mock_get_type, mock_get_service):
        """Tests that a RuntimeError is raised if both queries fail."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service

        mock_request = MagicMock()
        mock_get_type.return_value = mock_request

        mock_service.search_google_ads_fields.side_effect = Exception("Fail 1")

        with self.assertRaises(RuntimeError) as cm:
            get_resource_metadata.get_resource_metadata("campaign")

        self.assertIn(
            "API call to search_google_ads_fields failed: Fail 1",
            str(cm.exception),
        )

    def test_get_resource_metadata_rejects_invalid_resource_name(self):
        """Resource names cannot inject additional field-service conditions."""
        with self.assertRaisesRegex(ValueError, "valid Google Ads resource name"):
            get_resource_metadata.get_resource_metadata("campaign' OR selectable")


if __name__ == "__main__":
    unittest.main()
