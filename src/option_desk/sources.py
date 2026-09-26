from datetime import datetime, timedelta
import httpx
from .domain import now, stamp, digest
from .parser import parse
from .providers import ProviderError
from .budget import BudgetWait


def discord_authorization(token):
    token = token.strip()
    return token if token.startswith("Bot ") else "Bot " + token


def discord_text(item):
    parts = [item.get("content", "")]
    for e in item.get("embeds", []):
        parts += [e.get("title", ""), e.get("description", "")]
        parts += [
            f"{f.get('name', '')}: {f.get('value', '')}" for f in e.get("fields", [])
        ]
    return "\n".join(parts)


class Sources:
    def __init__(self, settings, store, budget):
        self.settings = settings
        self.store = store
        self.budget = budget

    def ingest(
        self,
        source,
        external_id,
        content,
        dt,
        payload,
        author="",
        url="",
        revision="original",
        origin=None,
    ):
        # Relay provenance: Discord messages posted by a webhook (relay bots)
        # or forwarded from another message (message_reference) are reposts,
        # not original flow. L2 resonance/counting must exclude them.
        if origin is None:
            origin = (
                "relay"
                if payload.get("message_reference") or payload.get("webhook_id")
                else "source"
            )
        try:
            events = parse(
                source,
                external_id,
                content,
                dt,
                payload,
                author,
                url,
                revision,
                origin=origin,
            )
            for event in events:
                if event.event_time < now() - timedelta(
                    days=self.settings.source_lookback_days
                ):
                    self.store.archive(
                        source,
                        external_id,
                        revision + ":" + digest(content)[:12],
                        payload,
                        "event_outside_backfill_window",
                    )
                    continue
                self.store.ingest(event)
        except (ValueError, TypeError) as e:
            reason = (
                str(e)
                if str(e)
                in {
                    "missing_event_time",
                    "incomplete_or_ambiguous_contract",
                    "multiple_events_require_stable_ids",
                    "noise:educational",
                }
                else "parse_validation_failed"
            )
            self.store.archive(
                source,
                external_id,
                revision + ":" + digest(content)[:12],
                payload,
                reason,
            )

    def discord(self):
        settings = self.settings
        if not settings.source_ingestion_enabled:
            return
        if not settings.discord_token or not settings.discord_channels:
            return
        channels = [
            ch.strip()
            for ch in settings.discord_channels.split(",")
            if ch.strip().isdigit()
        ]
        channels.sort(
            key=lambda ch: self.store.state("discord:" + ch).get("polled_at", "")
        )
        for channel in channels:
            channel = channel.strip()
            if not channel.isdigit():
                continue
            key = "discord:" + channel
            state = self.store.state(key)
            if (
                state.get("polled_at")
                and (now() - datetime.fromisoformat(state["polled_at"])).total_seconds()
                < 60
            ):
                continue
            self.budget.acquire("discord")
            # Ascending snowflake pagination. Only advance after all messages are committed.
            after = state.get("after") or str(
                int((now() - timedelta(days=1)).timestamp() * 1000 - 1420070400000)
                << 22
            )
            r = httpx.get(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                params={"after": after, "limit": 100},
                headers={
                    "Authorization": discord_authorization(settings.discord_token),
                    "User-Agent": "OptionDesk/0.1",
                },
                timeout=15,
            )
            if r.status_code == 429:
                self.budget.block("discord", 60)
                raise ProviderError("discord_rate_limited")
            if r.status_code != 200:
                self.store.state(
                    key,
                    {
                        **state,
                        "polled_at": stamp(now()),
                        "status": "error",
                        "http_status": r.status_code,
                    },
                )
                continue
            items = sorted(r.json(), key=lambda item: int(item["id"]))
            for item in items:
                dt = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                self.ingest(
                    key,
                    item["id"],
                    discord_text(item),
                    dt,
                    item,
                    item.get("author", {}).get("username", ""),
                    revision=item.get("edited_timestamp") or "original",
                )
            self.store.state(
                key,
                {
                    "after": items[-1]["id"] if items else after,
                    "polled_at": stamp(now()),
                    "status": "ok",
                    "revision_scan": "not_enabled",
                },
            )

    def backfill(self):
        s = self.settings
        if not s.source_ingestion_enabled or not s.discord_token:
            return
        candidates = []
        for channel in s.discord_channels.split(","):
            key = "backfill:discord:" + channel.strip()
            state = self.store.state(key)
            if state.get("status") == "complete":
                continue
            if (
                state.get("retry_at")
                and datetime.fromisoformat(state["retry_at"]) > now()
            ):
                continue
            candidates.append((state.get("polled_at", ""), channel.strip(), key, state))
        if not candidates:
            return
        _, channel, key, state = min(candidates)
        end = datetime.fromisoformat(state["end"]) if state.get("end") else now()
        start = (
            datetime.fromisoformat(state["start"])
            if state.get("start")
            else end - timedelta(days=s.source_lookback_days)
        )
        before = state.get("before") or str(
            int(end.timestamp() * 1000 - 1420070400000) << 22
        )
        self.budget.acquire("discord", "baseline")
        state.update(start=stamp(start), end=stamp(end), polled_at=stamp(now()))
        try:
            r = httpx.get(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                params={"before": before, "limit": 100},
                headers={
                    "Authorization": discord_authorization(s.discord_token),
                    "User-Agent": "OptionDesk/0.1",
                },
                timeout=15,
            )
            if r.status_code == 429:
                self.budget.block("discord", 60)
            if r.status_code != 200:
                state.update(
                    status="error",
                    http_status=r.status_code,
                    retry_at=stamp(now() + timedelta(minutes=10)),
                )
                self.store.state(key, state)
                return
            items = r.json()
            accepted = 0
            reached = False
            for item in sorted(items, key=lambda x: int(x["id"])):
                dt = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                if dt < start:
                    reached = True
                    continue
                if dt >= end:
                    continue
                self.ingest(
                    "discord:" + channel,
                    item["id"],
                    discord_text(item),
                    dt,
                    item,
                    item.get("author", {}).get("username", ""),
                    revision=item.get("edited_timestamp") or "original",
                )
                accepted += 1
            next_before = str(min(int(i["id"]) for i in items)) if items else before
            if items and int(next_before) >= int(before):
                raise ValueError("non_advancing_cursor")
            state.update(
                before=next_before,
                status="complete" if reached or len(items) < 100 else "running",
                pages=state.get("pages", 0) + 1,
                messages=state.get("messages", 0) + accepted,
            )
            state.pop("retry_at", None)
            state.pop("http_status", None)
            self.store.state(key, state)
        except BudgetWait:
            raise
        except Exception:
            state.update(
                status="error",
                reason="backfill_read_failed",
                retry_at=stamp(now() + timedelta(minutes=5)),
            )
            self.store.state(key, state)

    def mongo(self):
        settings = self.settings
        if not settings.source_ingestion_enabled:
            return
        if not settings.mongo_uri:
            return
        from pymongo import MongoClient

        key = (
            "mongo:"
            + settings.mongo_collection
            + ":"
            + digest(settings.mongo_channels)[:12]
        )
        state = self.store.state(key)
        if state.get("polled_at") and (
            now() - datetime.fromisoformat(state["polled_at"])
        ).total_seconds() < (5 if state.get("coverage") == "partial" else 60):
            return
        self.budget.acquire("mongo")
        from bson import ObjectId

        since = (
            datetime.fromisoformat(state["watermark"])
            if state.get("watermark")
            else now() - timedelta(days=settings.source_lookback_days)
        )
        query = {"created_on": {"$gte": since}}
        if state.get("last_id"):
            query = {
                "$or": [
                    {"created_on": {"$gt": since}},
                    {"created_on": since, "_id": {"$gt": ObjectId(state["last_id"])}},
                ]
            }
        if settings.mongo_channels:
            query["channel_ids"] = {
                "$in": [
                    int(c) for c in settings.mongo_channels.split(",") if c.isdigit()
                ]
            }
        with MongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            tz_aware=True,
        ) as client:
            docs = list(
                client[settings.mongo_db][settings.mongo_collection]
                .find(query)
                .sort([("created_on", 1), ("_id", 1)])
                .limit(100)
            )
        latest = since
        for doc in docs:
            dt = doc.get("created_on")
            content = doc.get("enrich_content") or doc.get("text") or ""
            if isinstance(dt, datetime):
                latest = max(latest, dt)
            payload = {
                k: (
                    stamp(v) if isinstance(v, datetime) else str(v) if k == "_id" else v
                )
                for k, v in doc.items()
            }
            self.ingest(
                key,
                str(doc.get("tweet_id") or doc["_id"]),
                content,
                dt,
                payload,
                doc.get("enrich_author") or doc.get("username", ""),
                doc.get("url", ""),
            )
        self.store.state(
            key,
            {
                "watermark": stamp(latest),
                "polled_at": stamp(now()),
                "status": "ok",
                "last_id": str(docs[-1]["_id"]) if docs else state.get("last_id"),
                "coverage": "partial" if len(docs) == 100 else "complete",
                "revision_scan": "not_enabled",
            },
        )
