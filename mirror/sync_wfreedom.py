#!/usr/bin/env python3
"""Route A: mirror wfreedom.option_flow_flows -> new cluster (watermark-based).

Copies new docs since the last watermark, preserving every original field
(including channel_label) and stamping mirror provenance:

    mirror_meta = {
        "synced_via": "wfreedom",          # which route brought this doc in
        "synced_at": <utc iso>,
        "origin_channel": <channel_label>, # per-channel tag for debugging
    }

Idempotent: docs are upserted by _id, so re-runs never duplicate.

Env:
    WFREEDOM_MDB_URI  connection string of the existing (wfreedom) cluster
    NEW_MDB_URI       connection string of the new Atlas M0 cluster
    MIRROR_DB         target database name (default: flow_mirror)
    MIRROR_BATCH      docs per run (default: 500)
"""
from __future__ import annotations

import datetime
import os
import sys

from pymongo import ASCENDING, MongoClient

DB_NAME = os.environ.get("MIRROR_DB", "flow_mirror")
SRC_DB = os.environ.get("WFREEDOM_DB", "wfreedom")
SRC_COLL = "option_flow_flows"
BATCH = int(os.environ.get("MIRROR_BATCH", "500"))
STATE_ID = "wfreedom"


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main() -> int:
    src_uri = os.environ.get("WFREEDOM_MDB_URI", "")
    new_uri = os.environ.get("NEW_MDB_URI", "")
    if not src_uri or not new_uri:
        print("WFREEDOM_MDB_URI and NEW_MDB_URI are required", file=sys.stderr)
        return 2

    src = MongoClient(src_uri, serverSelectionTimeoutMS=20000)
    dst = MongoClient(new_uri, serverSelectionTimeoutMS=20000)
    src_coll = src[SRC_DB][SRC_COLL]
    dst_coll = dst[DB_NAME][SRC_COLL]
    state_coll = dst[DB_NAME]["_sync_state"]

    state = state_coll.find_one({"_id": STATE_ID}) or {}
    watermark = state.get("last_first_seen_at", "")
    last_id = state.get("last_id", "")

    # first_seen_at is an ISO string in wfreedom -> plain string comparison works.
    # Tuple (first_seen_at, _id) resume: exact, never skips same-timestamp docs
    # when a batch is truncated by the limit.
    if watermark:
        query: dict = {
            "$or": [
                {"first_seen_at": {"$gt": watermark}},
                {"first_seen_at": watermark, "_id": {"$gt": last_id}},
            ]
        }
    else:
        query = {}
    cursor = (
        src_coll.find(query).sort([("first_seen_at", ASCENDING), ("_id", ASCENDING)]).limit(BATCH)
    )

    n = 0
    max_seen = watermark
    max_id = last_id
    for doc in cursor:
        doc = dict(doc)
        doc["mirror_meta"] = {
            "synced_via": "wfreedom",
            "synced_at": utcnow_iso(),
            "origin_channel": doc.get("channel_label", ""),
        }
        dst_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        n += 1
        fs = doc.get("first_seen_at", "")
        if fs >= max_seen:
            if fs > max_seen:
                max_seen, max_id = fs, str(doc["_id"])
            elif str(doc["_id"]) > max_id:
                max_id = str(doc["_id"])

    if n:
        state_coll.update_one(
            {"_id": STATE_ID},
            {"$set": {"last_first_seen_at": max_seen, "last_id": max_id,
                       "updated_at": utcnow_iso(), "docs_total": state.get("docs_total", 0) + n}},
            upsert=True,
        )
    print(f"route=wfreedom synced={n} watermark={max_seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
