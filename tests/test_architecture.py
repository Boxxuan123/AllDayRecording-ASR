from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "allday_asr"
SERVICES_ROOT = PROJECT_ROOT / "src" / "allday_asr" / "services"
DATABASE_PATH = PROJECT_ROOT / "src" / "allday_asr" / "storage" / "database.py"
MIGRATION_RUNNER_PATH = (
    PROJECT_ROOT
    / "src"
    / "allday_asr"
    / "infrastructure"
    / "sqlite"
    / "migration_runner.py"
)
REPOSITORIES_ROOT = (
    PROJECT_ROOT
    / "src"
    / "allday_asr"
    / "infrastructure"
    / "sqlite"
    / "repositories"
)
QUALITY_WORKFLOW_PATH = (
    PROJECT_ROOT
    / "src"
    / "allday_asr"
    / "application"
    / "workflows"
    / "quality.py"
)
QUALITY_WORKFLOW_COMPAT_PATH = (
    PROJECT_ROOT
    / "src"
    / "allday_asr"
    / "services"
    / "quality_workflow.py"
)

EXTRACTED_REPOSITORY_METHODS = {
    "create_recording_session",
    "create_semantic_candidate_revision",
    "create_semantic_snapshot",
    "find_recording_session",
    "find_session_manifest_by_hash",
    "finish_processing_run",
    "get_processing_run",
    "get_recording_session",
    "get_semantic_candidate",
    "get_semantic_exchange",
    "get_session_for_recording",
    "get_session_manifest",
    "list_processing_run_inputs",
    "list_processing_runs",
    "list_recording_sessions",
    "list_semantic_candidate_revisions",
    "list_semantic_candidates",
    "list_session_processing_runs",
    "list_session_sources",
    "resume_processing_run",
    "session_input_fingerprint",
    "start_processing_run",
    "update_processing_run_progress",
}
EXTRACTED_REPOSITORY_METHODS |= {
    "all_segments",
    "assign_person_to_segments",
    "assign_person_to_speaker",
    "clear_person_assignments",
    "create_asr_disagreement",
    "create_asr_hypothesis",
    "create_benchmark_prediction_set",
    "create_diarization_turns",
    "create_token_speaker_attributions",
    "create_truth_set",
    "delete_person_profile",
    "delete_v2d1_identity_label",
    "get_action_candidate",
    "get_asr_hypothesis",
    "get_benchmark_prediction_set",
    "get_diarization_turn",
    "get_person_profile",
    "get_segment",
    "get_self_profile",
    "get_truth_set",
    "get_v2d1_review_completion",
    "list_action_candidates",
    "list_asr_disagreements",
    "list_asr_hypotheses",
    "list_asr_token_sources",
    "list_asr_token_sources_for_run",
    "list_asr_tokens",
    "list_benchmark_prediction_sets",
    "list_benchmark_predictions",
    "list_benchmark_runs",
    "list_committed_asr_tokens",
    "list_diarization_turn_sources",
    "list_diarization_turns",
    "list_evaluation_runs",
    "list_identity_candidate_reviews",
    "list_identity_reference_intervals",
    "list_manual_identity_annotations",
    "list_person_profiles",
    "list_segment_annotations",
    "list_token_speaker_attributions",
    "list_truth_annotation_sources",
    "list_truth_annotations",
    "list_truth_sets",
    "list_v2d1_candidate_reviews",
    "list_v2d1_identity_labels",
    "list_voice_library_samples",
    "mark_segment_completed",
    "mark_segment_failed",
    "mark_segment_running",
    "pending_segments",
    "person_assignment_count",
    "record_benchmark_run",
    "record_evaluation_run",
    "replace_speaker_labels",
    "replace_vad_segments",
    "reset_all_asr_segments",
    "reset_failed_segments",
    "reset_interrupted_segments",
    "retract_manual_identity_annotation",
    "review_action_candidate",
    "segment_count",
    "segment_status_counts",
    "upsert_action_candidate",
    "upsert_identity_candidate_review",
    "upsert_identity_reference_interval",
    "upsert_known_person_profile",
    "upsert_manual_identity_annotation",
    "upsert_segment_annotations",
    "upsert_self_profile",
    "upsert_v2d1_candidate_review",
    "upsert_v2d1_identity_label",
    "upsert_v2d1_review_completion",
    "upsert_voice_library_sample",
}

