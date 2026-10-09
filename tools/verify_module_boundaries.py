"""Finite acceptance commands; writes logs and a machine-readable completion record."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TESTS = [
    "tests/test_module_boundaries_traceability.py", "tests/test_self_identity_regression.py",
    "tests/test_v3_durable_processing.py", "tests/test_v32_semantic_events.py",
    "tests/test_v36_daily_insights.py", "tests/test_v3_device_sync.py",
    "tests/test_model_execution_runner.py", "tests/test_v3_migrations.py",
    "tests/test_v3_architecture.py", "tests/test_daily_event_layer.py",
    "tests/test_daily_semantic_adapter.py", "tests/test_v34_open_speaker_identity.py",
    "tests/test_product_reminder_loop.py", "tests/test_daily_automation.py",
    "tests/test_daily_inventory_completion.py",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    changed = sorted(set(subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"], cwd=root, text=True).splitlines()
        + subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=root, text=True).splitlines()))
    python_files = [p for p in changed if p.endswith(".py") and not p.startswith("outputs/")]
    if not python_files:
        python_files = [p for p in subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], cwd=root, text=True).splitlines()
            if p.endswith(".py") and (root / p).exists()]
    commands = [
        ("boundary-regressions", [sys.executable, "-m", "pytest", *TESTS, "-q"], 180),
        ("changed-python-lint", [sys.executable, "-m", "ruff", "check", *python_files], 60),
    ] if python_files else [
        ("boundary-regressions", [sys.executable, "-m", "pytest", *TESTS, "-q"], 180)]
    results = []
    for name, command, budget in commands:
        with (args.output / f"{name}.log").open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(command, cwd=root, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=budget)
                item = {"check": name, "command": command, "timeout_seconds": budget,
                        "exit_code": result.returncode}
            except subprocess.TimeoutExpired:
                item = {"check": name, "command": command, "timeout_seconds": budget, "status": "TIMEOUT"}
        results.append(item)
        print(json.dumps(item))
    (args.output / "acceptance.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(i.get("exit_code") == 0 for i in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
