import ast
import copy
import unittest
from datetime import datetime
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).parents[2] / "plugins" / "astrbot_plugin_steam_skin_ops" / "main.py"
)


class AstrBotPluginContractTest(unittest.TestCase):
    @staticmethod
    def format_quote(data: dict) -> str:
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        plugin_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SteamSkinOpsPlugin"
        )
        formatter = copy.deepcopy(next(
            node for node in plugin_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "_format_quote"
        ))
        formatter.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[formatter], type_ignores=[]))
        namespace = {"datetime": datetime}
        exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
        return namespace["_format_quote"](data)

    @staticmethod
    def quote_data() -> dict:
        return {
            "name_zh": "棱彩：末日地牢", "name": "Prismatic: Dungeon Doom",
            "smis_id": 55316, "cached": False, "stale": False,
            "ratio": 0.7122, "steam_sell_price": 304.63,
            "steam_net": 264.89, "steam_transaction_quantity": 2,
            "lowest_platform": {"name": "BUFF", "sell_price": 188.65, "sell_num": 9},
            "platforms": [
                {"name": "BUFF", "sell_price": 188.65, "sell_num": 9, "is_lowest": True},
            ],
            "t7_steam_net_p25": 270.66, "t7_sample_count": 199,
            "t7_span_days": 6.98, "t7_sufficient": True,
            "forecast": {
                "status": "ready", "predicted_steam_net": 270.66,
                "change_pct": 2.2, "forecast_balance_ratio": 0.697,
                "window_days": 21, "mode_label": "当前价持平",
                "confidence": "normal",
            },
            "risk_assessment": {
                "status": "ready", "overall_level": "high", "confidence": "normal",
                "risk_steam_net": 264.89, "risk_balance_ratio": 0.7122,
                "dimensions": {
                    key: {"status": "ready", "level": level}
                    for key, level in {
                        "price": "low", "volatility": "low",
                        "inventory": "high", "volume": "high",
                    }.items()
                },
                "reasons": ["Steam 在售增加 43.8%"],
            },
            "source_updated_at": "2026-07-26T13:18:28+08:00",
            "links": {"smis": "https://example.test/smis", "steam": "https://example.test/steam"},
        }

    def test_plugin_source_compiles_and_registers_english_commands(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        command_names = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                value = decorator.args[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    command_names.add(value.value)
        expected = {
            "skin", "search", "quote", "items", "rule", "list", "add", "set",
            "remove", "test", "status", "help",
        }
        self.assertTrue(expected.issubset(command_names))
        self.assertIn("event.unified_msg_origin", source)
        self.assertIn("filter.PermissionType.ADMIN", source)
        self.assertIn('"/v2/rules"', source)
        self.assertIn('data.get("forecast")', source)
        self.assertIn('data.get("risk_assessment")', source)
        self.assertNotIn('data.get("risk_prediction")', source)

    def test_quote_removes_legacy_t7_ratio_and_deduplicates_current_risk_ratio(self):
        content = self.format_quote(self.quote_data())

        self.assertIn("即时倒余额比例：71.22%", content)
        self.assertIn("七日历史基准：Steam 到手 P25 ¥270.66（199 点 / 6.98 天）", content)
        self.assertNotIn("T+7 保守比例", content)
        self.assertNotIn("T+7 历史", content)
        self.assertIn("预测倒余额比例：69.70%", content)
        self.assertIn("风险倒余额比例：同即时比例（当前到手价为风险底价）", content)
        self.assertEqual(content.count("71.22%"), 1)

    def test_quote_deduplicates_forecast_risk_ratio_and_marks_missing_dimensions(self):
        data = self.quote_data()
        data["risk_assessment"]["risk_steam_net"] = 270.66
        data["risk_assessment"]["risk_balance_ratio"] = 0.697
        data["risk_assessment"]["confidence"] = "low"
        data["risk_assessment"]["dimensions"]["volume"] = {
            "status": "unavailable", "level": "unknown",
        }

        content = self.format_quote(data)

        self.assertIn("风险评估：总体高（低置信度）", content)
        self.assertIn("成交量不可用", content)
        self.assertIn("风险倒余额比例：同预测比例（七日预测价为风险底价）", content)
        self.assertEqual(content.count("69.70%"), 1)

    def test_quote_keeps_history_reference_when_forecast_is_unavailable(self):
        data = self.quote_data()
        data["t7_sufficient"] = False
        data["forecast"] = {
            "status": "insufficient_history", "reasons": ["历史不足 14 天"],
        }
        data["risk_assessment"] = {"status": "unavailable"}

        content = self.format_quote(data)

        self.assertIn("七日历史参考（样本不足）", content)
        self.assertIn("七日预测：不可用（历史不足 14 天）", content)
        self.assertIn("风险评估：不可用", content)


if __name__ == "__main__":
    unittest.main()
