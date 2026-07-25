import math
import unittest
from datetime import datetime, timedelta, timezone

from steam_skin_ops.monitor.market import MarketSnapshot, steam_net_amount
from steam_skin_ops.monitor.risk import canonical_two_hour_rows, risk_prediction


def make_snapshot(
    when: datetime,
    steam_price: float,
    *,
    buff_price: float = 7.0,
    buff_count: int = 100,
    steam_count: int = 100,
    volume: int = 100,
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


class RiskPredictionTestCase(unittest.TestCase):
    def test_declining_price_and_growing_inventory_is_high_risk(self):
        now = datetime.now(timezone.utc).replace(
            hour=22, minute=0, second=0, microsecond=0
        )
        rows = []
        for index in range(21 * 12 + 1):
            when = now - timedelta(hours=2 * (21 * 12 - index))
            progress = index / (21 * 12)
            steam_price = 12.0 * math.exp(-0.10 * progress)
            rows.append(as_row(make_snapshot(
                when, steam_price,
                buff_count=100 + round(100 * progress),
                steam_count=100 + round(100 * progress),
                volume=100,
            )))
        current = make_snapshot(
            now, float(rows[-1]["steam_sell_price"]),
            buff_count=200, steam_count=200, volume=100,
        )
        current_net = steam_net_amount(float(current.steam_sell_price))
        result = risk_prediction(
            rows, current,
            {"t7_steam_net_p25": current_net * 1.05},
            now=now,
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["level"], "high")
        self.assertGreaterEqual(result["score"], 4)
        self.assertLess(result["forecast_steam_net_t7"], current_net)
        self.assertGreater(result["risk_ratio"], 0)
        self.assertGreaterEqual(result["steam_sell_num_change_7d_pct"], 5)
        self.assertGreaterEqual(result["buff_sell_num_change_7d_pct"], 5)

    def test_fourteen_days_is_low_confidence_and_less_is_unavailable(self):
        now = datetime.now(timezone.utc).replace(
            hour=22, minute=0, second=0, microsecond=0
        )

        def rows_for(days: int) -> list[dict]:
            return [
                as_row(make_snapshot(now - timedelta(hours=2 * index), 10.0))
                for index in range(days * 12)
            ]

        current = make_snapshot(now, 10.0)
        low = risk_prediction(
            rows_for(14), current, {"t7_steam_net_p25": 8.5}, now=now
        )
        unavailable = risk_prediction(
            rows_for(13), current, {"t7_steam_net_p25": 8.5}, now=now
        )

        self.assertEqual(low["status"], "ready")
        self.assertEqual(low["confidence"], "low")
        self.assertEqual(unavailable["status"], "insufficient_history")

    def test_history_only_fills_grid_not_current_observation(self):
        now = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
        history = make_snapshot(now - timedelta(minutes=20), 12.0, kind="history")
        current = make_snapshot(now - timedelta(minutes=10), 10.0)

        rows = canonical_two_hour_rows(
            [as_row(history), as_row(current)], now=now
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "current")
        self.assertEqual(rows[0]["steam_sell_price"], 10.0)


if __name__ == "__main__":
    unittest.main()
