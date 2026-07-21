"""Production hosting configuration for the Grow My Ads deployment."""

from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from ads_mcp.firestore_store import FirestoreStore

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
DEFAULT_BASE_URL = "http://localhost:8080"


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _allowed_redirect_uris() -> list[str] | None:
    raw_value = os.environ.get("GMA_MCP_ALLOWED_REDIRECT_URIS")
    if not raw_value:
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "GMA_MCP_ALLOWED_REDIRECT_URIS must be a JSON array"
        ) from error
    if not isinstance(parsed, list) or not all(
        isinstance(value, str) and value for value in parsed
    ):
        raise RuntimeError(
            "GMA_MCP_ALLOWED_REDIRECT_URIS must be a JSON array of strings"
        )
    return parsed


def build_google_provider(
    *,
    client_id: str,
    client_secret: str,
) -> GoogleProvider:
    """Create the OAuth provider, requiring durable storage in production."""

    environment = os.environ.get("GMA_MCP_ENV", "development").lower()
    base_url = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", DEFAULT_BASE_URL)
    storage_mode = os.environ.get("GMA_MCP_OAUTH_STORAGE", "memory").lower()

    provider_kwargs: dict = {
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": base_url,
        "required_scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            ADS_SCOPE,
        ],
        "allowed_client_redirect_uris": _allowed_redirect_uris(),
    }

    if storage_mode == "firestore":
        project_id = os.environ.get("GOOGLE_PROJECT_ID") or os.environ.get(
            "GOOGLE_CLOUD_PROJECT"
        )
        if not project_id:
            raise RuntimeError(
                "GOOGLE_PROJECT_ID is required for Firestore OAuth storage"
            )
        jwt_signing_key = _required_environment("GMA_MCP_JWT_SIGNING_KEY")
        encryption_key = _required_environment("GMA_MCP_STORAGE_ENCRYPTION_KEY")
        try:
            fernet = Fernet(encryption_key.encode())
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "GMA_MCP_STORAGE_ENCRYPTION_KEY must be a Fernet key"
            ) from error

        firestore_store = FirestoreStore(
            project_id=project_id,
            database=os.environ.get("GMA_MCP_FIRESTORE_DATABASE", "(default)"),
            collection_name=os.environ.get(
                "GMA_MCP_FIRESTORE_COLLECTION",
                "gma_mcp_oauth_state",
            ),
        )
        provider_kwargs.update(
            {
                "jwt_signing_key": jwt_signing_key,
                "client_storage": FernetEncryptionWrapper(
                    key_value=firestore_store,
                    fernet=fernet,
                ),
            }
        )
    elif environment == "production":
        raise RuntimeError(
            "Production requires GMA_MCP_OAUTH_STORAGE=firestore so OAuth "
            "connections survive Cloud Run restarts"
        )
    elif storage_mode != "memory":
        raise RuntimeError(f"Unsupported GMA_MCP_OAUTH_STORAGE value: {storage_mode}")

    return GoogleProvider(**provider_kwargs)
