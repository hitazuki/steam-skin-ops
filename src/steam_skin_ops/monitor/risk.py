from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .market import MarketSnapshot, steam_net_amount


FORECAST_DAYS = 7
FORECAST_WINDOW_DAYS = 21
MIN_FORECAST_DAYS = 14
GRID_HOURS = 2


def snapshot_fingerprint(snapshot: MarketSnapshot | dict[str, Any]) -> tuple[Any, ...]:
    def value(name: str) -> Any:
        if isinstance(snapshot, dict):
            return snapshot.get(name)
        return getattr(snapshot, name)

    return tuple(
        value(name)
        for name in (
            "buff_sell_price", "buff_sell_num",
            "uuyp_sell_price", "uuyp_sell_num",
            "c5_sell_price", "c5_sell_num",
            "igxe_sell_price", "igxe_sell_num",
            "eco_sell_price", "eco_sell_num",
            "steam_sell_price", "steam_sell_num",
            "steam_transaction_quantity",
        )
    )


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _grid_time(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    hour = value.hour - value.hour % GRID_HOURS
    return value.replace(hour=hour, minute=0, second=0, microsecond=0)


def canonical_two_hour_rows(
    rows: list[dict[str, Any]], now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Merge history with successful current observations on a fixed two-hour grid."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current_time - timedelta(days=FORECAST_WINDOW_DAYS)
    buckets: dict[datetime, tuple[int, datetime, dict[str, Any]]] = {}
    for row in rows:
        kind = str(row.get("kind") or "history")
        event_time = _parse_time(
            row["observed_at"] if kind == "current" else row["source_updated_at"]
        )
        if event_time < cutoff or event_time > current_time + timedelta(minutes=5):
            continue
        bucket = _grid_time(event_time)
        # A successful current observation is more precise for the open local tail.
        priority = 1 if kind == "current" else 0
        candidate = (priority, event_time, row)
        previous = buckets.get(bucket)
        if previous is None or candidate[:2] > previous[:2]:
            buckets[bucket] = candidate
    return [
        {**candidate[2], "_grid_at": bucket.isoformat()}
        for bucket, candidate in sorted(buckets.items())
    ]


def _daily_price_points(rows: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    by_day: dict[datetime, list[tuple[datetime, float]]] = defaultdict(list)
    for row in rows:
        gross = float(row.get("steam_sell_price") or 0)
        net = steam_net_amount(gross)
        if net <= 0:
            continue
        grid_at = _parse_time(row["_grid_at"])
        day = grid_at.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day[day].append((grid_at, net))

    all_values = sorted(
        point for points in by_day.values() for point in points
    )
    result: list[tuple[datetime, float]] = []
    for day, points in sorted(by_day.items()):
        anchor = max(timestamp for timestamp, _ in points)
        window_start = anchor - timedelta(hours=24)
        values = [
            value for timestamp, value in all_values
            if window_start < timestamp <= anchor
        ]
        if values:
            result.append((day, median(values)))
    return result


def _theil_sen_log_slope(points: list[tuple[datetime, float]]) -> float:
    slopes: list[float] = []
    origin = points[0][0]
    xy = [
        ((timestamp - origin).total_seconds() / 86400, math.log(value))
        for timestamp, value in points if value > 0
    ]
    for index, (x1, y1) in enumerate(xy):
        for x2, y2 in xy[index + 1:]:
            if x2 > x1:
                slopes.append((y2 - y1) / (x2 - x1))
    return median(slopes) if slopes else 0.0


def _change(rows: list[dict[str, Any]], field: str, days: int = 7) -> float | None:
    valid = [
        (_parse_time(row["_grid_at"]), float(row.get(field) or 0))
        for row in rows if float(row.get(field) or 0) > 0
    ]
    if len(valid) < 2:
        return None
    last_time, last_value = valid[-1]
    cutoff = last_time - timedelta(days=days)
    first = min(valid, key=lambda pair: abs((pair[0] - cutoff).total_seconds()))
    if abs((first[0] - cutoff).total_seconds()) > GRID_HOURS * 2 * 3600:
        return None
    return last_value / first[1] - 1 if first[1] > 0 else None


def _stock_flow(row: dict[str, Any]) -> float | None:
    inventory = float(row.get("steam_sell_num") or 0)
    volume = float(row.get("steam_transaction_quantity") or 0)
    return inventory / volume if inventory > 0 and volume > 0 else None


def risk_prediction(
    rows: list[dict[str, Any]],
    snapshot: MarketSnapshot,
    t7_stats: dict[str, Any],
    *,
    stale: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    base = {
        "status": "unavailable",
        "level": "unknown",
        "confidence": "none",
        "forecast_horizon_days": FORECAST_DAYS,
        "window_days": FORECAST_WINDOW_DAYS,
        "actual_window_days": 0,
        "coverage": 0.0,
        "reasons": [],
    }
    if stale:
        return {**base, "status": "stale", "reasons": ["当前行情为过期快照"]}
    lowest = snapshot.lowest_platform
    if lowest is None or snapshot.steam_net <= 0:
        return {**base, "status": "price_missing", "reasons": ["当前价格不足"]}

    grid = canonical_two_hour_rows(rows, now=now or snapshot.observed_at)
    daily = _daily_price_points(grid)
    if len(daily) < MIN_FORECAST_DAYS:
        return {
            **base,
            "status": "insufficient_history",
            "actual_window_days": len(daily),
            "reasons": [f"历史不足 {MIN_FORECAST_DAYS} 天"],
        }

    used = daily[-FORECAST_WINDOW_DAYS:]
    actual_days = len(used)
    first_day, last_day = used[0][0], used[-1][0]
    span_days = max(1, int((last_day - first_day).total_seconds() / 86400) + 1)
    window_start = last_day - timedelta(days=min(FORECAST_WINDOW_DAYS, span_days) - 1)
    relevant_grid = [
        row for row in grid if _parse_time(row["_grid_at"]) >= window_start
    ]
    expected_buckets = max(1, span_days * (24 // GRID_HOURS))
    coverage = min(1.0, len(relevant_grid) / expected_buckets)
    if coverage < 0.8:
        return {
            **base,
            "status": "insufficient_coverage",
            "actual_window_days": actual_days,
            "coverage": round(coverage, 4),
            "reasons": ["两小时行情覆盖不足 80%"],
        }

    slope = _theil_sen_log_slope(used)
    forecast_net = snapshot.steam_net * math.exp(slope * FORECAST_DAYS)
    forecast_change = forecast_net / snapshot.steam_net - 1
    p25 = t7_stats.get("t7_steam_net_p25")
    risk_candidates = [snapshot.steam_net, forecast_net]
    if p25 is not None and float(p25) > 0:
        risk_candidates.append(float(p25))
    risk_net = min(risk_candidates)
    platform_price = float(lowest[1])
    forecast_ratio = platform_price / forecast_net if forecast_net > 0 else None
    risk_ratio = platform_price / risk_net if risk_net > 0 else None
    t7_ratio = platform_price / float(p25) if p25 else None

    steam_inventory_change = _change(grid, "steam_sell_num")
    buff_inventory_change = _change(grid, "buff_sell_num")
    stock_flow_now = _stock_flow(grid[-1]) if grid else None
    stock_flow_change = None
    if grid and stock_flow_now is not None:
        last_time = _parse_time(grid[-1]["_grid_at"])
        cutoff = last_time - timedelta(days=7)
        prior = min(
            grid, key=lambda row: abs(
                (_parse_time(row["_grid_at"]) - cutoff).total_seconds()
            )
        )
        prior_flow = _stock_flow(prior)
        if (
            prior_flow is not None
            and abs((_parse_time(prior["_grid_at"]) - cutoff).total_seconds())
            <= GRID_HOURS * 2 * 3600
        ):
            stock_flow_change = stock_flow_now / prior_flow - 1

    score = 0
    scored_reasons: list[tuple[int, str]] = []
    if forecast_change <= -0.05:
        score += 2
        scored_reasons.append((2, f"预计 7 日 Steam 到手下跌 {abs(forecast_change):.1%}"))
    elif forecast_change <= -0.02:
        score += 1
        scored_reasons.append((1, f"预计 7 日 Steam 到手下跌 {abs(forecast_change):.1%}"))
    if steam_inventory_change is not None and steam_inventory_change >= 0.05:
        score += 1
        scored_reasons.append((1, f"Steam 在售 7 日增加 {steam_inventory_change:.1%}"))
    if buff_inventory_change is not None and buff_inventory_change >= 0.05:
        score += 1
        scored_reasons.append((1, f"BUFF 在售 7 日增加 {buff_inventory_change:.1%}"))
    if stock_flow_change is not None and stock_flow_change >= 0.10:
        score += 1
        scored_reasons.append((1, f"库存消化天数 7 日恶化 {stock_flow_change:.1%}"))
    if (
        risk_ratio is not None and t7_ratio is not None
        and risk_ratio - t7_ratio >= 0.02
    ):
        score += 1
        scored_reasons.append(
            (1, f"风险比例较历史 P25 高 {(risk_ratio - t7_ratio):.1%}")
        )

    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    if not scored_reasons:
        scored_reasons.append((0, "未发现明显价格或库存压力"))
    reasons = [
        text for _, text in sorted(scored_reasons, key=lambda pair: -pair[0])[:3]
    ]
    missing_secondary = any(
        value is None
        for value in (
            steam_inventory_change, buff_inventory_change,
            stock_flow_now, stock_flow_change,
        )
    )
    confidence = (
        "normal"
        if actual_days >= FORECAST_WINDOW_DAYS and not missing_secondary
        else "low"
    )
    return {
        **base,
        "status": "ready",
        "level": level,
        "confidence": confidence,
        "actual_window_days": actual_days,
        "coverage": round(coverage, 4),
        "score": score,
        "forecast_steam_net_t7": round(forecast_net, 4),
        "forecast_change_pct": round(forecast_change * 100, 2),
        "forecast_ratio": round(forecast_ratio, 4) if forecast_ratio is not None else None,
        "risk_steam_net_t7": round(risk_net, 4),
        "risk_ratio": round(risk_ratio, 4) if risk_ratio is not None else None,
        "steam_sell_num_change_7d_pct": (
            round(steam_inventory_change * 100, 2)
            if steam_inventory_change is not None else None
        ),
        "buff_sell_num_change_7d_pct": (
            round(buff_inventory_change * 100, 2)
            if buff_inventory_change is not None else None
        ),
        "stock_to_flow_days": (
            round(stock_flow_now, 2) if stock_flow_now is not None else None
        ),
        "stock_to_flow_change_7d_pct": (
            round(stock_flow_change * 100, 2)
            if stock_flow_change is not None else None
        ),
        "reasons": reasons,
    }
