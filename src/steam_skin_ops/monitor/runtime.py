from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .manager import MonitoringManager

logger = logging.getLogger(__name__)


class ServiceRuntime:
    def __init__(
        self, manager: MonitoringManager, interval_seconds: int = 1800,
        backup_dir: Path = Path("./data/backups"),
        daily_summary_time: str = "09:00",
    ) -> None:
        self.manager = manager
        self.interval_seconds = max(1, interval_seconds)
        self.backup_dir = Path(backup_dir)
        try:
            parsed = datetime.strptime(daily_summary_time, "%H:%M")
        except ValueError as exc:
            raise ValueError("每日报告时间必须使用 HH:MM 格式") from exc
        self.daily_summary_minutes = parsed.hour * 60 + parsed.minute
        self.local_timezone = ZoneInfo("Asia/Shanghai")
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_at: str | None = None
        self.last_cycle_at: str | None = None
        self.last_cycle_ok: bool | None = None
        self.last_error: str | None = None
        self.last_backup_date: date | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.thread = threading.Thread(target=self._run, name="steam-skin-ops-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.last_cycle_ok = False
                self.last_error = str(exc)
                logger.exception("监控周期执行失败")
            finally:
                self.last_cycle_at = datetime.now(timezone.utc).isoformat()
            self.stop_event.wait(self.interval_seconds)

    def run_once(self, now: datetime | None = None) -> list[dict]:
        results = self.manager.run_cycle()
        self.last_cycle_ok = all(result.get("success", True) for result in results)
        self.last_error = None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("运行时间必须包含时区")
        now_local = current.astimezone(self.local_timezone)
        today = now_local.date()
        minute_of_day = now_local.hour * 60 + now_local.minute
        if minute_of_day >= self.daily_summary_minutes:
            self.manager.enqueue_daily_summary(today)
            self.manager.dispatch_outbox()
        if self.last_backup_date != today:
            self.manager.storage.prune_snapshots(retain_days=8)
            self.manager.backup(self.backup_dir)
            self.last_backup_date = today
        return results

    def status(self) -> dict:
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_ok": self.last_cycle_ok,
            "last_error": self.last_error,
            "items": self.manager.storage.count_rule_items(),
            "rules": len(self.manager.storage.list_rules()),
            "outbox": self.manager.storage.outbox_counts(),
        }
