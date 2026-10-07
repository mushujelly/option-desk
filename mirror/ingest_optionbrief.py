#!/usr/bin/env python3
"""Ingest optionbrief.com (KZG Option House) daily options-market aggregates.

The site embeds a gzip+base64 JSON pack as window.__KZG_PACK__ inside
/assets/kzg-frame-<hash>.js (hash parsed from index.html on every run).
Pure static fetch -- no browser needed.

Collections in MIRROR_DB (default: flow_mirror):
  ob_market_overview  one doc per date: market-wide totals
  ob_market_daily     one doc per (date, symbol): per-symbol detail

The pack only carries full per-symbol detail for the latest trading day;
`analytics.daily` carries 584d of market-wide aggregates (no per-symbol
breakdown). BACKFILL=1 upserts those aggregates as overview docs with
has_detail=false so the time series starts immediately.

Env:
    NEW_MDB_URI   Atlas connection string (required)
    MIRROR_DB     database name (default: flow_mirror)
    BACKFILL=1    also ingest the 584-day analytics.daily aggregates

Exit codes: 0 ok, 1 no new data, 2 schema/fetch failure (cron should alert).
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import sys
import urllib.request

BASE = "https://optionbrief.com"
UA = {"User-Agent": "muse-ob-ingest/1.0 (+cross-validation reference)"}

REQUIRED_TOP = {"packagedAt", "index", "analytics", "days"}
OVERVIEW_COL = "ob_market_overview"
DAILY_COL = "ob_market_daily"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_pack() -> dict:
    html = _get(BASE + "/").decode("utf-8", errors="replace")
    m = re.search(r'/assets/(kzg-frame-[a-f0-9]+\.js)', html)
    if not m:
        raise RuntimeError("kzg-frame js not found in index.html (site layout changed?)")
    js = _get(BASE + "/assets/" + m.group(1)).decode("utf-8", errors="replace")
    p = re.search(r'__KZG_PACK__="([^"]+)"', js)
    if not p:
        raise RuntimeError("__KZG_PACK__ not found in frame js (packaging changed?)")
    pack = json.loads(gzip.decompress(base64.b64decode(p.group(1))))
    missing = REQUIRED_TOP - set(pack.keys())
    if missing:
        raise RuntimeError(f"pack schema changed, missing top keys: {missing}")
    return pack


def build_docs(pack: dict, backfill: bool = False) -> tuple[list, list]:
    """Returns (overview_docs, daily_docs). Upsert by _id (idempotent)."""
    index = pack["index"]
    latest = index.get("latestDate")
    if not latest or latest not in pack.get("days", {}):
        raise RuntimeError("pack has no usable latest day detail")
    day = pack["days"][latest]
    ov = day.get("overview") or {}
    for k in ("totalVol", "totalPremium", "marketCp"):
        if k not in ov:
            raise RuntimeError(f"overview schema changed, missing {k}")

    overview_docs = [{
        "_id": latest,
        "date": latest,
        "source": "optionbrief",
        "packagedAt": pack.get("packagedAt"),
        "generatedAt": index.get("generatedAt"),
        "has_detail": True,
        "totalVol": ov.get("totalVol"),
        "totalTxn": ov.get("totalTxn"),
        "totalPremium": ov.get("totalPremium"),
        "totalCall": ov.get("totalCall"),
        "totalPut": ov.get("totalPut"),
        "marketCp": ov.get("marketCp"),
        "categories": ov.get("category"),
    }]

    daily_docs = []
    for rows in (day.get("stockRows") or [], day.get("etfRows") or [],
                 day.get("indexRows") or []):
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            daily_docs.append({
                "_id": f"{latest}:{sym}",
                "date": latest,
                "symbol": sym,
                "source": "optionbrief",
                "category": r.get("category"),
                "totalVol": r.get("totalVol"),
                "txn": r.get("txn"),
                "premiumNotional": r.get("premiumNotional"),
                "callVol": r.get("callVol"),
                "putVol": r.get("putVol"),
                "cpRatio": r.get("cpRatio"),
                "leapCall": r.get("leapCall"),
                "leapPut": r.get("leapPut"),
                "leapRatio": r.get("leapRatio"),
                "avgSize": r.get("avgSize"),
                "hottest": r.get("hottest"),
                "hottestShort": r.get("hottestShort"),
            })

    if backfill:
        have = {latest}
        for d in pack["analytics"].get("daily", []):
            dt = d.get("date")
            if not dt or dt in have:
                continue
            have.add(dt)
            overview_docs.append({
                "_id": dt,
                "date": dt,
                "source": "optionbrief",
                "packagedAt": pack.get("packagedAt"),
                "generatedAt": index.get("generatedAt"),
                "has_detail": False,
                "totalVol": d.get("totalVol"),
                "totalTxn": None,
                "totalPremium": d.get("totalPremium"),
                "totalCall": None,
                "totalPut": None,
                "marketCp": d.get("marketCp"),
                "categories": d.get("category"),
            })
    return overview_docs, daily_docs


def upsert_docs(uri: str, db_name: str, overview_docs: list, daily_docs: list) -> None:
    # Same upsert pattern as sync_wfreedom.py / poll_discord.py (proven in prod).
    from pymongo import MongoClient
    db = MongoClient(uri, serverSelectionTimeoutMS=20000)[db_name]
    for docs, coll_name in ((overview_docs, OVERVIEW_COL), (daily_docs, DAILY_COL)):
        n_up = n_mod = 0
        coll = db[coll_name]
        for d in docs:
            res = coll.update_one({"_id": d["_id"]}, {"$set": d}, upsert=True)
            if res.upserted_id:
                n_up += 1
            elif res.modified_count:
                n_mod += 1
        print(f"{coll_name}: upserted={n_up} modified={n_mod} total={len(docs)}")


def main() -> int:
    uri = os.environ.get("NEW_MDB_URI", "")
    if not uri:
        print("NEW_MDB_URI is required", file=sys.stderr)
        return 2
    db_name = os.environ.get("MIRROR_DB", "flow_mirror")
    try:
        pack = fetch_pack()
    except Exception as e:
        print(f"fetch/parse failed: {e}", file=sys.stderr)
        return 2
    try:
        overview_docs, daily_docs = build_docs(
            pack, backfill=os.environ.get("BACKFILL", "") == "1")
    except Exception as e:
        print(f"schema validation failed: {e}", file=sys.stderr)
        return 2
    latest = pack["index"]["latestDate"]
    print(f"pack latestDate={latest} overview_docs={len(overview_docs)} "
          f"daily_docs={len(daily_docs)}")
    upsert_docs(uri, db_name, overview_docs, daily_docs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
