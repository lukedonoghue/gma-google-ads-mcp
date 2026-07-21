"""Tests for hosted OAuth configuration guardrails."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from ads_mcp.hosted_config import build_google_provider


class HostedConfigTest(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "GMA_MCP_ENV": "production",
            "GMA_MCP_OAUTH_STORAGE": "firestore",
            "GOOGLE_PROJECT_ID": "test-project",
            "GOOGLE_ADS_MCP_BASE_URL": "https://ads-mcp.example.com",
            "GMA_MCP_JWT_SIGNING_KEY": "jwt-signing-key",
            "GMA_MCP_STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }

    @patch("ads_mcp.hosted_config.GoogleProvider")
    @patch("ads_mcp.hosted_config.FernetEncryptionWrapper")
    @patch("ads_mcp.hosted_config.FirestoreStore")
    def test_production_uses_encrypted_firestore(
        self,
        firestore_store,
        encryption_wrapper,
        google_provider,
    ):
        google_provider.return_value = MagicMock()
        encrypted_storage = MagicMock()
        encryption_wrapper.return_value = encrypted_storage
        with patch.dict(os.environ, self.environment, clear=True):
            build_google_provider(
                client_id="client-id",
                client_secret="client-secret",
            )

        firestore_store.assert_called_once_with(
            project_id="test-project",
            database="(default)",
            collection_name="gma_mcp_oauth_state",
        )
        encryption_wrapper.assert_called_once()
        kwargs = google_provider.call_args.kwargs
        self.assertEqual(
            kwargs["base_url"],
            "https://ads-mcp.example.com",
        )
        self.assertEqual(kwargs["jwt_signing_key"], "jwt-signing-key")
        self.assertIs(kwargs["client_storage"], encrypted_storage)

    def test_production_rejects_ephemeral_storage(self):
        environment = {
            "GMA_MCP_ENV": "production",
            "GMA_MCP_OAUTH_STORAGE": "memory",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Production requires"):
                build_google_provider(
                    client_id="client-id",
                    client_secret="client-secret",
                )

    def test_redirect_allowlist_must_be_json_array(self):
        environment = {
            **self.environment,
            "GMA_MCP_ALLOWED_REDIRECT_URIS": "not-json",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "JSON array"):
                build_google_provider(
                    client_id="client-id",
                    client_secret="client-secret",
                )


if __name__ == "__main__":
    unittest.main()
