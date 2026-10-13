"""Rebuild a review patch in an isolated Git index and compare exact tree/blob bytes.

No checkout, branch, production data or real index is changed. Git tree equality
checks all new/modified files, modes and final newlines across operating systems.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def verify(repo, baseline, expected, patch, frozen_path):
    repo, patch = Path(repo).resolve(), Path(patch).resolve()
    with tempfile.TemporaryDirectory(prefix="recall-review-index-") as temp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}

        def git(*args):
            result = subprocess.run(
                ["git", *args], cwd=repo, env=env, capture_output=True
            )
            if result.returncode:
                raise ValueError(
                    result.stderr.decode("utf-8", errors="replace").strip()
                )
            return result.stdout

        git("read-tree", baseline)
        git("apply", "--cached", "--binary", "--whitespace=nowarn", str(patch))
        actual_tree = git("write-tree").decode().strip()
        expected_tree = git("rev-parse", expected + "^{tree}").decode().strip()
        if actual_tree != expected_tree:
            paths = (
                git("diff", "--name-only", "-z", expected_tree, actual_tree)
                .decode("utf-8")
                .split("\0")
            )
            raise ValueError(
                "Reconstructed tree differs: " + ", ".join(p for p in paths if p)
            )
        frozen = git("show", actual_tree + ":" + frozen_path)
        changed = (
            git("diff", "--name-only", "-z", baseline, actual_tree)
            .decode("utf-8")
            .split("\0")
        )
        return {
            "baseline": git("rev-parse", baseline).decode().strip(),
            "expected_ref": git("rev-parse", expected).decode().strip(),
            "expected_tree": expected_tree,
            "reconstructed_tree": actual_tree,
            "changed_files": [p for p in changed if p],
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "frozen_path": frozen_path,
            "frozen_sha256": hashlib.sha256(frozen).hexdigest(),
            "frozen_bytes": len(frozen),
            "frozen_ends_with_lf": frozen.endswith(b"\n"),
            "status": "exact_tree_match",
        }


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument(
        "--frozen-path", default="tests/fixtures/recall_corrections/frozen.json"
    )
    parser.add_argument(
        "--expected-frozen-sha256",
        default="87cc6150d2d95db6dc925a10d68114f90730d8400bd8aff258750a4a132b8762",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(
            args.repo, args.baseline, args.expected, args.patch, args.frozen_path
        )
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    if result["frozen_sha256"] != args.expected_frozen_sha256:
        parser.exit(
            1, "Reconstructed frozen bytes differ from the immutable corpus hash\n"
        )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_bytes(encoded.encode("utf-8"))
    print(encoded)


if __name__ == "__main__":
    main()
