from __future__ import annotations
import json
import uuid
from zoneinfo import ZoneInfo
from datetime import timedelta
from sqlalchemy import (
    MetaData,
    Table,
    Column,
    String,
    Integer,
    DateTime,
    UniqueConstraint,
    create_engine,
    select,
    update,
    func,
    and_,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from .domain import SourceEvent, Pack, digest, now, stamp, compare

meta = MetaData()
raw = Table(
    "raw_events",
    meta,
    Column("id", String, primary_key=True),
    Column("source", String, nullable=False),
    Column("external_id", String, nullable=False),
    Column("revision", String, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, index=True),
    Column("payload", JSONB, nullable=False),
    Column("diagnostic", String),
    UniqueConstraint("source", "external_id", "revision"),
)
logical = Table(
    "logical_events",
    meta,
    Column("id", String, primary_key=True),
    Column("identity_key", String, unique=True, nullable=False),
)
instruments = Table(
    "instruments",
    meta,
    Column("id", String, primary_key=True),
    Column("contract", JSONB, nullable=False),
)
observations = Table(
    "observations",
    meta,
    Column("id", String, primary_key=True),
    Column("raw_id", String, nullable=False),
    Column("logical_id", String, nullable=False),
    Column("instrument_id", String, nullable=False),
    Column("subevent_id", String, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)
baselines = Table(
    "event_baselines",
    meta,
    Column("id", String, primary_key=True),
    Column("logical_event_id", String, nullable=False),
    Column("instrument_id", String, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", String, nullable=False),
    Column("applicability", String, nullable=False, default="active"),
    Column("reason", String),
    Column("payload", JSONB),
    Column("source_fields", JSONB, nullable=False),
    UniqueConstraint("logical_event_id", "instrument_id"),
)
close_references = Table(
    "baseline_close_references",
    meta,
    Column("baseline_id", String, primary_key=True),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
current = Table(
    "current_states",
    meta,
    Column("instrument_id", String, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("error", String),
)
watches = Table(
    "watchlists",
    meta,
    Column("instrument_id", String, primary_key=True),
    Column("interval", Integer, nullable=False),
)
jobs = Table(
    "jobs",
    meta,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("target", String, nullable=False),
    Column("state", String, nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("lease_until", DateTime(timezone=True)),
    Column("token", String),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", String),
    UniqueConstraint("kind", "target"),
)
collectors = Table(
    "collector_state",
    meta,
    Column("id", String, primary_key=True),
    Column("payload", JSONB, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
quotas = Table(
    "request_budgets",
    meta,
    Column("provider", String, primary_key=True),
    Column("window", Integer, nullable=False),
    Column("used", Integer, nullable=False),
    Column("by_class", JSONB, nullable=False),
    Column("blocked_until", DateTime(timezone=True)),
)


def mapping(row):
    return dict(row._mapping) if row else None


class Store:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True)

    def migrate(self):
        # Serialized bootstrap. Numbered migration recorded, never silently drops tables.
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(718524)"))
            c.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
            )
            meta.create_all(c)
            c.execute(
                text(
                    "INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING"
                )
            )

    def archive(self, source, external_id, revision, payload, diagnostic=None):
        rid = digest(f"{source}:{external_id}:{revision}")
        with self.engine.begin() as c:
            c.execute(
                insert(raw)
                .values(
                    id=rid,
                    source=source,
                    external_id=external_id,
                    revision=revision,
                    payload=payload,
                    diagnostic=diagnostic,
                    received_at=now(),
                )
                .on_conflict_do_nothing()
            )
        return rid

    def ingest(self, event: SourceEvent):
        payload = event.model_dump(mode="json")
        revision = (
            event.revision
            + ":"
            + digest(json.dumps(event.raw or payload, sort_keys=True))[:16]
        )
        rid = digest(f"{event.source}:{event.external_id}:{revision}")
        lid = digest(event.identity_key)[:32]
        bid = digest(f"{lid}:{event.contract.id}")[:32]
        received = now()
        with self.engine.begin() as c:
            c.execute(
                insert(raw)
                .values(
                    id=rid,
                    source=event.source,
                    external_id=event.external_id,
                    revision=revision,
                    received_at=received,
                    payload=payload,
                )
                .on_conflict_do_nothing()
            )
            c.execute(
                insert(logical)
                .values(id=lid, identity_key=event.identity_key)
                .on_conflict_do_nothing()
            )
            # Serialize revisions of a logical event before invalidating contract links.
            c.execute(
                select(logical).where(logical.c.id == lid).with_for_update()
            ).first()
            c.execute(
                insert(instruments)
                .values(
                    id=event.contract.id,
                    contract=event.contract.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
            oid = digest(f"{rid}:{lid}:{event.subevent_id}:{event.contract.id}")[:32]
            if c.execute(
                select(observations.c.id).where(observations.c.id == oid)
            ).first():
                return bid
            # Upgrade legacy message-based identity without rewriting frozen packs.
            if event.original_ref:
                legacy = (
                    c.execute(
                        select(observations.c.logical_id)
                        .join(raw, raw.c.id == observations.c.raw_id)
                        .where(
                            raw.c.source == event.source,
                            raw.c.external_id == event.external_id,
                            observations.c.subevent_id == event.subevent_id,
                            observations.c.logical_id != lid,
                            observations.c.payload["original_ref"].astext.is_(None),
                        )
                    )
                    .scalars()
                    .all()
                )
                if legacy:
                    c.execute(
                        update(baselines)
                        .where(baselines.c.logical_event_id.in_(legacy))
                        .values(
                            applicability="superseded",
                            reason="identity_upgraded:" + bid,
                        )
                    )
            # Only revisions from the SAME message/subevent invalidate earlier contract evidence.
            old = (
                c.execute(
                    select(observations.c.instrument_id)
                    .join(raw, raw.c.id == observations.c.raw_id)
                    .where(
                        raw.c.source == event.source,
                        raw.c.external_id == event.external_id,
                        observations.c.subevent_id == event.subevent_id,
                        observations.c.logical_id == lid,
                        observations.c.raw_id != rid,
                    )
                )
                .scalars()
                .all()
            )
            if old:
                c.execute(
                    update(baselines)
                    .where(
                        baselines.c.logical_event_id == lid,
                        baselines.c.instrument_id.in_(old),
                        baselines.c.instrument_id != event.contract.id,
                    )
                    .values(
                        applicability="invalidated", reason="source_contract_revision"
                    )
                )
            oid = digest(f"{rid}:{lid}:{event.subevent_id}:{event.contract.id}")[:32]
            c.execute(
                insert(observations)
                .values(
                    id=oid,
                    raw_id=rid,
                    logical_id=lid,
                    instrument_id=event.contract.id,
                    subevent_id=event.subevent_id,
                    payload=payload,
                    received_at=received,
                )
                .on_conflict_do_nothing()
            )
            c.execute(
                insert(baselines)
                .values(
                    id=bid,
                    logical_event_id=lid,
                    instrument_id=event.contract.id,
                    event_time=event.event_time,
                    created_at=received,
                    status="pending",
                    applicability="active",
                    source_fields={
                        k: v.model_dump(mode="json") for k, v in event.fields.items()
                    },
                )
                .on_conflict_do_nothing()
            )
            c.execute(
                update(baselines)
                .where(baselines.c.id == bid)
                .values(applicability="active")
            )
            c.execute(
                insert(jobs)
                .values(
                    id=str(uuid.uuid4()),
                    kind="baseline",
                    target=bid,
                    state="queued",
                    due_at=received,
                    attempts=0,
                )
                .on_conflict_do_nothing()
            )
        return bid

    def read_baseline(self, bid):
        with self.engine.connect() as c:
            row = c.execute(
                select(baselines, instruments.c.contract)
                .join(instruments, instruments.c.id == baselines.c.instrument_id)
                .where(baselines.c.id == bid)
            ).first()
            return mapping(row)

    def comparison(self, bid):
        with self.engine.connect() as c:
            row = c.execute(
                select(
                    baselines,
                    instruments.c.contract,
                    current.c.payload.label("current_pack"),
                    close_references.c.payload.label("close_reference"),
                    current.c.version.label("current_version"),
                    current.c.error.label("current_error"),
                    jobs.c.state.label("quote_job_state"),
                    jobs.c.error.label("quote_job_error"),
                )
                .join(instruments, instruments.c.id == baselines.c.instrument_id)
                .outerjoin(
                    current, current.c.instrument_id == baselines.c.instrument_id
                )
                .outerjoin(
                    close_references, close_references.c.baseline_id == baselines.c.id
                )
                .outerjoin(
                    jobs,
                    and_(
                        jobs.c.kind == "current",
                        jobs.c.target == baselines.c.instrument_id,
                    ),
                )
                .where(baselines.c.id == bid)
            ).first()
        if row is None:
            return None
        d = mapping(row)
        a = Pack.model_validate(d["payload"]) if d["payload"] else None
        b = Pack.model_validate(d["current_pack"]) if d["current_pack"] else None
        if d["close_reference"]:
            supplement = Pack.model_validate(d["close_reference"])
            if a is None:
                a = Pack(
                    provider="source",
                    instrument_id=d["instrument_id"],
                    fields=d["source_fields"],
                    time_basis="event_with_close_fields",
                )
            for key, v in supplement.fields.items():
                existing = a.fields.get(key)
                source = d["source_fields"].get(key)
                if source and source.get("value") is not None:
                    from .domain import Value

                    a.fields[key] = Value.model_validate(source)
                elif existing is None or existing.value is None:
                    a.fields[key] = v
            a.evidence["close_reference"] = supplement.evidence
        return {
            "schema_version": "1",
            "validation": (
                {
                    "status": "insufficient_data",
                    "reason": "aggregate_average_not_single_trade",
                    "rule": "validation-v2",
                }
                if a
                and a.fields.get("price")
                and a.fields["price"].price_type == "reported_average"
                else {
                    "status": a.evidence.get("validation", "insufficient_data")
                    if a
                    else "insufficient_data",
                    "rule": "validation-v2",
                }
            ),
            "baseline_id": bid,
            "logical_event_id": d["logical_event_id"],
            "instrument_id": d["instrument_id"],
            "contract": d["contract"],
            "status": d["status"],
            "applicability": d["applicability"],
            "reason": d["reason"],
            "event_time": stamp(d["event_time"]),
            "baseline": a.model_dump(mode="json") if a else None,
            "close_reference": d["close_reference"],
            "current": d["current_pack"],
            "current_version": d["current_version"],
            "source_fields": d["source_fields"],
            "current_status": "expired"
            if d["contract"]["expiration"]
            < str(now().astimezone(ZoneInfo("America/New_York")).date())
            else ("ready" if b else d["quote_job_state"] or "waiting"),
            "current_error": d["current_error"] or d["quote_job_error"],
            "comparisons": compare(a, b, d["applicability"] == "active"),
        }

    def list_baselines(self, include_discarded=False, limit=200):
        q = (
            select(baselines.c.id)
            .join(instruments, instruments.c.id == baselines.c.instrument_id)
            .where(
                instruments.c.contract["expiration"].astext
                >= str(now().astimezone(ZoneInfo("America/New_York")).date())
            )
            .order_by(baselines.c.event_time.desc(), baselines.c.id)
            .limit(limit)
        )
        if not include_discarded:
            q = q.where(
                baselines.c.status != "discarded", baselines.c.applicability == "active"
            )
        with self.engine.connect() as c:
            ids = c.execute(q).scalars().all()
        return [self.comparison(i) for i in ids]

    def supplement_close(self, pack):
        from .domain import Contract
        from .close_reference import close_reference

        with self.engine.begin() as c:
            rows = (
                c.execute(
                    select(baselines, instruments.c.contract)
                    .join(instruments, instruments.c.id == baselines.c.instrument_id)
                    .where(
                        baselines.c.instrument_id == pack.instrument_id,
                        baselines.c.applicability == "active",
                        baselines.c.status != "discarded",
                    )
                )
                .mappings()
                .all()
            )
            added = 0
            for row in rows:
                existing = c.execute(
                    select(close_references.c.baseline_id).where(
                        close_references.c.baseline_id == row["id"]
                    )
                ).first()
                if existing:
                    continue
                supplement = close_reference(
                    Contract.model_validate(row["contract"]), row["event_time"], pack
                )
                if supplement:
                    result = c.execute(
                        insert(close_references)
                        .values(
                            baseline_id=row["id"],
                            payload=supplement.model_dump(mode="json"),
                            created_at=now(),
                        )
                        .on_conflict_do_nothing()
                        .returning(close_references.c.baseline_id)
                    )
                    added += int(result.scalar_one_or_none() is not None)
            return added

    def put_current(self, pack: Pack):
        payload = pack.model_dump(mode="json")
        with self.engine.begin() as c:
            stmt = insert(current).values(
                instrument_id=pack.instrument_id,
                version=1,
                payload=payload,
                updated_at=now(),
                error=None,
            )
            return c.execute(
                stmt.on_conflict_do_update(
                    index_elements=[current.c.instrument_id],
                    set_={
                        "version": current.c.version + 1,
                        "payload": payload,
                        "updated_at": now(),
                        "error": None,
                    },
                ).returning(current.c.version)
            ).scalar_one()

    def current_error(self, iid, reason):
        with self.engine.begin() as c:
            c.execute(
                update(current)
                .where(current.c.instrument_id == iid)
                .values(error=reason)
            )

    def queue(self, kind, target):
        with self.engine.begin() as c:
            existing = c.execute(
                select(jobs)
                .where(jobs.c.kind == kind, jobs.c.target == target)
                .with_for_update()
            ).first()
            if existing:
                d = mapping(existing)
                if (
                    kind == "baseline"
                    or d["state"] in {"running", "queued"}
                    or (d["state"] == "failed" and d["due_at"] > now())
                ):
                    return d["id"]
                c.execute(
                    update(jobs)
                    .where(jobs.c.id == d["id"])
                    .values(state="queued", due_at=now(), attempts=0, error=None)
                )
                return d["id"]
            jid = str(uuid.uuid4())
            c.execute(
                insert(jobs)
                .values(
                    id=jid,
                    kind=kind,
                    target=target,
                    state="queued",
                    due_at=now(),
                    attempts=0,
                )
                .on_conflict_do_nothing()
            )
            return c.execute(
                select(jobs.c.id).where(jobs.c.kind == kind, jobs.c.target == target)
            ).scalar_one()

    def claim(self, kind=None, lease_seconds=90):
        with self.engine.begin() as c:
            q = select(jobs).where(
                or_(
                    and_(jobs.c.state == "queued", jobs.c.due_at <= now()),
                    and_(jobs.c.state == "running", jobs.c.lease_until < now()),
                )
            )
            if kind:
                q = q.where(jobs.c.kind == kind)
            row = c.execute(
                q.order_by(jobs.c.due_at).with_for_update(skip_locked=True).limit(1)
            ).first()
            if not row:
                return None
            d = mapping(row)
            token = str(uuid.uuid4())
            c.execute(
                update(jobs)
                .where(jobs.c.id == d["id"])
                .values(
                    state="running",
                    token=token,
                    lease_until=now() + timedelta(seconds=lease_seconds),
                    attempts=jobs.c.attempts + 1,
                )
            )
            return {**d, "token": token, "attempts": d["attempts"] + 1}

    def finish(
        self,
        job,
        status="done",
        reason=None,
        pack=None,
        baseline_status=None,
        delay=0,
        current_pack=None,
    ):
        with self.engine.begin() as c:
            locked = c.execute(
                select(jobs)
                .where(
                    jobs.c.id == job["id"],
                    jobs.c.token == job["token"],
                    jobs.c.state == "running",
                    jobs.c.lease_until > now(),
                )
                .with_for_update()
            ).first()
            if not locked:
                return False
            if current_pack is not None:
                stmt = insert(current).values(
                    instrument_id=current_pack.instrument_id,
                    version=1,
                    payload=current_pack.model_dump(mode="json"),
                    updated_at=now(),
                    error=None,
                )
                c.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[current.c.instrument_id],
                        set_={
                            "version": current.c.version + 1,
                            "payload": current_pack.model_dump(mode="json"),
                            "updated_at": now(),
                            "error": None,
                        },
                    )
                )
            if baseline_status:
                c.execute(
                    update(baselines)
                    .where(
                        baselines.c.id == job["target"],
                        baselines.c.status == "pending",
                        baselines.c.applicability == "active",
                    )
                    .values(
                        status=baseline_status,
                        reason=reason,
                        payload=pack.model_dump(mode="json") if pack else None,
                    )
                )
            changes = dict(
                state=status,
                error=reason,
                due_at=now() + timedelta(seconds=delay),
                token=None,
                lease_until=None,
            )
            if reason == "budget_wait":
                changes["attempts"] = func.greatest(0, jobs.c.attempts - 1)
            c.execute(update(jobs).where(jobs.c.id == job["id"]).values(**changes))
            return True

    def instrument(self, iid):
        with self.engine.connect() as c:
            return c.execute(
                select(instruments.c.contract).where(instruments.c.id == iid)
            ).scalar_one_or_none()

    def monitor_contracts(self, limit=100):
        with self.engine.connect() as c:
            active = select(baselines.c.instrument_id).where(
                baselines.c.status != "discarded", baselines.c.applicability == "active"
            )
            q = (
                select(
                    instruments.c.id,
                    instruments.c.contract,
                    current.c.updated_at,
                    watches.c.interval,
                )
                .outerjoin(current, current.c.instrument_id == instruments.c.id)
                .outerjoin(watches, watches.c.instrument_id == instruments.c.id)
                .outerjoin(
                    jobs,
                    and_(jobs.c.kind == "current", jobs.c.target == instruments.c.id),
                )
                .where(
                    instruments.c.contract["expiration"].astext
                    >= str(now().astimezone(ZoneInfo("America/New_York")).date()),
                    or_(
                        jobs.c.id.is_(None),
                        jobs.c.state != "failed",
                        jobs.c.due_at <= now(),
                    ),
                    or_(
                        instruments.c.id.in_(active),
                        watches.c.instrument_id.is_not(None),
                    ),
                )
                .order_by(current.c.updated_at.asc().nullsfirst(), instruments.c.id)
                .limit(limit)
            )
            return [mapping(r) for r in c.execute(q)]

    def state(self, key, payload=None):
        with self.engine.begin() as c:
            if payload is not None:
                c.execute(
                    insert(collectors)
                    .values(id=key, payload=payload, updated_at=now())
                    .on_conflict_do_update(
                        index_elements=[collectors.c.id],
                        set_={"payload": payload, "updated_at": now()},
                    )
                )
                return payload
            row = c.execute(
                select(collectors.c.payload).where(collectors.c.id == key)
            ).first()
            return row[0] if row else {}

    def health(self):
        with self.engine.connect() as c:
            counts = {
                t.name: c.execute(select(func.count()).select_from(t)).scalar_one()
                for t in (raw, baselines, current, jobs)
            }
            states = [mapping(r) for r in c.execute(select(collectors))]
            queue = [
                mapping(r)
                for r in c.execute(
                    select(jobs).order_by(jobs.c.due_at.desc()).limit(100)
                )
            ]
        return {"counts": counts, "collectors": states, "jobs": queue}
