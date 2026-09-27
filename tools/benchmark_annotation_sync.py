"""Isolated production SQLite/worker benchmark; synthetic facts, deterministic embedding.
These are PC-local service durations, not phone LAN T0-T6 results.
"""

import logging
import json
import time
import statistics
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tests.test_phase1_human_facts import people
from tests.test_phase2_samples import audio
from tests.annotation_sync_fixture import seed
from tests.annotation_plan_baseline import plans as baseline
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.annotation_sample_snapshot import load_snapshot
from allday_asr.v3.adapters.sqlite.annotation_sample_plan import compute_plans
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    SyncRequest,
    PROJECTION_VERSION,
)
from allday_asr.v3.domain.ids import new_ulid


def summary(values):
    ordered = sorted(values)
    return dict(
        n=len(values),
        median_ms=statistics.median(values),
        max_ms=max(values),
        p95_ms=ordered[int(0.95 * len(values)) - 1] if len(values) >= 20 else None,
    )


def run():
    transactions = []
    class Metrics(logging.Handler):
        def emit(self, record):
            if record.msg.startswith("sqlite transaction"):
                transactions.append(record.args)
    logger = logging.getLogger("allday_asr.v3.adapters.sqlite.database")
    logger.setLevel(logging.DEBUG)
    handler = Metrics()
    logger.addHandler(handler)
    results = []
    for size in (32, 1800):
        source = people.__wrapped__()
        f = next(source)
        synthetic = audio.__wrapped__(f)
        f, _, provider = next(synthetic)
        try:
            sid, pid, ids = seed(f, size)
            with SqliteUnitOfWork(f.core.database) as u:
                t = time.perf_counter()
                before = baseline(
                    u.people.connection,
                    u.evidence,
                    sid,
                    provider.model,
                    provider.model_version,
                )
                old_ms = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            with f.core.database.read() as db:
                snapshot = load_snapshot(db, sid)
            read_ms = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            after = compute_plans(snapshot, provider.model, provider.model_version)
            plan_ms = (time.perf_counter() - t) * 1000
            from dataclasses import asdict

            assert [asdict(p) for p in before[0]] == [asdict(p) for p in after[0]]
            results.append(
                dict(
                    size=size,
                    baseline_plan_write_hold_ms=old_ms,
                    new_snapshot_ms=read_ms,
                    new_detached_plan_ms=plan_ms,
                    projection_parses=len(snapshot.projections),
                    fact_count=len(snapshot.facts),
                )
            )
            for kind in ("speaker.assign", "segment.classify"):
                for count, repeats in ((1, 20), (32, 5)):
                    for active in (False, True):
                        transactions.clear()
                        durations = []
                        worker_results = []
                        with ThreadPoolExecutor(1) as pool:
                            for repeat in range(repeats):
                                with SqliteUnitOfWork(f.core.database) as u:
                                    selected = [
                                        u.evidence.get_utterance(uid)
                                        for uid in ids[:count]
                                    ]
                                    if active:
                                        u.people.enqueue_samples(sid)
                                work = (
                                    pool.submit(
                                        f.core.people.sample_worker.run_pending, 1
                                    )
                                    if active
                                    else None
                                )
                                operations = tuple(
                                    ClientOperation(
                                        new_ulid(),
                                        kind,
                                        None,
                                        dict(
                                            selections=[
                                                dict(
                                                    utterance_id=row.utterance_id,
                                                    revision=row.revision,
                                                )
                                            ],
                                            **(
                                                {"person_id": pid}
                                                if kind == "speaker.assign"
                                                else {
                                                    "sound_kind": "live_speech"
                                                    if repeat % 2
                                                    else "non_speech"
                                                }
                                            ),
                                        ),
                                    )
                                    for row in selected
                                )
                                t = time.perf_counter()
                                response = f.core.mobile_sync.synchronize(
                                    "device-1",
                                    SyncRequest(
                                        PROJECTION_VERSION, None, operations, 500
                                    ),
                                )
                                durations.append((time.perf_counter() - t) * 1000)
                                assert len(response.receipts) == count and all(
                                    r.status.value == "applied"
                                    for r in response.receipts
                                )
                                if work:
                                    worker_results.extend(
                                        r["status"] for r in work.result(timeout=10)
                                    )
                        results.append(
                            dict(
                                size=size,
                                kind=kind,
                                batch=count,
                                worker_active=active,
                                **summary(durations),
                                worker_results=worker_results,
                                sqlite_wait_max_ms=max((t[0] for t in transactions), default=0),
                                sqlite_hold_max_ms=max((t[1] for t in transactions), default=0),
                            )
                        )
            with f.core.database.transaction() as db:
                db.execute("UPDATE annotation_sample_queue SET retry_at=0,attempts=0")
            results.append(
                dict(
                    size=size,
                    final_worker=[
                        r["status"] for r in f.core.people.sample_worker.run_pending(1)
                    ],
                )
            )
        finally:
            for generator in (synthetic, source):
                try:
                    next(generator)
                except StopIteration:
                    pass
    output = Path("outputs/sync-live-diagnosis/benchmark.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    run()
