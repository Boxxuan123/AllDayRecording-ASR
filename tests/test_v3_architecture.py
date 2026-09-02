from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V3_ROOT = PROJECT_ROOT / "src" / "allday_asr" / "v3"
CORE_ROOTS = tuple(V3_ROOT / name for name in ("domain", "application", "ports"))
DEFAULT_RUNTIME_FILES = (
    V3_ROOT / "bootstrap" / "core.py",
    V3_ROOT / "adapters" / "models" / "native.py",
    V3_ROOT / "adapters" / "transfer" / "automation.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "allday_asr.storage",
    "allday_asr.services",
    "allday_asr.web",
    "allday_asr.v3.adapters",
    "fastapi",
    "flask",
    "funasr",
    "gradio",
    "numpy",
    "pyannote",
    "soundfile",
    "sqlite3",
    "starlette",
    "torch",
    "transformers",
)


class V3ArchitectureTests(unittest.TestCase):
    def test_domain_application_and_ports_do_not_import_ui_http_models_or_sqlite(
        self,
    ) -> None:
        violations: list[str] = []
        for root in CORE_ROOTS:
            for path in sorted(root.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    module = _imported_module(node)
                    if module is not None and module.startswith(
                        FORBIDDEN_IMPORT_PREFIXES
                    ):
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{module}"
                        )

        self.assertEqual(violations, [])

    def test_v3_core_has_no_vue_or_arkui_source_dependency(self) -> None:
        violations: list[str] = []
        for root in CORE_ROOTS:
            for path in sorted(root.rglob("*.py")):
                source = path.read_text(encoding="utf-8").lower()
                for forbidden in ("arkui", "node_modules", "vue"):
                    if forbidden in source:
                        violations.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{forbidden}"
                        )
        self.assertEqual(violations, [])

    def test_v3_tree_has_no_retired_runtime_dependency(self) -> None:
        forbidden = (
            "allday_asr.storage",
            "allday_asr.services",
            "allday_asr.v3.adapters.models_v2",
            "allday_asr.v3.adapters.legacy_v2",
        )
        violations: list[str] = []
        for path in V3_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = _imported_module(node)
                if module is not None and module.startswith(forbidden):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{module}"
                    )
        self.assertEqual(violations, [])
        self.assertFalse((V3_ROOT / "adapters" / "models_v2").exists())
        self.assertFalse((V3_ROOT / "adapters" / "legacy_v2").exists())
        self.assertFalse((V3_ROOT / "application" / "legacy_import.py").exists())


def _imported_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return node.names[0].name
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return None


if __name__ == "__main__":
    unittest.main()
