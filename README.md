# steam-skin-ops

Steam 饰品收益统计与行情规则监控。项目包含两个互相独立的运行边界：

- `steam_skin_ops.profit`：在本地使用个人 BUFF、C5、Steam Cookie 统计历史收益。
- `steam_skin_ops.monitor`：使用 SMIS 公共行情提供查询、T+7/到价规则和告警事件 API。

AstrBot 只是可选客户端与告警投递适配器；行情和规则服务可以单独部署。

## 本地收益统计

安装：

```bash
python -m pip install -e ".[profit]"
copy config\profit.example.yaml config\profit.yaml
```

常用命令：

```bash
python -m steam_skin_ops.profit cookies
python -m steam_skin_ops.profit sync
python -m steam_skin_ops.profit refresh
python -m steam_skin_ops.profit build
python -m steam_skin_ops.profit check-login
python -m steam_skin_ops.profit view
```

默认缓存位于 `data/profit/`，报告位于 `output/profit/`。第一次使用默认目录时，
程序会从旧版 `data/` 安全复制已知订单和汇率缓存；不会覆盖新文件或删除旧文件。

收益配置只包含个人交易客户端、汇率和报告设置，不包含监控配置。Cookie 文件
`config/profit.yaml` 已被 Git 忽略，禁止提交。

## 独立行情与监控服务

复制监控配置并启动：

```bash
copy config\monitor.example.yaml config\monitor.yaml
docker compose up -d
```

默认使用 `store` 告警驱动，服务只绑定 `127.0.0.1:8080`，不要求 AstrBot 或
`astrbot-internal` 网络。所有接口除 `/healthz` 外使用 Bearer Token：

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:8080/v2/market/quote?q=1579"
```

主要接口：

```text
GET  /v2/market/search?q=
GET  /v2/market/quote?q=
GET  /v2/market/history?q=&days=7
GET/POST/PATCH/DELETE /v2/rules
GET  /v2/events?recipient_key=&acknowledged=false
POST /v2/events/{id}/ack
POST /v2/events/test
GET  /v2/monitor/items
GET  /v2/monitor/status
GET  /healthz
```

规则类型：

- `ratio`：当前最低第三方平台价 ÷ 当前 Steam 到手价。
- `t7`：当前最低平台价 ÷ 过去 7 天 Steam 到手价 P25。
- `platform`：最低第三方平台价达到买入目标。
- `steam`：Steam 展示售价达到库存清理目标。

规则首次满足即产生事件；继续向有利方向变化时，每相对原阈值突破 3% 通知一次，
一次跨越多档只通知当前最高档。越过 3% 恢复回差后立即重新布防，不发送恢复通知。
每天北京时间 09:00 汇总仍满足原始阈值的活跃规则。

默认每 30 分钟轮询，真实请求连续失败三轮才生成异常事件。行情快照滚动保留
30 天：首次覆盖不足时请求一次 30 天历史，之后依靠当前行情轮询增量积累；只有本地
观测中断且恢复行情已经变化时，才按缺口大小请求最小整数天历史。连续成功但行情未变
也视为有效观测，不会因上游更新时间陈旧而回填。

报价和市场规则告警会附带 21 天稳健趋势计算的 T+7 风险预测。该预测综合 Steam
到手价趋势、Steam/BUFF 在售变化和库存消化天数，仅作提示，不参与四类规则的触发、
恢复或突破档位计算；历史不足或当前快照过期时明确显示预测不可用。

SMIS 请求在进程内统一串行限速，默认至少间隔 1 秒。`401/403` 和普通
`4xx` 不重试；`429` 遵循上游 `Retry-After`，连接错误及临时 `5xx` 使用
退避重试。可在监控配置中调整：

```yaml
smis:
  timeout_seconds: 15
  max_retries: 3
  min_request_interval_seconds: 1
```

监控参数统一位于 `config/monitor.yaml`，例如：

```yaml
alerts:
  breakthrough_step_percent: 3
  daily_summary_time: "09:00"
```

## AstrBot 集成

先创建外部网络并让 AstrBot 加入：

```bash
docker network create astrbot-internal
docker compose -f compose.yml -f compose.astrbot.yml up -d
```

将 `plugins/astrbot_plugin_steam_skin_ops` 复制到 AstrBot 数据目录的
`plugins/astrbot_plugin_steam_skin_ops`，配置：

```text
service_base_url=http://steam-skin-ops:8080
service_token=<与 config/monitor.yaml 的 service.token 相同>
```

插件提供 `/skin search`、`quote`、`rule`、`items`、`test`、`status`、`help`。
添加规则时会把 AstrBot UMO 当作不透明 `recipient_key`；核心服务不依赖其格式。

完整 VPS 部署与 v2 迁移见 [DEPLOY_ASTRBOT.md](DEPLOY_ASTRBOT.md)。

## 开发

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
```

目录边界测试会阻止 `profit` 与 `monitor` 相互导入，并确认监控镜像不包含 Cookie
收益模块。提交与 PR 标题必须遵循根目录 [AGENTS.md](AGENTS.md) 中带 scope 的
Conventional Commits。

## 破坏性变更

- 项目由 `buff2steam` 更名为 `steam-skin-ops`。
- Python 包改为 `steam_skin_ops`，不保留 `python src/main.py`。
- 收益和监控配置统一放在 `config/`；监控服务只读取
  `config/monitor.yaml`，不再读取旧环境变量。
- HTTP API 直接升级为 `/v2`，接收者字段由 `umo` 改为 `recipient_key`。
- AstrBot 插件 ID 改为 `astrbot_plugin_steam_skin_ops`。
- SQLite 启动时自动迁移规则接收者和告警事件，部署前仍必须备份数据库。
