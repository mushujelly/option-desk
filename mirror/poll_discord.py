#!/usr/bin/env python3
"""Route B: poll Discord channels directly -> new cluster.

For each configured channel:
  1. Fetch messages after the stored watermark (Discord snowflake cursor).
  2. Keep only options-related messages (contract patterns, options keywords,
     or flow-visual attachments/embeds). Pure-text noise (political posts,
     news headlines, chatter) is skipped entirely and never stored.
  3. Store the RAW message in `discord_raw`, tagged with channel_id/channel_label
     (unique on message_id -> re-runs never duplicate raw).
  4. Best-effort parse of the compact flow format
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
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
from pymongo import ASCENDING, MongoClient

DB_NAME = os.environ.get("MIRROR_DB", "flow_mirror")
API = "https://discord.com/api/v10"
MAX_OCR_PER_RUN = int(os.environ.get("MIRROR_MAX_OCR", "20"))
TESSERACT = shutil.which("tesseract")

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


# Options-relevance gate: only messages that look like options flow are kept.
# Matches contracts ($META 655 C 10/16/2026), options keywords, or flow visuals
# (image attachments / image embeds such as Bullflow cards, UW tables).
# Pure-text noise (political posts, news headlines, chatter) is skipped entirely.
OPTIONS_KW_RE = re.compile(
    r"\b(calls?|puts?|strike|expir(?:y|ation|ies)|premium|sweeps?|bullish|bearish"
    r"|unusual\s+flow|option\s*flow|dark\s*pool)\b"
    r"|\b\d+(?:\.\d+)?\s*[CP]\b"          # 205C / 755 P
    r"|\b[CP]\s*\d{1,2}/\d{1,2}/\d{2,4}",  # C 08/01/2025
    re.IGNORECASE,
)


# Phrases that look options-ish but aren't (earnings calls, insurance premiums...)
# are stripped before the relevance check so they can't false-positive.
NON_OPTIONS_PHRASE_RE = re.compile(
    r"\b(earnings|conference|phone|zoom|video)\s+calls?\b"
    r"|\b(insurance|health)\s+premiums?\b",
    re.IGNORECASE,
)


def _embed_text(e: dict) -> str:
    fields = e.get("fields") or []
    return " ".join([
        e.get("title") or "",
        e.get("description") or "",
        " ".join(f.get("value", "") for f in fields if isinstance(f, dict)),
        (e.get("footer") or {}).get("text", "") if isinstance(e.get("footer"), dict) else "",
    ])


def is_options_related(item: dict) -> bool:
    """True if the message looks like options-flow content."""
    content = NON_OPTIONS_PHRASE_RE.sub(" ", item.get("content", "") or "")
    if COMPACT_RE.search(content) or OPTIONS_KW_RE.search(content):
        return True
    for e in item.get("embeds", []) or []:
        if not isinstance(e, dict):
            continue
        text = NON_OPTIONS_PHRASE_RE.sub(" ", _embed_text(e))
        if COMPACT_RE.search(text) or OPTIONS_KW_RE.search(text):
            return True
        if e.get("image") or e.get("thumbnail") or e.get("video"):
            return True  # flow cards / tables are visuals; keep for OCR later
    if item.get("attachments"):
        return True  # image attachments are flow visuals
    return False


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


# --- Bullflow card OCR parsing ------------------------------------------------
# Card layout (tesseract --psm 6):
#   OKTA 197.5 Call
#   Exp. 10/02/26            $5.82
#   Ask: 704  Bid: 22  Mid: 2   Vol: 732   OI: 29
#   Prem: $403.3K   OTM: 0.9%   Multi: 0%
BF_HEADER_RE = re.compile(r"\b([A-Z]{1,6})\s+(\d+(?:\.\d+)?)\s+(Call|Put)\b")
BF_EXP_RE = re.compile(r"Exp\.\s*(\d{1,2})/(\d{1,2})/(\d{2,4})")
BF_PREM_RE = re.compile(r"Prem:\s*\$\s*([\d,]+(?:\.\d+)?)\s*([KM])", re.IGNORECASE)
BF_ABM_RE = re.compile(r"Ask:\s*(\d+)\D+Bid:\s*(\d+)\D+Mid:\s*(\d+)")
BF_VOL_RE = re.compile(r"\bVol:\s*(\d+)")
BF_OI_RE = re.compile(r"\bOI:\s*(\d+)")


def parse_bullflow_card(text: str):
    """Parse tesseract output of a Bullflow options card. Returns flow dict or None."""
    if not text:
        return None
    m = BF_HEADER_RE.search(text)
    if not m:
        return None
    # Require card structure (Exp. and/or Prem:) to avoid false positives.
    if not (BF_EXP_RE.search(text) or BF_PREM_RE.search(text)):
        return None
    sym, strike, cp = m.groups()
    cp_low = cp.lower()
    exp = ""
    me = BF_EXP_RE.search(text)
    if me:
        mo, day, yr = me.groups()
        yr = yr if len(yr) == 4 else "20" + yr
        exp = f"{yr}-{int(mo):02d}-{int(day):02d}"
    premium = 0.0
    mp = BF_PREM_RE.search(text)
    if mp:
        amt, unit = mp.groups()
        premium = float(amt.replace(",", "")) * (1_000_000 if unit.upper() == "M" else 1_000)
    ask = bid = mid = 0
    mab = BF_ABM_RE.search(text)
    if mab:
        ask, bid, mid = map(int, mab.groups())
    vol = int(BF_VOL_RE.search(text).group(1)) if BF_VOL_RE.search(text) else 0
    oi = int(BF_OI_RE.search(text).group(1)) if BF_OI_RE.search(text) else 0
    # Side: ask-heavy = contracts were bought. Calls bought = bullish,
    # puts bought = bearish.
    sentiment = "unknown"
    if ask + bid > 0:
        bought = ask >= bid
        sentiment = ("bullish" if bought else "bearish") if cp_low == "call" \
            else ("bearish" if bought else "bullish")
    return {
        "symbol": sym,
        "strike_text": strike,
        "option_type": cp_low,
        "contract": f"{sym} {exp} {strike}{cp[0].upper()}".strip(),
        "expiration_date": exp,
        "premium_usd": premium,
        "sentiment": sentiment,
        "ask_count": ask,
        "bid_count": bid,
        "mid_count": mid,
        "volume": vol,
        "open_interest": oi,
    }


def collect_image_urls(item: dict) -> list[str]:
    """Image attachment / embed URLs from a Discord message."""
    urls: list[str] = []
    for a in item.get("attachments", []) or []:
        if not isinstance(a, dict):
            continue
        ct = (a.get("content_type") or "").lower()
        url = a.get("url", "")
        if url and (ct.startswith("image/") or re.search(r"\.(png|jpe?g|webp)(\?|$)", url, re.I)):
            urls.append(url)
    for e in item.get("embeds", []) or []:
        if not isinstance(e, dict):
            continue
        for key in ("image", "thumbnail"):
            im = e.get(key) or {}
            if isinstance(im, dict) and im.get("url"):
                urls.append(im["url"])
    return list(dict.fromkeys(urls))


def ocr_url(client: httpx.Client, url: str) -> str:
    """Download an image and OCR it with tesseract. Returns text ('' on failure)."""
    if not TESSERACT:
        return ""
    try:
        r = client.get(url, timeout=30)
        r.raise_for_status()
        if len(r.content) > 8 * 1024 * 1024:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            f.write(r.content)
            path = f.name
        try:
            out = subprocess.run(
                [TESSERACT, path, "stdout", "--psm", "6", "-l", "eng"],
                capture_output=True, text=True, timeout=120,
            )
            return out.stdout.strip()
        finally:
            os.unlink(path)
    except Exception as e:
        print(f"ocr failed for {url[:70]}: {e}")
        return ""


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

    total_raw = total_parsed = total_skipped = total_ocr = 0
    ocr_budget = MAX_OCR_PER_RUN
    if not TESSERACT:
        print("tesseract not found, OCR disabled", file=sys.stderr)
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
            if not is_options_related(item):
                total_skipped += 1
                continue
            ts = item["timestamp"].replace("Z", "+00:00")
            content = item.get("content", "") or ""

            # OCR image attachments/embeds (Bullflow cards, UW tables, ...).
            # Download at ingest time: Discord CDN links expire.
            ocr_text = ""
            if ocr_budget > 0:
                for url in collect_image_urls(item):
                    if ocr_budget <= 0:
                        break
                    ocr_budget -= 1
                    txt = ocr_url(http, url)
                    if txt:
                        ocr_text += "\n" + txt
                        total_ocr += 1
            ocr_text = ocr_text.strip()

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
                "ocr_text": ocr_text[:4000],
                "mirror_meta": {"synced_via": "discord-direct", "origin_channel": label},
            }
            res = raw_coll.update_one({"message_id": msg_id}, {"$set": raw_doc}, upsert=True)
            if res.upserted_id:
                total_raw += 1

            # best-effort parse -> option_flow_flows (skip if route A already has it)
            if flows_coll.count_documents({"source_refs.message_id": msg_id}, limit=1):
                continue
            # candidates: (tag, parsed_dict); dedupe by contract key, first wins
            candidates: list[tuple[str, dict]] = [("t", p) for p in parse_compact(content)]
            if ocr_text:
                bf = parse_bullflow_card(ocr_text)
                if bf:
                    candidates.append(("ocard", bf))
                else:
                    candidates += [("ot", p) for p in parse_compact(ocr_text)]
            seen_keys: set[tuple] = set()
            di = 0
            for tag, p in candidates:
                key = (p["symbol"], p["expiration_date"], p["strike_text"], p["option_type"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                parser = {"t": "mirror-compact-v1", "ot": "mirror-compact-ocr-v1",
                          "ocard": "mirror-bullflow-ocr-v1"}[tag]
                doc = {
                    "_id": f"dd_{msg_id}_{tag}{di}",
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
                    "parser": parser,
                    "mirror_meta": {
                        "synced_via": "discord-direct",
                        "synced_at": utcnow_iso(),
                        "origin_channel": label,
                        "parse_note": "best-effort" + (" + ocr" if tag != "t" else ""),
                    },
                }
                for extra in ("ask_count", "bid_count", "mid_count", "volume", "open_interest"):
                    if p.get(extra) is not None:
                        doc[extra] = p[extra]
                if tag != "t":
                    doc["ocr_text"] = ocr_text[:2000]
                flows_coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                total_parsed += 1
                di += 1

        state_coll.update_one(
            {"_id": f"discord:{cid}"},
            {"$set": {"last_message_id": items[-1]["id"], "channel_label": label,
                      "updated_at": utcnow_iso()}},
            upsert=True,
        )
        print(f"channel {label}: {len(items)} msgs, +{total_raw} raw, {total_skipped} skipped (non-options)")

    print(f"route=discord-direct raw_new={total_raw} parsed_new={total_parsed} skipped={total_skipped} ocr_images={total_ocr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
