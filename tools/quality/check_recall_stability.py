"""Zero-model release gate: production retrieval/packing/validation plus stage receipts."""

from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def main():
    from tests.recall_scenario_driver import run_scenario
    from tests.test_recall_acceptance_scenarios import bridge_case, plan_case
    from tests.test_recall_stability_gate import rename, assert_flow

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    scenes = []
    for seed in (7, 19, 41):
        s, m = rename(bridge_case(), seed)
        s.update(
            protocol_citation_probe=True,
            directory_page_size=2,
            reverse_response_rows=True,
        )
        scenes.append(
            (
                f"renamed-{seed}",
                s,
                {m[f"rec:fixture-{210 + i}"] for i in range(5)},
                set(),
            )
        )
    s, m = rename(bridge_case(broken=True), 73)
    s["protocol_citation_probe"] = True
    scenes.append(
        ("bridge-transfer", s, set(), {m[f"rec:fixture-{210 + i}"] for i in range(5)})
    )
    s = plan_case()
    s["protocol_citation_probe"] = True
    scenes.append(
        ("daytime-plan", s, {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"}, set())
    )
    s = plan_case()
    s["records"] = s["records"][:3]
    scenes.append(("unanswered", s, set(), set()))
    s = plan_case()
    s.update(protocol_citation_probe=True, invalid_probe_citation=True)
    scenes.append(("invalid-citation", s, {"rec:fixture-3"}, set()))
    receipts = []
    with tempfile.TemporaryDirectory(prefix="recall-stability-") as tmp:
        for label, scene, required, forbidden in scenes:
            run = run_scenario(Path(tmp) / label, scene)
            flow = assert_flow(run, required, forbidden)
            receipts.append(
                dict(
                    case=label,
                    **flow,
                    wire_bytes=[
                        len(json.dumps(w, ensure_ascii=False).encode())
                        for w in run["wire"]
                    ],
                    source_requests=len(run["source_queries"]),
                    model_sdk_calls=0,
                    semantic_success_claimed=False,
                )
            )
    (args.output / "stage-receipts.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2), "utf8"
    )
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_recall_stability_gate.py",
            "tests/test_recall_acceptance_scenarios.py",
            "tests/test_recall_mobile_parity.py",
            "tests/test_recall_daypart_comparison.py",
            "tests/test_recall_reference_protocol.py",
            "tests/test_recall_bounded_exploration.py",
        ],
        cwd=ROOT,
        env=__import__("os").environ | {"PYTHONPATH": str(ROOT / "src")},
    )


if __name__ == "__main__":
    raise SystemExit(main())
