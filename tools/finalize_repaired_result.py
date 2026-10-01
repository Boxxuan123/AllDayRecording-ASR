"""Historical repair publication. Default preview; --apply writes Core/sync."""

import argparse
import json
from datetime import datetime, timezone

from allday_asr.v3.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from allday_asr.v3.application.durable_processing_support import (
    _session_projection,
    publish_superseded_utterances,
)
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core

SESSION_ID = "69WRQ94VSEQVX0TPXN0ASQSYHC"
RUN_ID = "01M3HE3D837N1HX2FD2DWHQ5NA"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    core = compose_v3_core(V3CorePaths.from_environment())
    try:
        snapshot = core.processing.get_for_run(RUN_ID)
        if (
            snapshot.job.status != "succeeded"
            or snapshot.run.input_revision != 2
            or snapshot.run.session_id != SESSION_ID
        ):
            raise RuntimeError("full recording run is not successful")
        if not args.apply:
            print(
                {
                    "session_id": SESSION_ID,
                    "run_id": RUN_ID,
                    "status": snapshot.job.status,
                    "apply": False,
                }
            )
            return
        core.initialize()
        with SqliteUnitOfWork(core.database) as uow:
            connection = uow.catalog.connection
            count, end = connection.execute(
                "SELECT COUNT(*), MAX(session_end_ms) FROM capture_segments WHERE session_id=?",
                (SESSION_ID,),
            ).fetchone()
            if (count, end) != (71, 4_227_580):
                raise RuntimeError("repaired audio coverage changed")
            current = connection.execute(
                "SELECT COUNT(*) FROM utterances WHERE run_id=? AND status='active'",
                (RUN_ID,),
            ).fetchone()[0]
            if current < 1:
                raise RuntimeError("full recording produced no active utterances")
            withdrawn = publish_superseded_utterances(uow, snapshot)
            latest = connection.execute(
                "SELECT payload_json FROM change_events WHERE resource_type='recording_session' "
                "AND resource_id=? ORDER BY sequence DESC LIMIT 1",
                (SESSION_ID,),
            ).fetchone()
            payload = json.loads(latest[0]) if latest and latest[0] else {}
            refreshed = False
            if payload.get("input_revision") != 2 or payload.get("segment_count") != 71:
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                connection.execute(
                    "UPDATE recording_sessions SET revision=revision+1,updated_at=? "
                    "WHERE session_id=?",
                    (now, SESSION_ID),
                )
                session = uow.catalog.get_session(SESSION_ID)
                uow.changes.append(
                    "recording_session",
                    SESSION_ID,
                    session.revision,
                    "upsert",
                    _session_projection(session),
                )
                uow.audit.append(
                    "phone.session.full_projection_published",
                    "system:controlled_repair",
                    "recording_session",
                    SESSION_ID,
                    {
                        "input_revision": 2,
                        "segment_count": 71,
                        "replacement_run_id": RUN_ID,
                    },
                )
                refreshed = True
        print(
            {
                "full_run": RUN_ID,
                "full_utterances": current,
                "old_utterances_withdrawn_from_phone": withdrawn,
                "full_phone_session_projection_published": refreshed,
            }
        )
    finally:
        core.close()


if __name__ == "__main__":
    main()
