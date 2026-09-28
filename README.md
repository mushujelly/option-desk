# Option Desk

独立运行的期权数据监控台，只做基础设施，不提供策略、评分或交易执行。

本项目拥有自己的 Python 环境、前端依赖、PostgreSQL 数据、任务队列、请求预算和配置。运行时不读取 midas 的代码、数据库、配置或进程；midas 可以完全停止。OpenD 是外部行情网关，需要单独保持运行和登录。

## 本地启动

要求 Python 3.12+、Node.js 22+。首次安装需要联网：

```sh
sh scripts/setup.sh
npm run dev
```

打开 http://127.0.0.1:8765 。启动器管理本项目数据库、API 和采集进程；停止时关闭它创建的子进程，已单独启动的数据库不会被关闭。数据库默认端口 55432，数据保存在 `.state/postgres`。

配置保存在本项目 `.env`，只使用 `OD_` 前缀。默认离线模式不请求外部数据。在独立演示库中运行 `.venv/bin/option-desk demo` 可加载标注 DEMO 的合成示例，请勿将演示库用于正式采集。

启用 OpenD 行情后重启服务：

```dotenv
OD_MODE=live
OD_PROVIDERS=opend
OD_OPEND_HOST=127.0.0.1
OD_OPEND_PORT=11111
OD_SOURCE_INGESTION_ENABLED=false
```

关注列表可以先添加标准美股期权合约查看当前行情，大单事件需另行导入或接入来源。Discord / MongoDB 采集默认关闭；经授权后配置本项目凭据和明确频道范围，再设置 `OD_SOURCE_INGESTION_ENABLED=true`。不要提交 `.env`、令牌或 `.state`。Schwab 令牌默认放在本项目 `.state/schwab-token.json`。

## 数据约定

- 每个来源事件与合约保留一份冻结基准；当前行情按合约覆盖更新，查询不新增快照。
- 来源值优先，缺失价格使用事件对应的 1 分钟 K 线收盘价估算，盘中不替换为邻近分钟。
- 非交易时段检查最近已结束交易日；没有有效 K 线则丢弃监控项，保留诊断。
- 不提供手工重抓基准。缺失的 IV / Greeks、报价、成交量与 OI 等字段允许一次性补入对应交易日收盘后观测值，明确标注替代口径；拿不到该日期数据则保持空值。模型估值尚未启用。
- 请求预算、重试和任务租约使用本项目数据库，不与 midas 共享。

## 验证与部署

本地数据库启动后运行：

```sh
.venv/bin/python -m pytest -q
npm run build
```

测试使用临时独立数据库 schema 并清理。Docker Compose 配置已提供，但尚未实际运行验证；Docker Desktop 容器访问宿主机 OpenD 时设置 `OD_OPEND_HOST=host.docker.internal`，并配置 `OD_DB_PASSWORD`。本版本面向本机使用，不应直接暴露到公网。

- [详细设计](docs/infrastructure-design.md)
- [开发进度、补全统计与交接记录](docs/implementation-status.md)

## 来源回填

当前已按 midas 参考清单配置 20 个频道（清单副本位于 `src/option_desk/source_channels.json`），不在运行时读取 midas。`OD_SOURCE_LOOKBACK_DAYS=30` 控制新回填任务的时间窗口；已有任务保留起止时间与游标，重启自动续跑，不因配置变化重置。Discord 历史与实时采集共用限流；MongoDB 按时间和文档 ID 分页，频道范围变更使用新游标。页面“数据源与任务”显示逐频道回填进度。

历史补采会排入首次基准构建队列；“消息已读取”不等于基准或当前行情已经取得。没有年份、截断文本、仅图片或无法确认合约的来源保持诊断。历史新闻、股票成交不会被强行转换为期权事件。

## API 查询

服务提供本机 HTTP API，交互文档为 [http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs)。

- 提醒列表：`GET /api/v1/baselines?ticker=VST&right=C&limit=50&offset=0`
- 支持股票代码、Call/Put、到期日、带时区的发生时间区间、基准状态和文本搜索组合筛选。
- 返回匹配总数与下一页偏移量；页面搜索覆盖全部记录，支持翻页。
- 已到期合约隐藏；列表每页最多 500 条。导出仍最多 500 条且不跟随筛选。

完整参数、时间规则和调用示例见 [API 查询说明](docs/api-query.md)，实现进度见 [开发进度](docs/implementation-status.md)。
