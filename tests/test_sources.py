from option_desk.sources import discord_authorization


def test_discord_bot_authorization():
    assert discord_authorization("fake-token") == "Bot fake-token"
    assert discord_authorization(" Bot fake-token ") == "Bot fake-token"


def test_aggregate_alert_repeated_links_and_comma_values():
    from datetime import datetime, UTC
    from decimal import Decimal
    from option_desk.parser import parse

    text = (
        "**[PGY 20 C 10/16/2026 (35 DTE)](https://example.com?chain=PGY261016C00020000)**\n"
        "[Time and sales](https://example.com?chain=PGY261016C00020000)\n"
        "Interval Volume: 1,001\nOpen Interest: 113\nPremium: $183,020\nAverage Fill: $1.83"
    )
    events = parse("discord:test", "1", text, datetime(2026, 9, 11, 14, 40, tzinfo=UTC))
    assert len(events) == 1
    event = events[0]
    assert event.contract.ticker == "PGY"
    assert event.fields["premium"].value == Decimal("183020")
    assert event.fields["price"].price_type == "reported_average"
    assert event.fields["interval_volume"].value == 1001
    assert "contracts" not in event.fields


def test_forwarded_alert_identity_and_legacy_upgrade(store):
    from datetime import datetime, UTC
    from option_desk.parser import parse
    from option_desk.sources import discord_text

    description = "**[PGY 20 C 10/16/2026 (35 DTE)](https://unusualwhales.com/flow/option_chains?chain=PGY261016C00020000)**\nAverage Fill: $1.83"
    raw = {
        "webhook_id": "webhook",
        "embeds": [
            {
                "title": "Interval (5 Min)",
                "description": description,
                "timestamp": "2026-09-11T14:38:16.121Z",
            }
        ],
    }
    dt = datetime(2026, 9, 11, 14, 40, tzinfo=UTC)

    def event(id, payload):
        return parse("discord:test", id, discord_text(payload), dt, payload)[0]

    first = event("1", raw)
    legacy = first.model_copy(update={"original_ref": None, "event_time": dt})
    old = store.ingest(legacy)
    canonical = store.ingest(first)
    assert store.ingest(event("2", raw)) == canonical
    assert store.ingest(first) == canonical
    assert len(store.list_baselines()) == 1
    assert store.comparison(old)["applicability"] == "superseded"
    assert first.event_time.isoformat() == "2026-09-11T14:38:16.121000+00:00"
    later = {
        **raw,
        "embeds": [{**raw["embeds"][0], "timestamp": "2026-09-11T14:39:16.121Z"}],
    }
    assert event("3", later).identity_key != first.identity_key
    changed = {
        **raw,
        "embeds": [
            {**raw["embeds"][0], "description": description.replace("1.83", "1.84")}
        ],
    }
    assert event("4", changed).identity_key != first.identity_key
    missing = {
        **raw,
        "embeds": [{k: v for k, v in raw["embeds"][0].items() if k != "timestamp"}],
    }
    assert event("5", missing).identity_key != event("6", missing).identity_key


def test_cheddar_and_compact_full_year():
    from datetime import datetime, UTC
    from option_desk.parser import parse

    dt = datetime(2026, 9, 25, 20, tzinfo=UTC)
    text = "Call Sweep\nSymbol: NVDA\nStrike: 225\nExpiration: 09/25/2026\nCall/Put: Call\nSize: 3,542\nPremium: $531.3K\nData by Cheddar Flow - <t:1790343118:R>"
    e = parse("discord:1", "1", text, dt)[0]
    assert e.fields["contracts"].value == 3542
    assert e.fields["premium"].value == 531300
    assert e.event_time == datetime.fromtimestamp(1790343118, UTC)
    e = parse("discord:1", "2", "$GOOG 255 C 10/03/2025 $468K", dt)[0]
    assert str(e.contract.expiration) == "2025-10-03"


def test_backfill_checkpoint_and_boundaries(monkeypatch, store):
    from option_desk.sources import Sources
    from option_desk.settings import Settings

    class Budget:
        def acquire(self, *args):
            pass

    class Response:
        status_code = 200

        def json(self):
            return []

    calls = []

    def get(*args, **kw):
        calls.append(kw)
        return Response()

    monkeypatch.setattr("option_desk.sources.httpx.get", get)
    s = Settings(
        _env_file=None,
        source_ingestion_enabled=True,
        discord_token="fake",
        discord_channels="123",
    )
    source = Sources(s, store, Budget())
    source.backfill()
    state = store.state("backfill:discord:123")
    assert state["status"] == "complete"
    assert state["messages"] == 0
    source.backfill()
    assert len(calls) == 1
    assert "before" in calls[0]["params"]
