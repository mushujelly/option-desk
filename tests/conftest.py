import os, uuid
import pytest
from sqlalchemy import text
from option_desk.store import Store, meta
from option_desk.domain import Contract, SourceEvent, UTC
from datetime import datetime


@pytest.fixture
def store():
    base = Store(
        os.getenv(
            "OD_TEST_DATABASE_URL",
            "postgresql+psycopg://optiondesk:optiondesk@127.0.0.1:55432/optiondesk",
        )
    )
    schema = "test_" + uuid.uuid4().hex
    with base.engine.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    s = Store(
        os.getenv(
            "OD_TEST_DATABASE_URL",
            "postgresql+psycopg://optiondesk:optiondesk@127.0.0.1:55432/optiondesk",
        )
    )
    s.engine = s.engine.execution_options(schema_translate_map={None: schema})
    meta.create_all(s.engine)
    yield s
    s.engine.dispose()
    with base.engine.begin() as c:
        c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    base.engine.dispose()


@pytest.fixture
def contract():
    return Contract(ticker="AAPL", expiration="2026-10-16", strike="260", right="C")


@pytest.fixture
def event(contract):
    return SourceEvent(
        source="test",
        external_id="message-1",
        event_time=datetime(2026, 9, 25, 18, 0, tzinfo=UTC),
        contract=contract,
    )
