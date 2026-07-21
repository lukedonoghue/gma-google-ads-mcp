"""Tests for durable OAuth state storage."""

from __future__ import annotations

import unittest

from cryptography.fernet import Fernet
from google.api_core.exceptions import AlreadyExists
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from ads_mcp.changeset_store import FirestoreReplayGuard
from ads_mcp.firestore_store import FirestoreStore


class FakeSnapshot:
    def __init__(self, value):
        self._value = value
        self.exists = value is not None

    def to_dict(self):
        return self._value


class FakeDocument:
    def __init__(self, documents, document_id):
        self._documents = documents
        self._document_id = document_id

    async def get(self):
        return FakeSnapshot(self._documents.get(self._document_id))

    async def set(self, value):
        self._documents[self._document_id] = value

    async def create(self, value):
        if self._document_id in self._documents:
            raise AlreadyExists("document exists")
        self._documents[self._document_id] = value

    async def delete(self):
        self._documents.pop(self._document_id, None)


class FakeCollection:
    def __init__(self, documents):
        self._documents = documents

    def document(self, document_id):
        return FakeDocument(self._documents, document_id)


class FakeFirestoreClient:
    def __init__(self):
        self.documents = {}

    def collection(self, _name):
        return FakeCollection(self.documents)


class FirestoreStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeFirestoreClient()
        self.store = FirestoreStore(
            project_id="test-project",
            client=self.client,
        )

    async def test_put_get_and_delete(self):
        await self.store.put("client/one", {"token": "encrypted"})
        self.assertEqual(
            await self.store.get("client/one"),
            {"token": "encrypted"},
        )
        self.assertTrue(await self.store.delete("client/one"))
        self.assertIsNone(await self.store.get("client/one"))

    async def test_collections_do_not_collide(self):
        await self.store.put("same", {"value": 1}, collection="a")
        await self.store.put("same", {"value": 2}, collection="b")
        self.assertEqual(
            await self.store.get("same", collection="a"),
            {"value": 1},
        )
        self.assertEqual(
            await self.store.get("same", collection="b"),
            {"value": 2},
        )

    async def test_reserved_field_names_are_stored_as_opaque_json(self):
        value = {
            "__encrypted_data__": "ciphertext",
            "__encryption_version__": 1,
        }

        await self.store.put("encrypted", value)

        stored_document = next(iter(self.client.documents.values()))
        self.assertIn("value_json", stored_document)
        self.assertNotIn("value", stored_document)
        self.assertEqual(await self.store.get("encrypted"), value)

    async def test_expired_value_is_removed(self):
        await self.store.put("expired", {"value": 1}, ttl=0)
        self.assertIsNone(await self.store.get("expired"))
        self.assertEqual(self.client.documents, {})

    async def test_bulk_methods(self):
        await self.store.put_many(
            ["one", "two"],
            [{"value": 1}, {"value": 2}],
        )
        self.assertEqual(
            await self.store.get_many(["one", "missing", "two"]),
            [{"value": 1}, None, {"value": 2}],
        )
        self.assertEqual(await self.store.delete_many(["one", "two"]), 2)

    async def test_fernet_wrapper_keeps_token_out_of_firestore_plaintext(self):
        encrypted_store = FernetEncryptionWrapper(
            key_value=self.store,
            fernet=Fernet(Fernet.generate_key()),
        )
        await encrypted_store.put(
            "oauth-client",
            {"refresh_token": "sensitive-refresh-token"},
        )

        self.assertNotIn(
            "sensitive-refresh-token",
            repr(self.client.documents),
        )
        self.assertEqual(
            await encrypted_store.get("oauth-client"),
            {"refresh_token": "sensitive-refresh-token"},
        )

    async def test_replay_guard_claim_is_atomic_and_stores_no_raw_token(self):
        guard = FirestoreReplayGuard(
            project_id="test-project",
            database="(default)",
            collection_name="replays",
            client=self.client,
        )

        self.assertTrue(await guard.claim("approval-token-hash", ttl_seconds=60))
        self.assertFalse(await guard.claim("approval-token-hash", ttl_seconds=60))
        self.assertNotIn("approval-token-hash", repr(self.client.documents))


if __name__ == "__main__":
    unittest.main()
