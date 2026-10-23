#!/usr/bin/env python3
"""Market-level option-volume factors from optionbrief 585d history.

Reads mirror/data/optionbrief_history_584d.csv (+ intraday csv) and prints
current factor readings: volume/premium/CP momentum vs 20d MA, CP z-score,
60d volume percentile, per-category breakdown, intraday structure.

--refresh: re-fetch the optionbrief pack (same static technique as
ingest_optionbrief.py, stdlib only), rebuild both CSVs from
analytics.daily (+ latest day if missing), then print factors.
Use in optionbrief-daily.yml so the reading stays fresh automatically.
"""
import base64
import csv
import gzip
import json
import os
import re
import statistics
import sys
import urllib.request

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "optionbrief_history_584d.csv")
INTRA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "optionbrief_intraday_585d.csv")

FLOAT_KEYS = ("totalVol", "totalPremium", "marketCp",
              "idx_vol", "idx_prem", "idx_cp",
              "etf_vol", "etf_prem", "etf_cp",
              "stk_vol", "stk_prem", "stk_cp")


def load():
    rows = list(csv.DictReader(open(DATA)))
    for r in rows:
        for k in FLOAT_KEYS:
            r[k] = float(r[k]) if r.get(k) else None
    return rows


def ma(rows, key, i, n=20):
    vals = [r[key] for r in rows[max(0, i - n + 1):i + 1]
            if r[key] is not None]
    return sum(vals) / len(vals) if vals else None


def main() -> int:
    rows = load()
    i = len(rows) - 1
    r = rows[i]
    out = [f"date: {r['date']}"]
    vol_ma = ma(rows, "totalVol", i)
    prem_ma = ma(rows, "totalPremium", i)
    cp_ma = ma(rows, "marketCp", i)
    cp_vals = [x["marketCp"] for x in rows[i - 19:i + 1]
               if x["marketCp"] is not None]
    cp_z = ((r["marketCp"] - statistics.mean(cp_vals))
            / statistics.stdev(cp_vals)) if len(cp_vals) > 1 else 0
    vol60 = sorted(x["totalVol"] for x in rows[i - 59:i + 1]
                   if x["totalVol"] is not None)
    vol_pct = sum(1 for v in vol60 if v <= r["totalVol"]) / len(vol60) * 100
    out.append(
        f"量能动量: {r['totalVol']/1e6:.1f}M张 "
        f"({r['totalVol']/vol_ma:.2f}x 20日均, 60日分位 {vol_pct:.0f}%)")
    out.append(
        f"权利金动量: ${r['totalPremium']/1e9:.1f}B "
        f"({r['totalPremium']/prem_ma:.2f}x 20日均)")
    out.append(f"CP动量: {r['marketCp']:.2f} (20日均 {cp_ma:.2f}, z={cp_z:+.2f})")
    for name, k in (("指数", "idx"), ("ETF", "etf"), ("个股", "stk")):
        m = ma(rows, f"{k}_vol", i)
        mc = ma(rows, f"{k}_cp", i)
        if r[f"{k}_vol"] and m:
            out.append(
                f"  {name}: 量 {r[f'{k}_vol']/1e6:.1f}M张 "
                f"({r[f'{k}_vol']/m:.2f}x) | CP {r[f'{k}_cp']:.2f} "
                f"(均 {mc:.2f})")
    try:
        intra = list(csv.DictReader(open(INTRA)))
        ir = intra[-1]
        slots = [c for c in ir.keys() if c != "date"]
        vals = [float(ir[c]) for c in slots]
        tot = sum(vals)
        out.append(
            f"日内结构: 开盘30min {vals[0]/tot:.1%} | "
            f"尾盘30min {vals[-1]/tot:.1%} | "
            f"午间12:00-13:30 {sum(vals[5:8])/tot:.1%}")
    except FileNotFoundError:
        pass
    print("\n".join(out))
    return 0


def fetch_pack() -> dict:
    """Static fetch of optionbrief's __KZG_PACK__ (mirrors ingest_optionbrief)."""
    base = "https://optionbrief.com"
    ua = {"User-Agent": "muse-ob-factors/1.0 (+cross-validation reference)"}

    def _get(url: str) -> bytes:
        req = urllib.request.Request(url, headers=ua)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    html = _get(base + "/").decode("utf-8", errors="replace")
    m = re.search(r'/assets/(kzg-frame-[a-f0-9]+\.js)', html)
    if not m:
        raise RuntimeError("kzg-frame js not found (site layout changed?)")
    js = _get(base + "/assets/" + m.group(1)).decode("utf-8",
                                                     errors="replace")
    p = re.search(r'__KZG_PACK__="([^"]+)"', js)
    if not p:
        raise RuntimeError("__KZG_PACK__ not found (packaging changed?)")
    return json.loads(gzip.decompress(base64.b64decode(p.group(1))))


def refresh_csvs() -> int:
    """Rebuild the two history CSVs from the pack's analytics.daily."""
    try:
        pack = fetch_pack()
    except Exception as e:  # noqa: BLE001 - surface as exit code for the cron
        print(f"refresh failed: {e}", file=sys.stderr)
        return 2
    daily = sorted((pack.get("analytics") or {}).get("daily") or [],
                   key=lambda d: d.get("date") or "")
    latest = (pack.get("index") or {}).get("latestDate")
    if latest and latest not in {d.get("date") for d in daily}:
        day = (pack.get("days") or {}).get(latest) or {}
        ov = day.get("overview") or {}
        buckets = (day.get("buckets") or {}).get("market")
        daily.append({"date": latest,
                      "totalVol": ov.get("totalVol"),
                      "totalPremium": ov.get("totalPremium"),
                      "marketCp": ov.get("marketCp"),
                      "category": ov.get("category"),
                      "buckets": buckets})
    by_date = {}
    for d in daily:
        if d.get("date"):
            by_date[d["date"]] = d  # dedupe, last wins
    rows = [by_date[k] for k in sorted(by_date)]
    if not rows:
        print("refresh failed: pack has no usable daily series",
              file=sys.stderr)
        return 2
    cols = ["date", "totalVol", "totalPremium", "marketCp",
            "idx_vol", "idx_prem", "idx_cp",
            "etf_vol", "etf_prem", "etf_cp",
            "stk_vol", "stk_prem", "stk_cp"]
    with open(DATA, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in rows:
            cat = d.get("category") or {}
            out = [d.get("date"), d.get("totalVol"),
                   d.get("totalPremium"), d.get("marketCp")]
            for cname in ("INDEX", "ETF", "STOCK"):
                c = cat.get(cname) or {}
                out += [c.get("volume"), c.get("premium"),
                        c.get("cpRatio")]
            w.writerow(out)
    header = None
    if os.path.exists(INTRA):
        with open(INTRA) as old:
            header = next(csv.reader(old), None)
    if not header:
        header = ["date"] + [f"slot{i}" for i in range(13)]
    n_slots = len(header) - 1
    with open(INTRA, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for d in rows:
            b = list(d.get("buckets") or [])
            b += [""] * (n_slots - len(b))
            w.writerow([d.get("date")] + b[:n_slots])
    print(f"refreshed {len(rows)} days ({rows[0]['date']}..{rows[-1]['date']})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    rc = 0
    if "--refresh" in sys.argv[1:]:
        rc = refresh_csvs()
    if rc == 0:
        rc = main()
    sys.exit(rc)
