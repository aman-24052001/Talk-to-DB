"""Mongo connection with defence-in-depth read-only enforcement.

Mongo has no session-level read-only flag the way SQLite has PRAGMA
query_only or Postgres has default_transaction_read_only. Enforcement here
is two layers, same philosophy as db/engine.py's SQL version:

Layer 1 (operational, recommended in docs): connect with credentials bound
to Mongo's built-in `read` role on the target database.

Layer 2 (this module): capability narrowing. ReadOnlyCollection wraps a
real pymongo Collection and exposes ONLY find/aggregate/count_documents/
estimated_document_count/distinct. There is no __getattr__ passthrough —
insert_one, update_one, delete_many, drop, create_index, etc. are not
reachable from this object at all, so a validator bug can't reach them
either. This is the same property the SQLite PRAGMA gives you: even if
the firewall has a gap, the connection itself cannot write.
"""
from __future__ import annotations

from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.config import AppConfig


class ReadOnlyCollection:
    """A pymongo Collection with only read methods reachable."""

    def __init__(self, collection: Collection):
        self._collection = collection

    @property
    def name(self) -> str:
        return self._collection.name

    def find(self, *args: Any, **kwargs: Any):
        return self._collection.find(*args, **kwargs)

    def aggregate(self, *args: Any, **kwargs: Any):
        return self._collection.aggregate(*args, **kwargs)

    def count_documents(self, *args: Any, **kwargs: Any) -> int:
        return self._collection.count_documents(*args, **kwargs)

    def estimated_document_count(self, *args: Any, **kwargs: Any) -> int:
        return self._collection.estimated_document_count(*args, **kwargs)

    def distinct(self, *args: Any, **kwargs: Any):
        return self._collection.distinct(*args, **kwargs)


class ReadOnlyDatabase:
    """A pymongo Database with only collection listing + read access reachable."""

    def __init__(self, database: Database):
        self._database = database

    def list_collection_names(self) -> list[str]:
        return self._database.list_collection_names()

    def get_collection(self, name: str) -> ReadOnlyCollection:
        return ReadOnlyCollection(self._database[name])

    def __getitem__(self, name: str) -> ReadOnlyCollection:
        return self.get_collection(name)


def build_mongo_client(cfg: AppConfig) -> MongoClient:
    return MongoClient(
        cfg.database.url,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )


def get_readonly_db(client: MongoClient, cfg: AppConfig) -> ReadOnlyDatabase:
    """Resolve the target database from the connection URL's path
    (mongodb://host/dbname) and wrap it as read-only. Raises clearly if
    the URL doesn't specify a database — there is no sane default to fall
    back to (unlike SQL, there's no single 'current database' notion
    without one)."""
    db = client.get_default_database()
    if db is None:
        raise ValueError(
            "database.url must include a database name, e.g. "
            "'mongodb://host:27017/mydb' — no default database in the URL."
        )
    return ReadOnlyDatabase(db)
