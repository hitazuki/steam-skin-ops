from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
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
        self.daily_summary_time = time(parsed.hour, parsed.minute)
        self.local_timezone = ZoneInfo("Asia/Shanghai")
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.summary_thread: threading.Thread | None = None
        self.started_at: str | None = None
        self.last_cycle_at: str | None = None
        self.last_cycle_ok: bool | None = None
        self.last_error: str | None = None
        self.next_daily_summary_at: str | None = None
        self.last_daily_summary_at: str | None = None
        self.last_daily_summary_ok: bool | None = None
        self.last_daily_summary_error: str | None = None
        self.last_backup_date: date | None = None

    def start(self) -> None:
        monitor_running = bool(self.thread and self.thread.is_alive())
        summary_running = bool(self.summary_thread and self.summary_thread.is_alive())
        if monitor_running and summary_running:
            return
        self.stop_event.clear()
        if not monitor_running and not summary_running:
            self.started_at = datetime.now(timezone.utc).isoformat()
        if not monitor_running:
            self.thread = threading.Thread(
                target=self._run, name="steam-skin-ops-monitor", daemon=True
            )
            self.thread.start()
        if not summary_running:
            self.summary_thread = threading.Thread(
                target=self._run_daily_scheduler,
                name="steam-skin-ops-daily-summary",
                daemon=True,
            )
            self.summary_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
        if self.summary_thread:
            self.summary_thread.join(timeout=15)

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

    def _next_daily_summary_target(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("运行时间必须包含时区")
        now_local = now.astimezone(self.local_timezone)
        target = datetime.combine(
            now_local.date(), self.daily_summary_time, self.local_timezone
        )
        completed_date = self.manager.storage.get_metadata("daily_summary:last_date")
        if now_local < target:
            return target
        if completed_date != now_local.date().isoformat():
            return now_local
        return target + timedelta(days=1)

    def _run_daily_scheduler(self) -> None:
        while not self.stop_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                target = self._next_daily_summary_target(now)
            except Exception as exc:
                self.last_daily_summary_ok = False
                self.last_daily_summary_error = str(exc)
                logger.exception("每日交易告警汇总调度失败")
                if self.stop_event.wait(300):
                    break
                continue
            self.next_daily_summary_at = target.isoformat()
            delay = max(0.0, (target - now.astimezone(self.local_timezone)).total_seconds())
            if delay > 0:
                if self.stop_event.wait(min(delay, 60.0)):
                    break
                continue
            try:
                self.run_daily_summary(now)
            except Exception:
                logger.exception("每日交易告警汇总执行失败")
                if self.stop_event.wait(300):
                    break
        self.next_daily_summary_at = None

    def run_daily_summary(self, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("运行时间必须包含时区")
        today = current.astimezone(self.local_timezone).date()
        self.last_daily_summary_at = current.astimezone(timezone.utc).isoformat()
        try:
            queued = self.manager.enqueue_daily_summary(today)
            delivery = self.manager.dispatch_outbox()
        except Exception as exc:
            self.last_daily_summary_ok = False
            self.last_daily_summary_error = str(exc)
            raise
        self.last_daily_summary_ok = True
        self.last_daily_summary_error = None
        result = {"queued": int(queued), **delivery}
        logger.info(
            "每日交易告警汇总完成 date=%s queued=%s sent=%s failed=%s",
            today.isoformat(), result["queued"], result["sent"], result["failed"],
        )
        return result

    def run_once(self, now: datetime | None = None) -> list[dict]:
        results = self.manager.run_cycle()
        self.last_cycle_ok = all(result.get("success", True) for result in results)
        self.last_error = None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("运行时间必须包含时区")
        now_local = current.astimezone(self.local_timezone)
        today = now_local.date()
        if self.last_backup_date != today:
            self.manager.storage.prune_snapshots(retain_days=30)
            self.manager.backup(self.backup_dir)
            self.last_backup_date = today
        return results

    def status(self) -> dict:
        monitor_running = bool(self.thread and self.thread.is_alive())
        summary_running = bool(self.summary_thread and self.summary_thread.is_alive())
        return {
            "running": monitor_running and summary_running,
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_ok": self.last_cycle_ok,
            "last_error": self.last_error,
            "daily_summary_running": summary_running,
            "next_daily_summary_at": self.next_daily_summary_at,
            "last_daily_summary_at": self.last_daily_summary_at,
            "last_daily_summary_ok": self.last_daily_summary_ok,
            "last_daily_summary_error": self.last_daily_summary_error,
            "items": self.manager.storage.count_rule_items(),
            "rules": len(self.manager.storage.list_rules()),
            "outbox": self.manager.storage.outbox_counts(),
        }