# Phase 1 cleared the original compatibility debts. These empty allowlists make
# any regression explicit instead of silently growing a new baseline.
KNOWN_PRIVATE_IMPORTS: set[tuple[str, str, str]] = set()
KNOWN_SERVICE_CYCLES: set[frozenset[str]] = set()
KNOWN_LOCAL_SERVICE_IMPORTS: set[tuple[str, str, str]] = set()


class ArchitectureBaselineTests(unittest.TestCase):
    def test_quality_workflow_has_explicit_stage_order(self) -> None:
        tree = ast.parse(
            QUALITY_WORKFLOW_PATH.read_text(encoding="utf-8"),
            filename=str(QUALITY_WORKFLOW_PATH),
        )
        workflow = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_run_stages"
        )
        expected = [
            "verify_admission",
            "resolve_or_run_asr",
            "resolve_or_run_diarization",
            "run_optional_speech_recall",
            "run_optional_identity_audit",
            "inspect_identity_mining_state",
            "resolve_or_run_semantic",
        ]
        calls = sorted(
            (
                node.lineno,
                node.func.id,
            )
            for node in ast.walk(workflow)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in expected
        )

        self.assertEqual([name for _line, name in calls], expected)

    def test_quality_workflow_service_is_only_a_compatibility_entrypoint(
        self,
    ) -> None:
        tree = ast.parse(
            QUALITY_WORKFLOW_COMPAT_PATH.read_text(encoding="utf-8"),
            filename=str(QUALITY_WORKFLOW_COMPAT_PATH),
        )

        self.assertFalse(
            any(isinstance(node, ast.FunctionDef) for node in tree.body)
        )

    def test_runtime_entrypoints_use_explicit_database_open(self) -> None:
        direct_calls: list[str] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            direct_calls.extend(
                f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Database"
            )

        self.assertEqual(direct_calls, [])

    def test_database_constructor_contains_no_filesystem_or_migration_io(
        self,
    ) -> None:
        database_tree = ast.parse(
            DATABASE_PATH.read_text(encoding="utf-8"), filename=str(DATABASE_PATH)
        )
        database_class = next(
            node
            for node in database_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Database"
        )
        constructor = next(
            node
            for node in database_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        calls = {
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
            for node in ast.walk(constructor)
            if isinstance(node, ast.Call)
        }

        self.assertTrue(
            {"MigrationRunner", "connect", "initialize", "mkdir"}.isdisjoint(calls)
        )

    def test_extracted_database_facade_methods_contain_no_sql(self) -> None:
        database_tree = ast.parse(
            DATABASE_PATH.read_text(encoding="utf-8"), filename=str(DATABASE_PATH)
        )
        database_class = next(
            node
            for node in database_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Database"
        )
        methods = {
            node.name: node
            for node in database_class.body
            if isinstance(node, ast.FunctionDef)
        }

        self.assertEqual(EXTRACTED_REPOSITORY_METHODS - methods.keys(), set())
        for method_name in EXTRACTED_REPOSITORY_METHODS:
            string_literals = {
                node.value
                for node in ast.walk(methods[method_name])
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertFalse(
                any(
                    keyword in value.upper()
                    for value in string_literals
                    for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
                ),
                method_name,
            )

    def test_sqlite_repositories_do_not_import_database_facade(self) -> None:
        for path in REPOSITORIES_ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_modules = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertNotIn("allday_asr.storage.database", imported_modules, path.name)

    def test_database_facade_does_not_define_migration_catalog(self) -> None:
        database_tree = ast.parse(
            DATABASE_PATH.read_text(encoding="utf-8"), filename=str(DATABASE_PATH)
        )
        runner_tree = ast.parse(
            MIGRATION_RUNNER_PATH.read_text(encoding="utf-8"),
            filename=str(MIGRATION_RUNNER_PATH),
        )

        self.assertTrue(
            {"LATEST_SCHEMA_VERSION", "MIGRATIONS", "SCHEMA"}.isdisjoint(
                _top_level_assignments(database_tree)
            )
        )
        self.assertIn("LATEST_SCHEMA_VERSION", _top_level_assignments(runner_tree))

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


def _top_level_assignments(tree: ast.Module) -> set[str]:
    assignments: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            assignments.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments.add(node.target.id)
    return assignments


if __name__ == "__main__":
    unittest.main()
