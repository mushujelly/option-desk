"""Read-only bounded capability check. Never accesses account/trading APIs."""

import json
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from futu import OpenQuoteContext, SysConfig, RET_OK, KLType, AuType

SysConfig.enable_console_log(False)
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
report = {
    "provider": "opend",
    "checked_at": datetime.now().isoformat(),
    "account_access": False,
}
try:
    ret, exp = ctx.get_option_expiration_date("US.AAPL")
    report["expiration_ret"] = ret
    if ret == RET_OK:
        expiry = exp.iloc[0]["strike_time"]
        ret, chain = ctx.get_option_chain("US.AAPL", start=expiry, end=expiry)
        report["chain_ret"] = ret
        if ret == RET_OK and len(chain):
            calls = chain[chain["option_type"] == "CALL"]
            # Select middle strike; probe is capability verification, not investment selection.
            row = calls.iloc[len(calls) // 2]
            code = row["code"]
            report["contract"] = code
            ret, snap = ctx.get_market_snapshot([code])
            report["snapshot_ret"] = ret
            if ret == RET_OK:
                report["snapshot_fields"] = [
                    c
                    for c in snap.columns
                    if c.startswith("option_")
                    or c in ("strike_time", "update_time", "bid_price", "ask_price")
                ]
            today = datetime.now(ZoneInfo("America/New_York")).date()
            ret, bars, page = ctx.request_history_kline(
                code,
                start=str(today - timedelta(days=1)),
                end=str(today),
                ktype=KLType.K_1M,
                autype=AuType.NONE,
                max_count=3,
            )
            report["minute_ret"] = ret
            if ret == RET_OK:
                report["minute_rows"] = len(bars)
                report["minute_columns"] = list(bars.columns)
            else:
                report["minute_error"] = str(bars)[:400]
    else:
        report["error"] = str(exp)[:400]
finally:
    ctx.close()
Path(".state").mkdir(exist_ok=True)
Path(".state/opend-capability.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2)
)
print(json.dumps(report, ensure_ascii=False, indent=2))
