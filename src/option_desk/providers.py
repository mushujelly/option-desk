from __future__ import annotations
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
import httpx
import time
from .domain import Pack, Bar, value, metrics, UTC
from .settings import ROOT


class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Provider:
    name = "unknown"

    def quote(self, contract):
        raise ProviderError("unsupported")

    def bars(self, contract, start, end, underlying=False):
        raise ProviderError("unsupported_option_minute_history")

    def close(self):
        pass


class Schwab(Provider):
    name = "schwab"

    def __init__(self, settings, budget):
        from schwab.auth import client_from_token_file

        token = Path(settings.schwab_token_path).expanduser()
        if not token.is_absolute():
            token = ROOT / token
        if not token.is_file():
            raise ProviderError("project_token_missing")
        if not settings.schwab_app_key or not settings.schwab_app_secret:
            raise ProviderError("credentials_missing")
        self.client = client_from_token_file(
            str(token), settings.schwab_app_key, settings.schwab_app_secret
        )
        self.budget = budget

    def _json(self, response):
        if response.status_code == 429:
            self.budget.block(self.name, 60)
            raise ProviderError("rate_limited")
        if response.status_code in (401, 403):
            raise ProviderError("permission_denied")
        if response.status_code != 200:
            raise ProviderError("provider_http_error")
        return response.json()

    def quote(self, contract):
        self.budget.acquire(self.name)
        data = self._json(self.client.get_quotes([contract.occ, contract.ticker]))
        row = data.get(contract.occ)
        if not row:
            raise ProviderError("contract_not_found")
        if row.get("symbol", "").replace(" ", "") != contract.occ.replace(" ", ""):
            raise ProviderError("identity_mismatch")
        q = row.get("quote", {})
        u = data.get(contract.ticker, {}).get("quote", {})
        ts = q.get("quoteTime")
        qt = datetime.fromtimestamp(ts / 1000, UTC) if ts else None
        fields = {}
        for target, key, unit in [
            ("bid", "bidPrice", "USD"),
            ("ask", "askPrice", "USD"),
            ("last", "lastPrice", "USD"),
            ("mark", "mark", "USD"),
            ("volume", "totalVolume", "contracts"),
            ("open_interest", "openInterest", "contracts"),
            ("iv", "volatility", "ratio"),
            ("delta", "delta", "ratio"),
            ("gamma", "gamma", "per_USD"),
            ("theta", "theta", "USD/day"),
            ("vega", "vega", "USD/percentage_point"),
        ]:
            num = q.get(key)
            if target == "iv" and num is not None:
                num = Decimal(str(num)) / 100
            fields[target] = value(
                num,
                provider=self.name,
                as_of=None if target == "open_interest" else qt,
                unit=unit,
            )
        fields["price"] = value(
            q.get("mark") if q.get("mark") is not None else q.get("lastPrice"),
            provider=self.name,
            as_of=qt,
            price_type="mark" if q.get("mark") is not None else "last",
        )
        ut = u.get("quoteTime")
        uts = datetime.fromtimestamp(ut / 1000, UTC) if ut else None
        fields["underlying_price"] = value(
            u.get("lastPrice"), provider=self.name, as_of=uts, price_type="last"
        )
        return metrics(
            Pack(
                provider=self.name,
                instrument_id=contract.id,
                fields=fields,
                quote_time=qt,
                delayed=not row.get("realtime", False),
                evidence={"symbol": contract.occ},
            ),
            contract,
        )

    # SDK price-history is not represented as verified option history without a real probe.


