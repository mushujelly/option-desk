#!/usr/bin/env python3
"""Enrich option_flow_flows docs with greeks/IV/OI/volume (CBOE free delayed chain).

Zero-cost enrichment for the flow mirror:
  - Picks docs with expiration_date >= today and no greeks_enriched_at yet
    (newest first, ENRICH_LIMIT per run).
  - Groups by symbol; one CBOE delayed-chain fetch per symbol
    (https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json, keyless).
  - Matches (expiration, strike, call/put) via the OCC-style option symbol and
    writes delta/gamma/theta/vega/rho/iv/theo_price. Fills open_interest/volume
    only when missing (source-reported values win).
  - Fetch failures are NOT marked (retried next run); match misses are marked
    greeks_quality="no_match" so they aren't retried forever.

Env:
    NEW_MDB_URI     Atlas connection string (required)
    MIRROR_DB       database name (default: flow_mirror)
    ENRICH_LIMIT    max docs per run (default: 200)
    ENRICH_DRY_RUN  "1" -> report only, no writes
"""
from __future__ import annotations

import datetime
import os
import sys
import time

import httpx
from pymongo import DESCENDING, MongoClient

DB_NAME = os.environ.get("MIRROR_DB", "flow_mirror")
LIMIT = int(os.environ.get("ENRICH_LIMIT", "200"))
DRY_RUN = os.environ.get("ENRICH_DRY_RUN", "") == "1"

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
# index options need the underscore prefix on the request; the option symbols
# themselves use the plain prefix (SPX261016C00200000, no underscore).
INDEX_REQUEST = {"SPX": "_SPX", "VIX": "_VIX", "NDX": "_NDX", "RUT": "_RUT", "DJX": "_DJX"}

GREEK_FIELDS = ["delta", "gamma", "theta", "vega", "rho", "iv", "theo"]


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def today_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def cboe_symbols(symbol: str) -> tuple[str, str]:
    """(request_symbol, option_symbol_prefix)"""
    s = (symbol or "").strip().upper()
    return INDEX_REQUEST.get(s, s), s


def option_key(opt_prefix: str, expiration: str, strike_text: str, option_type: str) -> str | None:
    """Build the CBOE option symbol, e.g. SPX261016C00200000."""
    try:
        y, m, d = expiration.split("-")
        yy = y[2:]
        cp = "C" if option_type.lower().startswith("call") else "P"
        strike_int = int(round(float(str(strike_text).replace(",", "")) * 1000))
        return f"{opt_prefix}{yy}{int(m):02d}{int(d):02d}{cp}{strike_int:08d}"
    except Exception:
        return None


def fetch_chain(client: httpx.Client, request_sym: str) -> dict | None:
    try:
        r = client.get(CBOE.format(request_sym), timeout=30)
        r.raise_for_status()
        data = r.json().get("data", {}).get("options", [])
        return {o.get("option", ""): o for o in data if o.get("option")}
    except Exception as e:
        print(f"cboe fetch failed for {request_sym}: {e}")
        return None


def main() -> int:
    new_uri = os.environ.get("NEW_MDB_URI", "")
    if not new_uri:
        print("NEW_MDB_URI is required", file=sys.stderr)
        return 2

    dst = MongoClient(new_uri, serverSelectionTimeoutMS=20000)
    coll = dst[DB_NAME]["option_flow_flows"]

    q = {
        "expiration_date": {"$gte": today_str()},
        "greeks_enriched_at": {"$exists": False},
    }
    docs = list(
        coll.find(q, {"_id": 1, "symbol": 1, "expiration_date": 1,
                      "strike_text": 1, "option_type": 1,
                      "open_interest": 1, "volume": 1})
        .sort("first_seen_at", DESCENDING)
        .limit(LIMIT)
    )
    print(f"enrich candidates: {len(docs)}")
    if not docs:
        return 0

    http = httpx.Client(headers={"User-Agent": "FlowMirrorEnrich/1.0"}, timeout=30,
                        follow_redirects=True)
    chains: dict[str, dict | None] = {}
    n_ok = n_miss = n_fail = n_dry = 0

    for doc in docs:
        symbol = doc.get("symbol") or ""
        exp = doc.get("expiration_date") or ""
        req_sym, opt_prefix = cboe_symbols(symbol)
        key = option_key(opt_prefix, exp, doc.get("strike_text"), doc.get("option_type") or "")
        if not key:
            continue
        if req_sym not in chains:
            chains[req_sym] = fetch_chain(http, req_sym)
            time.sleep(1)  # be polite to the free endpoint
        chain = chains[req_sym]
        if chain is None:
            n_fail += 1  # transient: not marked, retried next run
            continue
        o = chain.get(key)
        if not o:
            n_miss += 1
            update = {"greeks_enriched_at": utcnow_iso(),
                      "greeks_source": "cboe-delayed",
                      "greeks_quality": "no_match"}
        else:
            n_ok += 1
            iv = o.get("iv") or 0
            update = {
                "delta": o.get("delta"),
                "gamma": o.get("gamma"),
                "theta": o.get("theta"),
                "vega": o.get("vega"),
                "rho": o.get("rho"),
                "iv": iv,
                "theo_price": o.get("theo"),
                "greeks_enriched_at": utcnow_iso(),
                "greeks_source": "cboe-delayed",
                "greeks_quality": "ok" if iv and iv > 0 else "stale_quote",
            }
            # fill OI/volume only when the doc doesn't already have them
            if not doc.get("open_interest"):
                update["open_interest"] = o.get("open_interest")
            if not doc.get("volume"):
                update["volume"] = o.get("volume")
        if DRY_RUN:
            n_dry += 1
            continue
        coll.update_one({"_id": doc["_id"]}, {"$set": update})

    print(f"enrich done: ok={n_ok} no_match={n_miss} fetch_fail={n_fail}"
          + (" DRY_RUN" if DRY_RUN else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
