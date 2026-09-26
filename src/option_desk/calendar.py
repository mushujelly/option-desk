from datetime import timedelta
from functools import lru_cache
import pandas_market_calendars as mcal
from .domain import Contract


@lru_cache(maxsize=8)
def calendar(name):
    return mcal.get_calendar(name)


def locate(contract: Contract, event_time):
    cal = calendar(contract.calendar)
    day = event_time.date()
    schedule = cal.schedule(
        start_date=day - timedelta(days=20), end_date=day + timedelta(days=1)
    )
    previous = None
    for label, row in schedule.iterrows():
        opening, closing = (
            row.market_open.to_pydatetime(),
            row.market_close.to_pydatetime(),
        )
        if opening <= event_time < closing:
            minute = event_time.replace(second=0, microsecond=0)
            return {
                "basis": "event_minute",
                "session": str(label.date()),
                "start": minute,
                "end": minute + timedelta(minutes=1),
            }
        if closing <= event_time:
            previous = {
                "basis": "previous_session_proxy",
                "session": str(label.date()),
                "start": opening,
                "end": closing,
            }
    if previous is None:
        raise ValueError("calendar_session_unavailable")
    return previous
