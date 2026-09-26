from __future__ import annotations
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class Contract(BaseModel):
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    expiration: date
    strike: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    right: Literal["C", "P"]
    root: str | None = None
    multiplier: Decimal = Field(default=Decimal(100), gt=0)
    standard: bool = True
    calendar: Literal["CBOE_Equity_Options", "CBOE_Index_Options"] = (
        "CBOE_Equity_Options"
    )

    @model_validator(mode="after")
    def no_unverified_adjustments(self):
        if (
            not self.standard
            or self.multiplier != 100
            or (self.root and self.root != self.ticker)
        ):
            raise ValueError("non_standard_contract_requires_verified_deliverable")
        return self

    @property
    def identity(self) -> str:
        return f"{self.root or self.ticker}|{self.expiration}|{self.right}|{self.strike.normalize()}|{self.multiplier}"

    @property
    def id(self) -> str:
        return digest(self.identity)[:32]

    @property
    def occ(self) -> str:
        return f"{self.root or self.ticker:<6}{self.expiration:%y%m%d}{self.right}{int(self.strike * 1000):08d}"

    @property
    def futu(self) -> str:
        return f"US.{self.root or self.ticker}{self.expiration:%y%m%d}{self.right}{int(self.strike * 1000)}"


class Value(BaseModel):
    value: Decimal | None = None
    origin: Literal[
        "source_reported",
        "provider_reported",
        "minute_close_estimate",
        "session_close_proxy",
        "model_estimate",
        "derived",
        "unavailable",
    ] = "unavailable"
    unit: str = "USD"
    provider: str | None = None
    as_of: datetime | None = None
    reason: str | None = "not_provided"
    price_type: str | None = None
    method: str | None = None
    evidence: str | None = None

    @field_validator("value")
    @classmethod
    def finite(cls, v):
        if v is not None and not v.is_finite():
            raise ValueError("non_finite_value")
        return v


class SourceEvent(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(default="original", max_length=200)
    original_ref: str | None = Field(default=None, max_length=300)
    # Provenance of this event. "relay" = reposted/forwarded content (e.g. a
    # Discord relay bot reposting a tweet or another Discord message).
    # L2 resonance and counting logic MUST exclude origin="relay" events:
    # they duplicate the original source and would otherwise double-count.
    origin: Literal["source", "relay"] = "source"
    subevent_id: str = Field(default="single", max_length=200)
    event_time: datetime
    author: str = ""
    content: str = Field(default="", max_length=100000)
    url: str = ""
    contract: Contract
    fields: dict[str, Value] = Field(default_factory=dict)
    raw: dict = Field(default_factory=dict)

    @field_validator("event_time")
    @classmethod
    def aware(cls, v):
        if v.tzinfo is None:
            raise ValueError("event_time_requires_timezone")
        return v.astimezone(UTC)

    @property
    def identity_key(self):
        return f"{self.original_ref or self.source + ':' + self.external_id}|{self.subevent_id}"


class Bar(BaseModel):
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    filled: bool = False
    trade_semantics_verified: bool = False
    last_trade_time: datetime | None = None

    @property
    def valid(self):
        return (
            self.start.tzinfo is not None
            and self.volume > 0
            and not self.filled
            and self.trade_semantics_verified
            and self.low > 0
            and self.low <= self.open <= self.high
            and self.low <= self.close <= self.high
        )


class Pack(BaseModel):
    provider: str
    instrument_id: str
    fields: dict[str, Value] = Field(default_factory=dict)
    quote_time: datetime | None = None
    received_at: datetime = Field(default_factory=now)
    matched: bool = True
    delayed: bool = False
    time_basis: str = "current"
    evidence: dict = Field(default_factory=dict)


def value(
    number,
    *,
    origin="provider_reported",
    provider=None,
    as_of=None,
    unit="USD",
    **kwargs,
) -> Value:
    if number is None:
        return Value(unit=unit)
    try:
        d = Decimal(str(number))
        if not d.is_finite():
            return Value(unit=unit, reason="invalid")
        return Value(
            value=d,
            origin=origin,
            provider=provider,
            as_of=as_of,
            unit=unit,
            reason=None,
            **kwargs,
        )
    except (ValueError, ArithmeticError):
        return Value(unit=unit, reason="invalid")


def metrics(pack: Pack, contract: Contract) -> Pack:
    f = pack.fields

    def n(key):
        return f.get(key, Value()).value

    def put(key, v, unit="ratio"):
        f[key] = value(
            v,
            origin="derived",
            provider=pack.provider,
            as_of=pack.quote_time,
            unit=unit,
            method="arithmetic-v1",
        )

    bid, ask = n("bid"), n("ask")
    if bid is not None and ask is not None and 0 < bid <= ask:
        mid = (bid + ask) / 2
        put("mid", mid, "USD")
        put("spread", (ask - bid) / mid)
    if (
        n("open_interest") is not None
        and n("open_interest") > 0
        and n("volume") is not None
    ):
        put("volume_oi", n("volume") / n("open_interest"))
    if n("underlying_price") is not None and n("underlying_price") > 0:
        put(
            "strike_distance",
            (contract.strike - n("underlying_price")) / n("underlying_price"),
        )
    return pack


def compare(baseline: Pack | None, current: Pack | None, applicable=True) -> dict:
    keys = set(baseline.fields if baseline else {}) | set(
        current.fields if current else {}
    )
    result = {}
    for key in sorted(keys):
        a = baseline.fields.get(key, Value()) if baseline else Value()
        b = current.fields.get(key, Value()) if current else Value()
        reason = None
        if not applicable:
            reason = "baseline_invalidated"
        elif a.value is None or b.value is None:
            reason = "missing_value"
        elif a.unit != b.unit:
            reason = "unit_mismatch"
        elif key == "open_interest" and (
            not a.as_of or not b.as_of or a.as_of.date() == b.as_of.date()
        ):
            reason = "oi_as_of_not_comparable"
        elif key in {"iv", "delta", "gamma", "theta", "vega"} and (
            not a.method or a.method != b.method
        ):
            reason = "model_not_comparable"
        elif a.provider != b.provider and not (
            key in {"price", "underlying_price"}
            and a.origin == "source_reported"
            and a.price_type
            and b.price_type
        ):
            reason = "provider_mismatch"
        estimated = a.origin in {
            "source_reported",
            "minute_close_estimate",
            "session_close_proxy",
            "model_estimate",
        } or (baseline and baseline.time_basis == "previous_session_proxy")
        delta = None if reason else b.value - a.value
        result[key] = {
            "status": "incomparable"
            if reason
            else "estimate"
            if estimated
            else "comparable",
            "delta": str(delta) if delta is not None else None,
            "reason": reason,
            "rule": "comparison-v1",
            "unit": a.unit,
        }
    return result
