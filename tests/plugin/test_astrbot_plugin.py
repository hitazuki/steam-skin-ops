import ast
import unittest
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).parents[2] / "plugins" / "astrbot_plugin_steam_skin_ops" / "main.py"
)


class AstrBotPluginContractTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
