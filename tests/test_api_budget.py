from fastapi.testclient import TestClient
from option_desk.api import create_app
from option_desk.settings import Settings
from option_desk.budget import Budget, BudgetWait
import pytest


def test_read_queries_never_write(store, event):
    bid = store.ingest(event)
    app = create_app(Settings(), store)
    client = TestClient(app)
    before = store.health()["counts"]
    for _ in range(5):
        assert client.get("/api/v1/baselines/" + bid + "/comparison").status_code == 200
    assert store.health()["counts"] == before
    assert client.get("/api/v1/baselines/missing/comparison").status_code == 404
    assert client.post("/api/v1/baselines/" + bid + "/refresh").status_code == 404


def test_cursor_paging(store, event):
    for i in range(5):
        store.ingest(event.model_copy(update={"external_id": str(i)}))
    client = TestClient(create_app(Settings(), store))
    page = client.get("/api/v1/sources?limit=2").json()
    store.ingest(event.model_copy(update={"external_id": "later"}))
    ids = [r["id"] for r in page["items"]]
    while page["next_cursor"]:
        page = client.get(
            "/api/v1/sources", params={"cursor": page["next_cursor"], "limit": 2}
        ).json()
        ids += [r["id"] for r in page["items"]]
    assert len(ids) == len(set(ids)) == 5
    assert client.get("/api/v1/sources?cursor=oops").status_code == 400


def test_origin_guard(store, event):
    client = TestClient(create_app(Settings(), store))
    assert (
        client.post(
            "/api/v1/ingest",
            json=event.model_dump(mode="json"),
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )


def test_budget_hard_cap(store, monkeypatch):
    monkeypatch.setattr("option_desk.budget.time.time", lambda: 119.0)
    a = Budget(store, {"test": 4})
    b = Budget(store, {"test": 4})
    for _ in range(2):
        a.acquire("test")
        b.acquire("test")
    with pytest.raises(BudgetWait):
        a.acquire("test")
    monkeypatch.setattr("option_desk.budget.time.time", lambda: 179.0)
    b.acquire("test")


def test_expired_hidden_before_limit_but_history_retained(store, event, monkeypatch):
    from datetime import datetime, date, UTC

    monkeypatch.setattr(
        "option_desk.store.now", lambda: datetime(2026, 9, 26, tzinfo=UTC)
    )
    future_id = store.ingest(event)
    expired = event.model_copy(
        update={
            "external_id": "expired",
            "event_time": datetime(2026, 9, 25, 22, tzinfo=UTC),
            "contract": event.contract.model_copy(
                update={"expiration": date(2026, 9, 24)}
            ),
        }
    )
    expired_id = store.ingest(expired)
    client = TestClient(create_app(Settings(), store))
    for diagnostic in ("false", "true"):
        items = client.get(
            "/api/v1/baselines", params={"limit": 1, "include_discarded": diagnostic}
        ).json()["items"]
        assert [x["baseline_id"] for x in items] == [future_id]
    assert store.comparison(expired_id) is not None
