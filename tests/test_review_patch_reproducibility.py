"""The review artifact itself must preserve exact bytes, including final newlines."""

import hashlib
from pathlib import Path
import subprocess
import sys
import pytest
from tools.verify_review_patch import verify


def test_patch_rebuild_rejects_a_valid_patch_missing_one_final_newline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.PIPE)

    git("init")
    git("config", "user.name", "Fictional Review")
    git("config", "user.email", "review@example.invalid")
    git("config", "core.autocrlf", "false")
    (repo / "README.md").write_bytes(b"fixture\n")
    git("add", ".")
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD").decode().strip()
    data = Path("tests/fixtures/recall_corrections/frozen.json").read_bytes()
    (repo / "frozen.json").write_bytes(data)
    git("add", "frozen.json")
    expected = git("write-tree").decode().strip()
    patch = tmp_path / "correct.patch"
    patch.write_bytes(git("diff", "--cached", "--binary", "--full-index", baseline))
    before_index = (repo / ".git/index").read_bytes()
    good = verify(repo, baseline, expected, patch, "frozen.json")
    assert good["status"] == "exact_tree_match"
    assert (
        good["frozen_sha256"] == hashlib.sha256(data).hexdigest()
        and good["frozen_ends_with_lf"]
    )
    assert (repo / ".git/index").read_bytes() == before_index
    (repo / "frozen.json").write_bytes(data[:-1])
    git("add", "frozen.json")
    broken = tmp_path / "missing-newline.patch"
    broken.write_bytes(git("diff", "--cached", "--binary", "--full-index", baseline))
    with pytest.raises(ValueError, match="Reconstructed tree differs: frozen.json"):
        verify(repo, baseline, expected, broken, "frozen.json")

    # Even if the expected Git tree contains that wrong newline, the independent
    # immutable corpus hash must reject it (the precise previous delivery gap).
    wrong_expected = git("write-tree").decode().strip()
    cli = subprocess.run(
        [
            sys.executable,
            str(Path("tools/verify_review_patch.py").resolve()),
            "--repo",
            str(repo),
            "--baseline",
            baseline,
            "--expected",
            wrong_expected,
            "--patch",
            str(broken),
            "--frozen-path",
            "frozen.json",
        ],
        capture_output=True,
    )
    assert cli.returncode == 1 and b"immutable corpus hash" in cli.stderr
