from datetime import datetime, UTC
from decimal import Decimal
import pytest
from option_desk.parser import parse

DT = datetime(2026, 9, 25, 19, 30, tzinfo=UTC)


def alert(text):
    return parse("discord:test", "format-test", text, DT)[0]


@pytest.mark.parametrize(
    "title",
    ["Large Call Sweep Order", "2 Large Call Sweep Orders", "Call Golden Sweep"],
)
def test_sweep_fields(title):
    e = alert(
        f"{title}\nSymbol: MU\nStrike: 1075.0\nExpiration: 9/28/2026\nPremiums: 287.21K\nTime: 2026-09-25 14:15:41"
    )
    assert e.contract.ticker == "MU"
    assert e.contract.strike == 1075
    assert e.contract.right == "C"
    assert e.fields["premium"].value == 287210
    assert "contracts" not in e.fields  # Number of sweeps is not contract quantity.
    assert e.event_time == DT  # No invented timezone for an unzoned source time.


def test_comma_strike_and_bare_ticker():
    e = alert(
        "Call Sweep\nSymbol: LITE\nStrike: 1,000\nExpiration: 10/02/2026\nCall/Put: Call\nSize: 410\nPremium: $549.4K\nData by Cheddar Flow"
    )
    assert e.contract.strike == 1000
    assert e.fields["contracts"].value == 410
    e = alert("GTES 27 C 11/20/2026 $320k (Bullish)")
    assert e.contract.ticker == "GTES"
    assert e.fields["premium"].value == 320000


def test_fundspec_full_date_and_values():
    e = alert(
        "Bearish options flow in $VST\n$2.0M of Jan 21 2028 $170 calls traded at the bid. 900 contracts against 214 open interest."
    )
    assert str(e.contract.expiration) == "2028-01-21"
    assert e.fields["premium"].value == 2000000
    assert e.fields["contracts"].value == 900
    assert e.fields["open_interest"].value == 214
    assert e.fields["open_interest"].as_of is None
    assert "option_desk_expiry_inference" not in e.raw


def test_missing_year_is_event_year_even_if_already_past():
    e = alert(
        "Bearish options flow in $VST\n$2.1M of Jan 20 $150 puts traded at the ask. 1,300 contracts against 1,658 open interest."
    )
    assert str(e.contract.expiration) == "2026-01-20"
    assert e.raw["option_desk_expiry_inference"]["requires_market_validation"]
    assert "expiry_year_inferred=2026" in e.fields["premium"].evidence
    assert e.fields["contracts"].value == 1300


@pytest.mark.parametrize(
    "text,ticker,strike,expiry",
    [
        (
            "Someone is buying $HOOD 145 call strike expiring 9/17/2027 for $2.7M",
            "HOOD",
            145,
            "2027-09-17",
        ),
        (
            "Massive $AAPL leap for $8.6M on the 335 call strike for 9/17/2027",
            "AAPL",
            335,
            "2027-09-17",
        ),
        (
            "Discovery Print detected: $AAPL 335 Calls 09/14/26 @ 1.32 - Size: 2963x",
            "AAPL",
            335,
            "2026-09-14",
        ),
    ],
)
def test_prose_formats(text, ticker, strike, expiry):
    e = alert(text)
    assert (e.contract.ticker, e.contract.strike, str(e.contract.expiration)) == (
        ticker,
        Decimal(strike),
        expiry,
    )


def test_does_not_accept_news_or_silently_pick_one_contract():
    with pytest.raises(ValueError):
        alert("Earnings call: $CPB EPS 39C on 9/17/2026")
    with pytest.raises(ValueError, match="multiple_events_require_stable_ids"):
        alert("GTES 27 C 11/20/2026\nAAPL 335 C 09/14/26")