class Polygon(Provider):
    name = "polygon"

    def __init__(self, settings, budget):
        if not settings.polygon_api_key:
            raise ProviderError("credentials_missing")
        self.http = httpx.Client(
            base_url=settings.polygon_base_url,
            headers={"Authorization": f"Bearer {settings.polygon_api_key}"},
            timeout=15,
        )
        self.budget = budget

    def get(self, path, category, params=None):
        self.budget.acquire(self.name, category)
        r = self.http.get(path, params=params)
        if r.status_code == 429:
            self.budget.block(self.name, 60)
            raise ProviderError("rate_limited")
        if r.status_code in (401, 403):
            raise ProviderError("permission_denied")
        if r.status_code != 200:
            raise ProviderError("provider_http_error")
        return r.json()

    def bars(self, contract, start, end, underlying=False):
        symbol = contract.ticker if underlying else "O:" + contract.occ.replace(" ", "")
        data = self.get(
            f"/v2/aggs/ticker/{quote(symbol)}/range/1/minute/{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000) - 1}",
            "baseline",
            {"adjusted": "false", "sort": "asc", "limit": 50000},
        )
        if data.get("next_url"):
            raise ProviderError("partial_history")
        return [
            Bar(
                start=datetime.fromtimestamp(r["t"] / 1000, UTC),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=r["v"],
                trade_semantics_verified=True,
            )
            for r in data.get("results", [])
        ]

    def quote(self, contract):
        symbol = "O:" + contract.occ.replace(" ", "")
        data = self.get(
            f"/v3/snapshot/options/{contract.ticker}/{quote(symbol)}", "current"
        ).get("results", {})
        if data.get("details", {}).get("ticker") != symbol:
            raise ProviderError("identity_mismatch")
        q = data.get("last_quote", {})
        u = data.get("underlying_asset", {})
        ts = q.get("last_updated")
        qt = datetime.fromtimestamp(ts / 1e9, UTC) if ts else None
        f = {
            k: value(q.get(v), provider=self.name, as_of=qt)
            for k, v in [("bid", "bid"), ("ask", "ask")]
        }
        f["price"] = value(
            q.get("midpoint"), provider=self.name, as_of=qt, price_type="mid"
        )
        f["iv"] = value(
            data.get("implied_volatility"), provider=self.name, as_of=qt, unit="ratio"
        )
        f["volume"] = value(
            data.get("day", {}).get("volume"), provider=self.name, unit="contracts"
        )
        f["open_interest"] = value(
            data.get("open_interest"), provider=self.name, unit="contracts"
        )
        f["underlying_price"] = value(
            u.get("price"),
            provider=self.name,
            as_of=datetime.fromtimestamp(u["last_updated"] / 1e9, UTC)
            if u.get("last_updated")
            else None,
            price_type="provider_price",
        )
        for k in ("delta", "gamma", "theta", "vega"):
            f[k] = value(
                data.get("greeks", {}).get(k),
                provider=self.name,
                as_of=qt,
                unit="provider_native",
            )
        return metrics(
            Pack(
                provider=self.name,
                instrument_id=contract.id,
                fields=f,
                quote_time=qt,
                delayed=q.get("timeframe") != "REAL-TIME",
            ),
            contract,
        )

    def close(self):
        self.http.close()


