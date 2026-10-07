# 提醒查询 API

本机服务：`http://127.0.0.1:8765`，交互文档 `/docs`。

`GET /api/v1/baselines` 支持下列可组合参数；在数据库中先筛选，再分页。

| 参数 | 含义 |
|---|---|
| ticker | 股票代码精确匹配，忽略大小写，例如 VST |
| right | C 或 P |
| expiration | 到期日，YYYY-MM-DD |
| event_from | 发生时间起点，包含；必须携带时区 |
| event_to | 发生时间终点，不包含；必须携带时区 |
| status | 发生时基准状态：pending、ready、unavailable、discarded；不是当前行情任务状态 |
| search | 股票代码、到期日、行权价的文本包含搜索，忽略大小写 |
| include_discarded | 默认 false；true 才能查询丢弃及非活跃诊断记录，仍隐藏已到期合约 |
| limit | 每页条数，默认 200，范围 1–500 |
| offset | 跳过的条数，默认 0 |

返回 `items`、`total`（匹配总数）、`limit`、`offset`、`next_offset`（无下一页为 null）、`schema_version`。
按发生时间倒序、记录 ID 排序。偏移分页不是冻结快照，持续新增数据时翻页位置可能移动；刷新可重新读取。不保存查询快照。

示例：

```sh
curl --get 'http://127.0.0.1:8765/api/v1/baselines' \
  --data-urlencode 'ticker=VST' \
  --data-urlencode 'right=C' \
  --data-urlencode 'event_from=2026-09-25T00:00:00-04:00' \
  --data-urlencode 'event_to=2026-09-26T00:00:00-04:00' \
  --data-urlencode 'limit=50'
```

时间缺时区、倒置时间范围或非法状态会返回 422。使用东八区时务必 URL 编码 `+08:00`（上述 `--data-urlencode` 自动处理）。

页面搜索框和 Call/Put 筛选查询全部符合条件的记录，并提供上一页/下一页。日期与状态组合筛选目前通过 API 使用。导出接口仍为原有最多 500 条全局导出，不跟随页面筛选。
