"""Read-only public minute-history check; never persists a synthetic source event."""
from datetime import timedelta
import json
from option_desk.settings import Settings
from option_desk.store import Store
from option_desk.domain import Contract, now
from option_desk.calendar import locate
from option_desk.budget import Budget
from option_desk.providers import OpenD
from option_desk.baseline import build
s=Settings();store=Store(s.database_url);p=OpenD(s,Budget(store))
try:
 contract=Contract(ticker='AAPL',expiration='2026-09-28',strike=330,right='C')
 window=locate(contract,now())
 from zoneinfo import ZoneInfo
 eastern=ZoneInfo('America/New_York')
 start=now().astimezone(eastern).replace(hour=9,minute=30,second=0,microsecond=0)
 end=now()-timedelta(minutes=1)
 bars=p.bars(contract,start,end)
 good=[b for b in bars if b.valid]
 if good:
  # Choose an actually observed trade minute for a data capability check, not a claimed order.
  bar=good[-1]
  status,pack,reason=build(contract,bar.start,{},[p])
  report={'status':status,'reason':reason,'provider':pack.provider if pack else None,'price_origin':pack.fields.get('price').origin if pack and pack.fields.get('price') else None,'time_basis':pack.time_basis if pack else None,'has_bar':bool(pack and pack.evidence.get('bar')),'valid_minutes_observed':len(good),'event_persisted':False}
 else:report={'status':'insufficient_data','reason':'no_positive_volume_bar','event_persisted':False}
 store.state('baseline_check',report)
 print(json.dumps(report,ensure_ascii=False,indent=2))
finally:p.close()
