import pytest
from datetime import datetime
from option_desk.domain import Bar, UTC, value
from option_desk.baseline import build


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(
        "option_desk.baseline.now", lambda: datetime(2026, 9, 26, 12, tzinfo=UTC)
    )


class FixtureProvider:
    name = "fixture"

    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    def bars(self, contract, start, end, underlying=False):
        self.requests.append((start, end, underlying))
        return self.rows


def bar(time, **kwargs):
    return Bar(
        start=datetime.fromisoformat(time),
        open=2,
        high=4,
        low=1,
        close=3,
        volume=20,
        trade_semantics_verified=True,
        **kwargs,
    )


def test_intraday_exact_minute(contract):
    p = FixtureProvider(
        [bar("2026-09-25T18:00:00+00:00"), bar("2026-09-25T18:01:00+00:00")]
    )
    status, pack, reason = build(
        contract, datetime(2026, 9, 25, 18, 0, 42, tzinfo=UTC), {}, [p]
    )
    assert status == "ready"
    assert pack.fields["price"].value == 3
    assert pack.fields["price"].origin == "minute_close_estimate"
    assert pack.evidence["bar"]["start"].startswith("2026-09-25T18:00")


def test_no_adjacent_intraday(contract):
    p = FixtureProvider([bar("2026-09-25T18:01:00+00:00")])
    status, pack, _ = build(contract, datetime(2026, 9, 25, 18, tzinfo=UTC), {}, [p])
    assert status == "unavailable"
    assert "price" not in pack.fields


def test_source_precedes_bar(contract):
    p = FixtureProvider([bar("2026-09-25T18:00:00+00:00")])
    fields = {
        "price": value(
            2, origin="source_reported", price_type="reported_trade"
        ).model_dump(mode="json")
    }
    status, pack, _ = build(
        contract, datetime(2026, 9, 25, 18, tzinfo=UTC), fields, [p]
    )
    assert pack.fields["price"].value == 2
    assert pack.evidence["validation"] == "consistent"


def test_afterhours_last_real_bar(contract):
    p = FixtureProvider(
        [
            bar("2026-09-25T18:00:00+00:00"),
            bar("2026-09-25T19:00:00+00:00", filled=True),
        ]
    )
    status, pack, _ = build(contract, datetime(2026, 9, 25, 23, tzinfo=UTC), {}, [p])
    assert status == "ready"
    assert pack.time_basis == "previous_session_proxy"
    assert pack.evidence["bar"]["start"].startswith("2026-09-25T18:00")
    assert pack.evidence["validation"] == "insufficient_data"


def test_afterhours_no_history_discards_even_source_price(contract):
    p = FixtureProvider([])
    status, pack, reason = build(
        contract,
        datetime(2026, 9, 25, 23, tzinfo=UTC),
        {"price": value(3, origin="source_reported").model_dump(mode="json")},
        [p],
    )
    assert (status, pack, reason) == (
        "discarded",
        None,
        "no_valid_bar_in_latest_session",
    )
    assert p.requests[0][0].date() == datetime(2026, 9, 25).date()


def test_aggregate_average_does_not_validate_as_single_trade(contract):
    p = FixtureProvider([bar("2026-09-25T18:00:00+00:00")])
    fields = {
        "price": value(
            99, origin="source_reported", price_type="reported_average"
        ).model_dump(mode="json")
    }
    status, pack, _ = build(
        contract, datetime(2026, 9, 25, 18, tzinfo=UTC), fields, [p]
    )
    assert status == "ready"
    assert pack.fields["price"].value == 99
    assert pack.evidence["validation"] == "insufficient_data"
