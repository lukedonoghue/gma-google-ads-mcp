"""Firestore-backed key-value storage for production OAuth state.

FastMCP's OAuth proxy stores dynamic client registrations and upstream Google
tokens in an ``AsyncKeyValue`` implementation. Its Linux default is in-memory,
which is not safe on Cloud Run because instances are disposable. This adapter
keeps that state in Firestore; the caller must wrap it with FastMCP's Fernet
encryption wrapper before passing it to the OAuth provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, SupportsFloat

from google.cloud import firestore

DEFAULT_COLLECTION = "default_collection"


class FirestoreStore:
    """Minimal ``AsyncKeyValue`` implementation backed by Firestore."""

    def __init__(
        self,
        *,
        project_id: str,
        database: str = "(default)",
        collection_name: str = "gma_mcp_oauth_state",
        client: Any | None = None,
    ) -> None:
        if not project_id:
            raise ValueError("project_id is required for Firestore OAuth storage")
        self._client = client or firestore.AsyncClient(
            project=project_id,
            database=database,
        )
        self._collection = self._client.collection(collection_name)

    @staticmethod
    def _collection_name(collection: str | None) -> str:
        return collection or DEFAULT_COLLECTION

    @classmethod
    def _document_id(cls, key: str, collection: str | None) -> str:
        namespace = cls._collection_name(collection)
        return hashlib.sha256(f"{namespace}\0{key}".encode()).hexdigest()

    def _document(self, key: str, collection: str | None):
        return self._collection.document(self._document_id(key, collection))

    @staticmethod
    def _decode_value(data: Mapping[str, Any]) -> dict[str, Any] | None:
        value_json = data.get("value_json")
        if not isinstance(value_json, str):
            return None
        value = json.loads(value_json)
        return dict(value) if isinstance(value, Mapping) else None

    @staticmethod
    def _expires_at(ttl: SupportsFloat | None) -> datetime | None:
        if ttl is None:
            return None
        ttl_seconds = float(ttl)
        if ttl_seconds <= 0:
            return datetime.now(timezone.utc)
        return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    @staticmethod
    def _remaining_seconds(expires_at: datetime | None) -> float | None:
        if expires_at is None:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (expires_at - datetime.now(timezone.utc)).total_seconds(),
        )

    async def get(
        self,
        key: str,
        *,
        collection: str | None = None,
    ) -> dict[str, Any] | None:
        document = self._document(key, collection)
        snapshot = await document.get()
        if not snapshot.exists:
            return None

        data = snapshot.to_dict() or {}
        expires_at = data.get("expires_at")
        if expires_at is not None and self._remaining_seconds(expires_at) == 0:
            await document.delete()
            return None

        return self._decode_value(data)

    async def ttl(
        self,
        key: str,
        *,
        collection: str | None = None,
    ) -> tuple[dict[str, Any] | None, float | None]:
        document = self._document(key, collection)
        snapshot = await document.get()
        if not snapshot.exists:
            return (None, None)

        data = snapshot.to_dict() or {}
        expires_at = data.get("expires_at")
        remaining = self._remaining_seconds(expires_at)
        if expires_at is not None and remaining == 0:
            await document.delete()
            return (None, None)

        value = self._decode_value(data)
        if value is None:
            return (None, None)
        return (value, remaining)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        namespace = self._collection_name(collection)
        await self._document(key, collection).set(
            {
                "namespace": namespace,
                "key_hash": self._document_id(key, collection),
                # Firestore rejects nested field names wrapped in double
                # underscores. FastMCP's encryption envelope deliberately uses
                # names such as ``__encrypted_data__``, so store the opaque
                # mapping as JSON rather than as a nested Firestore map.
                "value_json": json.dumps(dict(value), separators=(",", ":")),
                "expires_at": self._expires_at(ttl),
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )

    async def delete(
        self,
        key: str,
        *,
        collection: str | None = None,
    ) -> bool:
        document = self._document(key, collection)
        snapshot = await document.get()
        if not snapshot.exists:
            return False
        await document.delete()
        return True

    async def get_many(
        self,
        keys: Sequence[str],
        *,
        collection: str | None = None,
    ) -> list[dict[str, Any] | None]:
        return list(
            await asyncio.gather(
                *(self.get(key, collection=collection) for key in keys)
            )
        )

    async def ttl_many(
        self,
        keys: Sequence[str],
        *,
        collection: str | None = None,
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        return list(
            await asyncio.gather(
                *(self.ttl(key, collection=collection) for key in keys)
            )
        )

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        if len(keys) != len(values):
            raise ValueError("keys and values must have the same length")
        await asyncio.gather(
            *(
                self.put(key, value, collection=collection, ttl=ttl)
                for key, value in zip(keys, values, strict=True)
            )
        )

    async def delete_many(
        self,
        keys: Sequence[str],
        *,
        collection: str | None = None,
    ) -> int:
        deleted = await asyncio.gather(
            *(self.delete(key, collection=collection) for key in keys)
        )
        return sum(deleted)
