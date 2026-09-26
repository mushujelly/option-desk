from datetime import timedelta
from .calendar import locate
from .domain import Pack, Value, value, metrics, now
from .providers import ProviderError
from .budget import BudgetWait


def build(contract, event_time, source_fields, providers):
    window = locate(contract, event_time)
    if window["end"] > now():
        raise BudgetWait((window["end"] - now()).total_seconds() + 5)
    errors = {}
    selected = None
    bar = None
    spot = None
    for provider in providers:
        try:
            rows = provider.bars(contract, window["start"], window["end"])
            good = sorted(
                [
                    b
                    for b in rows
                    if b.valid and window["start"] <= b.start < window["end"]
                ],
                key=lambda b: b.start,
            )
            if not good:
                errors[provider.name] = "no_valid_bar"
                continue
            bar = good[-1]
            selected = provider
            try:
                underlying = provider.bars(
                    contract,
                    bar.start,
                    bar.start + timedelta(minutes=1),
                    underlying=True,
                )
                spot = next(
                    (b for b in underlying if b.valid and b.start == bar.start), None
                )
            except ProviderError:
                pass
            break
        except ProviderError as e:
            errors[provider.name] = e.code
    fields = {k: Value.model_validate(v) for k, v in source_fields.items()}
    fields = {
        k: v
        for k, v in fields.items()
        if v.value is not None and v.origin == "source_reported"
    }
    evidence = {
        "window": {
            k: v.isoformat() if hasattr(v, "isoformat") else v
            for k, v in window.items()
        },
        "provider_errors": errors,
        "validation": "insufficient_data",
        "model_estimation": "disabled",
    }
    if not bar and window["basis"] == "previous_session_proxy":
        reason = (
            "no_valid_bar_in_latest_session"
            if errors and all(v == "no_valid_bar" for v in errors.values())
            else "history_fetch_failed"
        )
        return "discarded", None, reason
    if bar:
        provider = selected.name
        evidence["bar"] = bar.model_dump(mode="json")
        price = fields.get("price")
        size = fields.get("contracts")
        if window["basis"] == "event_minute":
            checks = []
            if price and price.price_type == "reported_trade":
                checks.append(bar.low <= price.value <= bar.high)
            if size:
                checks.append(size.value <= bar.volume)
            evidence["validation"] = (
                "inconsistent"
                if checks and not all(checks)
                else "consistent"
                if checks
                else "insufficient_data"
            )
        evidence["bar_presence_confirmed"] = True
        fields.setdefault(
            "price",
            value(
                bar.close,
                origin="minute_close_estimate",
                provider=provider,
                as_of=bar.start + timedelta(minutes=1),
                price_type="minute_close",
            ),
        )
        if spot:
            fields.setdefault(
                "underlying_price",
                value(
                    spot.close,
                    origin="minute_close_estimate",
                    provider=provider,
                    as_of=spot.start + timedelta(minutes=1),
                    price_type="minute_close",
                ),
            )
    else:
        provider = "source"
    if fields.get("price") and fields.get("contracts") and not fields.get("premium"):
        fields["premium_estimate"] = value(
            fields["price"].value * fields["contracts"].value * contract.multiplier,
            origin="derived",
            provider=provider,
            method="price_x_source_size_x_multiplier-v1",
        )
    pack = metrics(
        Pack(
            provider=provider,
            instrument_id=contract.id,
            fields=fields,
            quote_time=bar.start if bar else event_time,
            time_basis=window["basis"],
            evidence=evidence,
        ),
        contract,
    )
    return (
        ("ready", pack, None)
        if fields.get("price")
        else ("unavailable", pack, "event_minute_unavailable")
    )
