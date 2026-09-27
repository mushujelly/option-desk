#!/usr/bin/env python3
"""Route B: poll Discord channels directly -> new cluster.

For each configured channel:
  1. Fetch messages after the stored watermark (Discord snowflake cursor).
  2. Store the RAW message in `discord_raw`, tagged with channel_id/channel_label
     (unique on message_id -> re-runs never duplicate raw).
  3. Best-effort parse of the compact flow format
         $TICKER STRIKE C|P MM/DD/YYYY $X.Xm (Bullish|Bearish)
     into `option_flow_flows` docs, stamped:
         channel_label = <channel tag>
         mirror_meta.synced_via = "discord-direct"
     Skips messages already mirrored via route A (matched on
     source_refs.message_id).

Env:
    DISCORD_TOKEN     Discord token, used verbatim in the Authorization header
                      (user token as-is; bot token as "Bot <token>")
    NEW_MDB_URI       connection string of the new Atlas M0 cluster
    MIRROR_DB         target database name (default: flow_mirror)
    DISCORD_CHANNELS  comma-separated "channel_id[:label]" pairs, e.g.
                      "1386775915991797951:notableflow,123456789"
                      Labels omitted -> resolved via Discord API.
"""
from __future__ import annotations

import datetime
import os
import re
import sys
import time

import httpx
from pymongo import ASCENDING, MongoClient

DB_NAME = os.environ.get("MIRROR_DB", "flow_mirror")
API = "https://discord.com/api/v10"

# $META 655 C 10/16/2026 $20.6m (Bullish)   |   $ACN 225 P 11/21/2025 $2.6M (Bullish) STO
COMPACT_RE = re.compile(
    r"\$([A-Z]{1,6})\s+(\d+(?:\.\d+)?)\s*([CP])\s+"
    r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s+\$([\d.]+)\s*([mMkK])"
    r"(?:\s*\((bullish|bearish)\))?",
    re.IGNORECASE,
)


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def snowflake_after(days: int = 2) -> str:
    ts_ms = int(
        (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).timestamp()
        * 1000
    )
    return str((ts_ms - 1420070400000) << 22)


def api_get(client: httpx.Client, path: str, params: dict | None = None):
    while True:
        r = client.get(path, params=params)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            print(f"rate-limited, sleeping {wait:.1f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


def resolve_label(client: httpx.Client, channel_id: str) -> str:
    try:
        return api_get(client, f"/channels/{channel_id}").get("name", channel_id)
    except Exception:
        return channel_id


def parse_compact(text: str):
    """Yield best-effort flow dicts from the compact one-line format."""
    for m in COMPACT_RE.finditer(text):
        sym, strike, cp, mo, day, yr, amt, unit, senti = m.groups()
        yr = yr if len(yr) == 4 else ("20" + yr)
        exp = f"{yr}-{int(mo):02d}-{int(day):02d}"
        mult = 1_000_000 if unit.upper() == "M" else 1_000
        yield {
            "symbol": sym,
            "strike_text": strike,
            "option_type": "call" if cp.upper() == "C" else "put",
            "contract": f"{sym} {exp} {strike}{cp.upper()}",
            "expiration_date": exp,
            "premium_usd": float(amt) * mult,
            "sentiment": (senti or "unknown").lower(),
        }


def main() -> int:
    token = os.environ.get("DISCORD_TOKEN", "")
    new_uri = os.environ.get("NEW_MDB_URI", "")
    if not token or not new_uri:
        print("DISCORD_TOKEN and NEW_MDB_URI are required", file=sys.stderr)
        return 2

    raw_cfg = os.environ.get("DISCORD_CHANNELS", "1386775915991797951:notableflow")
    channels: list[tuple[str, str | None]] = []
    for part in raw_cfg.split(","):
        part = part.strip()
        if not part:
            continue
        cid, _, label = part.partition(":")
        channels.append((cid.strip(), label.strip() or None))
    if not channels:
        print("DISCORD_CHANNELS is empty", file=sys.stderr)
        return 2

    dst = MongoClient(new_uri, serverSelectionTimeoutMS=20000)
    db = dst[DB_NAME]
    raw_coll = db["discord_raw"]
    flows_coll = db["option_flow_flows"]
    state_coll = db["_sync_state"]

    http = httpx.Client(
        base_url=API,
        headers={"Authorization": token, "User-Agent": "FlowMirror/1.0"},
        timeout=20,
    )

    total_raw = total_parsed = 0
    for cid, label in channels:
        label = label or resolve_label(http, cid)
        state = state_coll.find_one({"_id": f"discord:{cid}"}) or {}
        after = state.get("last_message_id") or snowflake_after()

        try:
            items = api_get(http, f"/channels/{cid}/messages", {"after": after, "limit": 100})
        except Exception as e:
            print(f"channel {label} ({cid}): fetch failed: {e}")
            continue
        items = sorted(items, key=lambda x: int(x["id"]))
        if not items:
            print(f"channel {label}: no new messages")
            continue

        for item in items:
            msg_id = item["id"]
            ts = item["timestamp"].replace("Z", "+00:00")
            content = item.get("content", "") or ""
            embeds = [
                {"title": e.get("title") or "", "description": e.get("description") or "",
                 "fields": e.get("fields") or [], "footer": (e.get("footer") or {}).get("text", "")}
                for e in item.get("embeds", [])
            ]
            raw_doc = {
                "message_id": msg_id,
                "channel_id": cid,
                "channel_label": label,
                "author": item.get("author", {}).get("username", ""),
                "content": content,
                "embeds": embeds,
                "timestamp": ts,
                "fetched_at": utcnow_iso(),
                "mirror_meta": {"synced_via": "discord-direct", "origin_channel": label},
            }
            res = raw_coll.update_one({"message_id": msg_id}, {"$set": raw_doc}, upsert=True)
            if res.upserted_id:
                total_raw += 1

            # best-effort parse -> option_flow_flows (skip if route A already has it)
            if flows_coll.count_documents({"source_refs.message_id": msg_id}, limit=1):
                continue
            for i, p in enumerate(parse_compact(content)):
                doc = {
                    "_id": f"dd_{msg_id}_{i}",
                    "symbol": p["symbol"],
                    "contract": p["contract"],
                    "expiration_date": p["expiration_date"],
                    "strike_text": p["strike_text"],
                    "option_type": p["option_type"],
                    "premium_usd": p["premium_usd"],
                    "flow_direction_label": p["sentiment"],
                    "channel_label": label,
                    "source": "discord",
                    "source_refs": [
                        {
                            "source": "discord",
                            "message_id": msg_id,
                            "channel_id": cid,
                            "channel_label": label,
                            "created_at": ts,
                        }
                    ],
                    "raw_content": content[:2000],
                    "first_seen_at": ts,
                    "parser": "mirror-compact-v1",
                    "mirror_meta": {
                        "synced_via": "discord-direct",
                        "synced_at": utcnow_iso(),
                        "origin_channel": label,
                        "parse_note": "best-effort compact format",
                    },
                }
                flows_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                total_parsed += 1

        state_coll.update_one(
            {"_id": f"discord:{cid}"},
            {"$set": {"last_message_id": items[-1]["id"], "channel_label": label,
                      "updated_at": utcnow_iso()}},
            upsert=True,
        )
        print(f"channel {label}: {len(items)} msgs, +{total_raw} raw")

    print(f"route=discord-direct raw_new={total_raw} parsed_new={total_parsed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
