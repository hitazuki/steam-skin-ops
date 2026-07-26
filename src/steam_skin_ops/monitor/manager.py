from __future__ import annotations

import json
import logging
import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from urllib.parse import quote

from .events import AlertDriver
from .history import t7_stats
from .integrations.smis import MarketDataProvider
from .market import MarketSnapshot
from .repository import MonitorStorage
from .risk import forecast_and_risk, snapshot_fingerprint
from .rules import rule_value, validate_rule

logger = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, data: object = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data


def item_from_row(row: dict) -> dict:
    return {
        "smis_id": int(row["smis_id"]),
        "item_key": str(row["item_key"]),
        "appid": int(row["appid"]),
        "name": str(row["hash_name"]),
        "name_zh": str(row["cn_name"]),
    }


class MonitoringManager:
    def __init__(
        self,
        storage: MonitorStorage,
        source: MarketDataProvider,
        notifier: AlertDriver,
        *,
        max_items: int = 20,
        quote_cache_seconds: int = 60,
        confirmations: int = 1,
        clear_confirmations: int = 1,
        health_failure_threshold: int = 3,
        breakthrough_step_percent: float = 3,
        poll_interval_seconds: int = 1800,
    ) -> None:
        self.storage = storage
        self.source = source
        self.notifier = notifier
        self.max_items = max_items
        self.quote_cache_seconds = quote_cache_seconds
        self.confirmations = confirmations
        self.clear_confirmations = clear_confirmations
        self.health_failure_threshold = health_failure_threshold
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))
        if not math.isfinite(breakthrough_step_percent) or breakthrough_step_percent <= 0:
            raise ValueError("突破档位步长必须大于 0")
        self.breakthrough_step = float(breakthrough_step_percent) / 100
        self._locks_guard = threading.Lock()
        self._item_locks: dict[int, threading.Lock] = {}
        self._sync_breakthrough_step()

    def _sync_breakthrough_step(self) -> None:
        key = "alerting:breakthrough_step"
        value = f"{self.breakthrough_step:.12g}"
        if self.storage.get_metadata(key) == value:
            return
        for rule in self.storage.list_rules():
            state = self.storage.get_rule_state(int(rule["id"]))
            current = state.get("last_value")
            if not state.get("alert_active") or current is None:
                continue
            rule_type = str(rule["rule_type"])
            threshold = float(rule["threshold"])
            limit = threshold / 100 if rule_type in {"ratio", "t7"} else threshold
            level, _ = self._breakthrough_level(rule_type, limit, float(current))
            self.storage.update_rule_state(
                int(rule["id"]), highest_notified_level=level
            )
        self.storage.set_metadata(key, value)

    def _lock_for(self, smis_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._item_locks.setdefault(int(smis_id), threading.Lock())

    def list_items(self) -> list[dict]:
        result = []
        active_ids = {int(rule["smis_id"]) for rule in self.storage.list_rules()}
        for item in self.storage.list_items():
            if int(item["smis_id"]) not in active_ids:
                continue
            latest = self.storage.latest_snapshot(item["item_key"])
            result.append({**item, "latest": self._snapshot_row_to_dict(latest) if latest else None})
        return result

    def search_items(self, query: str, limit: int = 10) -> list[dict]:
        try:
            return self.source.search_items(query, limit=limit)
        except Exception as exc:
            logger.warning("SMIS 搜索失败：%s", exc)
            raise ServiceError(503, "smis_search_failed", "SMIS 搜索暂时不可用") from exc

    @staticmethod
    def _validate_rule(rule_type: str, threshold: float) -> None:
        error = validate_rule(rule_type, threshold)
        if error:
            raise ServiceError(422, error[0], error[1])

    def list_rules(self, recipient_key: str, smis_id: int | None = None) -> list[dict]:
        rows = self.storage.list_rules(recipient_key=recipient_key, smis_id=smis_id)
        for row in rows:
            row["state"] = self.storage.get_rule_state(row["id"])
        return rows

    def add_rule(
        self, recipient_key: str, smis_id: int, rule_type: str, threshold: float
    ) -> dict:
        rule_type = rule_type.strip().lower()
        self._validate_rule(rule_type, threshold)
        if (
            not self.storage.list_rules(smis_id=smis_id)
            and self.storage.count_rule_items() >= self.max_items
        ):
            raise ServiceError(409, "item_limit", f"最多只能监控 {self.max_items} 个饰品")
        item = self.storage.get_item(smis_id)
        if item is None:
            try:
                metadata = self.source.fetch_metadata(smis_id)
            except Exception as exc:
                raise ServiceError(503, "source_unavailable", f"SMIS 饰品信息获取失败：{exc}") from exc
            item = self.storage.upsert_item(metadata)
        rule = self.storage.add_rule(smis_id, recipient_key, rule_type, threshold)
        try:
            self._ensure_history(item_from_row(item))
        except Exception as exc:
            logger.warning("规则历史回填失败 smis_id=%s: %s", smis_id, exc)
        rule["state"] = self.storage.get_rule_state(rule["id"])
        return rule

    def update_rule(self, recipient_key: str, rule_id: int, threshold: float) -> dict:
        rule = self.storage.get_rule(rule_id)
        if not rule or str(rule["recipient_key"]) != recipient_key:
            raise ServiceError(404, "rule_not_found", "当前会话未找到该规则")
        self._validate_rule(str(rule["rule_type"]), threshold)
        return self.storage.update_rule(rule_id, recipient_key, threshold)

    def remove_rule(self, recipient_key: str, rule_id: int) -> None:
        if not self.storage.delete_rule(rule_id, recipient_key):
            raise ServiceError(404, "rule_not_found", "当前会话未找到该规则")

    def _resolve_local_item(self, query: str) -> dict | None:
        matches = self.storage.resolve_items(query)
        if not matches:
            return None
        if query.isdigit():
            return matches[0]
        exact = [
            row for row in matches
            if query.casefold() in {str(row["hash_name"]).casefold(), str(row["cn_name"]).casefold()}
        ]
        if len(exact) == 1:
            return exact[0]
        return None

    def _resolve_market_item(self, query: str) -> dict:
        try:
            if query.isdigit():
                metadata = self.source.fetch_metadata(int(query))
            else:
                matches = self.search_items(query, limit=10)
                exact = [
                    row for row in matches
                    if str(row.get("name_zh") or "").casefold() == query.casefold()
                ]
                candidates = exact if exact else matches
                if not candidates:
                    raise ServiceError(
                        404, "item_not_found", "SMIS 全市场未找到匹配饰品"
                    )
                if len(candidates) > 1:
                    raise ServiceError(
                        409,
                        "ambiguous_item",
                        "名称匹配到多个饰品，请使用 SMIS ID 查询",
                        candidates,
                    )
                metadata = self.source.fetch_metadata(int(candidates[0]["smis_id"]))
        except ServiceError:
            raise
        except Exception as exc:
            logger.warning("SMIS 全市场饰品解析失败：%s", exc)
            raise ServiceError(
                503, "source_unavailable", f"SMIS 饰品信息获取失败：{exc}"
            ) from exc
        return {
            "smis_id": int(metadata["smis_id"]),
            "item_key": str(metadata["item_key"]),
            "appid": int(metadata["appid"]),
            "hash_name": str(metadata["name"]),
            "cn_name": str(metadata["name_zh"]),
        }

    def _resolve_quote_item(self, query: str) -> dict:
        query = query.strip()
        local = self._resolve_local_item(query)
        return local if local is not None else self._resolve_market_item(query)

    def quote(self, query: str) -> dict:
        item_row = self._resolve_quote_item(query)
        item = item_from_row(item_row)
        bootstrap_status: bool | None = False
        try:
            bootstrap_status = self._ensure_history(item)
        except Exception as exc:
            bootstrap_status = None
            logger.warning("报价历史回填失败 smis_id=%s: %s", item["smis_id"], exc)
        latest = self.storage.latest_snapshot(item["item_key"])
        if latest and self._snapshot_age_seconds(latest) <= self.quote_cache_seconds:
            return self._quote_payload(item_row, latest, cached=True, stale=False)

        lock = self._lock_for(item["smis_id"])
        with lock:
            latest = self.storage.latest_snapshot(item["item_key"])
            if latest and self._snapshot_age_seconds(latest) <= self.quote_cache_seconds:
                return self._quote_payload(item_row, latest, cached=True, stale=False)
            try:
                snapshot = self.source.fetch_current(item)
                if bootstrap_status is False:
                    try:
                        self._backfill_changed_gap(
                            item, latest, snapshot,
                            self.storage.list_rules(smis_id=item["smis_id"]),
                        )
                    except Exception as exc:
                        logger.warning(
                            "报价缺口历史回填失败 smis_id=%s: %s",
                            item["smis_id"], exc,
                        )
                self.storage.save_snapshots([snapshot])
                row = self.storage.latest_snapshot(item["item_key"])
                return self._quote_payload(item_row, row, cached=False, stale=False)
            except Exception as exc:
                if latest:
                    payload = self._quote_payload(item_row, latest, cached=True, stale=True)
                    payload["warning"] = f"实时刷新失败，返回最近快照：{exc}"
                    return payload
                raise ServiceError(503, "source_unavailable", f"行情获取失败：{exc}") from exc

    def market_history(self, query: str, days: int = 7) -> dict:
        item_row = self._resolve_quote_item(query.strip())
        item = item_from_row(item_row)
        try:
            snapshots = self.source.fetch_history(item, days)
        except Exception as exc:
            raise ServiceError(503, "source_unavailable", f"历史行情获取失败：{exc}") from exc
        return {
            "smis_id": item["smis_id"],
            "name": item["name"],
            "name_zh": item["name_zh"],
            "days": int(days),
            "points": [
                {
                    "source_updated_at": value.source_updated_at.isoformat(),
                    "steam_sell_price": value.steam_sell_price,
                    "steam_net": value.steam_net,
                    "platforms": [
                        {"name": name, "sell_price": price, "sell_num": count}
                        for name, price, count in value.platform_quotes
                    ],
                }
                for value in snapshots
            ],
        }

    @staticmethod
    def _snapshot_age_seconds(row: dict) -> float:
        observed = datetime.fromisoformat(str(row["observed_at"]))
        return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())

    @staticmethod
    def _snapshot_from_row(row: dict) -> MarketSnapshot:
        return MarketSnapshot(
            item_key=row["item_key"], smis_id=row["smis_id"], appid=row["appid"],
            name=row["name"], name_zh=row["name_zh"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source_updated_at=datetime.fromisoformat(row["source_updated_at"]),
            buff_sell_price=row["buff_sell_price"], buff_sell_num=row["buff_sell_num"],
            uuyp_sell_price=row.get("uuyp_sell_price"), uuyp_sell_num=row.get("uuyp_sell_num"),
            c5_sell_price=row.get("c5_sell_price"), c5_sell_num=row.get("c5_sell_num"),
            igxe_sell_price=row.get("igxe_sell_price"), igxe_sell_num=row.get("igxe_sell_num"),
            eco_sell_price=row.get("eco_sell_price"), eco_sell_num=row.get("eco_sell_num"),
            steam_sell_price=row["steam_sell_price"], steam_sell_num=row["steam_sell_num"],
            steam_transaction_quantity=row["steam_transaction_quantity"],
            buff_to_steam_ratio=row["buff_to_steam_ratio"], kind=row["kind"], source=row["source"],
        )

    @classmethod
    def _snapshot_row_to_dict(cls, row: dict | None) -> dict | None:
        if not row:
            return None
        snapshot = cls._snapshot_from_row(row)
        lowest = snapshot.lowest_platform
        return {
            "observed_at": snapshot.observed_at.isoformat(),
            "source_updated_at": snapshot.source_updated_at.isoformat(),
            "buff_sell_price": snapshot.buff_sell_price,
            "buff_sell_num": snapshot.buff_sell_num,
            "steam_sell_price": snapshot.steam_sell_price,
            "steam_sell_num": snapshot.steam_sell_num,
            "steam_net": snapshot.steam_net,
            "steam_transaction_quantity": snapshot.steam_transaction_quantity,
            "ratio": snapshot.calculated_ratio,
            "platforms": [
                {
                    "name": name,
                    "sell_price": price,
                    "sell_num": count,
                    "is_lowest": lowest is not None and price == lowest[1],
                }
                for name, price, count in snapshot.platform_quotes
            ],
            "lowest_platform": (
                {"name": lowest[0], "sell_price": lowest[1], "sell_num": lowest[2]}
                if lowest else None
            ),
        }

    def _quote_payload(self, item: dict, row: dict, *, cached: bool, stale: bool) -> dict:
        snapshot_object = self._snapshot_from_row(row)
        snapshot = self._snapshot_row_to_dict(row)
        stats = self._t7_stats(str(item["item_key"]))
        forecast, risk = self._forecast_and_risk(
            str(item["item_key"]), snapshot_object, stats, stale=stale
        )
        return {
            "smis_id": int(item["smis_id"]), "appid": int(item["appid"]),
            "name": item["hash_name"], "name_zh": item["cn_name"],
            **snapshot, **stats, "forecast": forecast, "risk_assessment": risk,
            "cached": cached, "stale": stale,
            "links": {
                "smis": f"https://smis.club/commodity/{int(item['smis_id'])}",
                "steam": f"https://steamcommunity.com/market/listings/{int(item['appid'])}/{quote(str(item['hash_name']))}",
            },
        }

    def _ensure_history(self, item: dict) -> bool | None:
        earliest_text = self.storage.earliest_market_time(item["item_key"])
        if earliest_text:
            earliest = datetime.fromisoformat(earliest_text)
            if datetime.now(timezone.utc) - earliest >= timedelta(days=29):
                return False
        key = f"history-bootstrap:{item['item_key']}:30"
        attempted = self.storage.get_metadata(key)
        if attempted == "done":
            return False
        if attempted:
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(attempted) < timedelta(hours=6):
                    return None
            except ValueError:
                pass
        self.storage.set_metadata(key, datetime.now(timezone.utc).isoformat())
        snapshots = self.source.fetch_history(item, 30)
        self.storage.save_snapshots(snapshots)
        self.storage.set_metadata(key, "done")
        return True

    def _attempt_pending_gap_backfill(self, item: dict) -> bool:
        pending_key = f"history-gap-pending:{item['item_key']}"
        raw = self.storage.get_metadata(pending_key)
        if not raw:
            return False
        try:
            pending = json.loads(raw)
            attempted = pending.get("attempted_at")
            if attempted and (
                datetime.now(timezone.utc) - datetime.fromisoformat(attempted)
                < timedelta(hours=6)
            ):
                return True
            pending["attempted_at"] = datetime.now(timezone.utc).isoformat()
            self.storage.set_metadata(pending_key, json.dumps(pending))
            snapshots = self.source.fetch_history(item, int(pending["days"]))
            self.storage.save_snapshots(snapshots)
            self.storage.set_metadata(str(pending["done_key"]), "done")
            self.storage.set_metadata(pending_key, "")
            return False
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("忽略无效的缺口回填状态 item_key=%s", item["item_key"])
            self.storage.set_metadata(pending_key, "")
            return False

    def _backfill_changed_gap(
        self, item: dict, previous: dict | None, snapshot: MarketSnapshot, rules: list[dict],
    ) -> None:
        if self._attempt_pending_gap_backfill(item):
            return
        if previous is None:
            return
        previous_observed = datetime.fromisoformat(str(previous["observed_at"]))
        gap_seconds = max(0.0, (snapshot.observed_at - previous_observed).total_seconds())
        had_failure = any(
            int(self.storage.get_health_state(item["smis_id"], str(rule["recipient_key"]))[
                "fetch_failures"
            ]) > 0
            for rule in rules
        )
        missed_observation = gap_seconds > self.poll_interval_seconds * 2
        if not had_failure and not missed_observation:
            return
        if snapshot_fingerprint(previous) == snapshot_fingerprint(snapshot):
            return
        days = max(1, min(30, math.ceil(gap_seconds / 86400)))
        signature = (
            f"{item['item_key']}|{previous['observed_at']}|"
            f"{snapshot.observed_at.isoformat()}|{days}"
        )
        digest = sha1(signature.encode("utf-8")).hexdigest()[:16]
        key = f"history-gap:{item['item_key']}:{digest}"
        state = self.storage.get_metadata(key)
        if state == "done":
            return
        pending_key = f"history-gap-pending:{item['item_key']}"
        pending = {
            "done_key": key,
            "days": days,
            "gap_started_at": previous["observed_at"],
            "gap_ended_at": snapshot.observed_at.isoformat(),
            "attempted_at": None,
        }
        self.storage.set_metadata(pending_key, json.dumps(pending))
        self._attempt_pending_gap_backfill(item)

    def _t7_stats(self, item_key: str) -> dict:
        return t7_stats(self.storage.steam_history(item_key, days=7))

    def _forecast_and_risk(
        self, item_key: str, snapshot: MarketSnapshot, stats: dict, *, stale: bool = False,
    ) -> tuple[dict, dict]:
        rows = self.storage.market_history_rows(item_key, days=30)
        return forecast_and_risk(rows, snapshot, stats, stale=stale)

    def monitor_item(self, item_row: dict) -> dict:
        item = item_from_row(item_row)
        rules = self.storage.list_rules(smis_id=item["smis_id"])
        if not rules:
            return {"smis_id": item["smis_id"], "skipped": True}
        previous = self.storage.latest_snapshot(item["item_key"])
        bootstrap_status: bool | None = False
        try:
            try:
                bootstrap_status = self._ensure_history(item)
            except Exception as exc:
                bootstrap_status = None
                logger.warning("T+7 历史回填失败 smis_id=%s: %s", item["smis_id"], exc)
            with self._lock_for(item["smis_id"]):
                snapshot = self.source.fetch_current(item)
                if bootstrap_status is False:
                    try:
                        self._backfill_changed_gap(item, previous, snapshot, rules)
                    except Exception as exc:
                        logger.warning("缺口历史回填失败 smis_id=%s: %s", item["smis_id"], exc)
                self.storage.save_snapshots([snapshot])
        except Exception as exc:
            self._handle_item_failure(item, rules, exc)
            return {"smis_id": item["smis_id"], "success": False, "error": str(exc)}

        self._handle_item_recovery(item, rules, snapshot)
        stats = self._t7_stats(item["item_key"])
        forecast, risk = self._forecast_and_risk(item["item_key"], snapshot, stats)
        for rule in rules:
            self._evaluate_rule(snapshot, stats, forecast, risk, rule)
        return {"smis_id": item["smis_id"], "success": True}

    def _evaluate_rule(
        self, snapshot: MarketSnapshot, stats: dict, forecast: dict,
        risk: dict, rule: dict,
    ) -> None:
        rule_id = int(rule["id"])
        rule_type = str(rule["rule_type"])
        threshold = float(rule["threshold"])
        state = self.storage.get_rule_state(rule_id)
        value, baseline, status = rule_value(snapshot, stats, rule_type)
        observed = snapshot.observed_at.isoformat()
        changes = {
            "last_value": value, "last_baseline": baseline,
            "last_observed_at": observed, "status": status,
        }
        if value is None:
            changes.update({"qualifying_count": 0, "clearing_count": 0})
            self.storage.update_rule_state(rule_id, **changes)
            return

        limit = threshold / 100 if rule_type in {"ratio", "t7"} else threshold
        qualifies = value >= limit if rule_type == "steam" else value <= limit
        if not state["alert_active"]:
            qualifying = int(state["qualifying_count"]) + 1 if qualifies else 0
            changes.update({"qualifying_count": qualifying, "clearing_count": 0})
            if qualifying >= self.confirmations:
                initial_level, initial_depth = self._breakthrough_level(
                    rule_type, limit, value
                )
                if initial_level > 0:
                    title, content = self._format_breakthrough_alert(
                        snapshot, stats, forecast, risk, rule,
                        value, initial_depth, initial_level
                    )
                else:
                    title, content = self._format_rule_alert(
                        snapshot, stats, forecast, risk, rule, value
                    )
                signal = f"rule:{rule_id}:{observed}"
                self.storage.enqueue_notification(
                    signal, str(rule["recipient_key"]), f"rule_{rule_type}", title, content,
                    driver=self.notifier.name, rule_id=rule_id,
                )
                changes.update({
                    "alert_active": 1, "qualifying_count": qualifying,
                    "last_signal_at": observed,
                    "highest_notified_level": initial_level,
                })
        else:
            clear_boundary = limit * (0.97 if rule_type == "steam" else 1.03)
            clears = value < clear_boundary if rule_type == "steam" else value > clear_boundary
            clearing = int(state["clearing_count"]) + 1 if clears else 0
            changes.update({"qualifying_count": 0, "clearing_count": clearing})
            if clearing >= self.clear_confirmations:
                changes.update({
                    "alert_active": 0, "clearing_count": 0,
                    "highest_notified_level": 0,
                })
            elif qualifies:
                level, depth = self._breakthrough_level(rule_type, limit, value)
                if level > int(state["highest_notified_level"]):
                    title, content = self._format_breakthrough_alert(
                        snapshot, stats, forecast, risk, rule, value, depth, level
                    )
                    self.storage.enqueue_notification(
                        f"rule:{rule_id}:breakthrough:{level}:{observed}",
                        str(rule["recipient_key"]), "rule_breakthrough", title, content,
                        driver=self.notifier.name, rule_id=rule_id,
                    )
                    changes.update({
                        "highest_notified_level": level,
                        "last_signal_at": observed,
                    })
        self.storage.update_rule_state(rule_id, **changes)

    def _breakthrough_level(
        self, rule_type: str, limit: float, value: float
    ) -> tuple[int, float]:
        depth = (
            (value - limit) / limit
            if rule_type == "steam"
            else (limit - value) / limit
        )
        depth = max(0.0, depth)
        return math.floor((depth + 1e-12) / self.breakthrough_step), depth

    @classmethod
    def _format_breakthrough_alert(
        cls, snapshot: MarketSnapshot, stats: dict, forecast: dict,
        risk: dict, rule: dict,
        value: float, depth: float, level: int,
    ) -> tuple[str, str]:
        rule_type = str(rule["rule_type"])
        labels = {
            "ratio": "即时比例", "t7": "T+7 保守比例",
            "platform": "最低平台价", "steam": "Steam 售价",
        }
        value_text = f"{value:.2%}" if rule_type in {"ratio", "t7"} else f"¥{value:.2f}"
        threshold = float(rule["threshold"])
        threshold_text = (
            f"{threshold:.2f}%" if rule_type in {"ratio", "t7"} else f"¥{threshold:.2f}"
        )
        lines = [
            f"规则 #{int(rule['id'])} · {snapshot.name_zh} / {snapshot.name}",
            f"{labels[rule_type]}：{value_text}（阈值 {threshold_text}）",
            f"相对阈值继续突破：{depth:.2%}，达到第 {level} 档",
        ]
        lines.extend(cls._market_reference_lines(snapshot, stats, forecast, risk))
        return f"【继续突破·第 {level} 档】{snapshot.name_zh} {value_text}", "\n".join(lines)

    def enqueue_daily_summary(self, summary_date: date) -> int:
        date_text = summary_date.isoformat()
        key = "daily_summary:last_date"
        if self.storage.get_metadata(key) == date_text:
            return 0

        grouped: dict[str, list[tuple[dict, dict, int, float]]] = defaultdict(list)
        for rule in self.storage.list_rules():
            state = self.storage.get_rule_state(int(rule["id"]))
            value = state.get("last_value")
            if not state.get("alert_active") or value is None:
                continue
            rule_type = str(rule["rule_type"])
            threshold = float(rule["threshold"])
            limit = threshold / 100 if rule_type in {"ratio", "t7"} else threshold
            qualifies = float(value) >= limit if rule_type == "steam" else float(value) <= limit
            if not qualifies:
                continue
            level, depth = self._breakthrough_level(rule_type, limit, float(value))
            grouped[str(rule["recipient_key"])].append((rule, state, level, depth))

        queued = 0
        labels = {
            "ratio": "即时比例", "t7": "T+7 比例",
            "platform": "平台价", "steam": "Steam 价",
        }
        analysis_cache: dict[str, tuple[dict, dict]] = {}
        for recipient_key, entries in sorted(grouped.items()):
            lines = [f"当前仍满足条件的交易告警共 {len(entries)} 条："]
            for rule, state, level, depth in entries:
                rule_type = str(rule["rule_type"])
                value = float(state["last_value"])
                threshold = float(rule["threshold"])
                if rule_type in {"ratio", "t7"}:
                    values = f"{value:.2%} / 阈值 {threshold:.2f}%"
                else:
                    values = f"¥{value:.2f} / 阈值 ¥{threshold:.2f}"
                item_key = str(rule["item_key"])
                if item_key not in analysis_cache:
                    latest = self.storage.latest_snapshot(item_key)
                    if latest:
                        snapshot = self._snapshot_from_row(latest)
                        stats = self._t7_stats(item_key)
                        stale = (
                            self._snapshot_age_seconds(latest)
                            > self.poll_interval_seconds * 2
                        )
                        analysis_cache[item_key] = self._forecast_and_risk(
                            item_key, snapshot, stats, stale=stale
                        )
                    else:
                        unavailable = {"status": "unavailable"}
                        analysis_cache[item_key] = (unavailable, unavailable)
                forecast, risk = analysis_cache[item_key]
                analysis_suffix = self._daily_analysis_suffix(forecast, risk)
                lines.append(
                    f"#{int(rule['id'])} {rule['cn_name']} · {labels[rule_type]} "
                    f"{values} · 第 {level} 档（{depth:.2%}）{analysis_suffix}"
                )
            queued += int(self.storage.enqueue_notification(
                f"daily-summary:{date_text}", recipient_key, "daily_summary",
                f"【每日交易告警汇总】{date_text}", "\n".join(lines),
                driver=self.notifier.name,
            ))
        self.storage.set_metadata(key, date_text)
        return queued

    def _handle_item_failure(self, item: dict, rules: list[dict], exc: Exception) -> None:
        for recipient_key in sorted({str(rule["recipient_key"]) for rule in rules}):
            state = self.storage.get_health_state(item["smis_id"], recipient_key)
            failures = int(state["fetch_failures"]) + 1
            alerted = int(state["health_alerted"])
            if failures >= self.health_failure_threshold and not alerted:
                self.storage.enqueue_notification(
                    f"health:{item['item_key']}:{recipient_key}:down:{failures}",
                    recipient_key, "health_down",
                    f"【监控异常】{item['name_zh']}",
                    f"SMIS 行情连续 {failures} 轮请求失败：{exc}",
                    driver=self.notifier.name,
                )
                alerted = 1
            self.storage.update_health_state(
                item["smis_id"], recipient_key,
                fetch_failures=failures, health_alerted=alerted,
            )

    def _handle_item_recovery(self, item: dict, rules: list[dict], snapshot: MarketSnapshot) -> None:
        for recipient_key in sorted({str(rule["recipient_key"]) for rule in rules}):
            state = self.storage.get_health_state(item["smis_id"], recipient_key)
            if state["health_alerted"]:
                self.storage.enqueue_notification(
                    f"health:{item['item_key']}:{recipient_key}:recovered:"
                    f"{snapshot.observed_at.isoformat()}",
                    recipient_key, "health_recovered", f"【监控恢复】{item['name_zh']}",
                    "SMIS 行情请求已恢复。",
                    driver=self.notifier.name,
                )
            self.storage.update_health_state(
                item["smis_id"], recipient_key, fetch_failures=0, health_alerted=0
            )

    @staticmethod
    def _level_text(level: object) -> str:
        return {"low": "低", "medium": "中", "high": "高"}.get(
            str(level), "未知"
        )

    @classmethod
    def _analysis_reference_lines(cls, forecast: dict, risk: dict) -> list[str]:
        lines: list[str] = []
        if forecast.get("status") == "ready":
            ratio = forecast.get("forecast_balance_ratio")
            ratio_text = f"{float(ratio):.2%}" if ratio is not None else "不可用"
            window = int(forecast.get("window_days") or 0)
            window_text = f"{window} 日" if window else ""
            lines.extend([
                f"七日预测：¥{float(forecast['predicted_steam_net']):.2f}"
                f"（{float(forecast['change_pct']) / 100:+.1%}）"
                f"｜预测倒余额比例 {ratio_text}",
                f"预测模式：{window_text}{forecast['mode_label']}（"
                f"{'正常' if forecast.get('confidence') == 'normal' else '低'}置信度）",
            ])
        else:
            reasons = forecast.get("reasons") or ["历史或当前行情不足"]
            lines.append(f"七日预测：不可用（{reasons[0]}）")

        if risk.get("status") == "ready":
            dimensions = risk.get("dimensions") or {}
            labels = {"price": "价格", "volatility": "波动", "inventory": "库存", "volume": "成交量"}
            dimension_text = "、".join(
                f"{labels[key]}{cls._level_text((dimensions.get(key) or {}).get('level'))}"
                for key in labels
            )
            ratio = risk.get("risk_balance_ratio")
            ratio_text = f"{float(ratio):.2%}" if ratio is not None else "不可用"
            lines.append(
                f"风险评估：总体{cls._level_text(risk.get('overall_level'))}"
                f"｜{dimension_text}｜风险倒余额比例 {ratio_text}"
            )
            reasons = risk.get("reasons") or []
            if reasons:
                lines.append(f"风险说明：{'；'.join(str(reason) for reason in reasons[:3])}")
        else:
            lines.append("风险评估：不可用")
        return lines

    @classmethod
    def _daily_analysis_suffix(cls, forecast: dict, risk: dict) -> str:
        if forecast.get("status") == "ready":
            forecast_ratio = forecast.get("forecast_balance_ratio")
            forecast_text = (
                f"预测{float(forecast['change_pct']) / 100:+.1%}"
                f"/{float(forecast_ratio):.2%}"
                if forecast_ratio is not None else
                f"预测{float(forecast['change_pct']) / 100:+.1%}"
            )
            mode_text = str(forecast.get("mode_label") or "未知模式")
        else:
            forecast_text, mode_text = "预测不可用", ""
        if risk.get("status") == "ready":
            risk_ratio = risk.get("risk_balance_ratio")
            risk_text = f"风险{cls._level_text(risk.get('overall_level'))}"
            if risk_ratio is not None:
                risk_text += f"/{float(risk_ratio):.2%}"
        else:
            risk_text = "风险不可用"
        mode_suffix = f"({mode_text})" if mode_text else ""
        return f" · {forecast_text}{mode_suffix} · {risk_text}"

    @classmethod
    def _market_reference_lines(
        cls, snapshot: MarketSnapshot, stats: dict,
        forecast: dict, risk: dict,
    ) -> list[str]:
        lines: list[str] = []
        lowest = snapshot.lowest_platform
        if lowest:
            lines.append(f"最低平台：{lowest[0]} ¥{lowest[1]:.2f}（在售 {lowest[2]}）")
        lines.extend([
            f"Steam 售价：¥{float(snapshot.steam_sell_price or 0):.2f}",
            f"Steam 预计到手：¥{snapshot.steam_net:.2f}",
            f"Steam 在售/日成交：{snapshot.steam_sell_num or 0}/{snapshot.steam_transaction_quantity or 0}",
        ])
        if stats.get("t7_steam_net_p25") is not None:
            lines.extend([
                f"7 日 Steam 到手最低：¥{stats['t7_steam_net_low']:.2f}",
                f"7 日 Steam 到手 P25：¥{stats['t7_steam_net_p25']:.2f}",
                f"7 日 Steam 到手中位数：¥{stats['t7_steam_net_median']:.2f}",
            ])
        lines.extend(cls._analysis_reference_lines(forecast, risk))
        lines.extend([
            f"数据更新时间：{snapshot.source_updated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"SMIS：https://smis.club/commodity/{snapshot.smis_id}",
            f"Steam：https://steamcommunity.com/market/listings/{snapshot.appid}/{quote(snapshot.name)}",
        ])
        return lines

    @classmethod
    def _format_rule_alert(
        cls, snapshot: MarketSnapshot, stats: dict, forecast: dict, risk: dict,
        rule: dict, value: float,
    ) -> tuple[str, str]:
        labels = {
            "ratio": ("【即时挂刀】", "即时比例"),
            "t7": ("【T+7挂刀】", "T+7 保守比例"),
            "platform": ("【平台到价】", "最低平台价"),
            "steam": ("【Steam清仓】", "Steam 售价"),
        }
        rule_type = str(rule["rule_type"])
        prefix, metric_label = labels[rule_type]
        threshold = float(rule["threshold"])
        value_text = f"{value:.2%}" if rule_type in {"ratio", "t7"} else f"¥{value:.2f}"
        threshold_text = f"{threshold:.2f}%" if rule_type in {"ratio", "t7"} else f"¥{threshold:.2f}"
        lines = [
            f"规则 #{int(rule['id'])} · {snapshot.name_zh} / {snapshot.name}",
            f"{metric_label}：{value_text}（阈值 {threshold_text}）",
        ]
        lines.extend(cls._market_reference_lines(snapshot, stats, forecast, risk))
        return f"{prefix}{snapshot.name_zh} {value_text}", "\n".join(lines)

    def dispatch_outbox(self) -> dict[str, int]:
        sent = failed = 0
        for message in self.storage.due_notifications():
            result = self.notifier.send_to(
                message["recipient_key"], message["title"], message["content"]
            )
            if result.success:
                self.storage.mark_notification_sent(message["id"])
                sent += 1
            else:
                self.storage.mark_notification_failed(message["id"], result.message)
                failed += 1
        return {"sent": sent, "failed": failed}

    def run_cycle(self, max_workers: int = 4) -> list[dict]:
        active_ids = {int(rule["smis_id"]) for rule in self.storage.list_rules()}
        items = [
            item for item in self.storage.list_items(enabled_only=True)
            if int(item["smis_id"]) in active_ids
        ]
        results: list[dict] = []
        if items:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as executor:
                futures = [executor.submit(self.monitor_item, item) for item in items]
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        logger.exception("监控任务异常")
                        results.append({"success": False, "error": str(exc)})
        self.dispatch_outbox()
        return results

    def test_event(self, recipient_key: str) -> dict:
        now = datetime.now().astimezone()
        self.storage.enqueue_notification(
            f"test:{recipient_key}:{now.isoformat()}", recipient_key, "test",
            "【Steam Skin Ops】告警测试",
            f"服务链路正常。\n测试时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            driver=self.notifier.name,
        )
        self.dispatch_outbox()
        return self.storage.list_events(recipient_key, limit=1)[0]

    def backup(self, backup_dir: Path, retain: int = 7) -> Path:
        destination = backup_dir / f"monitor-{datetime.now().strftime('%Y%m%d')}.db"
        self.storage.backup(destination)
        backups = sorted(backup_dir.glob("monitor-*.db"), reverse=True)
        for old in backups[retain:]:
            old.unlink(missing_ok=True)
        return destination
