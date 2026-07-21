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

"""Test cases for the utils module."""

import unittest
from unittest.mock import patch
from google.ads.googleads.v24.enums.types.campaign_status import (
    CampaignStatusEnum,
)
from google.ads.googleads.v24.common.types.metrics import Metrics
from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import utils


class TestUtils(unittest.TestCase):
    """Test cases for the utils module."""

    def test_format_output_value(self):
        """Tests that output values are formatted correctly."""

        self.assertEqual(
            utils.format_output_value(CampaignStatusEnum.CampaignStatus.ENABLED),
            "ENABLED",
        )

    def test_format_output_value_primitive(self):
        """Tests that primitive values are returned as is."""
        self.assertEqual(utils.format_output_value(123), 123)
        self.assertEqual(utils.format_output_value("abc"), "abc")

    def test_format_output_value_message(self):
        """Tests that proto messages are converted to dict."""
        metrics = Metrics(clicks=10, impressions=100)
        formatted = utils.format_output_value(metrics)
        self.assertIsInstance(formatted, dict)
        self.assertEqual(formatted.get("clicks"), "10")
        self.assertEqual(formatted.get("impressions"), "100")

    def test_format_output_value_repeated_primitive(self):
        """Tests that repeated primitive values are formatted."""
        self.assertEqual(
            utils.format_output_value([1, 2, 3]),
            [1, 2, 3],
        )

    def test_format_output_value_repeated_message(self):
        """Tests that repeated proto messages are formatted."""
        metrics1 = Metrics(clicks=10)
        metrics2 = Metrics(clicks=20)
        formatted = utils.format_output_value([metrics1, metrics2])
        self.assertIsInstance(formatted, list)
        self.assertEqual(len(formatted), 2)
        self.assertEqual(formatted[0].get("clicks"), "10")
        self.assertEqual(formatted[1].get("clicks"), "20")

    def test_format_output_value_bare_protobuf(self):
        """Tests that bare protobuf messages are formatted correctly."""
        fm = FieldMask(paths=["foo", "bar"])
        formatted = utils.format_output_value(fm)
        self.assertEqual(formatted, "foo,bar")

    def test_prevent_stdio_inheritance(self):
        """Tests that prevent_stdio_inheritance sets stdin to DEVNULL if not specified."""
        import subprocess
        from unittest.mock import MagicMock, patch
        from ads_mcp.utils import prevent_stdio_inheritance

        mock_popen = MagicMock()
        with patch("subprocess.Popen", mock_popen):
            with prevent_stdio_inheritance():
                subprocess.Popen(["mock_cmd"])

        mock_popen.assert_called_once_with(["mock_cmd"], stdin=subprocess.DEVNULL)

    def test_prevent_stdio_inheritance_explicit_stdin(self):
        """Tests that prevent_stdio_inheritance preserves explicit stdin."""
        import subprocess
        from unittest.mock import MagicMock, patch
        from ads_mcp.utils import prevent_stdio_inheritance

        mock_popen = MagicMock()
        with patch("subprocess.Popen", mock_popen):
            with prevent_stdio_inheritance():
                subprocess.Popen(["mock_cmd"], stdin=subprocess.PIPE)

        mock_popen.assert_called_once_with(["mock_cmd"], stdin=subprocess.PIPE)

    def test_explicit_login_customer_id_is_normalized(self):
        """A per-request MCC ID overrides the optional global fallback."""
        from unittest.mock import MagicMock, patch

        credentials = MagicMock()
        with patch("ads_mcp.utils._create_credentials", return_value=credentials):
            with patch("ads_mcp.utils._get_developer_token", return_value="token"):
                with patch("ads_mcp.utils.GoogleAdsClient") as client_class:
                    utils._get_googleads_client(login_customer_id="987-654-3210")

        client_class.assert_called_once_with(
            credentials=credentials,
            developer_token="token",
            use_proto_plus=True,
            login_customer_id="9876543210",
        )

    def test_enforced_login_customer_id_is_used(self):
        """The hosted MCC boundary supplies the manager context."""
        with patch.dict(
            "os.environ",
            {"GMA_MCP_ENFORCED_LOGIN_CUSTOMER_ID": "207-327-4070"},
        ):
            self.assertEqual(
                utils.resolve_login_customer_id(),
                "2073274070",
            )
            self.assertEqual(
                utils.resolve_login_customer_id("2073274070"),
                "2073274070",
            )

    def test_enforced_login_customer_id_rejects_other_manager(self):
        """A caller cannot escape to the parent or a sibling MCC."""
        with patch.dict(
            "os.environ",
            {"GMA_MCP_ENFORCED_LOGIN_CUSTOMER_ID": "2073274070"},
        ):
            with self.assertRaisesRegex(
                ValueError,
                "restricted by this hosted connector",
            ):
                utils.resolve_login_customer_id("5294823448")
