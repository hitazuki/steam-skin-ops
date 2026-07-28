import unittest
import sqlite3
import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from steam_skin_ops.monitor.api import create_app
from steam_skin_ops.monitor.config import load_config
from steam_skin_ops.monitor.events import NotifyResult
from steam_skin_ops.monitor.integrations.astrbot import AstrBotNotifier
from steam_skin_ops.monitor.manager import MonitoringManager
from steam_skin_ops.monitor.market import MarketSnapshot, steam_net_amount
from steam_skin_ops.monitor.repository import MonitorStorage
from steam_skin_ops.monitor.runtime import ServiceRuntime


ITEM = {
    "smis_id": 1579,
    "item_key": "smis:1579",
    "appid": 730,
    "name": "Fracture Case",
    "name_zh": "裂空武器箱",
}


def snapshot(ratio=0.70, offset=0):
    now = datetime.now(timezone.utc) + timedelta(seconds=offset)
    steam_price = 5.34
    return MarketSnapshot(
        item_key=ITEM["item_key"], smis_id=1579, appid=730,
        name=ITEM["name"], name_zh=ITEM["name_zh"],
        observed_at=now, source_updated_at=now,
        buff_sell_price=round(steam_net_amount(steam_price) * ratio, 2),
        buff_sell_num=0, uuyp_sell_price=0, uuyp_sell_num=0,
        c5_sell_price=0, c5_sell_num=0, igxe_sell_price=0, igxe_sell_num=0,
        eco_sell_price=0, eco_sell_num=0,
        steam_sell_price=steam_price, steam_sell_num=0,
        steam_transaction_quantity=0, buff_to_steam_ratio=ratio,
    )


class FakeSource:
    def __init__(self, ratios=None):
        self.ratios = list(ratios or [0.70])
        self.current_calls = 0
        self.history_calls = 0
        self.history_days = []

    def fetch_metadata(self, smis_id):
        if int(smis_id) != 1579:
            raise RuntimeError("not found")
        return dict(ITEM)

    def search_items(self, query, limit=10):
        if "裂空" not in query and "Fracture" not in query:
            return []
        return [{"smis_id": 1579, "name_zh": "裂空武器箱", "rarity": "普通级"}][:limit]

    def fetch_current(self, item):
        self.current_calls += 1
        ratio = self.ratios.pop(0) if len(self.ratios) > 1 else self.ratios[0]
        if isinstance(ratio, Exception):
            raise ratio
        return snapshot(float(ratio), self.current_calls)

    def fetch_history(self, item, days):
        self.history_calls += 1
        self.history_days.append(days)
        result = []
        for index in range(13):
            history = snapshot(0.74, -(index * 12 * 3600))
            result.append(MarketSnapshot(**{**history.__dict__, "kind": "history"}))
        return result


class StaleSource(FakeSource):
    def fetch_current(self, item):
        value = super().fetch_current(item)
        return MarketSnapshot(**{
            **value.__dict__,
            "source_updated_at": datetime.now(timezone.utc) - timedelta(minutes=30),
        })


class FakeNotifier:
    def __init__(self, failing_umo=None):
        self.name = "fake"
        self.failing_umo = failing_umo
        self.messages = []

    def send_to(self, umo, title, content):
        self.messages.append((umo, title, content))
        if umo == self.failing_umo:
            return NotifyResult(False, "failed")
        return NotifyResult(True, "ok")


