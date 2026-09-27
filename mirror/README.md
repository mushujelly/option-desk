Flow Mirror — option-flow 数据镜像到独立 Atlas M0
把期权流数据镜像到独立的 Atlas M0 免费集群（flow_mirror 库），与 wfreedom 物理隔离。
每条数据都带来源标签，debug 时一眼看出是哪条路、哪个频道进来的。
数据流
路
脚本
数据源
写入集合
来源标签
A
sync_wfreedom.py
wfreedom.option_flow_flows（已有解析数据）
option_flow_flows
mirror_meta.synced_via="wfreedom" + channel_label
B
poll_discord.py
Discord 频道直捞
discord_raw（原文） + option_flow_flows（尽力解析）
mirror_meta.synced_via="discord-direct" + channel_label
去重：A 路按 _id upsert；B 路 raw 按 message_id 唯一索引；B 路解析前检查该 message_id 是否已在 A 路入库，撞车跳过。
水位线：存在 _sync_state 集合，A 路记 last_first_seen_at，B 路记各频道的 last_message_id。重跑不丢不重。
一次性 setup（本地跑一次）
export NEW_MDB_URI='mongodb+srv://<user>:<pass>@cluster0.xxxxx.mongodb.net/?appName=Cluster0'
python3 setup_new_db.py
GitHub Actions（持续运行，每 5 分钟）
把 flow-mirror.yml 放到仓库 .github/workflows/ 下。
仓库 Settings → Secrets and variables → Actions，填：
Secrets: NEW_MDB_URI（新集群连接串）、WFREEDOM_MDB_URI（旧 wfreedom 集群连接串）、DISCORD_TOKEN（Discord token，原样粘贴；bot token 带 Bot  前缀）
Variables: DISCORD_CHANNELS，格式 频道ID:标签,频道ID:标签，例如：
     1386775915991797951:notableflow,123456789012345678:flowgod
     标签不写则自动调 API 解析频道名。
Actions 页手动点一次 workflow_dispatch 验证，之后每 5 分钟自动跑。
Debug 查数
// 某频道今天进了多少
db.option_flow_flows.countDocuments({channel_label: "notableflow"})
// 哪条路进来的
db.option_flow_flows.countDocuments({"mirror_meta.synced_via": "discord-direct"})
// 各路水位线
db._sync_state.find()
Storage 估算（实测）
A 路：最近每天约 30 条 flow 文档，每条 ~2KB → 约 60KB/天，年增 ~22MB
B 路 raw：按每天 ~100 条 Discord 消息、每条 ~1.5KB → 约 150KB/天，年增 ~55MB
含索引开销，年增 < 100MB → M0 的 512MB 轻松扛 5 年以上
定时清理（可选）
cleanup_old.py + flow-mirror-cleanup.yml（每周日 03:00 UTC 跑，默认保留 365 天）。
本地试运行（只报告不删）：
NEW_MDB_URI='...' DRY_RUN=1 python3 cleanup_old.py
不想自动清理就把 flow-mirror-cleanup.yml 从 .github/workflows/ 删掉。
