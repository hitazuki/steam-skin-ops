import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from steam_skin_ops.profit.cli import build_parser, main
from steam_skin_ops.profit.clients.buff import BuffClient
from steam_skin_ops.profit.clients.c5 import C5Client
from steam_skin_ops.profit.clients.steam import SteamClient
from steam_skin_ops.profit.reporting.report import ReportGenerator


class TestCommandParser(unittest.TestCase):
    def test_command_is_required(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_legacy_options_are_removed(self):
        parser = build_parser()
        for option in ("--no-cache", "--view-csv", "--full"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["sync", option])

    def test_sync_and_refresh_are_explicit_commands(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["sync"]).command, "sync")
        self.assertEqual(parser.parse_args(["refresh"]).command, "refresh")
        self.assertEqual(parser.parse_args(["build"]).command, "build")

    def test_view_command_has_an_optional_csv_path(self):
        parser = build_parser()
        latest = parser.parse_args(["view"])
        specified = parser.parse_args(["view", "output/report.csv"])
        self.assertEqual(latest.command, "view")
        self.assertIsNone(latest.view_path)
        self.assertEqual(specified.view_path, "output/report.csv")

    def test_check_login_only_checks_selected_platform(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "profit.yaml"
            config.write_text("buff: {}\nsteam: {}\nc5: {enabled: false}\n", encoding="utf-8")
            with (
                patch.object(BuffClient, "check_login", return_value=True) as buff_check,
                patch.object(SteamClient, "check_login") as steam_check,
            ):
                main(["check-login", "--platform", "buff", "--config", str(config)])
            buff_check.assert_called_once_with()
            steam_check.assert_not_called()

    def test_failed_cookie_does_not_block_other_selected_platform(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "buff_csgo_orders.json").write_text("[]", encoding="utf-8")
            (data_dir / "steam_sales.json").write_text("[]", encoding="utf-8")
            config = root / "profit.yaml"
            config.write_text(
                "buff: {}\nsteam: {}\nc5: {enabled: false}\nsettings:\n"
                f"  data_dir: '{data_dir.as_posix()}'\n"
                f"  output_dir: '{(root / 'output').as_posix()}'\n",
                encoding="utf-8",
            )
            with (
                patch.object(BuffClient, "check_login", return_value=False),
                patch.object(BuffClient, "fetch_buy_orders") as buff_fetch,
                patch.object(SteamClient, "check_login", return_value=True),
                patch.object(SteamClient, "fetch_sell_history", return_value=[]) as steam_fetch,
            ):
                with self.assertRaises(SystemExit) as raised:
                    main([
                        "sync",
                        "--platform", "buff",
                        "--platform", "steam",
                        "--config", str(config),
                        "--no-export",
                    ])
            self.assertEqual(raised.exception.code, 1)
            self.assertFalse(any(
                call.kwargs.get("incremental") for call in buff_fetch.call_args_list
            ))
            self.assertTrue(any(
                call.kwargs.get("incremental") for call in steam_fetch.call_args_list
            ))

    def test_build_uses_cache_without_login_check_or_crawl(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "buff_csgo_orders.json").write_text("[]", encoding="utf-8")
            (data_dir / "steam_sales.json").write_text("[]", encoding="utf-8")
            config = root / "profit.yaml"
            config.write_text(
                "buff:\n"
                "  games: [csgo]\n"
                "steam: {}\n"
                "currency:\n"
                "  fallback_rates: {CNY: 1.0}\n"
                "settings:\n"
                f"  data_dir: '{data_dir.as_posix()}'\n"
                f"  output_dir: '{(root / 'output').as_posix()}'\n",
                encoding="utf-8",
            )
            with (
                patch.object(BuffClient, "check_login", side_effect=AssertionError),
                patch.object(BuffClient, "_fetch_page", side_effect=AssertionError),
                patch.object(SteamClient, "check_login", side_effect=AssertionError),
                patch.object(SteamClient, "_fetch_page", side_effect=AssertionError),
                patch.object(ReportGenerator, "print_full_report"),
            ):
                main(["build", "--config", str(config), "--no-export"])

    def test_sync_automatically_aggregates_after_fetching(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "buff_csgo_orders.json").write_text("[]", encoding="utf-8")
            (data_dir / "steam_sales.json").write_text("[]", encoding="utf-8")
            config = root / "profit.yaml"
            config.write_text(
                "buff:\n"
                "  games: [csgo]\n"
                "steam: {}\n"
                "c5: {enabled: false}\n"
                "settings:\n"
                f"  data_dir: '{data_dir.as_posix()}'\n"
                f"  output_dir: '{(root / 'output').as_posix()}'\n",
                encoding="utf-8",
            )
            with (
                patch.object(BuffClient, "fetch_buy_orders", return_value=[]) as buff_fetch,
                patch.object(SteamClient, "fetch_sell_history", return_value=[]) as steam_fetch,
                patch.object(ReportGenerator, "print_full_report") as print_report,
            ):
                main([
                    "sync", "--skip-login-check", "--no-export",
                    "--config", str(config),
                ])

            self.assertEqual(buff_fetch.call_count, 2)  # 抓取一次，再从缓存聚合一次
            self.assertEqual(steam_fetch.call_count, 2)
            print_report.assert_called_once()


class TestIncrementalFetch(unittest.TestCase):
    def test_buff_stops_at_cached_record_and_merges(self):
        client = BuffClient("session=fake")
        cached = [{"id": "old", "source": "buff"}]
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "buff.json"
            cache.write_text(json.dumps(cached), encoding="utf-8")
            client._fetch_page = Mock(return_value=(
                [{"id": "new", "source": "buff"}, {"id": "old", "source": "buff"}],
                True,
            ))

            result = client.fetch_buy_orders("csgo", cache, incremental=True)

        self.assertEqual([item["id"] for item in result], ["new", "old"])
        client._fetch_page.assert_called_once_with("csgo", 1)

    def test_steam_stops_at_cached_record_and_merges(self):
        client = SteamClient("session", "secure")
        cached = [{"id": "old", "source": "steam"}]
        with TemporaryDirectory() as directory:
            cache = Path(directory) / "steam.json"
            cache.write_text(json.dumps(cached), encoding="utf-8")
            client._fetch_page = Mock(return_value=(
                [{"id": "new", "source": "steam"}, {"id": "old", "source": "steam"}],
                100,
            ))

            result = client.fetch_sell_history(cache, incremental=True)

        self.assertEqual([item["id"] for item in result], ["new", "old"])
        client._fetch_page.assert_called_once_with(0, 500)

    def test_c5_stops_before_requesting_cached_order_detail(self):
        client = C5Client("sid=fake")
        client.account_checked = True
        cached = [{
            "id": "c5_old_asset_0_0",
            "game": "csgo",
            "order_no": "old",
            "source": "c5",
        }]
        payload = {
            "data": {
                "list": [
                    {
                        "orderId": "new",
                        "statusName": "交易成功",
                        "receiveSteamId": "76561198000000001",
                        "actualPay": "1.00",
                        "orderAssetList": [{
                            "orderAssetId": "asset",
                            "marketHashName": "New Item",
                            "name": "New Item",
                            "price": "1.00",
                            "quantity": 1,
                            "appId": 730,
                        }],
                    },
                    {"orderId": "old", "statusName": "交易成功"},
                ],
                "pages": 10,
            }
        }
        client._request_json = Mock(return_value=payload)

        with TemporaryDirectory() as directory:
            cache = Path(directory) / "c5.json"
            cache.write_text(json.dumps(cached), encoding="utf-8")
            result = client.fetch_buy_orders(
                games=["csgo"], cache_path=cache, incremental=True
            )

        self.assertEqual([item["order_no"] for item in result], ["new", "old"])
        self.assertEqual(client._request_json.call_count, 1)


if __name__ == "__main__":
    unittest.main()
