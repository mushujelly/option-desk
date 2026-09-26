"""Layer 1 noise gate (educational content) + relay provenance marking.

No DB dependency: parse() is exercised directly and Sources.ingest() runs
against a fake store so these tests stay green without Postgres.
"""
from datetime import datetime, timedelta

import pytest

from option_desk.domain import UTC, now
from option_desk.parser import is_educational_noise, parse
from option_desk.sources import Sources


EDUCATE_POST = (
    "I am posting this to educate new traders on how unusual options activity "
    "works. When you see a large block trade on the tape, remember that "
    "context matters: open interest, implied volatility, and the trader's "
    "existing position all change what the flow means. A buyer of calls into "
    "strength is very different from a seller of puts into weakness. Always "
    "manage risk."
)

FLOW_LINE = "$TXN 205 C 08/01/2025 $1.1M (Bullish)"


def dt():
    return now()


class FakeStore:
    def __init__(self):
        self.ingested = []
        self.archived = []

    def ingest(self, event):
        self.ingested.append(event)

    def archive(self, source, external_id, revision, payload, diagnostic=None):
        self.archived.append(
            {
                "source": source,
                "external_id": external_id,
                "revision": revision,
                "diagnostic": diagnostic,
            }
        )


class FakeSettings:
    source_lookback_days = 30


def sources():
    return Sources(FakeSettings(), FakeStore(), budget=None)


# --- Rule 1: educational noise gate -----------------------------------------


def test_educational_marker_text_is_noise():
    assert is_educational_noise(EDUCATE_POST)


def test_long_text_without_contract_is_noise():
    filler = "Market commentary and musings on price action. " * 20
    assert len(filler) > 500
    assert is_educational_noise(filler)


def test_short_plain_text_is_not_noise():
    assert not is_educational_noise("just some random words")


def test_educational_post_raises_noise_reason():
    with pytest.raises(ValueError, match="noise:educational"):
        parse("discord:test", "1", EDUCATE_POST, dt())


def test_long_thread_without_contract_raises_noise_reason():
    filler = "A long thread on market structure with no tradable contract. " * 15
    with pytest.raises(ValueError, match="noise:educational"):
        parse("discord:test", "1", filler, dt())


def test_short_noncontract_still_incomplete():
    with pytest.raises(ValueError, match="incomplete_or_ambiguous_contract"):
        parse("discord:test", "1", "just some random words", dt())


def test_contract_beats_noise_gate():
    # Educational markers must never swallow a real contract.
    text = EDUCATE_POST + "\n" + FLOW_LINE
    events = parse("discord:test", "1", text, dt())
    assert len(events) == 1
    assert events[0].contract.ticker == "TXN"


def test_noise_goes_to_archive_not_silently_dropped():
    s = sources()
    s.ingest("discord:test", "1", EDUCATE_POST, dt(), {})
    assert s.store.ingested == []
    assert len(s.store.archived) == 1
    assert s.store.archived[0]["diagnostic"] == "noise:educational"


# --- Rule 2: relay provenance -------------------------------------------------


def test_origin_defaults_to_source():
    events = parse("discord:test", "1", FLOW_LINE, dt())
    assert events[0].origin == "source"


def test_origin_relay_stamped_through_parse():
    events = parse("discord:test", "1", FLOW_LINE, dt(), origin="relay")
    assert events[0].origin == "relay"


def test_invalid_origin_rejected():
    with pytest.raises(ValueError):
        parse("discord:test", "1", FLOW_LINE, dt(), origin="bogus")


def test_ingest_marks_webhook_post_as_relay():
    s = sources()
    s.ingest("discord:test", "1", FLOW_LINE, dt(), {"webhook_id": "relay-bot"})
    assert len(s.store.ingested) == 1
    assert s.store.ingested[0].origin == "relay"


def test_ingest_marks_forwarded_message_as_relay():
    s = sources()
    s.ingest(
        "discord:test",
        "1",
        FLOW_LINE,
        dt(),
        {"message_reference": {"message_id": "0"}},
    )
    assert s.store.ingested[0].origin == "relay"


def test_ingest_plain_message_stays_source():
    s = sources()
    s.ingest("discord:test", "1", FLOW_LINE, dt(), {})
    assert s.store.ingested[0].origin == "source"


def test_explicit_origin_overrides_inference():
    s = sources()
    s.ingest(
        "discord:test", "1", FLOW_LINE, dt(), {"webhook_id": "relay-bot"},
        origin="source",
    )
    assert s.store.ingested[0].origin == "source"
