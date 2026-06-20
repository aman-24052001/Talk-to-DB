"""Builds a fully-wired Mongo Backend. Imported lazily from
app/backends/factory.py so a SQL-only deployment never needs pymongo
installed at all.
"""
from __future__ import annotations

import logging

from app.backends.factory import Backend
from app.backends.mongo.adapter import MongoAdapter
from app.backends.mongo.engine import (
    build_mongo_client,
    get_readonly_db,
    verify_readonly_credentials,
)
from app.backends.mongo.executor import MongoExecutor
from app.backends.mongo.introspect import MongoSchemaService
from app.config import AppConfig

log = logging.getLogger("talk_to_db")


def build_mongo_backend(cfg: AppConfig) -> Backend:
    client = build_mongo_client(cfg)
    db = get_readonly_db(client, cfg)

    status = verify_readonly_credentials(client, cfg)
    # Surface anything that isn't a clean read-only result as a warning, so
    # it's visible in logs rather than buried — but never block startup on it.
    if "good" in status or "demo mode" in status:
        log.info("mongo read-only check: %s", status)
    else:
        log.warning("mongo read-only check: %s", status)

    schema = MongoSchemaService(db, cfg)
    executor = MongoExecutor(db, cfg)
    adapter = MongoAdapter(executor)
    return Backend(
        engine=client, schema=schema, executor=executor, adapter=adapter,
        close=client.close,
    )
