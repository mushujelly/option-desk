from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from sqlalchemy import select, func, update
from option_desk.store import baselines, current, jobs, raw
from option_desk.domain import Pack, value, now


def count(store, table):
    with store.engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar_one()


def test_idempotent_ingest_and_frozen_baseline(store, event):
    bid = store.ingest(event)
    assert store.ingest(event) == bid
    job = store.claim()
    p = Pack(
        provider="test", instrument_id=event.contract.id, fields={"price": value(2)}
    )
    assert store.finish(job, pack=p, baseline_status="ready")
    store.ingest(event)
    assert store.read_baseline(bid)["payload"]["fields"]["price"]["value"] == "2"
    assert count(store, baselines) == 1
    assert count(store, raw) == 1
    assert store.claim() is None


def test_same_contract_new_event_shares_current(store, event):
    a = store.ingest(event)
    b = store.ingest(event.model_copy(update={"external_id": "another"}))
    for i in range(12):
        store.put_current(
            Pack(
                provider="test",
                instrument_id=event.contract.id,
                fields={"price": value(i)},
            )
        )
    assert a != b
    assert count(store, baselines) == 2
    assert count(store, current) == 1
    assert store.comparison(a)["current_version"] == 12
    assert store.comparison(b)["current_version"] == 12


def test_cross_source_and_distinct_events(store, event):
    a = event.model_copy(update={"original_ref": "x:123"})
    b = a.model_copy(update={"source": "mongo", "external_id": "relay"})
    assert store.ingest(a) == store.ingest(b)
    assert store.ingest(event) != store.ingest(a)


def test_concurrent_ingest_claim(store, event):
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(lambda _: store.ingest(event), range(12)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda _: store.claim(), range(12)))
    assert len([x for x in claimed if x]) == 1


def test_lease_fencing(store, event):
    store.ingest(event)
    first = store.claim()
    with store.engine.begin() as c:
        c.execute(
            update(jobs)
            .where(jobs.c.id == first["id"])
            .values(lease_until=now() - timedelta(seconds=1))
        )
    second = store.claim()
    assert second["token"] != first["token"]
    assert not store.finish(first, baseline_status="ready")
    assert store.finish(second, baseline_status="unavailable")


def test_revision_invalidation(store, event):
    a = store.ingest(event)
    changed = event.model_copy(
        update={
            "revision": "edited",
            "contract": event.contract.model_copy(update={"right": "P"}),
        }
    )
    b = store.ingest(changed)
    assert a != b
    assert store.read_baseline(a)["applicability"] == "invalidated"
    assert store.read_baseline(b)["applicability"] == "active"


def test_concurrent_atomic_read(store, event):
    bid = store.ingest(event)

    def writer():
        for i in range(1, 41):
            store.put_current(
                Pack(
                    provider="test",
                    instrument_id=event.contract.id,
                    fields={"price": value(i)},
                )
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f = pool.submit(writer)
        for _ in range(50):
            r = store.comparison(bid)
            if r["current"]:
                assert (
                    int(r["current"]["fields"]["price"]["value"])
                    == r["current_version"]
                )
        f.result()
    assert count(store, current) == 1


def test_replaying_old_message_does_not_restore_invalidated_baseline(store, event):
    a = store.ingest(event)
    b = store.ingest(
        event.model_copy(
            update={
                "revision": "edited",
                "contract": event.contract.model_copy(update={"right": "P"}),
            }
        )
    )
    store.ingest(event)
    assert store.read_baseline(a)["applicability"] == "invalidated"
    assert store.read_baseline(b)["applicability"] == "active"


def test_multileg_same_raw_revision_keeps_both_contracts_active(store, event):
    raw_payload = {"contracts": [{"leg": "call"}, {"leg": "put"}]}
    a = store.ingest(event.model_copy(update={"raw": raw_payload}))
    b = store.ingest(
        event.model_copy(
            update={
                "raw": raw_payload,
                "contract": event.contract.model_copy(update={"right": "P"}),
            }
        )
    )
    assert a != b
    assert store.read_baseline(a)["applicability"] == "active"
    assert store.read_baseline(b)["applicability"] == "active"


def test_stale_worker_cannot_write_current(store, event):
    store.ingest(event)
    store.queue("current", event.contract.id)
    job = store.claim("current")
    with store.engine.begin() as c:
        c.execute(
            update(jobs)
            .where(jobs.c.id == job["id"])
            .values(lease_until=now() - timedelta(seconds=1))
        )
    assert not store.finish(
        job, current_pack=Pack(provider="test", instrument_id=event.contract.id)
    )
    assert count(store, current) == 0
