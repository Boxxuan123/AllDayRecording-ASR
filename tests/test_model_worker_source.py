"""Real child bootstrapping with a local SDK that cannot make a remote call."""
import json
import sys
from pathlib import Path
import pytest
from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner, _worker_identity

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Owned child requires exclusive Windows Job APIs")


def fake_environment(root, monkeypatch):
    # A hostile inherited editable install/CWD must not choose the worker module.
    old = root / "allday_asr"
    old.mkdir()
    (old / "__init__.py").write_text('raise RuntimeError("OLD_SOURCE_USED")')
    (root / "openai_codex.py").write_text(
        'class CodexConfig:\n def __init__(self,**kw): pass\n'
        'class Codex:\n def __init__(self,*a,**kw): raise RuntimeError("LOCAL_SDK_REMOTE_DISABLED")\n')
    monkeypatch.setenv("PYTHONPATH", str(root))
    monkeypatch.chdir(root)


def prepared(runner):
    return runner.prepare_request(task="synthetic", prompt="{}", instructions="fixture",
                                  schema={"type":"object"}, model="fake", effort="low")


def test_actual_child_uses_parent_source_even_with_other_editable_install(tmp_path, monkeypatch):
    fake_environment(tmp_path, monkeypatch)
    runner = ModelExecutionRunner(tmp_path / "receipts")
    runner.receipt_dir.mkdir()
    identity = prepared(runner)
    with pytest.raises(RuntimeError, match="LOCAL_SDK_REMOTE_DISABLED"):
        runner._owned_call(identity, tmp_path, "fixture", 1, None, 5)
    phase = json.loads((runner.receipt_dir / "fixture.1.phases.json").read_text())
    assert phase["worker_identity"] == identity["worker_identity"] == _worker_identity()
    events = [p["phase"] for p in phase["events"]]
    assert "worker_identity_verified" in events and "sdk_initialize" in events
    assert "turn_submit" not in events
    process = json.loads((runner.receipt_dir / "fixture.1.process.json").read_text())
    assert process["assigned_before_any_child_or_model_call"]


@pytest.mark.parametrize("mismatch", ["module", "build"])
def test_child_identity_mismatch_stops_before_sdk_initialization(tmp_path, monkeypatch, mismatch):
    fake_environment(tmp_path, monkeypatch)
    runner = ModelExecutionRunner(tmp_path / "receipts")
    runner.receipt_dir.mkdir()
    identity = prepared(runner)
    if mismatch == "module":
        identity["worker_identity"]["module_sha256"] = "0" * 64
    else:
        import allday_asr.build_info as build_info
        expected = dict(build_info.runtime_build())
        monkeypatch.setattr(build_info, "runtime_build", lambda: {**expected, "git_commit": "different-build"})
    with pytest.raises(RuntimeError, match="WORKER_SOURCE_MISMATCH"):
        runner._owned_call(identity, tmp_path, "fixture", 1, None, 5)
    result = json.loads((runner.receipt_dir / "fixture.1.result.json").read_text())
    assert result["status"] == "failed" and result["error"] == "WORKER_SOURCE_MISMATCH"
    assert result["worker_identity"]["module_sha256"] != "0" * 64
    events = json.loads((runner.receipt_dir / "fixture.1.phases.json").read_text())["events"]
    assert [p["phase"] for p in events] == ["worker_launch"]


def test_legacy_completed_worker_output_without_identity_is_rejected(tmp_path, monkeypatch):
    from allday_asr.v3.adapters.codex import owned_attempt
    runner = ModelExecutionRunner(tmp_path)
    identity = prepared(runner)

    class LegacyChild:
        def __init__(self, argv, log, cwd):
            self.result = Path(argv[-1]); self.identity = {"fixture": True}
        def wait(self, *args):
            self.result.write_text(json.dumps({"status":"completed", "result":{}}))
            return 0

    monkeypatch.setattr(owned_attempt, "OwnedAttempt", LegacyChild)
    with pytest.raises(RuntimeError, match="WORKER_SOURCE_MISMATCH"):
        runner._owned_call(identity, tmp_path, "legacy", 1, None, 5)
