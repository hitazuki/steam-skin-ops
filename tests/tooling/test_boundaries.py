import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "steam_skin_ops"


class BoundaryTestCase(unittest.TestCase):
    def _imports(self, directory: Path) -> set[str]:
        imported: set[str] = set()
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
        return imported

    def test_profit_and_monitor_do_not_import_each_other(self):
        profit_imports = self._imports(PACKAGE / "profit")
        monitor_imports = self._imports(PACKAGE / "monitor")
        self.assertFalse(any("steam_skin_ops.monitor" in name for name in profit_imports))
        self.assertFalse(any("steam_skin_ops.profit" in name for name in monitor_imports))

    def test_dockerfile_copies_only_monitor_runtime(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("src/steam_skin_ops/monitor", dockerfile)
        self.assertNotIn("src/steam_skin_ops/profit", dockerfile)
        self.assertIn("steam_skin_ops.monitor.api:create_app", dockerfile)
        self.assertIn("--factory", dockerfile)
        self.assertIn("config/*.yaml", dockerignore)

    def test_standalone_compose_has_no_astrbot_dependency(self):
        standalone = (ROOT / "compose.yml").read_text(encoding="utf-8")
        overlay = (ROOT / "compose.astrbot.yml").read_text(encoding="utf-8")
        self.assertNotIn("astrbot-internal", standalone)
        self.assertIn("astrbot-internal", overlay)
        self.assertIn("config/monitor.yaml:/app/config/monitor.yaml:ro", standalone)
        self.assertNotIn("env_file", standalone)
        self.assertNotIn("environment:", standalone)