class FakeRuntime:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def status(self):
        return {"running": self.started, "items": 1, "rules": 1, "outbox": {}}


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.storage = MonitorStorage(Path(self.tmp.name) / "monitor.db")
        self.storage.upsert_item(ITEM)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, ratios=None, notifier=None, cache=60):
        return MonitoringManager(
            self.storage, FakeSource(ratios), notifier or FakeNotifier(),
            quote_cache_seconds=cache,
        )

    def test_existing_snapshot_table_gets_platform_columns(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.execute("""
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                item_key TEXT NOT NULL, smis_id INTEGER NOT NULL, appid INTEGER NOT NULL,
                name TEXT NOT NULL, name_zh TEXT NOT NULL, observed_at TEXT NOT NULL,
                source_updated_at TEXT NOT NULL, kind TEXT NOT NULL,
                buff_sell_price REAL, buff_sell_num INTEGER, steam_sell_price REAL,
                steam_sell_num INTEGER, steam_transaction_quantity INTEGER,
                buff_to_steam_ratio REAL,
                UNIQUE(source,item_key,observed_at,kind)
            )
        """)
        connection.commit()
        connection.close()
        migrated = MonitorStorage(legacy_path)
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(market_snapshots)")}
        self.assertTrue({"uuyp_sell_price", "c5_sell_price", "igxe_sell_price", "eco_sell_price"}.issubset(columns))

    def test_v2_database_migrates_recipient_and_outbox(self):
        legacy_path = Path(self.tmp.name) / "v2.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript("""
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, smis_id INTEGER NOT NULL,
                umo TEXT NOT NULL, rule_type TEXT NOT NULL, threshold REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE rule_health (
                smis_id INTEGER NOT NULL, umo TEXT NOT NULL,
                fetch_failures INTEGER NOT NULL DEFAULT 0,
                health_alerted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(smis_id,umo)
            );
            CREATE TABLE notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_key TEXT NOT NULL,
                umo TEXT NOT NULL, event_type TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                last_error TEXT, next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL,
                sent_at TEXT, UNIQUE(signal_key,umo)
            );
            INSERT INTO rules VALUES(1,1579,'recipient:a','steam',5.4,1,'now','now');
            INSERT INTO notification_outbox VALUES(
                1,'signal:a','recipient:a','test','title','body','sent',0,NULL,
                'now','now','now'
            );
        """)
        connection.commit()
        connection.close()

        migrated = MonitorStorage(legacy_path)
        migrated.upsert_item(ITEM)

        self.assertEqual(migrated.get_rule(1)["recipient_key"], "recipient:a")
        event = migrated.list_events("recipient:a", acknowledged=None)[0]
        self.assertEqual(event["delivery_status"], "sent")
        with migrated.connect() as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(version, "4")

        reopened = MonitorStorage(legacy_path)
        self.assertEqual(reopened.get_rule(1)["recipient_key"], "recipient:a")
        self.assertEqual(len(reopened.list_events("recipient:a", acknowledged=None)), 1)

    def test_newer_database_schema_is_rejected(self):
        path = Path(self.tmp.name) / "future.db"
        storage = MonitorStorage(path)
        with storage.connect() as connection:
            connection.execute(
                "UPDATE metadata SET value='99' WHERE key='schema_version'"
            )

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            MonitorStorage(path)

    def test_v3_database_migrates_active_breakthrough_level(self):
        path = Path(self.tmp.name) / "v3.db"
        storage = MonitorStorage(path)
        storage.upsert_item(ITEM)
        rule = storage.add_rule(1579, "umo:a", "ratio", 72)
        storage.update_rule_state(rule["id"], alert_active=1, last_value=0.67)
        with storage.connect() as connection:
            connection.execute("ALTER TABLE rule_states DROP COLUMN highest_notified_level")
            connection.execute(
                "UPDATE metadata SET value='3' WHERE key='schema_version'"
            )

        migrated = MonitorStorage(path)
        state = migrated.get_rule_state(rule["id"])
        self.assertEqual(state["highest_notified_level"], 2)
        MonitoringManager(
            migrated, FakeSource([0.67]), FakeNotifier(),
            breakthrough_step_percent=1,
        )
        self.assertEqual(
            migrated.get_rule_state(rule["id"])["highest_notified_level"], 6
        )

    def test_monitor_yaml_config_loads_nested_settings(self):
        path = Path(self.tmp.name) / "monitor.yaml"
        path.write_text("""
service:
  token: secret
  database: ./state.db
  backup_dir: ./backups
monitor:
  interval_seconds: 1800
  quote_cache_seconds: 90
  max_items: 10
alerts:
  driver: astrbot
  breakthrough_step_percent: 4
  daily_summary_time: '08:30'
smis:
  timeout_seconds: 12
  max_retries: 2
  min_request_interval_seconds: 1.5
astrbot:
  base_url: http://astrbot:6185
  api_key: key
  message_path: /api/v1/im/message
  timeout_seconds: 8
""", encoding="utf-8")

        config = load_config(path)

        self.assertEqual(config.service_token, "secret")
        self.assertEqual(config.alert_driver, "astrbot")
        self.assertEqual(config.breakthrough_step_percent, 4)
        self.assertEqual(config.daily_summary_time, "08:30")
        self.assertEqual(config.quote_cache_seconds, 90)
        self.assertEqual(config.smis_min_request_interval_seconds, 1.5)

    def test_monitor_yaml_config_rejects_missing_token_and_astrbot_key(self):
        path = Path(self.tmp.name) / "monitor.yaml"
        path.write_text("service: {}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "service.token"):
            load_config(path)

        path.write_text("""
