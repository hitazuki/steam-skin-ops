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
- `t7`：七日历史 P25 比例，即当前最低平台价 ÷ 过去 7 天 Steam 到手价 P25。
- `platform`：最低第三方平台价达到买入目标。
- `steam`：Steam 展示售价达到库存清理目标。

规则首次满足即产生事件；继续向有利方向变化时，每相对原阈值突破 3% 通知一次，
一次跨越多档只通知当前最高档。越过 3% 恢复回差后立即重新布防，不发送恢复通知。
每天北京时间 09:00 汇总仍满足原始阈值的活跃规则。

默认每 30 分钟轮询，真实请求连续失败三轮才生成异常事件。行情快照滚动保留
30 天：首次覆盖不足时请求一次 30 天历史，之后依靠当前行情轮询增量积累；只有本地
观测中断且恢复行情已经变化时，才按缺口大小请求最小整数天历史。连续成功但行情未变
也视为有效观测，不会因上游更新时间陈旧而回填。

报价和市场规则告警会分别附带七日预测与多维风险评估。两者仅作提示，不参与四类规则
的触发、恢复或突破档位计算；历史不足或当前快照过期时明确显示预测不可用。

### 七日预测

七日预测使用最多 30 天行情做模式验证，最终拟合窗口为 14 或 21 天。计算前将历史接口
与成功的当前轮询合并到两小时格：同一格优先使用本地当前观测，历史点只补空格。成功
轮询且行情未变仍计为有效覆盖；没有成功观测或历史点证明的区间不会自动填充。

设 Steam 展示价为 `G`，当前最低有效第三方平台价为 `B`。Steam 预计到手价 `S` 与
监控规则使用相同口径：

```text
S = max(min(floor(G × 100 ÷ 1.15) ÷ 100, G - 0.14), 0)
```

每个 UTC 日的最后一个有效格会生成向前滚动 24 小时到手价中位数 `M_i`。系统按近期
实际验证误差在以下模式中自动选择：

- `persistence`：七日后等于当前到手价。
- `recent_level`：七日后等于最近 3 个日值中位数。
- `theil_sen_linear`：对 14/21 天到手价做 Theil–Sen 稳健线性拟合。
- `theil_sen_log`：对 14/21 天对数到手价做 Theil–Sen 稳健拟合。

Theil–Sen 斜率与截距为：

```text
beta  = median((Y_j - Y_i) ÷ (t_j - t_i)), i < j
alpha = median(Y_i - beta × t_i)

线性模式：S_forecast_7d = alpha + beta × t_future
对数模式：S_forecast_7d = exp(alpha + beta × t_future)
预测涨跌幅 = S_forecast_7d ÷ S_now - 1
预测倒余额比例 = B ÷ S_forecast_7d
```

线性模式中 `Y=M`，对数模式中 `Y=ln(M)`。滚动验证覆盖 1–7 天并按预测跨度加权；非
持平模式必须比持平模式至少改善 5%。趋势模式还要求 14/21 日方向一致、Kendall 趋势
强度至少 0.30，且最近价格没有显著偏离拟合线，否则回退到持平或近期水平模式。

预测比例越低表示当前平台成本相对于预计七日 Steam 余额越低。平台价缺失时仍输出
Steam 到手价预测，但预测倒余额比例不可用。至少 21 天且验证样本充足时为正常置信度；
14–20 天或验证样本不足时为低置信度；少于 14 天或两小时格覆盖不足 80%时不可用。

### 多维风险评估

风险评估与预测独立，分别给出价格、波动、库存和成交量四个维度的低/中/高等级；总体
等级取可用维度中的最高等级。风险底价和风险倒余额比例为：

风险底价同时考虑当前价格、过去 7 天到手价 P25 和趋势预测，取三者中最保守的值：

```text
S_risk = min(S_now, S_p25_7d, S_forecast_7d)
风险倒余额比例 = B ÷ S_risk
T+7 P25 比例 = B ÷ S_p25_7d
```

缺少预测或 P25 时从剩余有效候选中取最小值。风险比例通常不低于预测比例，越高表示
同一平台买入成本相对于保守 Steam 到手余额越高。

报价展示不再重复输出旧称“T+7 保守比例”：七日历史部分只展示 P25 到手价、样本数和
覆盖天数；`t7` 规则触发消息将该指标明确标为“七日历史 P25 比例”。预测倒余额比例
只对应七日预测价，风险倒余额比例只对应风险底价。如果风险底价恰好等于当前到手价或
七日预测价，消息显示“同即时比例”或“同预测比例”及底价来源，不重复打印相同百分数。
样本不足、低置信度和单个风险维度不可用会分别标明，不以“未知”混合表示。

各维度口径：

- 价格：预测跌幅达到 2%/5%分别为中/高；风险比例比 P25 比例差至少 2 个百分点时至少
  为中；放量下跌再提升一级。
- 波动：按日对数收益计算 `sigma_7d = stdev(log(M_t/M_t-1)) × sqrt(7)`，低于 3%、
  3%–7%、达到 7%分别为低/中/高；近期方差放大至少 1.5 倍且波动率至少 2%时提升一级。
- 库存：Steam/BUFF 在售增长至少 5%、库存消化天数恶化至少 10%分别算压力信号；一个
  为中，两个为高，单项达到 15%/15%/25%也直接为高。
- 成交量：比较最近 7 日与此前 7 日的日成交量中位数；下降 10%/25%分别为中/高，最近
  七日为零时为高。预测下跌同时成交量增加会识别为放量卖压。

库存指标使用同一两小时规范序列：

```text
Steam 在售 7 日变化率 = Steam在售_now ÷ Steam在售_7d_ago - 1
BUFF 在售 7 日变化率  = BUFF在售_now  ÷ BUFF在售_7d_ago  - 1
库存消化天数           = Steam在售数 ÷ Steam成交量
消化天数 7 日变化率    = 消化天数_now ÷ 消化天数_7d_ago - 1
```

库存消化天数仍为 `Steam 在售数 ÷ Steam 日成交量`；成交量为零时该指标不可用，但
成交量维度会判定为高流动性风险。报价 API 分别通过 `forecast` 与 `risk_assessment`
返回预测和风险，不再返回旧的 `risk_prediction`。

报价响应中的分析结构示例：

```json
{
  "forecast": {
    "status": "ready",
    "mode": "theil_sen_log",
    "mode_label": "稳健对数趋势",
    "window_days": 21,
    "predicted_steam_net": 4.42,
    "change_pct": -5.4,
    "forecast_balance_ratio": 0.7217,
    "confidence": "normal"
  },
  "risk_assessment": {
    "status": "ready",
    "overall_level": "high",
    "risk_steam_net": 4.32,
    "risk_balance_ratio": 0.7384,
    "dimensions": {
      "price": {"level": "high"},
      "volatility": {"level": "medium"},
      "inventory": {"level": "high"},
      "volume": {"level": "medium"}
    }
  }
}
```

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
