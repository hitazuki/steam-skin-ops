import math
import unittest
from datetime import datetime, timedelta, timezone

from steam_skin_ops.monitor.market import MarketSnapshot
from steam_skin_ops.monitor.risk import (
    assess_risk,
    canonical_two_hour_rows,
    forecast_and_risk,
    forecast_seven_days,
)


NOW = datetime(2026, 7, 26, 22, tzinfo=timezone.utc)


def make_snapshot(
    when: datetime,
    steam_price: float,
    *,
    buff_price: float | None = 7.0,
    buff_count: int | None = 100,
    steam_count: int | None = 100,
    volume: int | None = 100,
    kind: str = "current",
) -> MarketSnapshot:
    return MarketSnapshot(
        item_key="smis:61753", smis_id=61753, appid=730,
        name="Test Item", name_zh="测试饰品",
        observed_at=when, source_updated_at=when,
        buff_sell_price=buff_price, buff_sell_num=buff_count,
        uuyp_sell_price=0, uuyp_sell_num=0,
        c5_sell_price=0, c5_sell_num=0,
        igxe_sell_price=0, igxe_sell_num=0,
        eco_sell_price=0, eco_sell_num=0,
        steam_sell_price=steam_price, steam_sell_num=steam_count,
        steam_transaction_quantity=volume, kind=kind,
    )


def as_row(snapshot: MarketSnapshot) -> dict:
    return {
        **snapshot.__dict__,
        "observed_at": snapshot.observed_at.isoformat(),
        "source_updated_at": snapshot.source_updated_at.isoformat(),
    }


def series_rows(
    prices: list[float], *, volumes: list[int] | None = None,
    steam_counts: list[int] | None = None,
    buff_counts: list[int] | None = None,
) -> list[dict]:
    rows = []
    days = len(prices)
    for day_index, price in enumerate(prices):
        day = NOW - timedelta(days=days - day_index - 1)
        for bucket in range(12):
            when = day.replace(hour=bucket * 2)
            rows.append(as_row(make_snapshot(
                when, price,
                volume=(volumes or [100] * days)[day_index],
                steam_count=(steam_counts or [100] * days)[day_index],
                buff_count=(buff_counts or [100] * days)[day_index],
            )))
    return rows


class ForecastSelectionTestCase(unittest.TestCase):
    def test_flat_market_uses_level_mode_and_reports_balance_ratio(self):
        prices = [10.0] * 30
        rows = series_rows(prices)
        current = make_snapshot(NOW, 10.0, buff_price=7.0)

        forecast, _, _ = forecast_seven_days(rows, current, now=NOW)

        self.assertEqual(forecast["status"], "ready")
        self.assertIn(forecast["mode"], {"persistence", "recent_level"})
        self.assertAlmostEqual(
            forecast["forecast_balance_ratio"],
            7.0 / forecast["predicted_steam_net"], places=4,
        )

    def test_linear_data_selects_linear_fit(self):
        prices = [20.0 + index * 0.25 for index in range(30)]
        rows = series_rows(prices)
        current = make_snapshot(NOW, prices[-1])

        forecast, _, _ = forecast_seven_days(rows, current, now=NOW)

        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(forecast["mode"], "theil_sen_linear")
        self.assertGreater(forecast["predicted_steam_net"], current.steam_net)

    def test_exponential_data_selects_log_fit(self):
        prices = [20.0 * math.exp(-0.012 * index) for index in range(30)]
        rows = series_rows(prices)
        current = make_snapshot(NOW, prices[-1])

        forecast, _, _ = forecast_seven_days(rows, current, now=NOW)

        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(forecast["mode"], "theil_sen_log")
        self.assertLess(forecast["predicted_steam_net"], current.steam_net)

    def test_structural_drop_rejects_old_trend(self):
        prices = [20.0 + index * 0.2 for index in range(27)] + [13.0, 13.1, 13.0]
        rows = series_rows(prices)
        current = make_snapshot(NOW, prices[-1])

        forecast, _, _ = forecast_seven_days(rows, current, now=NOW)

        self.assertEqual(forecast["status"], "ready")
        self.assertIn(forecast["mode"], {"persistence", "recent_level"})

    def test_fourteen_days_is_low_confidence_and_less_is_unavailable(self):
        low_rows = series_rows([10.0 + index * 0.05 for index in range(14)])
        insufficient_rows = series_rows([10.0] * 13)
        current = make_snapshot(NOW, 10.65)

        low, _, _ = forecast_seven_days(low_rows, current, now=NOW)
        unavailable, _, _ = forecast_seven_days(
            insufficient_rows, current, now=NOW
        )

        self.assertEqual(low["status"], "ready")
        self.assertEqual(low["confidence"], "low")
        self.assertEqual(unavailable["status"], "insufficient_history")

    def test_missing_platform_keeps_price_forecast_without_ratio(self):
        rows = series_rows([10.0] * 30)
        current = make_snapshot(NOW, 10.0, buff_price=None)

        forecast, _, _ = forecast_seven_days(rows, current, now=NOW)

        self.assertEqual(forecast["status"], "ready")
        self.assertIsNone(forecast["forecast_balance_ratio"])

    def test_history_only_fills_grid_not_current_observation(self):
        history = make_snapshot(NOW - timedelta(minutes=20), 12.0, kind="history")
        current = make_snapshot(NOW - timedelta(minutes=10), 10.0)

        rows = canonical_two_hour_rows(
            [as_row(history), as_row(current)], now=NOW
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "current")
        self.assertEqual(rows[0]["steam_sell_price"], 10.0)


