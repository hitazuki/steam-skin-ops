# steam-skin-ops + AstrBot 部署与 v3 迁移

## 全新部署

```bash
mkdir -p /home/ubuntu/steam-skin-ops
cd /home/ubuntu/steam-skin-ops
cp config/monitor.example.yaml config/monitor.yaml
docker network create astrbot-internal || true
sudo docker compose -f compose.yml -f compose.astrbot.yml up -d
```

`config/monitor.yaml` 必须配置：

```yaml
service:
  token: "<随机长字符串>"
alerts:
  driver: "astrbot"
astrbot:
  api_key: "<仅含 im 权限的 AstrBot Key>"
```

AstrBot Compose 必须持久加入 `astrbot-internal`，不能只执行一次
`docker network connect`。

安装插件：

```bash
ASTRBOT_DATA_DIR=/home/ubuntu/code-server/projects/AstrBot/data
mkdir -p "$ASTRBOT_DATA_DIR/plugins/astrbot_plugin_steam_skin_ops"
cp -a plugins/astrbot_plugin_steam_skin_ops/. \
  "$ASTRBOT_DATA_DIR/plugins/astrbot_plugin_steam_skin_ops/"
sudo docker restart astrbot
```

## 从 buff2steam v2 迁移

1. 在旧容器内使用 SQLite backup API 创建完整备份：

   ```bash
   sudo docker exec buff2steam python -c \
     "import sqlite3,datetime; p='/app/data/backups/pre-v3.0.0-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S')+'.db'; s=sqlite3.connect('/app/data/monitor.db'); d=sqlite3.connect(p); s.backup(d); d.close(); s.close(); print(p)"
   ```

2. 停止旧服务，确认目标目录不存在后移动部署目录：

   ```bash
   cd /home/ubuntu
   sudo docker stop buff2steam
   test ! -e /home/ubuntu/steam-skin-ops
   mv /home/ubuntu/buff2steam /home/ubuntu/steam-skin-ops
   ```

3. 替换 Compose 并创建 `config/monitor.yaml`，将旧 Token/Key 写入对应
   YAML 字段；旧环境变量不再生效。

4. 备份并移除旧插件，避免两个插件同时注册 `/skin`：

   ```bash
   ASTRBOT_PLUGINS=/home/ubuntu/code-server/projects/AstrBot/data/plugins
   mv "$ASTRBOT_PLUGINS/astrbot_plugin_buff2steam" \
      "$ASTRBOT_PLUGINS/astrbot_plugin_buff2steam.pre-v3.0.0"
   ```

5. 启动新镜像。第一次启动会在事务中迁移 `umo`、规则健康状态、旧通知队列；
   迁移失败时容器启动失败，原始数据库备份可用于回滚。

## 验证

```bash
sudo docker inspect --format='{{json .State.Health}}' steam-skin-ops
curl http://127.0.0.1:8080/healthz
sudo docker logs --tail=200 steam-skin-ops
sudo docker logs --tail=200 astrbot
```

随后验证：

- `/v2/market/quote?q=1579` 返回五平台行情和最低价标记。
- `/v2/market/history` 无需 AstrBot 接收者。
- 原规则 ID、阈值和状态仍可查询。
- `/v2/events/test` 可持久化并通过 AstrBot 投递。
- `/skin quote 1579`、`/skin rule list` 和重复 `rule add` 正常。

回滚时停止新容器，将目录移回 `/home/ubuntu/buff2steam`，恢复旧插件目录和备份的
`monitor.db`，使用 `hitazuki/buff2steam:2.0.0` 启动。