class OpenD(Provider):
    name = "opend"

    def __init__(self, settings, budget):
        from futu import OpenQuoteContext, SysConfig

        (ROOT / ".state" / "opend-logs").mkdir(parents=True, exist_ok=True)
        SysConfig.enable_console_log(False)
        self.context = OpenQuoteContext(
            host=settings.opend_host, port=settings.opend_port
        )
        self.budget = budget

    def quote_many(self, contracts):
        from futu import RET_OK

        self.budget.acquire(self.name)
        codes = list(
            dict.fromkeys(
                code for c in contracts for code in (c.futu, "US." + c.ticker)
            )
        )
        ret, data = self.context.get_market_snapshot(codes)
        if ret != RET_OK:
            raise ProviderError("opend_snapshot_unavailable")
        rows = {r["code"]: r for r in data.to_dict("records")}
        result = {}
        for contract in contracts:
            try:
                result[contract.id] = self._quote_rows(contract, rows)
            except ProviderError as error:
                result[contract.id] = error
        return result

    def quote(self, contract):
        result = self.quote_many([contract])[contract.id]
        if isinstance(result, ProviderError):
            raise result
        return result

    def _quote_rows(self, contract, rows):
        from zoneinfo import ZoneInfo

        r = rows.get(contract.futu)
        if not r:
            raise ProviderError("contract_not_found")
        # Code alone is insufficient: validate available contract metadata.
        if str(r.get("option_type", "")) not in (
            contract.right,
            "CALL" if contract.right == "C" else "PUT",
        ):
            raise ProviderError("identity_unverified")
        if Decimal(str(r.get("option_strike_price", -1))) != contract.strike:
            raise ProviderError("identity_mismatch")
        if str(r.get("strike_time", ""))[:10] != str(contract.expiration):
            raise ProviderError("identity_mismatch")
        qt = None
        if r.get("update_time"):
            try:
                qt = (
                    datetime.fromisoformat(r["update_time"])
                    .replace(tzinfo=ZoneInfo("America/New_York"))
                    .astimezone(UTC)
                )
            except ValueError:
                pass
        f = {}
        for k, source, unit in [
            ("bid", "bid_price", "USD"),
            ("ask", "ask_price", "USD"),
            ("volume", "volume", "contracts"),
            ("open_interest", "option_open_interest", "contracts"),
            ("iv", "option_implied_volatility", "ratio"),
            ("delta", "option_delta", "ratio"),
            ("gamma", "option_gamma", "per_USD"),
            ("theta", "option_theta", "USD/day"),
            ("vega", "option_vega", "USD/percentage_point"),
        ]:
            num = r.get(source)
            if k == "iv" and num is not None:
                num = Decimal(str(num)) / 100
            f[k] = value(
                num,
                provider=self.name,
                as_of=None if k == "open_interest" else qt,
                unit=unit,
            )
        f["price"] = value(
            r.get("last_price"), provider=self.name, as_of=qt, price_type="last"
        )
        f["underlying_price"] = value(
            rows.get("US." + contract.ticker, {}).get("last_price"),
            provider=self.name,
            price_type="last",
        )
        return metrics(
            Pack(
                provider=self.name,
                instrument_id=contract.id,
                fields=f,
                quote_time=qt,
                delayed=True,
                evidence={"realtime_entitlement": "unverified"},
            ),
            contract,
        )

    def close(self):
        self.context.close()

    def bars(self, contract, start, end, underlying=False):
        from futu import RET_OK, KLType, AuType
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("America/New_York")
        code = "US." + contract.ticker if underlying else contract.futu
        first_date = str(start.astimezone(eastern).date())
        last_date = str((end - timedelta(microseconds=1)).astimezone(eastern).date())
        key = (code, first_date, last_date)
        cache = getattr(self, "_history_cache", {})
        self._history_cache = cache
        cached = cache.get(key)
        ttl = 60 if last_date >= str(datetime.now(eastern).date()) else 3600
        if cached and time.monotonic() - cached[0] < ttl:
            records = cached[1]
        else:
            self.budget.acquire(self.name, "baseline")
            ret, data, page = self.context.request_history_kline(
                code,
                start=first_date,
                end=last_date,
                ktype=KLType.K_1M,
                autype=AuType.NONE,
                max_count=1000,
            )
            if ret != RET_OK:
                raise ProviderError("opend_history_unavailable")
            if page:
                raise ProviderError("partial_history")
            records = data.to_dict("records")
            if any(r.get("code") != code for r in records):
                raise ProviderError("identity_mismatch")
            if len(cache) >= 128:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic(), records)
        bars = []
        for r in records:
            if r.get("code") != code:
                raise ProviderError("identity_mismatch")
            ts = (
                datetime.fromisoformat(r["time_key"])
                .replace(tzinfo=eastern)
                .astimezone(UTC)
            )
            if start <= ts < end:
                bars.append(
                    Bar(
                        start=ts,
                        open=r["open"],
                        high=r["high"],
                        low=r["low"],
                        close=r["close"],
                        volume=r["volume"],
                        trade_semantics_verified=True,
                        filled=r["volume"] <= 0,
                    )
                )
        return bars


def create_providers(settings, budget):
    result = []
    errors = {}
    for name in ("schwab", "opend", "polygon"):
        if name not in settings.providers.split(","):
            continue
        try:
            result.append(
                {"schwab": Schwab, "opend": OpenD, "polygon": Polygon}[name](
                    settings, budget
                )
            )
        except Exception as e:
            errors[name] = getattr(e, "code", "initialization_failed")
    return result, errors
