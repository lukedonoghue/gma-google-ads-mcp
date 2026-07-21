"""Tests for hosted customer discovery boundaries."""

import unittest
from unittest.mock import patch

from ads_mcp.tools import core


class TestCoreTools(unittest.TestCase):
    @patch("ads_mcp.utils.get_googleads_service")
    def test_enforced_manager_is_only_discovered_customer(self, get_service):
        """Customer discovery must not expose the parent MCC."""
        with patch.dict(
            "os.environ",
            {"GMA_MCP_ENFORCED_LOGIN_CUSTOMER_ID": "207-327-4070"},
        ):
            customers = core.list_accessible_customers()

        self.assertEqual(customers, ["2073274070"])
        get_service.assert_not_called()

    @patch("ads_mcp.utils.get_googleads_service")
    def test_access_root_is_discovered_instead_of_top_level_login(self, get_service):
        """Discovery exposes the PPC Navigator root, not its API login parent."""
        with patch.dict(
            "os.environ",
            {
                "GMA_MCP_ENFORCED_LOGIN_CUSTOMER_ID": "5294823448",
                "GMA_MCP_ACCESS_ROOT_CUSTOMER_ID": "2073274070",
            },
            clear=True,
        ):
            customers = core.list_accessible_customers()

        self.assertEqual(customers, ["2073274070"])
        get_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
