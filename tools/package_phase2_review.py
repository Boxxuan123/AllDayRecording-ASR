"""Export runnable current sources and only this turn's verified baseline diff.

No runtime state, audio, databases, vector stores, credentials or caches are read.
Source directories called data/models are deliberately retained.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import zipfile
import uuid


def sha(data):
    return hashlib.sha256(data).hexdigest()


TEXT = {
    ".py",
    ".ets",
    ".ts",
    ".js",
    ".mjs",
    ".cjs",
    ".json",
    ".json5",
    ".md",
    ".toml",
    ".vue",
    ".css",
    ".html",
    ".yaml",
    ".yml",
    ".sql",
    ".lock",
    ".xml",
    ".properties",
    ".svg",
    ".png",
    ".webp",
    ".ttf",
}
NEW = {
    "server": [
        "src/allday_asr/v3/interfaces/review_audio.py",
        "tests/test_review_audio_boundaries.py",
        "tests/test_review_audio_guards.py",
        "tests/review_audio_browser_server.py",
        "all_day_recording_front/v3/components/VoiceSampleAudition.vue",
        "all_day_recording_front/tests/review-audio-browser.mjs",
        "all_day_recording_front/tests/review-audio-live-browser.mjs",
        "src/allday_asr/v3/adapters/sqlite/migrations/v015_annotation_receipts.py",
        "tests/test_phase2_closeout_audit.py",
        "tests/test_phase2_closeout_guards.py",
        "tests/phase2_closeout_export.py",
        "src/allday_asr/v3/adapters/sqlite/annotation_sample_plan.py",
        "src/allday_asr/v3/adapters/sqlite/annotation_sample_repository.py",
        "src/allday_asr/v3/adapters/sqlite/migrations/v014_annotation_samples.py",
        "src/allday_asr/v3/application/annotation_samples.py",
        "src/allday_asr/v3/application/annotation_sample_status.py",
        "tests/test_phase2_original_audit.py",
        "tests/test_phase2_preflight.py",
        "tests/test_phase2_samples.py",
        "tests/test_phase2_migration.py",
        "tests/phase2_server_bridge.py",
        "tools/package_phase2_review.py",
    ],
    "phone": [
        "tools/quality/check-review-audio.cjs",
        "tools/quality/check-phase2-closeout.cjs",
        "phone/src/main/ets/v3/domain/PhoneV3PersonFact.ets",
        "phone/src/main/ets/v3/domain/PhoneV3SampleStatus.ets",
        "tools/quality/phone-sqlite-harness.cjs",
        "tools/quality/check-phase2-sync.cjs",
        "tools/quality/check-phase2-e2e.cjs",
        "tools/quality/check-phase2-original-sync.cjs",
    ],
}


def source_paths(root, name):
    directories = (
        [
            "src",
            "contracts",
            "all_day_recording_front/v3",
            "all_day_recording_front/tests",
        ]
        if name == "server"
        else [
            "phone/src",
            "entry/src",
            "common/src",
            "AppScope",
            "contracts",
            "tools/quality",
            "hvigor",
        ]
    )
    result = set()
    for directory in directories:
        for current, children, files in os.walk(root / directory):
            children[:] = [c for c in children if c != "__pycache__"]
            for file in files:
                path = Path(current) / file
                if path.suffix in TEXT:
                    result.add(path.relative_to(root).as_posix())
    if name == "server":
        # Shared fixture modules are part of the source package, not test output.
        result.update(
            p.relative_to(root).as_posix() for p in (root / "tests").glob("*.py")
        )
        result.update(
            [
                "pyproject.toml",
                "README.md",
                "docs/phone-speaker-annotation.md",
                "tools/evaluate_annotation_benefit.py",
                "tools/package_phase2_review.py",
            ]
        )
        result.update(
            p.relative_to(root).as_posix()
            for p in (root / "all_day_recording_front").iterdir()
            if p.is_file() and p.suffix in TEXT
        )
    else:
        for directory in ["", "phone", "entry", "common"]:
            result.update(
                p.relative_to(root).as_posix()
                for p in (root / directory).iterdir()
                if p.is_file() and p.suffix in TEXT and p.name != "local.properties"
            )
        result.discard("build-profile.json5")  # signing-bearing local file
        result.discard("README.md")  # historical device notes aren't build inputs
        result = {p for p in result if not ("/" not in p and p.endswith(".md"))}
    return sorted(p for p in result if (root / p).is_file())


def package(
    baseline,
    output,
    readme,
    logs="state/phase2-logs",
    audit="state/phase2-audit-input",
    phone_log_prefix="phase2-host",
):
    manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    content, changes = {}, {}
    for name, entry in manifest.items():
        root = Path(entry["root"])
        paths = source_paths(root, name)
        destination = "ASR" if name == "server" else "Harmony"
        for rel in paths:
            content[f"source/{destination}/{rel}"] = (root / rel).read_bytes()
        diff, changed = [], []
        for rel in sorted(set(entry["files"]) | set(NEW[name])):
            old_path, new_path = baseline / name / rel, root / rel
            old = old_path.read_bytes() if old_path.is_file() else b""
            if rel in entry["files"]:
                assert sha(old) == entry["files"][rel], f"baseline changed: {rel}"
            new = new_path.read_bytes() if new_path.is_file() else b""
            if old == new:
                continue
            a, b = (v.decode("utf-8-sig").replace("\r\n", "\n") for v in (old, new))
            if a == b:
                continue
            assert rel in paths, f"changed file outside source allowlist: {rel}"
            lines = difflib.unified_diff(
                a.splitlines(keepends=True),
                b.splitlines(keepends=True),
                fromfile="a/" + rel if old else "/dev/null",
                tofile="b/" + rel if new else "/dev/null",
            )
            for line in lines:
                diff.append(
                    line
                    if line.endswith("\n")
                    else line + "\n\\ No newline at end of file\n"
                )
            changed.append(
                {
                    "path": rel,
                    "before_sha256": sha(old) if old else None,
                    "after_sha256": sha(new),
                    "normalized_after_sha256": sha(b.encode()),
                }
            )
        patch_path = output / (name + ".patch")
        patch_path.write_text("".join(diff), encoding="utf-8", newline="\n")
        verify = output / ("verify-" + name + "-" + uuid.uuid4().hex[:8])
        verify.mkdir(exist_ok=True)
        for value in changed:
            path = verify / value["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            old = baseline / name / value["path"]
            if old.exists():
                path.write_bytes(old.read_bytes())
        subprocess.run(
            [
                "git",
                "apply",
                "--unsafe-paths",
                "--directory=" + str(verify.resolve()),
                str(patch_path.resolve()),
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )
        for value in changed:
            data = (
                (verify / value["path"])
                .read_text(encoding="utf-8-sig")
                .replace("\r\n", "\n")
                .encode()
            )
            assert sha(data) == value["normalized_after_sha256"], value["path"]
        content[f"diffs/{name}.patch"] = patch_path.read_bytes()
        changes[name] = {
            "head_at_start": entry["head"],
            "baseline_files": len(entry["files"]),
            "files": changed,
            "patch_applied_to_baseline_and_verified": True,
        }
    phone = Path(manifest["phone"]["root"])
    server = Path(manifest["server"]["root"])
    # Parse JSON-with-comments using the already installed TypeScript parser.
    script = """const fs=require('fs'),ts=require(process.argv[1]);
