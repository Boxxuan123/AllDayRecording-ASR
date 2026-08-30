from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = PROJECT_ROOT / "src" / "allday_asr" / "services"

# Phase 1 removes these compatibility debts. Keeping the allowlist here makes
# additions fail while allowing later cleanup without rewriting this test first.
KNOWN_PRIVATE_IMPORTS = {
    ("timeline.py", "allday_asr.exporters", "_absolute_timestamp"),
    ("timeline.py", "allday_asr.exporters", "_group_segments"),
}
KNOWN_SERVICE_CYCLES = {
    frozenset({"session_backup", "session_readiness"}),
}
KNOWN_LOCAL_SERVICE_IMPORTS = {
    (
        "session_readiness.py",
        "allday_asr.services.session_backup",
        "verify_session_backup",
    ),
}


class ArchitectureBaselineTests(unittest.TestCase):
    def test_no_new_private_cross_module_imports(self) -> None:
        actual: set[tuple[str, str, str]] = set()
        for path, tree in _service_trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if not module.startswith("allday_asr"):
                    continue
                for alias in node.names:
                    if alias.name.startswith("_"):
                        actual.add((path.name, module, alias.name))

        self.assertEqual(actual - KNOWN_PRIVATE_IMPORTS, set())

    def test_no_new_service_import_cycles(self) -> None:
        graph = _service_import_graph()
        reachability = {module: _reachable(graph, module) for module in graph}
        cycles = {
            frozenset(
                other
                for other in graph
                if other in reachability[module]
                and module in reachability[other]
            )
            for module in graph
        }
        cycles = {cycle for cycle in cycles if len(cycle) > 1}

        self.assertEqual(cycles - KNOWN_SERVICE_CYCLES, set())

    def test_no_new_function_local_service_imports(self) -> None:
        actual: set[tuple[str, str, str]] = set()
        for path, tree in _service_trees():
            for function in (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                for node in ast.walk(function):
                    if not isinstance(node, ast.ImportFrom):
                        continue
                    module = node.module or ""
                    if not module.startswith("allday_asr.services."):
                        continue
                    for alias in node.names:
                        actual.add((path.name, module, alias.name))

        self.assertEqual(actual - KNOWN_LOCAL_SERVICE_IMPORTS, set())


def _service_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(SERVICES_ROOT.glob("*.py"))
    ]


def _service_import_graph() -> dict[str, set[str]]:
    modules = {path.stem for path in SERVICES_ROOT.glob("*.py")}
    graph = {module: set() for module in modules}
    prefix = "allday_asr.services."
    for path, tree in _service_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if imported.startswith(prefix):
                    target = imported.removeprefix(prefix).split(".", 1)[0]
                    if target in modules:
                        graph[path.stem].add(target)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(prefix):
                        target = alias.name.removeprefix(prefix).split(".", 1)[0]
                        if target in modules:
                            graph[path.stem].add(target)
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    visited: set[str] = set()
    pending = list(graph[start])
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        pending.extend(graph[module] - visited)
    return visited


if __name__ == "__main__":
    unittest.main()
