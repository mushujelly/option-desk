"""Conservative parser: structured contracts / OCC / explicit ISO-date text only."""

import re
import json
from datetime import datetime, date
from decimal import Decimal
from .domain import SourceEvent, Contract, value, digest

OCC = re.compile(r"(?<![A-Z0-9])(?:O:)?([A-Z]{1,6})\s*(\d{6})([CP])(\d{8})(?!\d)")
EXPLICIT = re.compile(
    r"\$?([A-Z][A-Z0-9.\-]{0,9})\s+(\d{4}-\d{2}-\d{2})\s+\$?(\d+(?:\.\d+)?)\s*(CALL|PUT|[CP])\b",
    re.I,
)


def parse(
    source,
    external_id,
    content,
    event_time,
    raw=None,
    author="",
    url="",
    revision="original",
):
    if event_time is None or event_time.tzinfo is None:
        raise ValueError("missing_event_time")
    # Discord twitter embeds escape dots (28\.5, \.76avg); normalize before matching.
    parse_content = content.replace("\\.", ".")
    cheddar = "Data by Cheddar Flow" in content
    if cheddar:
        labels = dict(re.findall(r"(?m)^([^:\n]+):\s*([^\n]+)", content))
        if all(k in labels for k in ("Symbol", "Strike", "Expiration", "Call/Put")):
            expiry = datetime.strptime(labels["Expiration"].strip(), "%m/%d/%Y").date()
            parse_content = (
                f"{labels['Symbol'].strip()} {expiry} {labels['Strike'].strip()} {labels['Call/Put'].strip()[0]}\n"
                + parse_content
            )
            ts = re.findall(r"<t:(\d+):[A-Za-z]>", content)
            if len(set(ts)) == 1:
                from .domain import UTC

                supplied_time = datetime.fromtimestamp(int(ts[0]), UTC)
                if supplied_time <= event_time:
                    event_time = supplied_time
    # Full-year compact contracts. Missing-year expiry remains diagnostic.
    compact = re.compile(
        r"\$([A-Z][A-Z0-9.\-]{0,6})\s+\$?(\d+(?:\.\d+)?)\s*([CP])\s+(\d{1,2}/\d{1,2}/\d{4})\b",
        re.I,
    )
    # Positional premium / avg-fill: "$TXN 205 C 08/01/2025 $1.1M (Bullish)" carries
    # the premium as the first $K/M/B token after the date, with no "Premium:"
    # label, so the generic labeled-field pass below never sees it. Capture it
    # per compact match, keyed by the expanded contract identity. Same-line only.
    positional = {}

    def expand(m):
        ticker, strike, right, expiry = m.groups()
        exp_date = datetime.strptime(expiry, "%m/%d/%Y").date()
        line_end = parse_content.find("\n", m.end())
        tail = parse_content[m.end() : line_end if line_end != -1 else len(parse_content)]
        found = {}
        pm = re.search(r"\$([\d,]+(?:\.\d+)?)\s*([KMB])\b", tail)
        if pm:
            amount = Decimal(pm.group(1).replace(",", "")) * {
                "K": 1000,
                "M": 1000000,
                "B": 1000000000,
            }[pm.group(2).upper()]
            found["premium"] = (amount, "USD", None)
        am = re.search(r"(\d*\.?\d+)\s*avg\b", tail, re.I)
        if am:
            found["price"] = (Decimal(am.group(1)), "USD", "reported_average")
        if found:
            positional[(ticker.upper(), exp_date.isoformat(), strike, right.upper())] = found
        return f"{ticker} {exp_date} {strike} {right}"

    parse_content = compact.sub(expand, parse_content)
    structured = (raw or {}).get("contracts")
    contracts = []
    if structured:
        contracts = [
            (
                Contract.model_validate(r["contract"] if "contract" in r else r),
                r.get("event_id"),
                r.get("fields", {}),
            )
            for r in structured
        ]
    else:
        # One alert header with repeated chain links describes one aggregate alert.
        hot = list(
            re.finditer(
                r"\*\*\[([A-Z][A-Z0-9.\-]{0,6})\s+(\d+(?:\.\d+)?)\s+([CP])\s+(\d{2}/\d{2}/\d{4})\s+\(\d+\s+DTE\)\]",
                content,
                re.I,
            )
        )
        if len(hot) > 1:
            raise ValueError("multiple_events_require_stable_ids")
        if hot:
            ticker, strike, right, expiry = hot[0].groups()
            contract = Contract(
                ticker=ticker.upper(),
                expiration=datetime.strptime(expiry, "%m/%d/%Y").date(),
                strike=strike,
                right=right.upper(),
            )
            for link in OCC.finditer(content):
                root, exp, side, amount = link.groups()
                other = Contract(
                    ticker=root,
                    expiration=datetime.strptime(exp, "%y%m%d").date(),
                    strike=Decimal(amount) / 1000,
                    right=side,
                )
                if other.id != contract.id:
                    raise ValueError("incomplete_or_ambiguous_contract")
            fields = {}
            for label, name, unit in [
                ("Average Fill", "price", "USD"),
                ("Premium", "premium", "USD"),
                ("Open Interest", "open_interest", "contracts"),
                ("Overall Volume", "volume", "contracts"),
                ("Interval Volume", "interval_volume", "contracts"),
            ]:
                m = re.search(
                    r"(?:^|\n)" + label + r":\s*\$?([\d,]+(?:\.\d+)?)([KMB])?(?=\s|$)",
                    content,
                    re.I,
                )
                if m:
                    amount = Decimal(m[1].replace(",", "")) * {
                        "K": 1000,
                        "M": 1000000,
                        "B": 1000000000,
                    }.get((m[2] or "").upper(), 1)
                    fields[name] = value(
                        amount,
                        origin="source_reported",
                        provider=source,
                        as_of=None if name == "open_interest" else event_time,
                        unit=unit,
                        price_type="reported_average" if name == "price" else None,
                        evidence=f"{source}:{external_id}; aggregate alert, message timestamp",
                    ).model_dump(mode="json")
            contracts.append((contract, None, fields))
        for m in [] if hot else OCC.finditer(content):
            root, expiry, right, strike = m.groups()
            contracts.append(
                (
                    Contract(
                        ticker=root,
                        expiration=datetime.strptime(expiry, "%y%m%d").date(),
                        strike=Decimal(strike) / 1000,
                        right=right,
                    ),
                    None,
                    {},
                )
            )
        if not contracts:
            for m in EXPLICIT.finditer(parse_content):
                ticker, expiry, strike, right = m.groups()
                fields = {}
                for fname, (amount, unit, price_type) in positional.get(
                    (ticker.upper(), expiry, strike, right[0].upper()), {}
                ).items():
                    fields[fname] = value(
                        amount,
                        origin="source_reported",
                        provider=source,
                        as_of=event_time,
                        unit=unit,
                        price_type=price_type,
                        evidence=f"{source}:{external_id}; positional token after contract",
                    ).model_dump(mode="json")
                contracts.append(
                    (
                        Contract(
                            ticker=ticker.upper(),
                            expiration=date.fromisoformat(expiry),
                            strike=strike,
                            right=right[0].upper(),
                        ),
                        None,
                        fields,
                    )
                )
    if not contracts:
        raise ValueError("incomplete_or_ambiguous_contract")
    # Multiple event correspondence requires authoritative child IDs, not text position.
    if len(contracts) > 1 and any(not item[1] for item in contracts):
        raise ValueError("multiple_events_require_stable_ids")
    twitter = re.search(
        r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^/\s]+/status/(\d+)",
        url + " " + content,
    )
    original_ref = "x:" + twitter.group(1) if twitter else None
    if (
        cheddar
        and source.startswith("discord:")
        and (raw or {}).get("webhook_id")
        and len(set(re.findall(r"<t:(\d+):[A-Za-z]>", content))) == 1
    ):
        original_ref = "cheddar-alert:" + digest(
            json.dumps(
                {"source": source, "webhook": raw["webhook_id"], "content": content},
                sort_keys=True,
            )
        )
    # UW forwards retain the original embed timestamp and full alert payload.
    # Scope fingerprint to this source/webhook; no timestamp rounding or contract-only merge.
    embeds = (raw or {}).get("embeds", [])
    if (
        not original_ref
        and source.startswith("discord:")
        and len(embeds) == 1
        and (raw or {}).get("webhook_id")
    ):
        embed = embeds[0]
        if "unusualwhales.com/flow/option_chains?chain=" in str(
            embed.get("description", "")
        ) and embed.get("timestamp"):
            try:
                original_time = datetime.fromisoformat(
                    embed["timestamp"].replace("Z", "+00:00")
                )
                if original_time.tzinfo is not None and original_time <= event_time:
                    identity = {
                        "source": source,
                        "webhook": raw["webhook_id"],
                        "timestamp": original_time.isoformat(),
                        "title": embed.get("title"),
                        "description": embed.get("description"),
                        "fields": embed.get("fields", []),
                        "content": raw.get("content", ""),
                    }
                    original_ref = "uw-alert:" + digest(
                        json.dumps(identity, sort_keys=True)
                    )
                    event_time = original_time
                    for _, _, fields in contracts:
                        for field in fields.values():
                            if field.get("as_of"):
                                field["as_of"] = original_time.isoformat()
                            field["evidence"] = (
                                f"{source}:{external_id}; aggregate alert, original embed timestamp"
                            )
            except (ValueError, TypeError):
                pass
    output = []
    for contract, child, fields in contracts:
        if not fields:
            for name, pattern, unit in [
                ("price", r"(?:price|成交价)\s*[:=@]?\s*\$?([\d,.]+)", "USD"),
                (
                    "premium",
                    r"(?:premium|权利金)\s*[:=]?\s*\$?([\d,.]+)\s*([KMB])?",
                    "USD",
                ),
                (
                    "contracts",
                    r"(?:contracts|张数|Size)\s*[:=]?\s*([\d,.]+)",
                    "contracts",
                ),
            ]:
                m = re.search(pattern, content, re.I)
                if m:
                    v = Decimal(m[1].replace(",", ""))
                    suffix = m[2].upper() if m.lastindex == 2 and m[2] else ""
                    v *= {"K": 1000, "M": 1000000, "B": 1000000000}.get(suffix, 1)
                    fields[name] = value(
                        v,
                        origin="source_reported",
                        provider=source,
                        as_of=event_time,
                        unit=unit,
                        price_type="reported_trade" if name == "price" else None,
                        evidence=f"{source}:{external_id}",
                    ).model_dump(mode="json")
        output.append(
            SourceEvent(
                source=source,
                external_id=external_id,
                revision=revision,
                original_ref=original_ref,
                subevent_id=child or "single",
                event_time=event_time,
                author=author,
                url=url,
                content=content,
                contract=contract,
                fields=fields,
                raw=raw or {},
            )
        )
    return output
