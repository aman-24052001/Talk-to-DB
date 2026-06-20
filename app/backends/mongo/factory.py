"""Builds a fully-wired Mongo Backend. Imported lazily from
app/backends/factory.py so a SQL-only deployment never needs pymongo
installed at all.
"""
from __future__ import annotations

from app.backends.factory import Backend
from app.backends.mongo.adapter import MongoAdapter
from app.backends.mongo.engine import build_mongo_client, get_readonly_db
from app.backends.mongo.executor import MongoExecutor
from app.backends.mongo.introspect import MongoSchemaService
from app.config import AppConfig


def build_mongo_backend(cfg: AppConfig) -> Backend:
    client = build_mongo_client(cfg)
    db = get_readonly_db(client, cfg)
    schema = MongoSchemaService(db, cfg)
    executor = MongoExecutor(db, cfg)
    adapter = MongoAdapter(executor)
    return Backend(
        engine=client, schema=schema, executor=executor, adapter=adapter,
        close=client.close,
    )
