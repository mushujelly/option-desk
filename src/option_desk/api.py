from __future__ import annotations
import asyncio
import base64
import json
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, and_, or_, delete
from sqlalchemy.dialects.postgresql import insert
from pydantic import BaseModel, Field
from .settings import Settings, ROOT
from .channels import CHANNELS
from .domain import SourceEvent, Contract, now, stamp
from .store import Store, raw, instruments, watches, current, baselines, jobs, mapping


def create_app(settings=None, store=None):
    settings = settings or Settings()
    store = store or Store(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        store.migrate()
        yield

    app = FastAPI(title="Option Desk", version="0.1.0", lifespan=lifespan)
    app.state.store = store

    @app.middleware("http")
    async def local_guard(request, call_next):
        # Bound to loopback by launcher. Block cross-origin mutations including DNS rebinding.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin not in (
                "http://127.0.0.1:8765",
                "http://localhost:8765",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ):
                from starlette.responses import JSONResponse

                return JSONResponse(
                    {"detail": "cross_origin_write_rejected"}, status_code=403
                )
        return await call_next(request)

    @app.get("/api/v1/health")
    def health():
        return {
            **store.health(),
            "mode": settings.mode,
            "model_estimation": "disabled",
            "private_sources_enabled": settings.source_ingestion_enabled,
            "source_coverage": [
                {
                    "channel": ch,
                    "label": CHANNELS.get(ch, ch),
                    "live": store.state("discord:" + ch),
                    "backfill": store.state("backfill:discord:" + ch),
                }
                for ch in settings.discord_channels.split(",")
                if ch
            ],
            "providers_configured": settings.providers.split(",")
            if settings.providers
            else [],
            "server_time": stamp(now()),
        }

    @app.get("/api/v1/baselines")
    def listing(include_discarded: bool = False, limit: int = Query(200, ge=1, le=500)):
        return {
            "items": store.list_baselines(include_discarded, limit),
            "schema_version": "1",
        }

    @app.get("/api/v1/baselines/{baseline_id}/comparison")
    def comparison(baseline_id: str):
        result = store.comparison(baseline_id)
        if result is None:
            raise HTTPException(404, "baseline_not_found")
        return result

    @app.post("/api/v1/ingest", status_code=201)
    def ingest(event: SourceEvent):
        return {"baseline_id": store.ingest(event)}

    @app.get("/api/v1/sources")
    def sources(cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
        water = now()
        last = None
        if cursor:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                water = datetime.fromisoformat(data["watermark"])
                last = data.get("last")
                if water.tzinfo is None:
                    raise ValueError()
                if last:
                    datetime.fromisoformat(last[0])
            except Exception:
                raise HTTPException(400, "invalid_cursor")
        q = select(raw).where(raw.c.received_at <= water)
        if last:
            dt = datetime.fromisoformat(last[0])
            q = q.where(
                or_(
                    raw.c.received_at < dt,
                    and_(raw.c.received_at == dt, raw.c.id < last[1]),
                )
            )
        with store.engine.connect() as c:
            rows = [
                mapping(r)
                for r in c.execute(
                    q.order_by(raw.c.received_at.desc(), raw.c.id.desc()).limit(
                        limit + 1
                    )
                )
            ]
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            r = items[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "watermark": stamp(water),
                        "last": [stamp(r["received_at"]), r["id"]],
                    }
                ).encode()
            ).decode()
        return {"items": items, "next_cursor": next_cursor}

    class Watch(BaseModel):
        contract: Contract
        interval: int = Field(default=60, ge=15, le=3600)

    @app.get("/api/v1/watchlists")
    def watchlist():
        with store.engine.connect() as c:
            return {
                "items": [
                    mapping(r)
                    for r in c.execute(
                        select(
                            watches,
                            instruments.c.contract,
                            current.c.payload,
                            current.c.version,
                        )
                        .join(instruments, instruments.c.id == watches.c.instrument_id)
                        .where(
                            instruments.c.contract["expiration"].astext
                            >= str(
                                now().astimezone(ZoneInfo("America/New_York")).date()
                            )
                        )
                        .outerjoin(
                            current, current.c.instrument_id == watches.c.instrument_id
                        )
                    )
                ]
            }

    @app.post("/api/v1/watchlists")
    def add_watch(watch: Watch):
        with store.engine.begin() as c:
            c.execute(
                insert(instruments)
                .values(
                    id=watch.contract.id,
                    contract=watch.contract.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
            c.execute(
                insert(watches)
                .values(instrument_id=watch.contract.id, interval=watch.interval)
                .on_conflict_do_update(
                    index_elements=[watches.c.instrument_id],
                    set_={"interval": watch.interval},
                )
            )
        return {"instrument_id": watch.contract.id}

    @app.delete("/api/v1/watchlists/{instrument_id}")
    def remove_watch(instrument_id: str):
        with store.engine.begin() as c:
            c.execute(delete(watches).where(watches.c.instrument_id == instrument_id))
        return {"removed": True}

    @app.post("/api/v1/instruments/{instrument_id}/refresh", status_code=202)
    def refresh(instrument_id: str):
        with store.engine.connect() as c:
            if not c.execute(
                select(instruments.c.id).where(instruments.c.id == instrument_id)
            ).first():
                raise HTTPException(404, "instrument_not_found")
        if settings.mode != "live":
            raise HTTPException(409, "offline_mode_no_live_refresh")
        return {"job_id": store.queue("current", instrument_id)}

    @app.get("/api/v1/events")
    async def events(request: Request):
        async def generate():
            yield "event: resync_required\ndata: {}\n\n"
            previous = {}
            while not await request.is_disconnected():

                def snapshot():
                    with store.engine.connect() as c:
                        a = [
                            (
                                "baseline",
                                r.id,
                                r.status,
                                r.applicability,
                                r.instrument_id,
                            )
                            for r in c.execute(
                                select(
                                    baselines.c.id,
                                    baselines.c.status,
                                    baselines.c.applicability,
                                    baselines.c.instrument_id,
                                )
                            )
                        ]
                        b = [
                            (
                                "current",
                                r.instrument_id,
                                r.version,
                                r.payload.get("provider"),
                                None,
                            )
                            for r in c.execute(
                                select(
                                    current.c.instrument_id,
                                    current.c.version,
                                    current.c.payload,
                                )
                            )
                        ]
                        j = [
                            ("job", r.id, r.state, r.error, None)
                            for r in c.execute(
                                select(jobs.c.id, jobs.c.state, jobs.c.error)
                                .where(jobs.c.state == "failed")
                                .limit(100)
                            )
                        ]
                        return a + b + j

                rows = await asyncio.to_thread(snapshot)
                for kind, key, status, applicable, iid in rows:
                    state = (status, applicable)
                    if previous.get((kind, key)) != state:
                        if kind == "current":
                            old = previous.get((kind, key))
                            if old and old[1] != applicable:
                                yield (
                                    "event: provider_changed\ndata: "
                                    + json.dumps(
                                        {
                                            "instrument_id": key,
                                            "current_version": status,
                                            "previous_provider": old[1],
                                            "provider": applicable,
                                        }
                                    )
                                    + "\n\n"
                                )
                            name = "current_updated"
                            data = {"instrument_id": key, "current_version": status}
                        elif kind == "job":
                            name = "job_failed"
                            data = {"job_id": key, "reason": applicable}
                        else:
                            name = (
                                "baseline_applicability_changed"
                                if applicable != "active"
                                else {
                                    "ready": "baseline_ready",
                                    "discarded": "baseline_discarded",
                                    "unavailable": "baseline_unavailable",
                                }.get(status, "resync_required")
                            )
                            data = {"baseline_id": key, "instrument_id": iid}
                        yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
                        previous[(kind, key)] = state
                yield ": heartbeat\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/export")
    def export():
        return {
            "schema_version": "1",
            "exported_at": stamp(now()),
            "items": store.list_baselines(True, 500),
        }

    dist = ROOT / "web" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (dist / "index.html").exists():
            return {
                "message": "Frontend not built. Run npm --prefix web run build",
                "docs": "/docs",
            }
        return FileResponse(dist / "index.html")

    return app