service:
  token: secret
alerts:
  driver: astrbot
""", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "astrbot.api_key"):
            load_config(path)

    def test_quote_uses_sixty_second_cache(self):
        manager = self.manager([0.70])
        first = manager.quote("1579")
        second = manager.quote("裂空")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(manager.source.current_calls, 1)

    def test_quote_by_id_queries_entire_market_without_registering_item(self):
        with self.storage.connect() as conn:
            conn.execute("DELETE FROM items WHERE smis_id=1579")
        manager = self.manager([0.70])

        result = manager.quote("1579")

        self.assertEqual(result["smis_id"], 1579)
        self.assertFalse(result["cached"])
        self.assertIsNone(self.storage.get_item(1579))
        self.assertEqual(self.storage.count_items(), 0)

    def test_quote_by_name_queries_entire_market(self):
        with self.storage.connect() as conn:
            conn.execute("DELETE FROM items WHERE smis_id=1579")
        manager = self.manager([0.70])

        result = manager.quote("裂空")

        self.assertEqual(result["name"], "Fracture Case")
        self.assertEqual(result["name_zh"], "裂空武器箱")
        self.assertEqual(manager.source.current_calls, 1)

    def test_quote_returns_all_available_platforms_and_marks_every_lowest(self):
        manager = self.manager()
        current = snapshot()
        manager.source.fetch_current = lambda item: MarketSnapshot(**{
            **current.__dict__,
            "buff_sell_price": 3.30, "buff_sell_num": 100,
            "uuyp_sell_price": 3.20, "uuyp_sell_num": 80,
            "c5_sell_price": 3.10, "c5_sell_num": 60,
            "igxe_sell_price": 3.40, "igxe_sell_num": 40,
            "eco_sell_price": 3.10, "eco_sell_num": 20,
        })

        result = manager.quote("1579")

        self.assertEqual(
            [row["name"] for row in result["platforms"]],
            ["BUFF", "悠悠有品", "C5", "IGXE", "ECO"],
        )
        self.assertEqual(
            [row["name"] for row in result["platforms"] if row["is_lowest"]],
            ["C5", "ECO"],
        )

    def test_search_uses_smis_catalog(self):
        results = self.manager().search_items("裂空", limit=5)
        self.assertEqual(results, [{
            "smis_id": 1579, "name_zh": "裂空武器箱", "rarity": "普通级",
        }])

    def test_search_failure_uses_service_error_envelope(self):
        manager = self.manager()
        manager.source.search_items = lambda query, limit=10: (_ for _ in ()).throw(
            RuntimeError("down")
        )
        app = create_app(manager=manager, runtime=FakeRuntime(), service_token="secret")
        with TestClient(app) as client:
            response = client.get(
                "/v2/market/search",
                headers={"Authorization": "Bearer secret"},
                params={"q": "裂空"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "smis_search_failed")

    def test_stale_quote_falls_back_when_source_fails(self):
        self.storage.save_snapshots([snapshot(0.70, -120)])
        manager = self.manager([RuntimeError("down")], cache=1)
        result = manager.quote("1579")
        self.assertTrue(result["stale"])
        self.assertIn("实时刷新失败", result["warning"])
        self.assertEqual(result["forecast"]["status"], "stale")
        self.assertEqual(result["risk_assessment"]["status"], "unavailable")

    def test_ratio_rules_are_session_isolated_and_rearm_after_three_percent_margin(self):
        notifier = FakeNotifier()
        manager = self.manager([0.70, 0.70, 0.75, 0.75, 0.70, 0.70], notifier)
        first = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        second = self.storage.add_rule(1579, "umo:b", "ratio", 68)
        for _ in range(6):
            manager.run_cycle(max_workers=1)
        recipients = [message[0] for message in notifier.messages]
        self.assertEqual(recipients, ["umo:a", "umo:a"])
        self.assertEqual(self.storage.get_rule_state(second["id"])["alert_active"], 0)
        self.assertEqual(self.storage.get_rule_state(first["id"])["alert_active"], 1)

    def test_rule_fires_on_first_qualifying_cycle(self):
        notifier = FakeNotifier()
        manager = self.manager([0.70], notifier)
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)

        manager.run_cycle(max_workers=1)

        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(self.storage.get_rule_state(rule["id"])["alert_active"], 1)

    def test_breakthrough_levels_are_arithmetic_and_do_not_repeat(self):
        notifier = FakeNotifier()
        manager = self.manager([0.72, 0.69, 0.70, 0.66, 0.75, 0.69], notifier)
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)

        manager.run_cycle(max_workers=1)  # initial, level 0
        manager.run_cycle(max_workers=1)  # level 1
        manager.run_cycle(max_workers=1)  # back below level 1, no repeat
        manager.run_cycle(max_workers=1)  # jumps to level 2
        manager.run_cycle(max_workers=1)  # clears in one cycle
        manager.run_cycle(max_workers=1)  # new incident

        titles = [message[1] for message in notifier.messages]
        self.assertEqual(len(titles), 4)
        self.assertIn("第 1 档", titles[1])
        self.assertIn("第 2 档", titles[2])
        self.assertIn("第 1 档", titles[3])
        breakthrough = notifier.messages[1][2]
        self.assertIn("最低平台：", breakthrough)
        self.assertIn("Steam 预计到手：", breakthrough)
        self.assertIn("七日历史基准：", breakthrough)
        self.assertIn("七日预测：", breakthrough)
        self.assertIn("风险评估：", breakthrough)
        self.assertIn("https://smis.club/commodity/1579", breakthrough)
        self.assertIn("https://steamcommunity.com/market/listings/730/", breakthrough)
        self.assertEqual(self.storage.get_rule_state(rule["id"])["highest_notified_level"], 1)

    def test_breakthrough_level_includes_exact_three_percent_boundary(self):
        manager = self.manager()
        self.assertEqual(manager._breakthrough_level("ratio", 0.72, 0.6984)[0], 1)
        self.assertEqual(manager._breakthrough_level("t7", 0.72, 0.6984)[0], 1)
        self.assertEqual(manager._breakthrough_level("platform", 100, 97)[0], 1)
        self.assertEqual(manager._breakthrough_level("steam", 100, 106)[0], 2)

    def test_ready_analysis_message_separates_forecast_and_risk_ratios(self):
        forecast = {
            "status": "ready", "predicted_steam_net": 4.42,
            "change_pct": -5.4, "forecast_balance_ratio": 0.7217,
            "window_days": 21, "mode_label": "稳健对数趋势",
            "confidence": "normal",
        }
        risk = {
            "status": "ready", "overall_level": "high",
            "confidence": "normal",
            "risk_balance_ratio": 0.7384,
            "dimensions": {
                "price": {"level": "high"},
                "volatility": {"level": "medium"},
                "inventory": {"level": "high"},
                "volume": {"level": "medium"},
            },
            "reasons": ["预计七日下跌 5.4%"],
        }

        lines = self.manager()._analysis_reference_lines(forecast, risk)
        content = "\n".join(lines)

        self.assertIn("预测倒余额比例 72.17%", content)
        self.assertIn("风险倒余额比例：73.84%", content)
        self.assertIn("价格高、波动中、库存高、成交量中", content)

    def test_analysis_message_deduplicates_risk_ratio_by_floor_source(self):
        forecast = {
            "status": "ready", "predicted_steam_net": 4.42,
            "change_pct": -5.4, "forecast_balance_ratio": 0.7217,
            "window_days": 21, "mode_label": "稳健对数趋势",
            "confidence": "normal",
        }
        risk = {
            "status": "ready", "overall_level": "high", "confidence": "normal",
            "risk_steam_net": 4.67, "risk_balance_ratio": 0.6831,
            "dimensions": {
                key: {"level": "low"}
                for key in ("price", "volatility", "inventory", "volume")
            },
            "reasons": [],
        }

        current_lines = self.manager()._analysis_reference_lines(
            forecast, risk, current_steam_net=4.67, t7_steam_net_p25=4.60,
        )
        self.assertIn(
            "风险倒余额比例：同即时比例（当前到手价为风险底价）",
            current_lines,
        )

        risk.update({"risk_steam_net": 4.42, "risk_balance_ratio": 0.7217})
        forecast_lines = self.manager()._analysis_reference_lines(
            forecast, risk, current_steam_net=4.67, t7_steam_net_p25=4.60,
        )
        self.assertIn(
            "风险倒余额比例：同预测比例（七日预测价为风险底价）",
            forecast_lines,
        )

    def test_daily_summary_groups_active_rules_once_per_day(self):
        notifier = FakeNotifier()
        manager = self.manager([0.70], notifier)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        self.storage.add_rule(1579, "umo:a", "platform", 3.40)
        manager.run_cycle(max_workers=1)
        notifier.messages.clear()

        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 1)
        manager.dispatch_outbox()
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("共 2 条", notifier.messages[0][2])
        self.assertIn("预测", notifier.messages[0][2])
        self.assertIn("风险", notifier.messages[0][2])
        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)
        manager.dispatch_outbox()
        self.assertEqual(len(notifier.messages), 1)

    def test_empty_daily_check_prevents_later_same_day_summary(self):
        manager = self.manager([0.70])
        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        manager.run_cycle(max_workers=1)
        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)

    def test_daily_summary_excludes_recovery_band_and_missing_values(self):
        manager = self.manager()
        recovery = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        missing = self.storage.add_rule(1579, "umo:a", "platform", 3.40)
        self.storage.update_rule_state(
            recovery["id"], alert_active=1, last_value=0.73
        )
        self.storage.update_rule_state(
            missing["id"], alert_active=1, last_value=None
        )

        self.assertEqual(manager.enqueue_daily_summary(date(2026, 7, 25)), 0)

    def test_runtime_daily_summary_is_independent_of_poll_cycle(self):
        notifier = FakeNotifier()
        manager = self.manager([0.70], notifier)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        runtime = ServiceRuntime(
            manager, backup_dir=Path(self.tmp.name) / "backups",
            daily_summary_time="09:00",
        )

        runtime.run_once(datetime(2026, 7, 25, 0, 30, tzinfo=timezone.utc))
        notifier.messages.clear()
        runtime.run_once(datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc))

        self.assertEqual(len(notifier.messages), 0)

        result = runtime.run_daily_summary(
            datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc)
        )

        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("每日交易告警汇总", notifier.messages[0][1])
        self.assertEqual(result, {"queued": 1, "sent": 1, "failed": 0})
        self.assertTrue(runtime.last_daily_summary_ok)

    def test_runtime_daily_scheduler_targets_beijing_nine_and_catches_up(self):
        runtime = ServiceRuntime(
            self.manager(), backup_dir=Path(self.tmp.name) / "backups",
            daily_summary_time="09:00",
        )

        before = datetime(2026, 7, 25, 0, 59, tzinfo=timezone.utc)
        target = runtime._next_daily_summary_target(before)
        self.assertEqual(target.astimezone(timezone.utc), datetime(
            2026, 7, 25, 1, 0, tzinfo=timezone.utc
        ))

        after = datetime(2026, 7, 25, 1, 26, tzinfo=timezone.utc)
        catch_up = runtime._next_daily_summary_target(after)
        self.assertEqual(catch_up, after.astimezone(runtime.local_timezone))

        self.storage.set_metadata("daily_summary:last_date", "2026-07-25")
        next_day = runtime._next_daily_summary_target(after)
        self.assertEqual(next_day.astimezone(timezone.utc), datetime(
            2026, 7, 26, 1, 0, tzinfo=timezone.utc
        ))

    def test_runtime_starts_and_stops_both_schedulers(self):
        runtime = ServiceRuntime(
            self.manager(), interval_seconds=3600,
            backup_dir=Path(self.tmp.name) / "backups",
            daily_summary_time="23:59",
        )

        runtime.start()
        try:
            status = runtime.status()
            self.assertTrue(status["running"])
            self.assertTrue(status["daily_summary_running"])
        finally:
            runtime.stop()

        self.assertFalse(runtime.thread.is_alive())
        self.assertFalse(runtime.summary_thread.is_alive())

    def test_breakthrough_step_and_daily_time_are_validated(self):
        with self.assertRaisesRegex(ValueError, "突破档位步长"):
            MonitoringManager(
                self.storage, FakeSource(), FakeNotifier(),
                breakthrough_step_percent=0,
            )
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            ServiceRuntime(self.manager(), daily_summary_time="9am")

    def test_lowest_platform_ignores_liquidity(self):
        value = snapshot()
        value = MarketSnapshot(**{
            **value.__dict__, "buff_sell_price": 3.30, "buff_sell_num": 1000,
            "c5_sell_price": 3.10, "c5_sell_num": 0,
            "uuyp_sell_price": 3.20, "uuyp_sell_num": 1,
        })
        self.assertEqual(value.lowest_platform, ("C5", 3.10, 0))
        self.assertEqual(value.calculated_ratio, round(3.10 / value.steam_net, 4))

    def test_t7_rule_uses_seven_day_steam_net_p25(self):
        notifier = FakeNotifier()
        manager = self.manager([0.70, 0.70], notifier)
        rule = manager.add_rule("umo:a", 1579, "t7", 72)
        manager.run_cycle(max_workers=1)
        manager.run_cycle(max_workers=1)
        state = self.storage.get_rule_state(rule["id"])
        self.assertEqual(state["alert_active"], 1)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("T+7挂刀", notifier.messages[0][1])

    def test_platform_and_steam_rules_trigger_without_volume(self):
        notifier = FakeNotifier()
        manager = self.manager([0.70, 0.70], notifier)
        self.storage.add_rule(1579, "umo:a", "platform", 3.30)
        self.storage.add_rule(1579, "umo:a", "steam", 5.30)
        manager.run_cycle(max_workers=1)
        manager.run_cycle(max_workers=1)
        self.assertEqual({message[1].split("】", 1)[0] + "】" for message in notifier.messages}, {
            "【平台到价】", "【Steam清仓】",
        })

    def test_outbox_failure_isolated_by_umo(self):
        notifier = FakeNotifier(failing_umo="umo:b")
        manager = self.manager(notifier=notifier)
        self.storage.enqueue_notification(
            "sig:a", "umo:a", "test", "title", "body", driver="fake"
        )
        self.storage.enqueue_notification(
            "sig:b", "umo:b", "test", "title", "body", driver="fake"
        )
        result = manager.dispatch_outbox()
        self.assertEqual(result, {"sent": 1, "failed": 1})
        self.assertEqual(self.storage.outbox_counts(), {"pending": 1, "sent": 1})

    def test_concurrent_outbox_dispatch_sends_each_job_once(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingNotifier(FakeNotifier):
            def send_to(self, umo, title, content):
                entered.set()
                release.wait(timeout=2)
                return super().send_to(umo, title, content)

        notifier = BlockingNotifier()
        manager = self.manager(notifier=notifier)
        self.storage.enqueue_notification(
            "concurrent:1", "umo:a", "test", "title", "content",
            driver=notifier.name,
        )
        results = []
        first = threading.Thread(target=lambda: results.append(manager.dispatch_outbox()))
        second = threading.Thread(target=lambda: results.append(manager.dispatch_outbox()))

        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second.start()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(sum(result["sent"] for result in results), 1)

    def test_stale_source_time_does_not_trigger_health_failure(self):
        notifier = FakeNotifier()
        manager = MonitoringManager(self.storage, StaleSource([0.70]), notifier)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        for _ in range(3):
            manager.run_cycle(max_workers=1)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("即时挂刀", notifier.messages[0][1])
        state = self.storage.get_health_state(1579, "umo:a")
        self.assertEqual(state["fetch_failures"], 0)
        self.assertEqual(state["health_alerted"], 0)

    def test_three_real_request_failures_trigger_health_alert(self):
        notifier = FakeNotifier()
        manager = self.manager([RuntimeError("down")] * 3, notifier)
        self.storage.add_rule(1579, "umo:a", "ratio", 72)
        for _ in range(3):
            manager.run_cycle(max_workers=1)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("监控异常", notifier.messages[0][1])

    def test_api_auth_crud_and_validation_envelope(self):
        manager = self.manager([0.70])
        app = create_app(manager=manager, runtime=FakeRuntime(), service_token="secret")
        with TestClient(app) as client:
            self.assertEqual(client.get("/v2/monitor/items").status_code, 401)
            headers = {"Authorization": "Bearer secret"}
            search_response = client.get(
                "/v2/market/search", headers=headers, params={"q": "裂空"}
            )
            self.assertEqual(search_response.status_code, 200)
            self.assertEqual(search_response.json()["data"][0]["smis_id"], 1579)
            response = client.post("/v2/rules", headers=headers, json={
                "recipient_key": "astrQQ:FriendMessage:test", "smis_id": 1579,
                "rule_type": "ratio", "threshold": 72,
            })
            self.assertEqual(response.status_code, 200, response.text)
            rule_id = response.json()["data"]["id"]
            listed = client.get(
                "/v2/rules", headers=headers,
                params={"recipient_key": "astrQQ:FriendMessage:test"},
            )
            self.assertEqual(listed.json()["data"][0]["rule_type"], "ratio")
            self.assertIn(
                "highest_notified_level", listed.json()["data"][0]["state"]
            )
            quote_response = client.get(
                "/v2/market/quote", headers=headers, params={"q": "1579"}
            )
            self.assertTrue(quote_response.json()["ok"])
            quote_data = quote_response.json()["data"]
            self.assertIn("forecast", quote_data)
            self.assertIn("risk_assessment", quote_data)
            self.assertNotIn("risk_prediction", quote_data)
            bad = client.patch(f"/v2/rules/{rule_id}", headers=headers, json={
                "recipient_key": "astrQQ:FriendMessage:test", "threshold": 0,
            })
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(bad.json()["error"]["code"], "validation_error")

    def test_event_api_persists_and_acknowledges_without_astrbot(self):
        manager = self.manager()
        app = create_app(manager=manager, runtime=FakeRuntime(), service_token="secret")
        headers = {"Authorization": "Bearer secret"}
        recipient = "standalone:local"
        with TestClient(app) as client:
            created = client.post(
                "/v2/events/test", headers=headers, json={"recipient_key": recipient}
            )
            self.assertEqual(created.status_code, 200, created.text)
            event_id = created.json()["data"]["id"]
            listed = client.get(
                "/v2/events", headers=headers,
                params={"recipient_key": recipient, "acknowledged": "false"},
            )
            self.assertEqual([row["id"] for row in listed.json()["data"]], [event_id])
            acknowledged = client.post(
                f"/v2/events/{event_id}/ack", headers=headers,
                json={"recipient_key": recipient},
            )
            self.assertIsNotNone(acknowledged.json()["data"]["acknowledged_at"])
            empty = client.get(
                "/v2/events", headers=headers,
                params={"recipient_key": recipient, "acknowledged": "false"},
            )
            self.assertEqual(empty.json()["data"], [])

    def test_market_history_api_is_independent_of_recipient(self):
        manager = self.manager()
        app = create_app(manager=manager, runtime=FakeRuntime(), service_token="secret")
        with TestClient(app) as client:
            response = client.get(
                "/v2/market/history",
                headers={"Authorization": "Bearer secret"},
                params={"q": "1579", "days": 7},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["smis_id"], 1579)
        self.assertEqual(len(response.json()["data"]["points"]), 13)
        with self.storage.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        self.assertEqual(count, 0)

    def test_snapshot_pruning_keeps_only_rolling_eight_days(self):
        old = snapshot(0.70, -(9 * 86400))
        recent = snapshot(0.71, -(7 * 86400))
        self.storage.save_snapshots([old, recent])

        self.assertEqual(self.storage.prune_snapshots(retain_days=8), 1)
        with self.storage.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM market_snapshots"
            ).fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_history_backfill_only_runs_when_local_coverage_is_insufficient(self):
        manager = self.manager()
        item = dict(ITEM)
        manager._ensure_history(item)
        self.assertEqual(manager.source.history_calls, 1)
        self.assertEqual(manager.source.history_days, [30])
        manager._ensure_history(item)
        self.assertEqual(manager.source.history_calls, 1)

    def test_unchanged_market_after_observation_gap_does_not_backfill(self):
        manager = self.manager()
        old = snapshot(0.70, -(4 * 3600))
        current = MarketSnapshot(**{
            **old.__dict__,
            "observed_at": datetime.now(timezone.utc),
        })
        previous = {
            **old.__dict__,
            "observed_at": old.observed_at.isoformat(),
            "source_updated_at": old.source_updated_at.isoformat(),
        }

        manager._backfill_changed_gap(dict(ITEM), previous, current, [])

        self.assertEqual(manager.source.history_calls, 0)

    def test_changed_gap_uses_minimum_integer_day_window(self):
        manager = self.manager()
        now = datetime.now(timezone.utc)
        for hours, expected_days in ((23, 1), (25, 2)):
            old = snapshot(0.70, -(hours * 3600))
            current = snapshot(0.75)
            previous = {
                **old.__dict__,
                "observed_at": (now - timedelta(hours=hours)).isoformat(),
                "source_updated_at": old.source_updated_at.isoformat(),
            }
            current = MarketSnapshot(**{
                **current.__dict__,
                "observed_at": now,
            })
            manager._backfill_changed_gap(dict(ITEM), previous, current, [])
            self.assertEqual(manager.source.history_days[-1], expected_days)

        self.assertEqual(manager.source.history_days, [1, 2])

    def test_failed_gap_backfill_is_retained_for_six_hour_retry(self):
        class FailOnceHistorySource(FakeSource):
            def fetch_history(self, item, days):
                self.history_calls += 1
                self.history_days.append(days)
                if self.history_calls == 1:
                    raise RuntimeError("temporary history failure")
                return []

        source = FailOnceHistorySource()
        manager = MonitoringManager(self.storage, source, FakeNotifier())
        now = datetime.now(timezone.utc)
        old = snapshot(0.70, -(25 * 3600))
        previous = {
            **old.__dict__,
            "observed_at": (now - timedelta(hours=25)).isoformat(),
            "source_updated_at": old.source_updated_at.isoformat(),
        }
        current = MarketSnapshot(**{
            **snapshot(0.75).__dict__,
            "observed_at": now,
        })

        with self.assertRaisesRegex(RuntimeError, "temporary"):
            manager._backfill_changed_gap(dict(ITEM), previous, current, [])
        manager._backfill_changed_gap(dict(ITEM), previous, current, [])
        self.assertEqual(source.history_calls, 1)

        pending_key = f"history-gap-pending:{ITEM['item_key']}"
        pending = json.loads(self.storage.get_metadata(pending_key))
        pending["attempted_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=6, minutes=1)
        ).isoformat()
        self.storage.set_metadata(pending_key, json.dumps(pending))
        manager._backfill_changed_gap(dict(ITEM), previous, current, [])

        self.assertEqual(source.history_days, [2, 2])
        self.assertEqual(self.storage.get_metadata(pending_key), "")

    def test_pruning_uses_successful_observation_time_for_cold_items(self):
        old_source = datetime.now(timezone.utc) - timedelta(days=40)
        current = snapshot(0.70)
        current = MarketSnapshot(**{
            **current.__dict__,
            "source_updated_at": old_source,
        })
        self.storage.save_snapshots([current])

        self.assertEqual(self.storage.prune_snapshots(retain_days=30), 0)
        self.assertIsNotNone(self.storage.latest_snapshot(ITEM["item_key"]))

    def test_adding_same_rule_updates_existing_rule_and_resets_state(self):
        manager = self.manager()
        first = manager.add_rule("umo:a", 1579, "steam", 5.50)
        self.assertEqual(first["action"], "created")
        self.storage.update_rule_state(
            first["id"], alert_active=1, qualifying_count=2, status="active"
        )

        second = manager.add_rule("umo:a", 1579, "steam", 5.40)

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["action"], "updated")
        self.assertEqual(second["previous_threshold"], 5.50)
        self.assertEqual(second["threshold"], 5.40)
        self.assertEqual(
            len(self.storage.list_rules(recipient_key="umo:a", smis_id=1579)), 1
        )
        self.assertEqual(self.storage.get_rule_state(first["id"])["alert_active"], 0)

        unchanged = manager.add_rule("umo:a", 1579, "steam", 5.40)
        self.assertEqual(unchanged["id"], first["id"])
        self.assertEqual(unchanged["action"], "unchanged")
        self.assertEqual(
            len(self.storage.list_rules(recipient_key="umo:a", smis_id=1579)), 1
        )

    def test_item_limit_is_enforced_before_source_request(self):
        for smis_id in range(1, 21):
            self.storage.upsert_item({
                "smis_id": smis_id,
                "item_key": f"smis:{smis_id}",
                "appid": 730,
                "name": f"Item {smis_id}",
                "name_zh": f"饰品 {smis_id}",
            })
            self.storage.add_rule(smis_id, "umo:test", "ratio", 72)
        manager = self.manager()
        with self.assertRaisesRegex(Exception, "最多只能监控 20 个饰品"):
            manager.add_rule("umo:test", 9999, "ratio", 72)

    def test_removing_last_rule_releases_item_slot(self):
        rule = self.storage.add_rule(1579, "umo:a", "ratio", 72)
        manager = self.manager()
        manager.remove_rule("umo:a", rule["id"])
        self.assertIsNone(self.storage.get_item(1579))


class AstrBotNotifierTestCase(unittest.TestCase):
    def test_sends_expected_openapi_payload(self):
        from unittest.mock import Mock

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "ok", "data": {}}
        session = Mock()
        session.post.return_value = response
        notifier = AstrBotNotifier(
            "http://astrbot:6185", "abk_test", session=session
        )
        result = notifier.send_to("astrQQ:FriendMessage:test", "标题", "正文")
        self.assertTrue(result.success)
        call = session.post.call_args
        self.assertEqual(call.kwargs["headers"], {"X-API-Key": "abk_test"})
        self.assertEqual(call.kwargs["json"], {
            "umo": "astrQQ:FriendMessage:test", "message": "标题\n正文",
        })


if __name__ == "__main__":
    unittest.main()