class RiskDimensionTestCase(unittest.TestCase):
    def _assessment(
        self, prices: list[float], *, volumes: list[int] | None = None,
        steam_counts: list[int] | None = None,
        buff_counts: list[int] | None = None,
        forecast_change: float = 0.0,
    ) -> dict:
        rows = series_rows(
            prices, volumes=volumes,
            steam_counts=steam_counts, buff_counts=buff_counts,
        )
        current = make_snapshot(
            NOW, prices[-1], volume=(volumes or [100])[-1],
            steam_count=(steam_counts or [100])[-1],
            buff_count=(buff_counts or [100])[-1],
        )
        grid = canonical_two_hour_rows(rows, now=NOW)
        ready, _, daily = forecast_seven_days(rows, current, now=NOW)
        ready.update({
            "status": "ready",
            "predicted_steam_net": current.steam_net * (1 + forecast_change),
            "change_pct": forecast_change * 100,
        })
        return assess_risk(
            grid, daily, current,
            {"t7_steam_net_p25": current.steam_net}, ready,
        )

    def test_high_volatility_is_separate_dimension(self):
        prices = [10.0 * (1.08 if index % 2 else 0.92) for index in range(30)]

        risk = self._assessment(prices)

        self.assertEqual(risk["dimensions"]["volatility"]["level"], "high")
        self.assertEqual(risk["overall_level"], "high")

    def test_medium_volatility_boundary_is_reported(self):
        prices = [10.0]
        for index in range(29):
            prices.append(prices[-1] * math.exp(0.014 if index % 2 else -0.014))

        risk = self._assessment(prices)

        self.assertEqual(risk["dimensions"]["volatility"]["level"], "medium")

    def test_recent_variance_acceleration_upgrades_level(self):
        prices = [10.0] * 16
        for change in [1.01, 0.99, 1.01, 0.99, 1.02, 0.98, 1.02, 0.98,
                       1.03, 0.97, 1.03, 0.97, 1.03, 0.97]:
            prices.append(prices[-1] * change)

        risk = self._assessment(prices)
        dimension = risk["dimensions"]["volatility"]

        self.assertIn(dimension["level"], {"medium", "high"})
        self.assertGreaterEqual(
            dimension["metrics"]["variance_ratio_7d_vs_prior7d"], 1.5
        )

    def test_inventory_pressure_uses_three_indicators(self):
        steam_counts = [100] * 23 + [100, 103, 106, 109, 112, 115, 120]
        buff_counts = [100] * 23 + [100, 102, 104, 106, 108, 110, 112]

        risk = self._assessment(
            [10.0] * 30, steam_counts=steam_counts, buff_counts=buff_counts
        )

        self.assertEqual(risk["dimensions"]["inventory"]["level"], "high")

    def test_volume_decline_and_zero_are_high_risk(self):
        declining = [100] * 16 + [100] * 7 + [50] * 7
        zero = [100] * 23 + [0] * 7

        declining_risk = self._assessment([10.0] * 30, volumes=declining)
        zero_risk = self._assessment([10.0] * 30, volumes=zero)

        self.assertEqual(declining_risk["dimensions"]["volume"]["level"], "high")
        self.assertEqual(zero_risk["dimensions"]["volume"]["level"], "high")

    def test_missing_volume_is_unavailable_not_high(self):
        risk = self._assessment([10.0] * 30, volumes=[None] * 30)

        self.assertEqual(risk["dimensions"]["volume"]["status"], "unavailable")
        self.assertEqual(risk["dimensions"]["volume"]["level"], "unknown")

    def test_falling_price_with_rising_volume_confirms_sell_pressure(self):
        volumes = [100] * 23 + [130] * 7

        risk = self._assessment(
            [10.0] * 30, volumes=volumes, forecast_change=-0.06
        )

        self.assertEqual(risk["dimensions"]["price"]["level"], "high")
        self.assertEqual(risk["dimensions"]["volume"]["level"], "high")

    def test_forecast_and_risk_are_separate_objects(self):
        rows = series_rows([10.0] * 30)
        current = make_snapshot(NOW, 10.0)

        forecast, risk = forecast_and_risk(
            rows, current, {"t7_steam_net_p25": current.steam_net}, now=NOW
        )

        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(risk["status"], "ready")
        self.assertIn("dimensions", risk)
        self.assertNotIn("overall_level", forecast)

    def test_missing_steam_price_does_not_create_invalid_risk_floor(self):
        rows = series_rows([10.0] * 30)
        current = make_snapshot(NOW, 0.0)

        forecast, risk = forecast_and_risk(
            rows, current, {"t7_steam_net_p25": None}, now=NOW
        )

        self.assertEqual(forecast["status"], "price_missing")
        self.assertIsNone(risk["risk_steam_net"])
        self.assertIsNone(risk["risk_balance_ratio"])

    def test_risk_ratio_uses_more_conservative_denominator(self):
        rows = series_rows([10.0] * 30)
        current = make_snapshot(NOW, 10.0, buff_price=7.0)
        forecast, grid, daily = forecast_seven_days(rows, current, now=NOW)
        risk = assess_risk(
            grid, daily, current,
            {"t7_steam_net_p25": current.steam_net * 0.95}, forecast,
        )

        self.assertGreaterEqual(
            risk["risk_balance_ratio"], forecast["forecast_balance_ratio"]
        )


if __name__ == "__main__":
    unittest.main()
