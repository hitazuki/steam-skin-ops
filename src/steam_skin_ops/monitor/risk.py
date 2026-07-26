from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median, stdev, variance
from typing import Any

from .market import MarketSnapshot, steam_net_amount


FORECAST_DAYS = 7
FORECAST_WINDOW_DAYS = 21
MIN_FORECAST_DAYS = 14
ANALYSIS_DAYS = 30
GRID_HOURS = 2
MIN_COVERAGE = 0.8
MIN_VALIDATION_PAIRS = 12
MIN_MODEL_IMPROVEMENT = 0.05

LEVEL_ORDER = {"unknown": -1, "low": 0, "medium": 1, "high": 2}
MODE_LABELS = {
    "persistence": "当前价持平",
    "recent_level": "近期水平",
    "theil_sen_linear": "稳健线性趋势",
    "theil_sen_log": "稳健对数趋势",
}


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
    """Merge history and successful observations on a fixed two-hour grid."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current_time - timedelta(days=ANALYSIS_DAYS)
    buckets: dict[datetime, tuple[int, datetime, dict[str, Any]]] = {}
    for row in rows:
        kind = str(row.get("kind") or "history")
        event_time = _parse_time(
            row["observed_at"] if kind == "current" else row["source_updated_at"]
        )
        if event_time < cutoff or event_time > current_time + timedelta(minutes=5):
            continue
        bucket = _grid_time(event_time)
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
        net = steam_net_amount(float(row.get("steam_sell_price") or 0))
        if net <= 0:
            continue
        grid_at = _parse_time(row["_grid_at"])
        day = grid_at.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day[day].append((grid_at, net))

    all_values = sorted(point for points in by_day.values() for point in points)
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


def _daily_field_points(
    rows: list[dict[str, Any]], field: str,
) -> list[tuple[datetime, float]]:
    grouped: dict[datetime, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        day = _parse_time(row["_grid_at"]).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        grouped[day].append(float(value))
    return [(day, median(values)) for day, values in sorted(grouped.items())]


def _theil_sen_fit(
    points: list[tuple[datetime, float]], *, logarithmic: bool,
) -> tuple[float, float, datetime, float]:
    origin = points[0][0]
    xy = []
    for timestamp, value in points:
        if value <= 0:
            continue
        y = math.log(value) if logarithmic else value
        xy.append(((timestamp - origin).total_seconds() / 86400, y))
    slopes = [
        (y2 - y1) / (x2 - x1)
        for index, (x1, y1) in enumerate(xy)
        for x2, y2 in xy[index + 1:]
        if x2 > x1
    ]
    slope = median(slopes) if slopes else 0.0
    intercept = median([y - slope * x for x, y in xy])
    residual_mad = median([abs(y - (intercept + slope * x)) for x, y in xy])
    return slope, intercept, origin, residual_mad


def _fit_predict(
    points: list[tuple[datetime, float]], target: datetime, *, logarithmic: bool,
) -> tuple[float, float, float]:
    slope, intercept, origin, residual_mad = _theil_sen_fit(
        points, logarithmic=logarithmic
    )
    x = (target - origin).total_seconds() / 86400
    fitted = intercept + slope * x
    predicted = math.exp(fitted) if logarithmic else fitted
    if logarithmic:
        relative_mad = math.exp(residual_mad) - 1
    else:
        base = max(abs(predicted), 1e-9)
        relative_mad = residual_mad / base
    return predicted, slope, relative_mad


def _kendall_tau(points: list[tuple[datetime, float]]) -> float:
    concordance = 0
    pairs = 0
    for index, (_, first) in enumerate(points):
        for _, second in points[index + 1:]:
            pairs += 1
            concordance += (second > first) - (second < first)
    return concordance / pairs if pairs else 0.0


def _candidate_prediction(
    points: list[tuple[datetime, float]], mode: str, window: int,
    target: datetime,
) -> float | None:
    if not points:
        return None
    if mode == "persistence":
        return points[-1][1]
    if mode == "recent_level":
        return median(value for _, value in points[-3:])
    if len(points) < window:
        return None
    selected = points[-window:]
    logarithmic = mode == "theil_sen_log"
    predicted, _, _ = _fit_predict(selected, target, logarithmic=logarithmic)
    return predicted if predicted > 0 and math.isfinite(predicted) else None


def _validation_score(
    points: list[tuple[datetime, float]], mode: str, window: int,
    common_required: int,
) -> tuple[float | None, int, float | None]:
    required = window if mode.startswith("theil_sen") else MIN_FORECAST_DAYS
    weighted_error = 0.0
    total_weight = 0
    pairs = 0
    persistence_error = 0.0
    for origin_index in range(max(required, common_required) - 1, len(points) - 1):
        train = points[:origin_index + 1]
        for horizon in range(1, min(FORECAST_DAYS, len(points) - origin_index - 1) + 1):
            actual_time, actual = points[origin_index + horizon]
            predicted = _candidate_prediction(train, mode, window, actual_time)
            baseline = train[-1][1]
            if predicted is None or actual <= 0:
                continue
            weight = horizon
            weighted_error += weight * abs(predicted / actual - 1)
            persistence_error += weight * abs(baseline / actual - 1)
            total_weight += weight
            pairs += 1
    if not total_weight:
        return None, 0, None
    return (
        weighted_error / total_weight,
        pairs,
        persistence_error / total_weight,
    )


def _trend_diagnostics(
    points: list[tuple[datetime, float]], mode: str, window: int,
    current_net: float,
) -> tuple[bool, dict[str, float | bool], list[str]]:
    selected = points[-window:]
    logarithmic = mode == "theil_sen_log"
    target = points[-1][0] + timedelta(days=FORECAST_DAYS)
    predicted, slope, residual_mad = _fit_predict(
        selected, target, logarithmic=logarithmic
    )
    fitted_now, _, _ = _fit_predict(
        selected, points[-1][0], logarithmic=logarithmic
    )
    tau = _kendall_tau(selected)
    recent_level = median(value for _, value in points[-3:])
    structural_limit = max(0.03, 2 * residual_mad)
    structural_shift = abs(recent_level / fitted_now - 1) > structural_limit
    forecast_change = predicted / current_net - 1 if current_net > 0 else 0.0
    reasons: list[str] = []
    valid = True
    if abs(tau) < 0.30:
        valid = False
        reasons.append("趋势一致性不足")
    if abs(forecast_change) < 0.01:
        valid = False
        reasons.append("七日趋势变化不足 1%")
    if structural_shift:
        valid = False
        reasons.append("最近价格偏离旧拟合趋势")

    if len(points) >= FORECAST_WINDOW_DAYS:
        slope14 = _theil_sen_fit(
            points[-14:], logarithmic=logarithmic
        )[0]
        slope21 = _theil_sen_fit(
            points[-21:], logarithmic=logarithmic
        )[0]
        direction_consistent = slope14 == 0 or slope21 == 0 or slope14 * slope21 > 0
        if not direction_consistent:
            valid = False
            reasons.append("14 日与 21 日趋势方向冲突")
    else:
        direction_consistent = False

    return valid, {
        "slope": slope,
        "kendall_tau": tau,
        "residual_mad": residual_mad,
        "structural_shift": structural_shift,
        "direction_consistent": direction_consistent,
    }, reasons


def _coverage(
    grid: list[dict[str, Any]], daily: list[tuple[datetime, float]],
) -> tuple[float, int]:
    used = daily[-FORECAST_WINDOW_DAYS:]
    if not used:
        return 0.0, 0
    first_day, last_day = used[0][0], used[-1][0]
    span_days = max(1, int((last_day - first_day).total_seconds() / 86400) + 1)
    start = last_day - timedelta(days=span_days - 1)
    relevant = [row for row in grid if _parse_time(row["_grid_at"]) >= start]
    expected = max(1, span_days * (24 // GRID_HOURS))
    return min(1.0, len(relevant) / expected), span_days


def forecast_seven_days(
    rows: list[dict[str, Any]], snapshot: MarketSnapshot, *,
    stale: bool = False, now: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[datetime, float]]]:
    base = {
        "status": "unavailable",
        "horizon_days": FORECAST_DAYS,
        "mode": None,
        "mode_label": None,
        "window_days": 0,
        "actual_window_days": 0,
        "coverage": 0.0,
        "confidence": "none",
        "current_steam_net": round(snapshot.steam_net, 4),
        "predicted_steam_net": None,
        "change_pct": None,
        "lowest_platform": None,
        "forecast_balance_ratio": None,
        "validation_error_pct": None,
        "validation_sample_count": 0,
        "diagnostics": {},
        "reasons": [],
    }
    lowest = snapshot.lowest_platform
    if lowest:
        base["lowest_platform"] = {
            "name": lowest[0], "sell_price": lowest[1], "sell_num": lowest[2]
        }
    if stale:
        return {**base, "status": "stale", "reasons": ["当前行情为过期快照"]}, [], []
    if snapshot.steam_net <= 0:
        return {**base, "status": "price_missing", "reasons": ["当前 Steam 价格不足"]}, [], []

    grid = canonical_two_hour_rows(rows, now=now or snapshot.observed_at)
    daily = _daily_price_points(grid)
    coverage, span_days = _coverage(grid, daily)
    base.update({"actual_window_days": span_days, "coverage": round(coverage, 4)})
    if span_days < MIN_FORECAST_DAYS or len(daily) < MIN_FORECAST_DAYS:
        return {
            **base, "status": "insufficient_history",
            "reasons": [f"历史不足 {MIN_FORECAST_DAYS} 天"],
        }, grid, daily
    if coverage < MIN_COVERAGE:
        return {
            **base, "status": "insufficient_coverage",
            "reasons": ["两小时行情覆盖不足 80%"],
        }, grid, daily

    candidates: list[tuple[str, int]] = [
        ("persistence", 0), ("recent_level", 3),
        ("theil_sen_linear", 14), ("theil_sen_log", 14),
    ]
    if len(daily) >= FORECAST_WINDOW_DAYS:
        candidates.extend([
            ("theil_sen_linear", 21), ("theil_sen_log", 21),
        ])
    common_validation_window = (
        FORECAST_WINDOW_DAYS
        if len(daily) >= FORECAST_WINDOW_DAYS else MIN_FORECAST_DAYS
    )

    evaluated: list[dict[str, Any]] = []
    for mode, window in candidates:
        target = daily[-1][0] + timedelta(days=FORECAST_DAYS)
        predicted = _candidate_prediction(daily, mode, window, target)
        if predicted is None:
            continue
        error, pairs, baseline_error = _validation_score(
            daily, mode, window, common_validation_window
        )
        valid = True
        diagnostics: dict[str, Any] = {}
        rejection_reasons: list[str] = []
        if mode.startswith("theil_sen"):
            valid, diagnostics, rejection_reasons = _trend_diagnostics(
                daily, mode, window, snapshot.steam_net
            )
        improvement = None
        if error is not None and baseline_error and baseline_error > 0:
            improvement = 1 - error / baseline_error
        if mode != "persistence" and pairs >= MIN_VALIDATION_PAIRS:
            if improvement is None or improvement < MIN_MODEL_IMPROVEMENT:
                valid = False
                rejection_reasons.append("相对持平模式的验证改善不足 5%")
        evaluated.append({
            "mode": mode, "window": window, "predicted": predicted,
            "error": error, "pairs": pairs, "improvement": improvement,
            "valid": valid, "diagnostics": diagnostics,
            "rejection_reasons": rejection_reasons,
        })

    persistence = next(row for row in evaluated if row["mode"] == "persistence")
    eligible = [row for row in evaluated if row["valid"]]
    validated = [
        row for row in eligible
        if row["pairs"] >= MIN_VALIDATION_PAIRS and row["error"] is not None
    ]
    non_baseline = [row for row in validated if row["mode"] != "persistence"]
    if non_baseline:
        selected = min(non_baseline, key=lambda row: row["error"])
    else:
        diagnostic_trends = [
            row for row in eligible
            if row["mode"].startswith("theil_sen")
        ]
        if diagnostic_trends and not validated:
            selected = min(
                diagnostic_trends,
                key=lambda row: float(row["diagnostics"].get("residual_mad", 1e9)),
            )
        else:
            recent = next(
                (row for row in eligible if row["mode"] == "recent_level"), None
            )
            selected = recent if recent and recent in validated else persistence

    predicted = float(selected["predicted"])
    change = predicted / snapshot.steam_net - 1
    ratio = float(lowest[1]) / predicted if lowest and predicted > 0 else None
    normal_confidence = (
        span_days >= FORECAST_WINDOW_DAYS
        and selected["pairs"] >= MIN_VALIDATION_PAIRS
    )
    reasons = [
        f"{selected['window']} 日{MODE_LABELS[selected['mode']]}"
        if selected["window"] else MODE_LABELS[selected["mode"]]
    ]
    if selected["improvement"] is not None and selected["mode"] != "persistence":
        reasons.append(f"验证误差较持平模式改善 {selected['improvement']:.1%}")
    rejected = [
        reason
        for row in evaluated if not row["valid"]
        for reason in row["rejection_reasons"]
    ]
    if selected["mode"] in {"persistence", "recent_level"} and rejected:
        reasons.append(rejected[0])

    return {
        **base,
        "status": "ready",
        "mode": selected["mode"],
        "mode_label": MODE_LABELS[selected["mode"]],
        "window_days": selected["window"],
        "confidence": "normal" if normal_confidence else "low",
        "predicted_steam_net": round(predicted, 4),
        "change_pct": round(change * 100, 2),
        "forecast_balance_ratio": round(ratio, 4) if ratio is not None else None,
        "validation_error_pct": (
            round(float(selected["error"]) * 100, 2)
            if selected["error"] is not None else None
        ),
        "validation_sample_count": selected["pairs"],
        "diagnostics": selected["diagnostics"],
        "reasons": reasons[:3],
    }, grid, daily


def _change(rows: list[dict[str, Any]], field: str, days: int = 7) -> float | None:
    valid = [
        (_parse_time(row["_grid_at"]), float(row[field]))
        for row in rows if row.get(field) is not None and float(row[field]) > 0
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
    inventory = row.get("steam_sell_num")
    volume = row.get("steam_transaction_quantity")
    if inventory is None or volume is None:
        return None
    inventory_value, volume_value = float(inventory), float(volume)
    return inventory_value / volume_value if inventory_value > 0 and volume_value > 0 else None


def _upgrade(level: str) -> str:
    return {"low": "medium", "medium": "high", "high": "high"}.get(level, level)


def _dimension(
    status: str, level: str, metrics: dict[str, Any], reasons: list[str],
) -> dict[str, Any]:
    return {"status": status, "level": level, "metrics": metrics, "reasons": reasons}


def _volume_metrics(
    grid: list[dict[str, Any]], last_day: datetime,
) -> tuple[float | None, float | None, int, int]:
    points = _daily_field_points(grid, "steam_transaction_quantity")
    recent_start = last_day - timedelta(days=6)
    prior_start = last_day - timedelta(days=13)
    recent = [value for day, value in points if recent_start <= day <= last_day]
    prior = [value for day, value in points if prior_start <= day < recent_start]
    if len(recent) < 5 or len(prior) < 5:
        return None, None, len(recent), len(prior)
    recent_median, prior_median = median(recent), median(prior)
    if prior_median > 0:
        change = recent_median / prior_median - 1
    elif recent_median > 0:
        change = None
    else:
        change = 0.0
    return recent_median, change, len(recent), len(prior)


def assess_risk(
    grid: list[dict[str, Any]], daily: list[tuple[datetime, float]],
    snapshot: MarketSnapshot, t7_stats: dict[str, Any], forecast: dict[str, Any],
) -> dict[str, Any]:
    lowest = snapshot.lowest_platform
    p25 = t7_stats.get("t7_steam_net_p25")
    predicted = (
        float(forecast["predicted_steam_net"])
        if forecast.get("status") == "ready" else None
    )
    candidates = [snapshot.steam_net]
    if p25 is not None and float(p25) > 0:
        candidates.append(float(p25))
    if predicted is not None and predicted > 0:
        candidates.append(predicted)
    valid_candidates = [value for value in candidates if value > 0]
    risk_net = min(valid_candidates) if valid_candidates else None
    platform_price = float(lowest[1]) if lowest else None
    risk_ratio = (
        platform_price / risk_net
        if platform_price is not None and risk_net is not None else None
    )
    t7_ratio = (
        platform_price / float(p25)
        if platform_price is not None and p25 is not None and float(p25) > 0 else None
    )
    ratio_delta = risk_ratio - t7_ratio if risk_ratio is not None and t7_ratio is not None else None

    last_day = daily[-1][0] if daily else snapshot.observed_at.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    volume_median, volume_change, recent_volume_days, prior_volume_days = _volume_metrics(
        grid, last_day
    )
    forecast_change = (
        float(forecast["change_pct"]) / 100
        if forecast.get("change_pct") is not None else None
    )

    price_reasons: list[str] = []
    if forecast_change is None and ratio_delta is None:
        price = _dimension("unavailable", "unknown", {}, ["预测与比例数据不足"])
    else:
        level = "low"
        if forecast_change is not None:
            if forecast_change <= -0.05:
                level = "high"
                price_reasons.append(f"预计七日下跌 {abs(forecast_change):.1%}")
            elif forecast_change <= -0.02:
                level = "medium"
                price_reasons.append(f"预计七日下跌 {abs(forecast_change):.1%}")
        if ratio_delta is not None and ratio_delta >= 0.02:
            if LEVEL_ORDER[level] < LEVEL_ORDER["medium"]:
                level = "medium"
            price_reasons.append(f"风险比例较 P25 比例高 {ratio_delta:.1%}")
        if forecast_change is not None and volume_change is not None:
            if forecast_change <= -0.02 and volume_change >= 0.10:
                level = _upgrade(level)
                price_reasons.append(f"下跌同时成交量增加 {volume_change:.1%}")
        if not price_reasons:
            price_reasons.append("未发现明显价格下行压力")
        price = _dimension(
            "ready" if forecast_change is not None else "partial", level,
            {
                "forecast_change_pct": (
                    round(forecast_change * 100, 2) if forecast_change is not None else None
                ),
                "risk_vs_t7_ratio_delta_pct": (
                    round(ratio_delta * 100, 2) if ratio_delta is not None else None
                ),
                "volume_confirmation": bool(
                    forecast_change is not None and volume_change is not None
                    and forecast_change <= -0.02 and volume_change >= 0.10
                ),
            }, price_reasons,
        )

    returns = [
        math.log(second / first)
        for first, second in zip(
            [value for _, value in daily], [value for _, value in daily][1:]
        ) if first > 0 and second > 0
    ]
    recent_returns = returns[-7:]
    prior_returns = returns[-14:-7]
    if len(recent_returns) < 4:
        volatility = _dimension("unavailable", "unknown", {}, ["日收益数据不足"])
    else:
        recent_variance = variance(recent_returns) if len(recent_returns) >= 2 else 0.0
        sigma7 = stdev(recent_returns) * math.sqrt(7) if len(recent_returns) >= 2 else 0.0
        prior_variance = variance(prior_returns) if len(prior_returns) >= 2 else None
        variance_ratio = None
        if prior_variance is not None:
            variance_ratio = (
                recent_variance / prior_variance
                if prior_variance > 1e-12 else (math.inf if recent_variance > 1e-12 else 1.0)
            )
        level = "high" if sigma7 >= 0.07 else "medium" if sigma7 >= 0.03 else "low"
        volatility_reasons = [f"七日波动率 {sigma7:.1%}"]
        if variance_ratio is not None and variance_ratio >= 1.5 and sigma7 >= 0.02:
            level = _upgrade(level)
            volatility_reasons.append(f"近期方差放大 {variance_ratio:.2f} 倍")
        volatility = _dimension(
            "ready" if prior_variance is not None else "partial", level,
            {
                "daily_log_return_variance": round(recent_variance, 8),
                "realized_volatility_7d_pct": round(sigma7 * 100, 2),
                "variance_ratio_7d_vs_prior7d": (
                    None if variance_ratio is None or math.isinf(variance_ratio)
                    else round(variance_ratio, 4)
                ),
                "variance_ratio_infinite": bool(
                    variance_ratio is not None and math.isinf(variance_ratio)
                ),
            }, volatility_reasons,
        )

    steam_inventory_change = _change(grid, "steam_sell_num")
    buff_inventory_change = _change(grid, "buff_sell_num")
    stock_flow_now = _stock_flow(grid[-1]) if grid else None
    stock_flow_change = None
    if grid and stock_flow_now is not None:
        cutoff = _parse_time(grid[-1]["_grid_at"]) - timedelta(days=7)
        prior = min(
            grid,
            key=lambda row: abs((_parse_time(row["_grid_at"]) - cutoff).total_seconds()),
        )
        prior_flow = _stock_flow(prior)
        if prior_flow is not None and abs(
            (_parse_time(prior["_grid_at"]) - cutoff).total_seconds()
        ) <= GRID_HOURS * 2 * 3600:
            stock_flow_change = stock_flow_now / prior_flow - 1
    inventory_values = [steam_inventory_change, buff_inventory_change, stock_flow_change]
    if all(value is None for value in inventory_values):
        inventory = _dimension("unavailable", "unknown", {}, ["库存历史不足"])
    else:
        pressure = sum([
            steam_inventory_change is not None and steam_inventory_change >= 0.05,
            buff_inventory_change is not None and buff_inventory_change >= 0.05,
            stock_flow_change is not None and stock_flow_change >= 0.10,
        ])
        severe = any([
            steam_inventory_change is not None and steam_inventory_change >= 0.15,
            buff_inventory_change is not None and buff_inventory_change >= 0.15,
            stock_flow_change is not None and stock_flow_change >= 0.25,
        ])
        level = "high" if severe or pressure >= 2 else "medium" if pressure == 1 else "low"
        inventory_reasons = []
        if steam_inventory_change is not None and steam_inventory_change >= 0.05:
            inventory_reasons.append(f"Steam 在售增加 {steam_inventory_change:.1%}")
        if buff_inventory_change is not None and buff_inventory_change >= 0.05:
            inventory_reasons.append(f"BUFF 在售增加 {buff_inventory_change:.1%}")
        if stock_flow_change is not None and stock_flow_change >= 0.10:
            inventory_reasons.append(f"库存消化天数恶化 {stock_flow_change:.1%}")
        if not inventory_reasons:
            inventory_reasons.append("未发现明显库存压力")
        inventory = _dimension(
            "ready" if all(value is not None for value in inventory_values) else "partial",
            level,
            {
                "steam_sell_num_change_7d_pct": (
                    round(steam_inventory_change * 100, 2)
                    if steam_inventory_change is not None else None
                ),
                "buff_sell_num_change_7d_pct": (
                    round(buff_inventory_change * 100, 2)
                    if buff_inventory_change is not None else None
                ),
                "stock_to_flow_days": round(stock_flow_now, 2) if stock_flow_now is not None else None,
                "stock_to_flow_change_7d_pct": (
                    round(stock_flow_change * 100, 2) if stock_flow_change is not None else None
                ),
            }, inventory_reasons,
        )

    if volume_median is None:
        volume = _dimension(
            "unavailable", "unknown",
            {"recent_days": recent_volume_days, "prior_days": prior_volume_days},
            ["两个七日区间的成交量有效日不足 5 天"],
        )
    else:
        level = "low"
        volume_reasons = []
        if volume_median == 0:
            level = "high"
            volume_reasons.append("最近七日成交量为零")
        elif volume_change is not None and volume_change <= -0.25:
            level = "high"
            volume_reasons.append(f"成交量下降 {abs(volume_change):.1%}")
        elif volume_change is not None and volume_change <= -0.10:
            level = "medium"
            volume_reasons.append(f"成交量下降 {abs(volume_change):.1%}")
        if forecast_change is not None and volume_change is not None:
            if forecast_change <= -0.05 and volume_change >= 0.20:
                level = "high"
                volume_reasons.append("显著放量下跌")
            elif forecast_change <= -0.02 and volume_change >= 0.10:
                if LEVEL_ORDER[level] < LEVEL_ORDER["medium"]:
                    level = "medium"
                volume_reasons.append("放量下跌")
        if not volume_reasons:
            volume_reasons.append("成交量未显示明显流动性或卖压风险")
        volume = _dimension(
            "ready", level,
            {
                "median_7d": round(volume_median, 2),
                "change_vs_prior7d_pct": (
                    round(volume_change * 100, 2) if volume_change is not None else None
                ),
                "recent_days": recent_volume_days,
                "prior_days": prior_volume_days,
            }, volume_reasons,
        )

    dimensions = {
        "price": price, "volatility": volatility,
        "inventory": inventory, "volume": volume,
    }
    available = [value for value in dimensions.values() if value["level"] != "unknown"]
    overall = max(available, key=lambda value: LEVEL_ORDER[value["level"]])["level"] if available else "unknown"
    overall_reasons = [
        value["reasons"][0]
        for value in dimensions.values()
        if value["level"] == overall and value["reasons"]
    ]
    if len(overall_reasons) < 3:
        overall_reasons.extend([
            value["reasons"][0]
            for value in dimensions.values()
            if value["level"] not in {overall, "unknown"} and value["reasons"]
        ])

    return {
        "status": "ready" if available else "unavailable",
        "overall_level": overall,
        "confidence": "normal" if len(available) >= 3 else "low",
        "risk_steam_net": round(risk_net, 4) if risk_net is not None else None,
        "risk_balance_ratio": round(risk_ratio, 4) if risk_ratio is not None else None,
        "dimensions": dimensions,
        "reasons": overall_reasons[:3],
    }


def forecast_and_risk(
    rows: list[dict[str, Any]], snapshot: MarketSnapshot,
    t7_stats: dict[str, Any], *, stale: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    forecast, grid, daily = forecast_seven_days(
        rows, snapshot, stale=stale, now=now
    )
    if stale:
        unavailable = _dimension(
            "unavailable", "unknown", {}, ["当前行情为过期快照"]
        )
        return forecast, {
            "status": "unavailable",
            "overall_level": "unknown",
            "confidence": "none",
            "risk_steam_net": None,
            "risk_balance_ratio": None,
            "dimensions": {
                key: dict(unavailable)
                for key in ("price", "volatility", "inventory", "volume")
            },
            "reasons": ["当前行情为过期快照"],
        }
    risk = assess_risk(grid, daily, snapshot, t7_stats, forecast)
    return forecast, risk
