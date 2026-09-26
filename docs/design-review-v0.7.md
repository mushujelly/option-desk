# Option Desk v0.7 设计评审意见

- 评审对象：`docs/infrastructure-design.md` v0.7
- 日期：2026-09-26
- 状态：评审意见，不直接修改设计文档；是否采纳由设计作者决定
- 前序评审：`docs/design-review-v0.2.md`、`docs/design-review-v0.4.md`、`docs/design-review-v0.6.md`

## 总体评价

v0.7 已落实 v0.6 评审的全部意见，无设计层面的 open 问题。剩余事项均为阶段 0/1 实施规格中待填的内容。

## v0.6 评审意见在 v0.7 的处理情况

| 意见 | 状态 | 说明 |
|---|---|---|
| P2 对照接口定位粒度 | 已解决 | 第 16.1 节改用 `GET /api/v1/baselines/{baseline_id}/comparison`；响应同时返回 `baseline_id`、`logical_event_id`、`instrument_id`；未知 id 返回 404 |
| P2 SSE 事件词汇表 | 已解决 | 第 16.3 节定义事件类型（`baseline_ready`、`baseline_discarded`、`current_updated`、`provider_changed`、`job_failed`、`resync_required`）与重连语义：先建流暂存通知再读取，读完合并；版本低忽略、版本高重查；页面获焦重同步 + 低频 API 校验 |
| P3 验收补两项 | 已解决 | 第 18 节新增“可比性矩阵”“原子读取”“SSE 恢复”三类验收 |
| 待确认 非交易时段丢弃后果 | 已澄清 | 第 18 节明确“最近交易日无有效期权分钟 K 线才因无数据退出监控，不因笼统的‘流动性差’丢弃” |
| 文字：“来源差异已解释”主语不明 | 已解决 | 第 10.3 节改为结构化口径判定（`price_type`、`unit`、`currency`、`provider`、`method_id`、`method_version`、有效时间、`provenance_ref`、`normalization_rule_id`）；明确不得根据人工备注或自由文本放行；未知模型的 IV/Greeks 比较不借此放行 |
| 文字：DDL 唯一约束 | 已解决 | 第 7 节与第 20.1 节写死：`event_baselines` 唯一约束 `(logical_event_id, instrument_id)`，`current_states` 唯一约束 `instrument_id` |

## P2：`logical_event` 的去重判定规则仍是空的

整个基准体系已 key 在 `(logical_event_id, instrument_id)` 上，但“什么算同一单子的重复转发、什么算新事件”的判定规则，第 9.3 节目前只有原则（相似度只能辅助关联、保留修订关系等），阶段 1 的解析器无法据此确定性地分配 `logical_event_id`。

建议：在阶段 0“身份规则”交付物中把去重判定写死，至少包括：

1. 合并时间窗口（同一作者 + 同一合约在 N 分钟内出现，算重复转发还是新事件）；
2. 跨来源关联规则（Discord 转发与 MongoDB 中同一 X 原文如何认定为同一逻辑事件）；
3. 修订 vs 新事件的边界（修订改变 strike / 到期日时，是关联修订还是拆分为新事件）。

## P3：`logical_events` 的“合约集合”建议派生而非存储

第 7 节 `logical_events` 存了“合约集合”，但该集合可从 `event_baselines` 按 `logical_event_id` 聚合派生。建议不单独存储，避免与基准表漂移；第 7.1 节要求的“多合约事件返回明确的合约条目数组”走派生查询。

## P3：并发创建基准时的唯一冲突需要 worker 侧行为

第 18 节已要求“数据库拒绝重复 `(logical_event_id, instrument_id)`”，建议补一句 worker 侧行为：使用 `INSERT ... ON CONFLICT DO NOTHING`（或等价机制），冲突后认领已有记录继续流程，而不是直接报错中断。

## 文字修订建议

第 16.3 节“通知版本低于已展示版本时忽略”：`baseline_ready` / `baseline_discarded` 类通知本身没有版本号。建议明确版本语义：`current_updated` / `provider_changed` 用 `current_version` 比较；baseline 类通知按状态机单向流转（pending → ready/discarded，不回退），重复或乱序到达时以状态机为准。

## 修订记录

| 日期 | 内容 |
|---|---|
| 2026-09-26 | v0.7 首轮评审：1 个 P2、2 个 P3、1 个文字修订；同步 v0.6 意见处理情况 |
