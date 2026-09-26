from datetime import datetime, UTC
from option_desk.domain import Pack, value
from option_desk.close_reference import close_reference


def quote(
    contract, captured="2026-09-25T21:00:00+00:00", quoted="2026-09-25T19:59:00+00:00"
):
    return Pack(
        provider="opend",
        instrument_id=contract.id,
        received_at=datetime.fromisoformat(captured),
        quote_time=datetime.fromisoformat(quoted),
        fields={
            "iv": value(0.4, provider="opend", as_of=datetime.fromisoformat(quoted)),
            "open_interest": value(100, provider="opend"),
            "price": value(99, provider="opend"),
        },
    )


def test_same_day_after_close_only(contract):
    event = datetime(2026, 9, 25, 15, tzinfo=UTC)
    p = close_reference(contract, event, quote(contract))
    assert p.fields["iv"].origin == "session_close_proxy"
    assert p.fields["open_interest"].as_of is None
    assert "price" not in p.fields
    assert (
        close_reference(
            contract, event, quote(contract, captured="2026-09-25T19:59:00+00:00")
        )
        is None
    )
    assert (
        close_reference(
            contract, datetime(2026, 9, 24, 15, tzinfo=UTC), quote(contract)
        )
        is None
    )
    assert (
        close_reference(
            contract, event, quote(contract, captured="2026-09-28T15:00:00+00:00")
        )
        is None
    )


def test_close_supplement_preserves_source_and_is_frozen(store, event):
    event = event.model_copy(
        update={"fields": {"iv": value(0.3, origin="source_reported")}}
    )
    bid = store.ingest(event)
    p = quote(event.contract)
    assert store.supplement_close(p) == 1
    assert (
        store.supplement_close(p.model_copy(update={"fields": {"iv": value(0.9)}})) == 0
    )
    row = store.comparison(bid)
    assert row["baseline"]["fields"]["iv"]["value"] == "0.3"
    assert row["baseline"]["fields"]["open_interest"]["origin"] == "session_close_proxy"
    assert store.read_baseline(bid)["payload"] is None
