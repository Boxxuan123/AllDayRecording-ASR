"""Durable day attempts, atomic claims and cross-process single-day exclusion."""

import json
from datetime import timedelta
from uuid import uuid4

from allday_asr.v3.domain.hashing import canonical_json_sha256


from allday_asr.v3.application.file_lock import day_worker_lock as day_worker_lock


class DailyGenerationQueue:
    def __init__(self, database, now):
        self.database, self.now = database, now

    def pending_dates(self, limit: int = 64) -> tuple[str, ...]:
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT DISTINCT local_date FROM daily_generation_jobs
                WHERE status IN ('PENDING','FAILED_RETRYABLE')
                  AND attempts<attempt_limit AND automatic_retry_blocked=0
                  AND julianday(available_at)<=julianday(?)
                ORDER BY local_date DESC LIMIT ?""",
                (self.now().isoformat(), limit),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def observe(self, items, *, origin="RECENT_DAILY"):
        now = self.now().isoformat()
        created = []
        with self.database.transaction() as c:
            for item in items:
                state = item["classification"]
                if state == "NO_SOURCE":
                    continue
                job_id = canonical_json_sha256(
                    [
                        item["date"],
                        item["timezone"],
                        item["source_fingerprint"],
                        item["generation_version"],
                    ]
                )
                status = (
                    "SUCCEEDED"
                    if state in ("CURRENT_COMPLETE", "CURRENT_COMPLETE_EMPTY")
                    else "WAITING_UPSTREAM"
                    if state == "UPSTREAM_INCOMPLETE"
                    else "DIRTY_SOURCE_CHANGED"
                    if "Current day" in item["reason"]
                    else "PENDING"
                    if item["needs_backfill"]
                    else "FAILED_TERMINAL"
                )
                inserted = c.execute(
                    """INSERT OR IGNORE INTO daily_generation_jobs
                    (job_id,local_date,timezone,source_fingerprint,generation_version,status,
                     origin,available_at,summary_id,summary_revision,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        item["date"],
                        item["timezone"],
                        item["source_fingerprint"],
                        item["generation_version"],
                        status,
                        origin,
                        now,
                        item["summary_id"] if status == "SUCCEEDED" else None,
                        item["summary_revision"] if status == "SUCCEEDED" else None,
                        now,
                        now,
                    ),
                ).rowcount
                if inserted:
                    created.append(job_id)
                # Rollover/upstream completion do not reset existing attempt counters.
                if status == "PENDING":
                    c.execute(
                        """UPDATE daily_generation_jobs SET status='PENDING',updated_at=?
                        WHERE job_id=? AND status IN ('DIRTY_SOURCE_CHANGED','WAITING_UPSTREAM')
                        AND attempts<attempt_limit AND automatic_retry_blocked=0""",
                        (now, job_id),
                    )
        return created

    def recover_abandoned(self):
        # Caller holds the OS lock for this database. A previous RUNNING worker
        # cannot still be executing, even if its persisted lease was not expired.
        with self.database.transaction() as c:
            return c.execute(
                """UPDATE daily_generation_jobs SET
                status=CASE WHEN attempts>=attempt_limit OR automatic_retry_blocked=1
                  THEN 'FAILED_TERMINAL' ELSE 'FAILED_RETRYABLE' END,
                lease_owner=NULL,heartbeat_at=NULL,error='crash_before_completion',updated_at=?,available_at=?
                WHERE status='RUNNING'""",
                (self.now().isoformat(), self.now().isoformat()),
            ).rowcount

    def claim(self, items, *, allowed_dates=None, backfill=False):
        identities = {
            (i["date"], i["source_fingerprint"], i["generation_version"])
            for i in items
            if i["needs_backfill"]
            and (allowed_dates is None or i["date"] in allowed_dates)
        }
        now = self.now().isoformat()
        with self.database.transaction() as c:
            candidates = c.execute(
                """SELECT * FROM daily_generation_jobs
                WHERE status IN ('PENDING','FAILED_RETRYABLE') AND attempts<attempt_limit
                  AND automatic_retry_blocked=0 AND julianday(available_at)<=julianday(?)
                ORDER BY CASE origin WHEN 'RECENT_DAILY' THEN 0 ELSE 1 END,
                         local_date DESC,created_at,job_id""",
                (now,),
            ).fetchall()
            job = next(
                (
                    dict(r)
                    for r in candidates
                    if (
                        r["local_date"],
                        r["source_fingerprint"],
                        r["generation_version"],
                    )
                    in identities
                ),
                None,
            )
            if job is None:
                return None
            owner = uuid4().hex
            c.execute(
                """UPDATE daily_generation_jobs SET status='RUNNING',attempts=attempts+1,
                automatic_retry_blocked=?,lease_owner=?,heartbeat_at=?,updated_at=? WHERE job_id=?""",
                (int(backfill), owner, now, now, job["job_id"]),
            )
            job.update(
                status="RUNNING",
                attempts=job["attempts"] + 1,
                lease_owner=owner,
                automatic_retry_blocked=int(backfill),
            )
            return job

    def heartbeat(self, job):
        with self.database.transaction() as c:
            n = c.execute(
                """UPDATE daily_generation_jobs SET heartbeat_at=?,updated_at=?
                WHERE job_id=? AND status='RUNNING' AND lease_owner=?""",
                (
                    self.now().isoformat(),
                    self.now().isoformat(),
                    job["job_id"],
                    job["lease_owner"],
                ),
            ).rowcount
        if n != 1:
            raise RuntimeError("daily generation lease lost")

    def finish(self, job, result, *, success, retryable=True, error=None):
        status = (
            "SUCCEEDED"
            if success
            else "FAILED_RETRYABLE"
            if retryable
            and job["attempts"] < job["attempt_limit"]
            and not job["automatic_retry_blocked"]
            else "FAILED_TERMINAL"
        )
        now = self.now()
        with self.database.transaction() as c:
            n = c.execute(
                """UPDATE daily_generation_jobs SET status=?,summary_id=?,summary_revision=?,
                error=?,lease_owner=NULL,heartbeat_at=NULL,updated_at=?,available_at=?
                WHERE job_id=? AND status='RUNNING' AND lease_owner=?""",
                (
                    status,
                    result.get("summary_id") if success else None,
                    result.get("revision") if success else None,
                    error,
                    now.isoformat(),
                    (now + timedelta(minutes=5)).isoformat(),
                    job["job_id"],
                    job["lease_owner"],
                ),
            ).rowcount
        if n != 1:
            raise RuntimeError("daily generation completion lease lost")
        return status

    def retry(self, day, version):
        with self.database.transaction() as c:
            rows = c.execute(
                """SELECT job_id FROM daily_generation_jobs WHERE local_date=?
                AND generation_version=? AND status IN ('FAILED_TERMINAL','FAILED_RETRYABLE','WAITING_PROVIDER')
                ORDER BY updated_at DESC LIMIT 1""",
                (day, version),
            ).fetchall()
            if not rows:
                raise ValueError("No failed daily job to retry")
            c.execute(
                """UPDATE daily_generation_jobs SET status='PENDING',attempt_limit=attempts+2,
                automatic_retry_blocked=0,available_at=?,updated_at=? WHERE job_id=?""",
                (self.now().isoformat(), self.now().isoformat(), rows[0][0]),
            )

    def states(self):
        with self.database.read() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM daily_generation_jobs ORDER BY local_date DESC,created_at"
                )
            ]

    def hold_provider_outage(self, items):
        with self.database.transaction() as c:
            for item in items:
                c.execute(
                    """UPDATE daily_generation_jobs SET status='WAITING_PROVIDER',
                    automatic_retry_blocked=1,error='NOT_ATTEMPTED_PROVIDER_OUTAGE',updated_at=?
                    WHERE local_date=? AND timezone=? AND source_fingerprint=? AND generation_version=?
                    AND status IN ('PENDING','FAILED_RETRYABLE')""",
                    (
                        self.now().isoformat(),
                        item["date"],
                        item["timezone"],
                        item["source_fingerprint"],
                        item["generation_version"],
                    ),
                )

    @staticmethod
    def freeze_manifest(items):
        days = [
            i
            for i in items
            if i["classification"]
            in ("LEGACY_ONLY", "MISSING", "STALE_SOURCE", "FAILED_RESUMABLE")
            and i["needs_backfill"]
        ]
        manifest = {"days": sorted(days, key=lambda i: i["date"], reverse=True)}
        manifest["sha256"] = canonical_json_sha256(manifest["days"])
        return json.loads(json.dumps(manifest))