const result=ts.parseConfigFileTextToJson('build-profile.json5',fs.readFileSync(process.argv[2],'utf8'));
if(result.error)throw Error('build profile parse failed');
const v=result.config;delete v.app.signingConfigs;
for(const p of v.app.products||[])delete p.signingConfig;
process.stdout.write(JSON.stringify(v,null,2)+'\\n');"""
    template = subprocess.check_output(
        [
            "node",
            "-e",
            script,
            str(server / "all_day_recording_front/node_modules/typescript"),
            str(phone / "build-profile.json5"),
        ]
    )
    assert b"password" not in template.lower() and b"keystore" not in template.lower()
    content["source/Harmony/build-profile.json5"] = template
    content["metadata/export-transformations.json"] = json.dumps(
        {
            "source/Harmony/build-profile.json5": "Current local profile parsed; signingConfigs and product signingConfig removed. All other exported sources are byte-identical.",
            "excluded_roots": [
                "ASR/state",
                "ASR/data",
                "ASR/models",
                "ASR/outputs",
                ".git",
                ".venv",
                "node_modules",
                "oh_modules",
                "module build/.test/.hvigor",
            ],
            "excluded_files": [
                "ASR/allday-asr.toml",
                "Harmony/local.properties",
                "local signing material",
            ],
            "source_data_models_directories_retained": True,
        },
        ensure_ascii=False,
        indent=2,
    ).encode()
    content["metadata/baseline.json"] = (baseline / "manifest.json").read_bytes()
    content["diffs/changes.json"] = json.dumps(
        changes, ensure_ascii=False, indent=2
    ).encode()
    for path in (server / logs).glob("*.log"):
        content["validation/" + path.name] = path.read_bytes()
    for path in (phone / ".test").glob(phone_log_prefix + "*.log"):
        name = path.name
        if path.exists():
            content["validation/" + name] = path.read_bytes()
    for path in (server / audit).rglob("*"):
        if path.is_file() and path.suffix in {
            ".md",
            ".py",
            ".cjs",
            ".log",
            ".txt",
            ".json",
        }:
            content["original-audit/" + path.relative_to(server / audit).as_posix()] = (
                path.read_bytes()
            )
    content["README.md"] = readme.read_bytes()
    content["metadata/environment.txt"] = subprocess.check_output(
        [
            str(server / ".venv/Scripts/python.exe"),
            "-c",
            "import sys,platform,importlib.metadata as m;print(sys.version);print(platform.platform());print('\\n'.join(n+'=='+m.version(n) for n in ['pytest','numpy','soundfile','ruff','funasr']))",
        ]
    )
    assert any("/v3/data/PhoneProjectionRepository.ets" in p for p in content)
    assert any("/adapters/models/" in p for p in content)
    forbidden = {
        ".wav",
        ".mp3",
        ".m4a",
        ".flac",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".npy",
        ".npz",
        ".pt",
        ".pth",
    }
    assert not any(Path(p).suffix.lower() in forbidden for p in content)
    pem = rb"-----BEGIN [A-Z ]*PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{20}"
    assert not any(re.search(pem, data) for data in content.values())
    sums = "".join(f"{sha(data)}  {name}\n" for name, data in sorted(content.items()))
    content["SHA256SUMS.txt"] = sums.encode()
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(content.items()):
            z.writestr(name, data)
    (output / "changes.json").write_bytes(content["diffs/changes.json"])
    print(
        json.dumps(
            {
                "archive": str(archive),
                "sha256": sha(archive.read_bytes()),
                "files": len(content),
                "changed_files": {n: len(v["files"]) for n, v in changes.items()},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--readme", type=Path, required=True)
    parser.add_argument("--logs", default="state/phase2-logs")
    parser.add_argument("--audit", default="state/phase2-audit-input")
    parser.add_argument("--phone-log-prefix", default="phase2-host")
    args = parser.parse_args()
    package(
        args.baseline,
        args.output,
        args.readme,
        args.logs,
        args.audit,
        args.phone_log_prefix,
    )
