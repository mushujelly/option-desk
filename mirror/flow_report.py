#!/usr/bin/env python3
"""Unified weekly brief across the three cross-validation sources.

1. flow_mirror.option_flow_flows (Atlas) — Discord flow mirror
2. flow_mirror.ob_market_overview / ob_market_daily (Atlas) — optionbrief.com
3. mirror/data/email_options_daily.jsonl (repo-local, pushed from VM) —
   Cboe Daily Recap / Barchart unusual / MC sentiment

Usage:
  python3 mirror/flow_report.py --days 7 --out mirror/data/weekly_brief.md

Writes <out> (markdown) and <out>.json sidecar. Designed to run in GitHub
Actions (has NEW_MDB_URI); the email JSONL arrives via repo push from the VM.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import Counter, defaultdict

EMAIL_JSONL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "email_options_daily.jsonl")


def fmt_money(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="mirror/data/weekly_brief.md")
    args = ap.parse_args()

    uri = os.environ.get("NEW_MDB_URI", "")
    if not uri:
        print("NEW_MDB_URI not set", file=sys.stderr)
        return 2
    from pymongo import MongoClient
    db = MongoClient(uri, serverSelectionTimeoutMS=30000)[
        os.environ.get("MIRROR_DB", "flow_mirror")]

    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=args.days)
    since = start.strftime("%Y-%m-%d")
    start_d = start.strftime("%Y-%m-%d")
    end_d = end.strftime("%Y-%m-%d")

    flows = db["option_flow_flows"]
    md: list[str] = []
    md.append(f"# Flow 周报（{start_d} ~ {end_d}）\n")

    # ---- 1. Discord flow mirror ----
    md.append("## 1. 自家 Flow（Discord mirror）")
    total = flows.count_documents({"first_seen_at": {"$gte": since}})
    md.append(f"- 新增 {total} 条 flow 记录")
    by_sym = list(flows.aggregate([
        {"$match": {"first_seen_at": {"$gte": since}}},
        {"$group": {"_id": "$symbol",
                    "n": {"$sum": 1},
                    "prem": {"$sum": "$premium_usd"}}},
        {"$sort": {"prem": -1}},
        {"$limit": 15},
    ]))
    if by_sym:
        md.append("- 权利金 Top 15：")
        for r in by_sym:
            md.append(f"  - {r['_id']}: {r['n']} 条, {fmt_money(r['prem'])}")
    big = list(flows.find(
        {"first_seen_at": {"$gte": since},
         "premium_usd": {"$gte": 500000}},
        {"symbol": 1, "premium_usd": 1, "expiration_date": 1,
         "strike_text": 1, "option_type": 1, "flow_direction_label": 1,
         "channel_label": 1, "first_seen_at": 1}
    ).sort("premium_usd", -1).limit(10))
    if big:
        md.append("- 大单 Top 10（≥$500K）：")
        for b in big:
            md.append(
                f"  - {b.get('symbol')} {b.get('expiration_date')} "
                f"{b.get('strike_text')}{b.get('option_type')} "
                f"{fmt_money(b.get('premium_usd'))} "
                f"{b.get('flow_direction_label') or ''} "
                f"[{b.get('channel_label')}]")
    by_ch = list(flows.aggregate([
        {"$match": {"first_seen_at": {"$gte": since}}},
        {"$group": {"_id": "$channel_label", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 10},
    ]))
    if by_ch:
        md.append("- 渠道分布：" +
                  ", ".join(f"{r['_id']} {r['n']}" for r in by_ch))

    # ---- 2. optionbrief ----
    md.append("\n## 2. 全市场（optionbrief）")
    ob = list(db["ob_market_overview"].find(
        {"date": {"$gte": start_d, "$lte": end_d}}).sort("date", 1))
    if ob:
        vols = [d.get("totalVol") for d in ob if d.get("totalVol")]
        prems = [d.get("totalPremium") for d in ob if d.get("totalPremium")]
        cps = [d.get("marketCp") for d in ob if d.get("marketCp")]
        md.append(
            f"- {len(ob)} 个交易日：日均成交 {fmt_money(sum(vols) / len(vols)) if vols else '—'} 张, "
            f"日均权利金 {fmt_money(sum(prems) / len(prems)) if prems else '—'}, "
            f"平均 C/P {sum(cps) / len(cps):.2f}" if cps else "")
        top_syms = list(db["ob_market_daily"].aggregate([
            {"$match": {"date": {"$gte": start_d, "$lte": end_d}}},
            {"$group": {"_id": "$symbol",
                        "prem": {"$sum": "$premiumNotional"},
                        "vol": {"$sum": "$totalVol"}}},
            {"$sort": {"prem": -1}},
            {"$limit": 10},
        ]))
        if top_syms:
            md.append("- 权利金 Top 10 标的：" +
                      ", ".join(f"{r['_id']} {fmt_money(r['prem'])}"
                                for r in top_syms))
    else:
        md.append("- 本周无 optionbrief 数据（workflow 未上线或网站未更新）")

    # ---- 3. email sources ----
    md.append("\n## 3. 邮件源（Cboe / Barchart / MC）")
    email_docs = []
    if os.path.exists(EMAIL_JSONL):
        with open(EMAIL_JSONL) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if start_d <= d.get("date", "") <= end_d:
                    email_docs.append(d)
    if email_docs:
        by_src = Counter(d["src"] for d in email_docs)
        md.append("- 条数：" +
                  ", ".join(f"{s} {n}" for s, n in by_src.most_common()))
        blocks = sorted(
            [d for d in email_docs if d.get("kind") == "size_block"
             and d.get("size")],
            key=lambda d: d["size"], reverse=True)[:8]
        if blocks:
            md.append("- Cboe 大宗 Top：")
            for b in blocks:
                md.append(
                    f"  - {b['symbol']} {b.get('expiry')} {b.get('strike')}"
                    f"{(b.get('pc') or '').upper()} x{int(b['size'])} "
                    f"{b.get('side')}")
        unusual = set()
        for d in email_docs:
            if d.get("kind") == "recap" and d.get("unusual_names"):
                unusual.update(d["unusual_names"])
        if unusual:
            md.append("- Cboe 点名 unusual： " + ", ".join(sorted(unusual)))
        bc = sorted(
            [d for d in email_docs if d.get("kind") == "unusual_contract"
             and d.get("volume")],
            key=lambda d: d["volume"], reverse=True)[:8]
        if bc:
            md.append("- Barchart unusual Top：")
            for b in bc:
                md.append(
                    f"  - {b['symbol']} {b.get('expiry')} {b.get('strike')}"
                    f"{b.get('option_type')} Vol {int(b['volume']):,} "
                    f"OI {int(b.get('open_interest') or 0):,}")
        for side in ("bullish", "bearish"):
            se = sorted(
                [d for d in email_docs
                 if d.get("kind") == "sentiment" and d.get("side") == side
                 and d.get("net_delta_volume")],
                key=lambda d: abs(d["net_delta_volume"]),
                reverse=True)[:5]
            if se:
                md.append(
                    f"- MC {side} Top： " +
                    ", ".join(
                        f"{d['symbol']} {fmt_money(d['net_delta_volume'])}"
                        for d in se))
    else:
        md.append("- 邮件 JSONL 未随 repo 下发（VM 推送未配置）")

    md.append(f"\n_生成时间 {end.strftime('%Y-%m-%d %H:%M')} UTC_")
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        f.write("\n".join(md) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
