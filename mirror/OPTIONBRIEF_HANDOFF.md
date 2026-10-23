# 给接管 Agent 的说明（先读这段）

你正在接管一条已建成、已验证的 OptionBrief 每日数据管线。不要重写，按下面做：

1. 代码全在 repo 的 mirror/ 目录：ingest_optionbrief.py（抓取+入库+快照）、ob_premarket_brief.py（盘前简报，独立运行不依赖数据库）、ob_factors.py（因子计算）、push_file.py（推文件回 repo）。
2. 依赖：pip install -r mirror/requirements.txt（pymongo 等）。
3. 必需环境变量：NEW_MDB_URI（MongoDB Atlas 连接串，向用户索取，不要写进代码或日志）、MIRROR_DB=flow_mirror。
4. 每日运行：11:30 UTC 后跑 ingest_optionbrief.py（BACKFILL=1，幂等）；盘前跑 ob_premarket_brief.py，把输出发给用户。
5. 数据是 T+1：网站每天约 10:03 UTC 发布上个交易日的数据包；周一拿到的是周五数据，正常。
6. 已知问题和恢复步骤见文末，不要重复踩坑。

---

# OptionBrief 日报管线 — 交接文档

> 2026-10-23 整理。管线已建成并验证，当前处于暂停状态（2026-10-20 起全局暂停），恢复只需重新启用两个 cron。

## 这是什么

每天抓取 optionbrief.com（KZG Option House）的全市场期权聚合数据包（T+1），做三件事：
1. 入库 MongoDB Atlas（flow_mirror 库）
2. 落盘快照到 GitHub 仓库 mushujelly/option-desk
3. 盘前生成中文 brief（站方 digest + 全市场量/CP + 权利金 Top8）推送给用户

数据内容：全市场总量/总权利金/市场 CP、INDEX/ETF/STOCK 三类拆分、日内 13 个半小时槽量能（含 VIX 热力图）、38 个核心标的当日明细、站方中文 digest 摘要。

## 组件清单（都在 ~/workspace/mirror/，同步在 repo mirror/ 目录）

| 文件 | 作用 |
|---|---|
| ingest_optionbrief.py | 主抓取：拉数据包 → Atlas upsert（ob_market_overview / ob_market_daily / ob_intraday）+ 写本地快照 mirror/data/optionbrief_snapshots/YYYY-MM-DD.json；env BACKFILL=1 时回填 analytics.daily 全部历史（幂等） |
| ob_premarket_brief.py | 盘前 brief：直接拉网站数据包，输出中文简报，不依赖数据库 |
| ob_factors.py | 市场级因子：量能/权利金/CP 相对 20 日均值、CP z 分数、60 日量能分位、三类拆分、日内结构、activity_top8（按权利金取 Top8） |
| push_file.py | 把本地文件推到 GitHub 仓库（Contents API） |

## 数据存档（repo mirror/data/）

- optionbrief_history_584d.json / .csv — 584+ 天市场级历史（date/totalVol/totalPremium/marketCp/三类拆分/13 槽 buckets）
- optionbrief_intraday_585d.csv — 585 天 × 13 槽日内量能
- optionbrief_symbols_agg.json — 255 个标的累计成交量
- optionbrief_snapshots/YYYY-MM-DD.json — 每日快照，覆盖到 2026-10-19

## 定时任务（当前均 disabled，2026-10-20 全局暂停）

| cron id | 时间 | 做什么 |
|---|---|---|
| ob-daily-ingest-vm | 每天 11:30 UTC | 跑 ingest（抓取 + 快照推送）；Atlas 直写见下方已知问题 |
| ob-premarket-brief | 工作日 08:00 America/Chicago | 跑 ob_premarket_brief.py，输出原样推送到用户 side chat |

另有 GitHub Actions 备用线：.github/workflows/optionbrief-daily.yml（schedule 11:30 UTC，实际常延迟 5–9 小时），从 GitHub runner 写 Atlas，是目前 Atlas 数据的实际来源。

## 凭据

- Atlas 连接串：GitHub secret NEW_MDB_URI；用户另在 Muse Secure Vault 存了一份（connector 名 custom.mongodb-atlas-flowmirror）
- 数据库名：flow_mirror（env MIRROR_DB）

## 已知问题 / 未决事项

1. **VM 直写 Atlas 被拦**：Muse VM 的 egress 拦截 Mongo wire 协议（Direct network protocols → databases = Deny）。需用户在 Muse 设置里改成 Ask/Allow 才能启用 VM 直写；在此之前靠 GitHub 备用线写 Atlas（延迟数小时，不丢数据）。
2. **逐标的因子未完**：标的级轮动/动量需要 ~20 个交易日的逐标的快照积累；暂停前攒了 10-06~10-19 约 10 个交易日，恢复后继续攒。注意：网站数据包只含最新一天的逐标的明细，暂停期间错过的天数补不回；市场级历史可随时全量回填（BACKFILL=1）。
3. 网站发布时间：每天约 10:03 UTC 发布新包（T+1）；周一早上拿到的是周五数据，正常。

## 恢复步骤

1. 启用 cron ob-daily-ingest-vm 和 ob-premarket-brief
2. 手动跑一次 ingest_optionbrief.py（BACKFILL=1）回填市场级历史缺口
3. 验证：Atlas ob_market_overview 文档数应 ≥594；repo 出现当天快照文件
