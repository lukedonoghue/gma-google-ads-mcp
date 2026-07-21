"""Encrypted changeset state and one-time approval replay protection."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from cryptography.fernet import Fernet
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from ads_mcp.firestore_store import FirestoreStore


class AsyncChangesetStore(Protocol):
    async def get(
        self, key: str, *, collection: str | None = None
    ) -> dict[str, Any] | None: ...

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None: ...


class ReplayGuard(Protocol):
    async def claim(self, approval_token_hash: str, *, ttl_seconds: int) -> bool: ...


class MemoryReplayGuard:
    """Process-local replay guard for development and tests."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, approval_token_hash: str, *, ttl_seconds: int) -> bool:
        del ttl_seconds
        async with self._lock:
            if approval_token_hash in self._claimed:
                return False
            self._claimed.add(approval_token_hash)
            return True


class FirestoreReplayGuard:
    """Cross-instance one-time token guard using Firestore create semantics."""

    def __init__(
        self,
        *,
        project_id: str,
        database: str,
        collection_name: str,
        client: Any | None = None,
    ) -> None:
        self._client = client or firestore.AsyncClient(
            project=project_id,
            database=database,
        )
        self._collection = self._client.collection(collection_name)

    async def claim(self, approval_token_hash: str, *, ttl_seconds: int) -> bool:
        document_id = hashlib.sha256(approval_token_hash.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        try:
            await self._collection.document(document_id).create(
                {
                    "token_hash_hash": document_id,
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "expires_at": expires_at,
                }
            )
            return True
        except AlreadyExists:
            return False


_store: AsyncChangesetStore | None = None
_replay_guard: ReplayGuard | None = None


def _project_id() -> str:
    value = os.environ.get("GOOGLE_PROJECT_ID") or os.environ.get(
        "GOOGLE_CLOUD_PROJECT"
    )
    if not value:
        raise RuntimeError("GOOGLE_PROJECT_ID is required for changeset storage")
    return value


def _fernet() -> Fernet:
    value = os.environ.get("GMA_MCP_STORAGE_ENCRYPTION_KEY")
    if not value:
        raise RuntimeError(
            "GMA_MCP_STORAGE_ENCRYPTION_KEY is required for changeset storage"
        )
    try:
        return Fernet(value.encode())
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "GMA_MCP_STORAGE_ENCRYPTION_KEY must be a Fernet key"
        ) from error


def get_changeset_store() -> AsyncChangesetStore:
    """Return the configured encrypted state store.

    Production refuses process-local state because Cloud Run can run several
    disposable instances. Development defaults to memory for unit tests and
    local stdio usage.
    """

    global _store
    if _store is not None:
        return _store

    environment = os.environ.get("GMA_MCP_ENV", "development").lower()
    storage_mode = os.environ.get(
        "GMA_MCP_CHANGESET_STORAGE",
        os.environ.get("GMA_MCP_OAUTH_STORAGE", "memory"),
    ).lower()
    if storage_mode == "firestore":
        raw_store = FirestoreStore(
            project_id=_project_id(),
            database=os.environ.get("GMA_MCP_FIRESTORE_DATABASE", "(default)"),
            collection_name=os.environ.get(
                "GMA_MCP_CHANGESET_COLLECTION", "gma_mcp_changesets"
            ),
        )
        _store = FernetEncryptionWrapper(key_value=raw_store, fernet=_fernet())
    elif storage_mode == "memory" and environment != "production":
        _store = MemoryStore()
    elif storage_mode == "memory":
        raise RuntimeError(
            "Production changesets require GMA_MCP_CHANGESET_STORAGE=firestore"
        )
    else:
        raise RuntimeError(
            f"Unsupported GMA_MCP_CHANGESET_STORAGE value: {storage_mode}"
        )
    return _store


def get_replay_guard() -> ReplayGuard:
    global _replay_guard
    if _replay_guard is not None:
        return _replay_guard

    environment = os.environ.get("GMA_MCP_ENV", "development").lower()
    storage_mode = os.environ.get(
        "GMA_MCP_CHANGESET_STORAGE",
        os.environ.get("GMA_MCP_OAUTH_STORAGE", "memory"),
    ).lower()
    if storage_mode == "firestore":
        _replay_guard = FirestoreReplayGuard(
            project_id=_project_id(),
            database=os.environ.get("GMA_MCP_FIRESTORE_DATABASE", "(default)"),
            collection_name=os.environ.get(
                "GMA_MCP_REPLAY_COLLECTION", "gma_mcp_changeset_replays"
            ),
        )
    elif storage_mode == "memory" and environment != "production":
        _replay_guard = MemoryReplayGuard()
    else:
        raise RuntimeError("Production approval replay protection requires Firestore")
    return _replay_guard


def reset_changeset_backends_for_tests() -> None:
    """Clear module singletons so tests can change environment safely."""

    global _store, _replay_guard
    _store = None
    _replay_guard = None
