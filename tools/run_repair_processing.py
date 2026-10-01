"""Historical repair utility. Default preview; --apply executes only its job.

This is not a generic queue drainer and never recovers other expired jobs.
"""

import argparse

from allday_asr.v3.adapters.models import build_native_model_pipeline
from allday_asr.v3.application import DurableProcessingWorker
from allday_asr.v3.application.durable_processing_support import _publish_processing
from allday_asr.v3.bootstrap import compose_v3_core
from allday_asr.v3.model_config import load_model_config
from allday_asr.v3.paths import DEFAULT_CONFIG_PATH

JOB_ID = "01M3HE3D83C6YPXADY7CJVJG3E"
SESSION_ID = "69WRQ94VSEQVX0TPXN0ASQSYHC"


class TargetOnlyService:
    """Roll back a mismatched claim before any model/stage is executed."""

    def __init__(self, service, job_id):
        self.service, self.job_id = service, job_id

    def __getattr__(self, name):
        return getattr(self.service, name)

    def recover_expired(self):
        return ()

    def claim(self, worker_id, config):
        with self.service._uow_factory() as uow:
            claim = uow.processing.claim_next(
                worker_id, self.service.lease_seconds, config
            )
            if claim is None:
                return None
            if claim.job.job_id != self.job_id:
                raise RuntimeError(
                    "repair job is not next; refused unrelated queue work"
                )
            _publish_processing(uow, uow.processing.get_snapshot(self.job_id))
            uow.audit.append(
                "processing.stage.claimed",
                f"worker:{worker_id}",
                "stage_run",
                claim.stage.stage_run_id,
                {
                    "attempt": claim.attempt.attempt_number,
                    "lease_id": claim.lease.lease_id,
                    "purpose": "targeted_repair",
                },
            )
            return claim


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    core = compose_v3_core()
    try:
        snapshot = core.processing.get(JOB_ID)
        if snapshot.run.session_id != SESSION_ID or snapshot.run.input_revision != 2:
            raise ValueError(
                "repair job does not belong to the expected recovered input"
            )
        print({"job_id": JOB_ID, "status": snapshot.job.status, "apply": args.apply})
        if not args.apply or snapshot.job.status == "succeeded":
            return
        if snapshot.job.status != "queued":
            raise ValueError(
                "repair job must already be queued; no implicit retry/recovery"
            )
        core.initialize()
        config = load_model_config(DEFAULT_CONFIG_PATH)
        adapter = build_native_model_pipeline(core.database, core.audio_store, config)
        worker = DurableProcessingWorker(
            TargetOnlyService(core.processing, JOB_ID),
            adapter,
            worker_id="controlled-session-repair",
            config={"pipeline": "v3-native.1"},
        )
        while True:
            snapshot = core.processing.get(JOB_ID)
            print(
                snapshot.job.status,
                snapshot.run.current_stage,
                round(snapshot.run.progress, 4),
                flush=True,
            )
            if snapshot.job.status == "succeeded":
                break
            if snapshot.job.status in {"failed_final", "cancelled"}:
                raise RuntimeError("repair processing did not succeed")
            worked = worker.run_once()
            if not worked:
                raise RuntimeError("repair job could not be claimed")
    finally:
        core.close()


if __name__ == "__main__":
    main()
