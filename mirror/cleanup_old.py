#!/usr/bin/env python3
"""Periodic cleanup for the flow_mirror database (opt-in).

Deletes docs older than RETENTION_DAYS from option_flow_flows and discord_raw.
first_seen_at / timestamp are ISO strings -> plain string comparison works.

Env:
    NEW_MDB_URI     connection string of the new Atlas M0 cluster
    MIRROR_DB       database name (default: flow_mirror)
    RETENTION_DAYS  keep this many days (default: 365)
    DRY_RUN=1       only report, delete nothing

At current growth (~30 flow docs/day, ~2KB each) the M0 512MB quota lasts
5+ years, so this is a safety valve, not a necessity.
"""
from __future__ import annotations

import datetime
import os
import sys

from pymongo import MongoClient

DB_NAME = os.environ.get("MIRROR_DB", "flow_mirror")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "365"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"


def main() -> int:
    uri = os.environ.get("NEW_MDB_URI", "")
    if not uri:
        print("NEW_MDB_URI is required", file=sys.stderr)
        return 2

    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=RETENTION_DAYS)
    ).isoformat()
    print(f"cutoff={cutoff} (keeping last {RETENTION_DAYS}d) dry_run={DRY_RUN}")

    db = MongoClient(uri, serverSelectionTimeoutMS=20000)[DB_NAME]
    stats = db.command("dbstats")
    print(f"db dataSize={stats['dataSize']/1048576:.1f}MB storageSize={stats['storageSize']/1048576:.1f}MB")

    targets = [
        ("option_flow_flows", "first_seen_at"),
        ("discord_raw", "timestamp"),
    ]
    for coll_name, field in targets:
        coll = db[coll_name]
        filt = {field: {"$lt": cutoff}}
        n = coll.count_documents(filt)
        if DRY_RUN:
            print(f"{coll_name}: would delete {n}")
        else:
            r = coll.delete_many(filt)
            print(f"{coll_name}: deleted {r.deleted_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
