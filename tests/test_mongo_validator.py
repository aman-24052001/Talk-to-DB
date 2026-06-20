"""Mongo firewall tests: every way a model (or a prompt-injected model)
could try to write data, execute arbitrary JS, or reach an unlisted
collection must be blocked — mirrors test_guardrails.py's coverage shape
for the SQL firewall, applied to validate_mongo_query.
"""
import json

import pytest

from app.backends.mongo.validator import MongoRejected, validate_mongo_query

COLLECTIONS = {"orders", "customers", "tickets"}


def v(spec: dict, max_rows: int = 200):
    return validate_mongo_query(
        json.dumps(spec), dialect="mongodb", known_tables=COLLECTIONS, max_rows=max_rows
    )


# ---------------------------------------------------------------- allowed
def test_valid_find_passes():
    out = v({"operation": "find", "collection": "orders", "query": {"status": "completed"}})
    assert out.operation == "find"
    assert out.collection == "orders"
    assert out.spec["filter"] == {"status": "completed"}


def test_valid_find_with_projection_sort_limit():
    out = v({
        "operation": "find", "collection": "tickets", "query": {},
        "projection": {"subject": 1}, "sort": {"created_at": -1}, "limit": 10,
    })
    assert out.spec["projection"] == {"subject": 1}
    assert out.spec["sort"] == {"created_at": -1}
    assert out.spec["limit"] == 10


def test_valid_aggregate_passes():
    out = v({
        "operation": "aggregate", "collection": "orders",
        "query": [
            {"$match": {"status": "completed"}},
            {"$group": {"_id": "$customer", "total": {"$sum": "$amount"}}},
            {"$sort": {"total": -1}},
            {"$limit": 5},
        ],
    })
    assert out.operation == "aggregate"
    assert out.spec[-1] == {"$limit": 5}


def test_valid_lookup_to_known_collection_passes():
    out = v({
        "operation": "aggregate", "collection": "orders",
        "query": [{"$lookup": {"from": "customers", "localField": "customer_id",
                                "foreignField": "_id", "as": "cust"}}],
    })
    assert any("$lookup" in s for s in out.spec)


# ---------------------------------------------------------------- blocked: operation/shape
def test_unknown_operation_blocked():
    with pytest.raises(MongoRejected):
        v({"operation": "delete", "collection": "orders", "query": {}})


def test_unknown_collection_blocked():
    with pytest.raises(MongoRejected, match="not in the allowed schema"):
        v({"operation": "find", "collection": "ssn_table", "query": {}})


def test_empty_query_blocked():
    with pytest.raises(MongoRejected):
        validate_mongo_query("", dialect="mongodb", known_tables=COLLECTIONS, max_rows=200)


def test_invalid_json_blocked():
    with pytest.raises(MongoRejected, match="not valid JSON"):
        validate_mongo_query("{not json", dialect="mongodb", known_tables=COLLECTIONS, max_rows=200)


def test_non_object_top_level_blocked():
    with pytest.raises(MongoRejected):
        validate_mongo_query("[1,2,3]", dialect="mongodb", known_tables=COLLECTIONS, max_rows=200)


def test_aggregate_non_list_query_blocked():
    with pytest.raises(MongoRejected, match="array of pipeline stages"):
        v({"operation": "aggregate", "collection": "orders", "query": {"$match": {}}})


def test_multi_key_stage_object_blocked():
    with pytest.raises(MongoRejected, match="exactly one key"):
        v({
            "operation": "aggregate", "collection": "orders",
            "query": [{"$match": {}, "$sort": {}}],
        })


# ---------------------------------------------------------------- blocked: write/JS escapes
def test_out_stage_blocked():
    with pytest.raises(MongoRejected):
        v({"operation": "aggregate", "collection": "orders",
           "query": [{"$out": "stolen_copy"}]})


def test_merge_stage_blocked():
    with pytest.raises(MongoRejected):
        v({"operation": "aggregate", "collection": "orders",
           "query": [{"$merge": {"into": "orders"}}]})


def test_where_in_find_filter_blocked():
    with pytest.raises(MongoRejected, match=r"\$where"):
        v({"operation": "find", "collection": "orders",
           "query": {"$where": "function(){return true}"}})


def test_function_nested_inside_project_blocked():
    """$function tucked inside an otherwise-allowed $project stage must
    still be caught — the firewall walks recursively, not just top-level
    stage names."""
    with pytest.raises(MongoRejected, match=r"\$function"):
        v({
            "operation": "aggregate", "collection": "orders",
            "query": [{"$project": {"x": {"$function": {
                "body": "function(){return 1}", "args": [], "lang": "js"
            }}}}],
        })


def test_accumulator_blocked():
    with pytest.raises(MongoRejected, match=r"\$accumulator"):
        v({
            "operation": "aggregate", "collection": "orders",
            "query": [{"$group": {"_id": "$x", "y": {"$accumulator": {}}}}],
        })


def test_unallowed_stage_name_blocked():
    with pytest.raises(MongoRejected, match="not allowed"):
        v({"operation": "aggregate", "collection": "orders",
           "query": [{"$indexStats": {}}]})


def test_lookup_to_unknown_collection_blocked():
    with pytest.raises(MongoRejected, match="unknown collection"):
        v({
            "operation": "aggregate", "collection": "orders",
            "query": [{"$lookup": {"from": "ssn_table", "localField": "a",
                                    "foreignField": "b", "as": "x"}}],
        })


# ---------------------------------------------------------------- limit enforcement
def test_limit_injected_when_missing_in_aggregate():
    out = v({
        "operation": "aggregate", "collection": "orders",
        "query": [{"$match": {"status": "completed"}}],
    })
    assert out.spec[-1] == {"$limit": 200}


def test_oversized_limit_clamped_in_aggregate():
    out = v({
        "operation": "aggregate", "collection": "orders",
        "query": [{"$match": {}}, {"$limit": 999999}],
    }, max_rows=50)
    assert {"$limit": 50} in out.spec
    assert {"$limit": 999999} not in out.spec


def test_find_limit_clamped():
    out = v({"operation": "find", "collection": "orders", "query": {}, "limit": 999999}, max_rows=50)
    assert out.spec["limit"] == 50


def test_find_default_limit_when_missing():
    out = v({"operation": "find", "collection": "orders", "query": {}})
    assert out.spec["limit"] == 200
