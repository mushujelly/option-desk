#!/usr/bin/env python3
"""One-time setup for the new Atlas M0 mirror database.

Creates database `flow_mirror` with:
  - option_flow_flows : parsed flow docs (mirrored from wfreedom + discord-direct)
  - discord_raw       : raw Discord messages (B route), tagged per channel
  - _sync_state       : watermarks for both routes

Usage:
    NEW_MDB_URI='mongodb+srv://...' python3 setup_new_db.py

Safe to re-run: only creates missing collections/indexes.
"""
from __future__ import annotations

import os
import sys

from pymongo import ASCENDING, MongoClient

DB_NAME = os.environ.get("MIRROR_DB", "flow_mirror")


def main() -> int:
    uri = os.environ.get("NEW_MDB_URI", "")
    if not uri:
        print("NEW_MDB_URI is required", file=sys.stderr)
        return 2

    client = MongoClient(uri, serverSelectionTimeoutMS=20000)
    db = client[DB_NAME]

    # --- option_flow_flows: the parsed mirror ---
    flows = db["option_flow_flows"]
    flows.create_index([("first_seen_at", ASCENDING)], name="first_seen_at_1")
    flows.create_index([("channel_label", ASCENDING)], name="channel_label_1")
    flows.create_index(
        [("source_refs.message_id", ASCENDING)],
        name="source_refs.message_id_1",
        sparse=True,
    )
    flows.create_index(
        [("mirror_meta.synced_via", ASCENDING)], name="mirror_meta.synced_via_1"
    )
    flows.create_index([("symbol", ASCENDING)], name="symbol_1")

    # --- discord_raw: raw Discord messages, one doc per message ---
    raw = db["discord_raw"]
    raw.create_index([("message_id", ASCENDING)], name="message_id_1", unique=True)
    raw.create_index([("channel_id", ASCENDING)], name="channel_id_1")
    raw.create_index([("channel_label", ASCENDING)], name="channel_label_1")
    raw.create_index([("timestamp", ASCENDING)], name="timestamp_1")

    # --- _sync_state: watermarks (_id is unique by default) ---
    db["_sync_state"]

    print(f"OK: database '{DB_NAME}' ready")
    print("collections:", sorted(db.list_collection_names()))
    for cname in ("option_flow_flows", "discord_raw"):
        print(f"indexes on {cname}:", sorted(db[cname].index_information()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
