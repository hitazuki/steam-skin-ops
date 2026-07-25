from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = Path("config/monitor.yaml")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"监控配置 {name} 必须是映射")
    return value


def _positive(value: Any, name: str, cast):
    if isinstance(value, bool):
        raise ValueError(f"监控配置 {name} 必须是正数")
    try:
        result = cast(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"监控配置 {name} 必须是正数") from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError(f"监控配置 {name} 必须是正数")
    if result <= 0:
        raise ValueError(f"监控配置 {name} 必须是正数")
    return result


@dataclass(frozen=True)
class MonitorConfig:
    service_token: str
    database: Path
    backup_dir: Path
    alert_driver: str
    interval_seconds: int
    quote_cache_seconds: int
    max_items: int
    breakthrough_step_percent: float
    daily_summary_time: str
    smis_timeout_seconds: float
    smis_max_retries: int
    smis_min_request_interval_seconds: float
    astrbot_base_url: str
    astrbot_api_key: str
    astrbot_message_path: str
    astrbot_timeout_seconds: float


def load_config(path: str | Path = DEFAULT_CONFIG) -> MonitorConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"监控配置不存在：{config_path}；请复制 config/monitor.example.yaml "
            "为 config/monitor.yaml 并填写 service.token"
        )
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("监控配置根节点必须是映射")

    service = _section(raw, "service")
    monitor = _section(raw, "monitor")
    alerts = _section(raw, "alerts")
    smis = _section(raw, "smis")
    astrbot = _section(raw, "astrbot")

    token = str(service.get("token", "")).strip()
    if not token:
        raise ValueError("监控配置 service.token 不能为空")
    driver = str(alerts.get("driver", "store")).strip().lower()
    if driver not in {"store", "astrbot"}:
        raise ValueError("监控配置 alerts.driver 必须是 store 或 astrbot")
    api_key = str(astrbot.get("api_key", "")).strip()
    if driver == "astrbot" and not api_key:
        raise ValueError("使用 astrbot 告警驱动时 astrbot.api_key 不能为空")

    daily_time = str(alerts.get("daily_summary_time", "09:00")).strip()
    try:
        datetime.strptime(daily_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("监控配置 alerts.daily_summary_time 必须使用 HH:MM 格式") from exc

    return MonitorConfig(
        service_token=token,
        database=Path(str(service.get("database", "./data/monitor.db"))),
        backup_dir=Path(str(service.get("backup_dir", "./data/backups"))),
        alert_driver=driver,
        interval_seconds=_positive(
            monitor.get("interval_seconds", 1800), "monitor.interval_seconds", int
        ),
        quote_cache_seconds=_positive(
            monitor.get("quote_cache_seconds", 60), "monitor.quote_cache_seconds", int
        ),
        max_items=_positive(monitor.get("max_items", 20), "monitor.max_items", int),
        breakthrough_step_percent=_positive(
            alerts.get("breakthrough_step_percent", 3),
            "alerts.breakthrough_step_percent", float,
        ),
        daily_summary_time=daily_time,
        smis_timeout_seconds=_positive(
            smis.get("timeout_seconds", 15), "smis.timeout_seconds", float
        ),
        smis_max_retries=_positive(
            smis.get("max_retries", 3), "smis.max_retries", int
        ),
        smis_min_request_interval_seconds=_positive(
            smis.get("min_request_interval_seconds", 1),
            "smis.min_request_interval_seconds", float,
        ),
        astrbot_base_url=str(astrbot.get("base_url", "http://astrbot:6185")).rstrip("/"),
        astrbot_api_key=api_key,
        astrbot_message_path=str(
            astrbot.get("message_path", "/api/v1/im/message")
        ),
        astrbot_timeout_seconds=_positive(
            astrbot.get("timeout_seconds", 10), "astrbot.timeout_seconds", float
        ),
    )
