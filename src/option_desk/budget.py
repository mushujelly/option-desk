"""Atomic, process-shared provider budgets; attempts consume capacity before I/O."""

import time
from datetime import timedelta
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from .store import quotas
from .domain import now


class BudgetWait(Exception):
    def __init__(self, seconds=5):
        self.seconds = max(1, int(seconds))
        super().__init__("request_budget_wait")


class Budget:
    def __init__(self, store, limits=None):
        self.store = store
        # Conservative application caps, NOT claims about provider entitlements.
        self.limits = limits or {
            "schwab": 30,
            "opend": 20,
            "polygon": 5,
            "discord": 20,
            "mongo": 20,
        }

    def acquire(self, provider, category="current", cost=1):
        limit = self.limits.get(provider, 5)
        window = int(time.time() // 60)
        with self.store.engine.begin() as c:
            c.execute(
                insert(quotas)
                .values(provider=provider, window=window, used=0, by_class={})
                .on_conflict_do_nothing()
            )
            row = (
                c.execute(
                    select(quotas)
                    .where(quotas.c.provider == provider)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if row["blocked_until"] and row["blocked_until"] > now():
                raise BudgetWait((row["blocked_until"] - now()).total_seconds())
            used = row["used"] if row["window"] == window else 0
            counts = dict(row["by_class"]) if row["window"] == window else {}
            # Until last 10 seconds, protect half for current/source, 30% baseline.
            reserved = {
                "current": max(1, int(limit * 0.5)),
                "baseline": max(1, int(limit * 0.3)),
            }
            protected = 0
            if time.time() % 60 < 50:
                protected = sum(
                    max(0, n - counts.get(k, 0))
                    for k, n in reserved.items()
                    if k != category
                )
            if used + cost > limit - protected:
                raise BudgetWait(60 - time.time() % 60)
            counts[category] = counts.get(category, 0) + cost
            c.execute(
                update(quotas)
                .where(quotas.c.provider == provider)
                .values(window=window, used=used + cost, by_class=counts)
            )

    def block(self, provider, seconds):
        with self.store.engine.begin() as c:
            c.execute(
                update(quotas)
                .where(quotas.c.provider == provider)
                .values(blocked_until=now() + timedelta(seconds=max(1, seconds)))
            )
