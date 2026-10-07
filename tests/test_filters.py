from datetime import datetime, UTC
from fastapi.testclient import TestClient
from option_desk.api import create_app
from option_desk.settings import Settings


def test_filters_run_before_pagination_and_expiry_is_hidden(store, event):
    for i, ticker in enumerate(["AAPL", "VST", "VST", "AAPL"]):
        contract = event.contract.model_copy(update={"ticker": ticker})
        store.ingest(
            event.model_copy(
                update={
                    "external_id": str(i),
                    "contract": contract,
                    "event_time": datetime(2026, 9, 25, 14 + i, tzinfo=UTC),
                }
            )
        )
    expired = event.contract.model_copy(
        update={"expiration": datetime(2020, 1, 1).date(), "ticker": "VST"}
    )
    store.ingest(
        event.model_copy(update={"external_id": "expired", "contract": expired})
    )
    client = TestClient(create_app(Settings(mode="offline"), store))
    first = client.get("/api/v1/baselines", params={"ticker": "vst", "limit": 1}).json()
    assert first["total"] == 2 and first["next_offset"] == 1
    assert first["items"][0]["contract"]["ticker"] == "VST"
    second = client.get(
        "/api/v1/baselines", params={"ticker": "VST", "limit": 1, "offset": 1}
    ).json()
    assert second["next_offset"] is None
    assert first["items"][0]["baseline_id"] != second["items"][0]["baseline_id"]
    rows = client.get(
        "/api/v1/baselines",
        params={
            "ticker": "VST",
            "right": "C",
            "expiration": "2026-10-16",
            "event_from": "2026-09-25T15:00:00Z",
            "event_to": "2026-09-25T16:00:00Z",
            "status": "pending",
        },
    ).json()
    assert rows["total"] == 1
    assert client.get("/api/v1/baselines?right=P").json()["total"] == 0
    assert client.get("/api/v1/baselines?search=vst").json()["total"] == 2
    assert client.get("/api/v1/baselines?search=%25").json()["total"] == 0
    assert client.get("/api/v1/baselines?status=ready").json()["total"] == 0
    for query in [
        "right=X",
        "offset=-1",
        "expiration=bad",
        "status=oops",
        "event_from=2026-09-25T00:00:00",
        "event_from=2026-09-26T00:00:00Z&event_to=2026-09-25T00:00:00Z",
    ]:
        assert client.get("/api/v1/baselines?" + query).status_code == 422
