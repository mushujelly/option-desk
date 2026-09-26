"""A single after-close reference for otherwise missing baseline fields."""

from datetime import timedelta
from zoneinfo import ZoneInfo
from functools import lru_cache
from .calendar import calendar
from .domain import Pack

CLOSE_FIELDS = {
    "iv",
    "bid",
    "ask",
    "mid",
    "delta",
    "gamma",
    "theta",
    "vega",
    "spread",
    "volume",
    "open_interest",
    "volume_oi",
}


@lru_cache(maxsize=128)
def session_schedule(name, day):
    return calendar(name).schedule(
        start_date=day - timedelta(days=20), end_date=day + timedelta(days=14)
    )


def close_reference(contract, event_time, pack):
    eastern = ZoneInfo("America/New_York")
    day = event_time.astimezone(eastern).date()
    schedule = session_schedule(contract.calendar, day)
    rows = [
        (
            label.date(),
            row.market_open.to_pydatetime(),
            row.market_close.to_pydatetime(),
        )
        for label, row in schedule.iterrows()
    ]
    past = [r for r in rows if r[0] <= day]
    if not past or not pack.quote_time or not pack.matched:
        return None
    session, _, closing = past[-1]
    following = next((r[1] for r in rows if r[0] > session), None)
    if (
        not following
        or not closing + timedelta(minutes=5) <= pack.received_at < following
    ):
        return None
    if (
        pack.quote_time.astimezone(eastern).date() != session
        or pack.quote_time > pack.received_at
    ):
        return None
    fields = {}
    for key in CLOSE_FIELDS:
        v = pack.fields.get(key)
        if v is None or v.value is None:
            continue
        if v.as_of and v.as_of.astimezone(eastern).date() != session:
            continue
        fields[key] = v.model_copy(
            update={
                "origin": "session_close_proxy",
                "evidence": f"after_close_observation; session={session}; captured_at={pack.received_at.isoformat()}; effective_as_of={v.as_of.isoformat() if v.as_of else 'unknown'}",
            }
        )
    if not fields:
        return None
    return Pack(
        provider=pack.provider,
        instrument_id=pack.instrument_id,
        fields=fields,
        quote_time=pack.quote_time,
        received_at=pack.received_at,
        delayed=pack.delayed,
        time_basis="session_close_proxy",
        evidence={
            "session": str(session),
            "captured_at": pack.received_at.isoformat(),
            "basis": "observed_after_close_not_official_auction",
            "effective_oi_date": "provider_value_or_unknown",
        },
    )
