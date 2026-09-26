"""One bounded, read-only full pipeline check against project-configured sources."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from option_desk.settings import Settings
from option_desk.store import Store, instruments, watches
from option_desk.providers import OpenD
from option_desk.budget import Budget
from option_desk.sources import Sources
from option_desk.domain import Contract
from sqlalchemy.dialects.postgresql import insert

s = Settings()
store = Store(s.database_url)
store.migrate()
budget = Budget(store)
report = {}
source = Sources(s, store, budget)
for name in ("discord", "mongo"):
    try:
        getattr(source, name)()
        report[name] = "read_completed"
    except Exception as e:
        report[name] = getattr(e, "code", type(e).__name__)
p = OpenD(s, budget)
try:
    from futu import RET_OK

    ret, data = p.context.get_option_expiration_date("US.AAPL")
    if ret == RET_OK:
        future = data[
            data["strike_time"] > str(datetime.now(ZoneInfo("America/New_York")).date())
        ]
        expiry = (future if len(future) else data).iloc[0]["strike_time"]
        ret, chain = p.context.get_option_chain("US.AAPL", start=expiry, end=expiry)
        if ret == RET_OK and len(chain):
            calls = chain[chain["option_type"] == "CALL"]
            row = calls.iloc[len(calls) // 2]
            contract = Contract(
                ticker="AAPL",
                expiration=row["strike_time"],
                strike=row["strike_price"],
                right="C",
            )
            pack = p.quote(contract)
            store.put_current(pack)
            with store.engine.begin() as c:
                c.execute(
                    insert(instruments)
                    .values(id=contract.id, contract=contract.model_dump(mode="json"))
                    .on_conflict_do_nothing()
                )
                c.execute(
                    insert(watches)
                    .values(instrument_id=contract.id, interval=60)
                    .on_conflict_do_nothing()
                )
            report["quote"] = {
                "contract": contract.model_dump(mode="json"),
                "fields_present": [
                    k for k, v in pack.fields.items() if v.value is not None
                ],
                "quote_time": str(pack.quote_time),
            }
finally:
    p.close()
store.state("live_check", report)
print(json.dumps(report, ensure_ascii=False, indent=2))
