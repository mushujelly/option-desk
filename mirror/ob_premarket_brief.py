#!/usr/bin/env python3
"""OptionBrief 盘前 brief：抓最新数据包，输出中文简报。

直接读 optionbrief.com（不等 GitHub workflow），供盘前 cron 调用。
输出：市场总览 + 站方 digest + activity top8 + 关键变化。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_optionbrief as I


def main() -> int:
    try:
        pack = I.fetch_pack()
    except Exception as e:
        print(f"optionbrief 抓取失败：{e}")
        return 1
    ov_docs, daily_docs, intra_docs = I.build_docs(pack, backfill=False)
    ov = ov_docs[0]
    date = ov["date"]

    lines = [f"📊 OptionBrief 日报（{date}，T+1）", ""]
    # 站方 digest
    for d in (ov.get("digest") or []):
        lines.append(f"• {d}")
    lines.append("")
    # 关键数字
    tv = ov.get("totalVol") or 0
    tp = ov.get("totalPremium") or 0
    cp = ov.get("marketCp") or 0
    lines.append(
        f"全市场：{tv/1e6:.1f}M张｜${tp/1e9:.1f}B｜CP {cp:.2f}")
    cats = ov.get("categories") or {}
    for name, key in (("指数", "INDEX"), ("ETF", "ETF"), ("个股", "STOCK")):
        c = cats.get(key) or {}
        if c.get("volume"):
            lines.append(
                f"  {name}：{c['volume']/1e6:.1f}M张｜CP {c.get('cpRatio', 0):.2f}")
    lines.append("")
    # activity top8
    top8 = sorted(
        [d for d in daily_docs if d.get("activity_top8")],
        key=lambda d: -(d.get("premiumNotional") or 0))
    if top8:
        lines.append("🔥 成交活动 Top8（按权利金）：")
        for d in top8:
            prem = (d.get("premiumNotional") or 0) / 1e9
            lines.append(
                f"  {d['symbol']} ${prem:.2f}B｜{d.get('totalVol', 0)/1e4:.1f}万张"
                f"｜CP {d.get('cpRatio', 0):.2f}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
