from option_desk.providers import OpenD, ProviderError


def test_snapshot_batches_and_checks_each_contract(contract, monkeypatch):
    import sys
    from types import SimpleNamespace

    RET_OK = 0
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(RET_OK=RET_OK))

    provider = OpenD.__new__(OpenD)
    calls = []

    class Budget:
        def acquire(self, *args):
            calls.append("budget")

    second = contract.model_copy(update={"ticker": "MSFT"})

    class Frame:
        def to_dict(self, *args):
            return [
                {
                    "code": contract.futu,
                    "option_type": "CALL",
                    "option_strike_price": str(contract.strike),
                    "strike_time": str(contract.expiration),
                    "last_price": 2,
                    "option_implied_volatility": 40,
                    "bid_price": 1.9,
                    "ask_price": 2.1,
                },
                {"code": "US." + contract.ticker, "last_price": 250},
            ]

    class Context:
        def get_market_snapshot(self, codes):
            calls.append(codes)
            return RET_OK, Frame()

    provider.budget = Budget()
    provider.context = Context()
    result = provider.quote_many([contract, second])
    assert len(calls) == 2
    assert len(calls[1]) == 4
    assert result[contract.id].fields["price"].value == 2
    assert isinstance(result[second.id], ProviderError)


def test_history_cache_reuses_day_but_keeps_exact_minute(contract, monkeypatch):
    import sys
    from types import SimpleNamespace
    from datetime import datetime, UTC

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            RET_OK=0,
            KLType=SimpleNamespace(K_1M="1m"),
            AuType=SimpleNamespace(NONE="none"),
        ),
    )
    calls = []

    class Budget:
        def acquire(self, *args):
            calls.append("budget")

    class Frame:
        def to_dict(self, *args):
            return [
                {
                    "code": contract.futu,
                    "time_key": "2026-09-11 10:38:00",
                    "open": 1,
                    "high": 2,
                    "low": 1,
                    "close": 2,
                    "volume": 3,
                }
            ]

    class Context:
        def request_history_kline(self, *args, **kwargs):
            calls.append("request")
            return 0, Frame(), None

    p = OpenD.__new__(OpenD)
    p.context = Context()
    p.budget = Budget()
    assert (
        len(
            p.bars(
                contract,
                datetime(2026, 9, 11, 14, 38, tzinfo=UTC),
                datetime(2026, 9, 11, 14, 39, tzinfo=UTC),
            )
        )
        == 1
    )
    assert (
        p.bars(
            contract,
            datetime(2026, 9, 11, 14, 39, tzinfo=UTC),
            datetime(2026, 9, 11, 14, 40, tzinfo=UTC),
        )
        == []
    )
    assert calls == ["budget", "request"]


def test_expired_and_cooling_contracts_do_not_block_rotation(store, event):
    from datetime import datetime, UTC
    from unittest.mock import patch

    store.ingest(event)
    expired = event.model_copy(
        update={
            "external_id": "old",
            "contract": event.contract.model_copy(
                update={"expiration": datetime(2020, 1, 1).date()}
            ),
        }
    )
    store.ingest(expired)
    with patch("option_desk.store.now", return_value=datetime(2026, 9, 25, tzinfo=UTC)):
        assert [r["id"] for r in store.monitor_contracts(1)] == [event.contract.id]
        store.queue("current", event.contract.id)
        job = store.claim(kind="current")
        store.finish(job, "failed", "contract_not_found", delay=21600)
        assert store.monitor_contracts(1) == []
