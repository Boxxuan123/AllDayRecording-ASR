"""Package this repair against its captured dirty-worktree baseline, not HEAD.

No recordings, databases, private transcripts, vector stores or build caches are
read. Only explicitly allowed source/document/contract files and test logs enter.
"""

from pathlib import Path
import argparse
import difflib
import hashlib
import json
import os
import subprocess
import zipfile
from uuid import uuid4


def digest(data):
    return hashlib.sha256(data).hexdigest()


def patch_lines(text):
    chunks = text.split("\n")
    return [line + "\n" for line in chunks[:-1]] + ([chunks[-1]] if chunks[-1] else [])


def package(baseline, output):
    manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    summary = {}
    additions = {
        "server": {
            "src/allday_asr/v3/adapters/sqlite/annotation_fact_repository.py",
            "src/allday_asr/v3/adapters/sqlite/annotation_fact_migration.py",
            "src/allday_asr/v3/adapters/sqlite/annotation_fact_migration_effects.py",
            "src/allday_asr/v3/adapters/sqlite/migrations/v013_annotation_facts.py",
            "src/allday_asr/v3/adapters/sqlite/reminder_source_repository.py",
            "tests/test_phase1_human_facts.py",
            "tests/test_phase1_migration.py",
            "tests/test_phase1_guards.py",
            "tools/package_phase1_review.py",
        },
        "phone": {"tools/quality/check-phase1-reminder.mjs"},
    }
    for name, start in manifest.items():
        root = Path(start["root"])
        listed = (
            subprocess.check_output(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=root,
            )
            .decode()
            .split("\0")
        )
        paths = sorted(set(listed) | set(start["files"]))
        patch, changed = [], []
        for rel in paths:
            # The initial safe snapshot excluded directories named data/models,
            # including source directories that this round never edited. Their
            # absence is not evidence that they are new. Only authored additions
            # may be packaged without baseline bytes.
            if rel not in start["files"] and rel not in additions[name]:
                continue
            if not rel or rel.split("/")[0] not in {
                "src",
                "tests",
                "tools",
                "contracts",
                "docs",
                "phone",
                "all_day_recording_front",
            }:
                continue
            if rel.startswith("tests/") and not (
                rel.startswith("tests/test_phase1_")
                or rel == "tests/test_v33_intelligent_reminders.py"
            ):
                continue
            if Path(rel).suffix not in {
                ".py",
                ".ets",
                ".ts",
                ".mjs",
                ".js",
                ".json",
                ".md",
                ".vue",
                ".css",
                ".html",
            }:
                continue
            if any(
                part in rel.split("/")
                for part in [
                    "node_modules",
                    ".test",
                    ".hvigor",
                    "build",
                    "dist",
                    "__pycache__",
                ]
            ):
                continue
            before_file = baseline / name / rel
            after_file = root / rel
            before = before_file.read_bytes() if before_file.is_file() else b""
            after = after_file.read_bytes() if after_file.is_file() else b""
            if before == after:
                continue
            a, b = (
                before.decode("utf-8-sig").replace("\r\n", "\n"),
                after.decode("utf-8-sig").replace("\r\n", "\n"),
            )
            if a == b:
                continue
            for line in difflib.unified_diff(
                patch_lines(a),
                patch_lines(b),
                fromfile="a/" + rel if before_file.is_file() else "/dev/null",
                tofile="b/" + rel if after_file.is_file() else "/dev/null",
            ):
                patch.append(
                    line
                    if line.endswith("\n")
                    else line + "\n\\ No newline at end of file\n"
                )
            changed.append(
                {
                    "path": rel,
                    "before_sha256": digest(before) if before_file.is_file() else None,
                    "after_sha256": digest(after) if after_file.is_file() else None,
                    "after_normalized_sha256": digest(b.encode()),
                }
            )
        patch_path = output / (name + ".patch")
        patch_path.write_text("".join(patch), encoding="utf-8", newline="\n")
        # Apply to an isolated source-only copy of the real starting bytes.
        verify = baseline / ("verify-" + name + "-" + uuid4().hex)
        verify.mkdir()
        for entry in changed:
            src = baseline / name / entry["path"]
            if src.exists():
                dest = verify / entry["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(
                    src.read_text(encoding="utf-8-sig"), encoding="utf-8", newline="\n"
                )
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(verify.parent.resolve())}
        for extra in [["--check"], []]:
            subprocess.run(
                ["git", "apply", *extra, str(patch_path.resolve())],
                cwd=verify,
                env=env,
                check=True,
            )
        for entry in changed:
            applied = verify / entry["path"]
            value = applied.read_text(encoding="utf-8-sig") if applied.exists() else ""
            assert digest(value.encode()) == entry["after_normalized_sha256"], entry[
                "path"
            ]
        summary[name] = {
            "starting_head": start["head"],
            "starting_status": start["status"],
            "files": changed,
            "patch_verified": True,
        }
    (output / "changes.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logs = [
        "reproduce.log",
        "focused-final.log",
        "regression-final.log",
        "ruff.log",
        "frontend-tests.log",
        "frontend-build.log",
        "frontend-build-passed.log",
    ]
    for name in logs:
        (output / name).write_bytes((baseline / name).read_bytes())
    phone = Path(manifest["phone"]["root"])
    (output / "phone-host.log").write_bytes(
        (phone / "phone/.test/phase1-host.log").read_bytes()
    )
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for file in sorted(output.iterdir()):
            if file.is_file():
                z.write(file, file.name)
    print(
        json.dumps(
            {
                "archive": str(archive.resolve()),
                "files": {k: len(v["files"]) for k, v in summary.items()},
                "zip_sha256": digest(archive.read_bytes()),
                "patches_verified": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline", type=Path, default=Path("state/phase1-repair-baseline")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/reviews/phase1-20260924")
    )
    args = parser.parse_args()
    package(args.baseline, args.output)
