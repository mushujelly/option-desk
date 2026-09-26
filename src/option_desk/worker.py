import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .settings import Settings
from .store import Store
from .budget import Budget, BudgetWait
from .domain import Contract, now, stamp
from .providers import create_providers, ProviderError
from .baseline import build
from .sources import Sources


class Worker:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.budget = Budget(store)
        self.providers, self.errors = (
            create_providers(settings, self.budget)
            if settings.mode == "live"
            else ([], {})
        )
        self.sources = Sources(settings, store, self.budget)
        self.next_kind = "current"

    def tick(self):
        self.store.state(
            "worker",
            {
                "status": "running",
                "at": stamp(now()),
                "mode": self.settings.mode,
                "providers": [p.name for p in self.providers],
                "provider_errors": self.errors,
            },
        )
        if self.settings.mode != "live":
            return
        for method in (self.sources.backfill, self.sources.discord, self.sources.mongo):
            failure_key = "source_retry:" + method.__name__
            failure = self.store.state(failure_key)
            if failure.get("retry_at") and now() < datetime.fromisoformat(
                failure["retry_at"]
            ):
                continue
            try:
                method()
                if failure:
                    self.store.state(failure_key, {"status": "ok"})
            except BudgetWait:
                pass
            except Exception as e:
                self.store.state(
                    failure_key,
                    {
                        "status": "error",
                        "reason": getattr(e, "code", "source_read_failed"),
                        "retry_at": stamp(now() + timedelta(minutes=5)),
                    },
                )
                self.store.state(
                    "source_error",
                    {
                        "status": "error",
                        "reason": getattr(e, "code", "source_read_failed"),
                        "at": stamp(now()),
                    },
                )
        contracts = self.store.monitor_contracts(self.settings.max_contracts)
        for row in contracts:
            interval = max(15, row["interval"] or self.settings.refresh_seconds)
            if (
                not row["updated_at"]
                or (now() - row["updated_at"]).total_seconds() >= interval
            ):
                self.store.queue("current", row["id"])
        job = self.store.claim(kind=self.next_kind) or self.store.claim()
        self.next_kind = "baseline" if self.next_kind == "current" else "current"
        if not job:
            return
        try:
            if job["kind"] == "baseline":
                row = self.store.read_baseline(job["target"])
                if (
                    not row
                    or row["status"] != "pending"
                    or row["applicability"] != "active"
                ):
                    self.store.finish(job)
                    return
                status, pack, reason = build(
                    Contract.model_validate(row["contract"]),
                    row["event_time"],
                    row["source_fields"],
                    self.providers,
                )
                if status in ("unavailable", "discarded") and job["attempts"] < 3:
                    self.store.finish(job, "queued", reason, delay=30 * job["attempts"])
                    return
                self.store.finish(job, reason=reason, pack=pack, baseline_status=status)
            else:
                if len(self.providers) == 1 and hasattr(
                    self.providers[0], "quote_many"
                ):
                    self.current_batch(job)
                    return
                row = next((r for r in contracts if r["id"] == job["target"]), None)
                if row is None:
                    self.store.finish(job)
                    return
                contract = Contract.model_validate(row["contract"])
                packs = []
                errors = {}
                for provider in self.providers:
                    try:
                        p = provider.quote(contract)
                        if p.instrument_id != contract.id or not p.matched:
                            raise ProviderError("identity_mismatch")
                        packs.append(p)
                        iv = p.fields.get("iv")
                        if (
                            p.quote_time
                            and (now() - p.quote_time).total_seconds() < 120
                            and not p.delayed
                            and iv
                            and iv.value
                            and iv.value > 0
                        ):
                            break
                    except ProviderError as e:
                        errors[provider.name] = e.code
                if not packs:
                    raise ProviderError("no_provider_quote")

                def rank(p):
                    fresh = (
                        p.quote_time
                        and 0 <= (now() - p.quote_time).total_seconds() < 120
                        and not p.delayed
                    )
                    iv = p.fields.get("iv")
                    return (
                        0 if fresh else 1,
                        0 if iv and iv.value and iv.value > 0 else 1,
                    )

                pack = sorted(packs, key=rank)[0]
                if self.store.finish(job, current_pack=pack):
                    self.store.supplement_close(pack)
        except BudgetWait as e:
            self.store.finish(job, "queued", "budget_wait", delay=e.seconds)
        except Exception as e:
            reason = getattr(e, "code", "provider_request_failed")
            if job["kind"] == "current":
                self.store.current_error(job["target"], reason)
            self.store.finish(
                job, "queued" if job["attempts"] < 3 else "failed", reason, delay=60
            )

    def current_batch(self, first):
        claimed = [first]
        for _ in range(0 if first["attempts"] > 1 else 19):
            job = self.store.claim(kind="current")
            if not job:
                break
            claimed.append(job)
        valid = []
        today = now().astimezone(ZoneInfo("America/New_York")).date()
        for job in claimed:
            data = self.store.instrument(job["target"])
            if not data:
                self.store.finish(job)
                continue
            contract = Contract.model_validate(data)
            if contract.expiration < today:
                self.store.finish(job, reason="contract_expired")
                continue
            valid.append((job, contract))
        if not valid:
            return
        try:
            packs = self.providers[0].quote_many([c for _, c in valid])
        except BudgetWait as e:
            for job, _ in valid:
                self.store.finish(job, "queued", "budget_wait", delay=e.seconds)
            return
        except Exception as e:
            packs = {
                c.id: ProviderError(getattr(e, "code", "provider_request_failed"))
                for _, c in valid
            }
        for job, contract in valid:
            result = packs.get(contract.id, ProviderError("contract_not_found"))
            if isinstance(result, ProviderError):
                self.store.current_error(contract.id, result.code)
                self.store.finish(
                    job,
                    "queued" if job["attempts"] < 3 else "failed",
                    result.code,
                    delay=60 if job["attempts"] < 3 else 21600,
                )
            else:
                if self.store.finish(job, current_pack=result):
                    self.store.supplement_close(result)

    def close(self):
        for p in self.providers:
            p.close()


def run():
    settings = Settings()
    store = Store(settings.database_url)
    store.migrate()
    worker = Worker(settings, store)
    try:
        while True:
            worker.tick()
            time.sleep(2)
    finally:
        worker.close()
