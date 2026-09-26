from datetime import datetime
from decimal import Decimal
import pytest
from pydantic import ValidationError
from option_desk.domain import Contract, SourceEvent, Pack, value, compare, UTC
from option_desk.parser import parse
from option_desk.calendar import locate


def test_exact_identity(contract):
    assert contract.id == contract.model_copy(update={"strike": Decimal("260.000")}).id
    assert contract.occ == "AAPL  261016C00260000"
    assert contract.futu == "US.AAPL261016C260000"


@pytest.mark.parametrize(
    "updates", [{"standard": False}, {"root": "AAPL1"}, {"multiplier": 10}]
)
def test_adjusted_contract_not_assumed(contract, updates):
    with pytest.raises(ValidationError):
        Contract.model_validate({**contract.model_dump(), **updates})


def test_naive_event_rejected(contract):
    with pytest.raises(ValidationError):
        SourceEvent(
            source="test",
            external_id="a",
            contract=contract,
            event_time="2026-09-25T10:00:00",
        )


def test_parser_ambiguous_year_not_guessed():
    with pytest.raises(ValueError):
        parse("test", "a", "AAPL 10/16 260 C", datetime(2026, 9, 25, 18, tzinfo=UTC))


def test_parser_x_cross_source_identity():
    args = (
        "AAPL 2026-10-16 260 C price: 3.5 premium: 1.2M https://x.com/author/status/12345",
        datetime(2026, 9, 25, 18, tzinfo=UTC),
    )
    a = parse("discord:1", "a", *args)[0]
    b = parse("mongo:tweets", "b", *args)[0]
    assert a.identity_key == b.identity_key
    assert a.fields["premium"].value == 1200000


def test_multievent_without_ids_rejected():
    with pytest.raises(ValueError, match="stable_ids"):
        parse(
            "test",
            "a",
            "AAPL 2026-10-16 260 C\nAAPL 2026-10-16 265 C",
            datetime(2026, 9, 25, 18, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "when,session,basis",
    [
        ("2026-09-26T14:00:00+00:00", "2026-09-25", "previous_session_proxy"),
        ("2026-09-25T22:00:00+00:00", "2026-09-25", "previous_session_proxy"),
        ("2026-09-25T12:00:00+00:00", "2026-09-24", "previous_session_proxy"),
        ("2026-09-25T18:00:00+00:00", "2026-09-25", "event_minute"),
        ("2026-11-27T18:30:00+00:00", "2026-11-27", "previous_session_proxy"),
        ("2026-03-09T13:30:00+00:00", "2026-03-09", "event_minute"),
        ("2026-03-06T14:30:00+00:00", "2026-03-06", "event_minute"),
    ],
)
def test_calendar(contract, when, session, basis):
    w = locate(contract, datetime.fromisoformat(when))
    assert w["session"] == session
    assert w["basis"] == basis


def test_null_not_zero():
    assert value(None).value is None
    assert value(0).value == 0
    assert value(float("nan")).reason == "invalid"


@pytest.mark.parametrize(
    "left,right,key,expected",
    [
        (value(2, provider="a"), value(3, provider="a"), "price", "comparable"),
        (
            value(2, provider="a", origin="minute_close_estimate"),
            value(3, provider="a"),
            "price",
            "estimate",
        ),
        (value(2, provider="a"), value(3, provider="b"), "price", "incomparable"),
        (
            value(
                2,
                provider="source",
                origin="source_reported",
                price_type="reported_trade",
            ),
            value(3, provider="a", price_type="mark"),
            "price",
            "estimate",
        ),
        (
            value(0.2, provider="a", method="m", unit="ratio"),
            value(0.3, provider="a", method="m", unit="ratio"),
            "iv",
            "comparable",
        ),
        (
            value(0.2, provider="a", method="m", unit="ratio", origin="model_estimate"),
            value(0.3, provider="a", unit="ratio"),
            "iv",
            "incomparable",
        ),
        (value(None), value(3, provider="a"), "price", "incomparable"),
        (
            value(2, provider="a", unit="USD"),
            value(3, provider="a", unit="HKD"),
            "price",
            "incomparable",
        ),
    ],
)
def test_comparison_matrix(left, right, key, expected):
    a = Pack(provider="a", instrument_id="i", fields={key: left})
    b = Pack(provider="a", instrument_id="i", fields={key: right})
    assert compare(a, b)[key]["status"] == expected
