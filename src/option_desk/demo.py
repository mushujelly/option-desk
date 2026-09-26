from datetime import datetime, date
from .domain import Contract, SourceEvent, Pack, value, now, UTC, metrics
from .store import baselines
from sqlalchemy import update


def seed(store):
    examples = [
        ("NVDA", 180, "C", 4.8, 5.35, 0.48, 0.52, 8200, 4900),
        ("AAPL", 260, "C", 3.15, 3.02, 0.29, 0.31, 2400, 8800),
        ("TSLA", 420, "P", 12.4, 13.6, 0.68, 0.72, 4100, 3100),
        ("SPY", 700, "C", 2.1, 2.65, 0.18, 0.19, 28500, 44200),
    ]
    for index, (
        ticker,
        strike,
        right,
        initial,
        latest,
        iv0,
        iv1,
        volume,
        oi,
    ) in enumerate(examples):
        c = Contract(
            ticker=ticker, expiration=date(2026, 10, 16), strike=strike, right=right
        )
        t = datetime(2026, 9, 25, 18, index * 7, tzinfo=UTC)
        event = SourceEvent(
            source="DEMO",
            external_id=f"synthetic-{index}",
            event_time=t,
            author="演示数据 · 非真实消息",
            contract=c,
            content=f"{ticker} {c.expiration} {strike}{right} · 人工合成数据，用于验证页面和数据流程。",
            fields={
                "price": value(
                    initial,
                    origin="source_reported",
                    provider="demo",
                    as_of=t,
                    price_type="reported_trade",
                ),
                "premium": value(
                    initial * 500 * 100,
                    origin="source_reported",
                    provider="demo",
                    as_of=t,
                ),
            },
        )
        bid = store.ingest(event)
        pack = Pack(
            provider="demo",
            instrument_id=c.id,
            quote_time=t,
            time_basis="event_minute",
            fields={
                "price": value(
                    initial,
                    origin="minute_close_estimate",
                    provider="demo",
                    as_of=t,
                    price_type="minute_close",
                ),
                "iv": value(
                    iv0, provider="demo", as_of=t, unit="ratio", method="synthetic"
                ),
                "underlying_price": value(
                    strike - 2, provider="demo", as_of=t, price_type="last"
                ),
                "premium": event.fields["premium"],
            },
            evidence={"synthetic": True, "validation": "demo_only"},
        )
        with store.engine.begin() as conn:
            conn.execute(
                update(baselines)
                .where(baselines.c.id == bid, baselines.c.status == "pending")
                .values(status="ready", payload=pack.model_dump(mode="json"))
            )
        curr = Pack(
            provider="demo",
            instrument_id=c.id,
            quote_time=now(),
            fields={
                "price": value(latest, provider="demo", as_of=now(), price_type="mark"),
                "bid": value(latest - 0.05, provider="demo"),
                "ask": value(latest + 0.05, provider="demo"),
                "iv": value(iv1, provider="demo", unit="ratio", method="synthetic"),
                "volume": value(volume, provider="demo", unit="contracts"),
                "open_interest": value(oi, provider="demo", unit="contracts"),
                "underlying_price": value(
                    strike - 1, provider="demo", price_type="last"
                ),
            },
            evidence={"synthetic": True},
        )
        store.put_current(metrics(curr, c))
