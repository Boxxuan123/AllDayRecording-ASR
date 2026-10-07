"""Synthetic history cost and source-change scope; no production data or model work."""

from datetime import datetime, timedelta, timezone
from time import perf_counter

import pytest

from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.adapters.sqlite.daily_inventory import DailyHistoryInventory
from allday_asr.v3.application.daily_automation import DailyGenerationCoordinator


@pytest.mark.parametrize("history_days", (7, 90, 365))
def test_one_changed_day_reads_one_inventory_range(tmp_path, history_days):
    database = V3Database.open(tmp_path / "inventory.sqlite3")
    first = datetime(2025, 1, 1, tzinfo=timezone.utc)
    changed_index = min(299, history_days - 1)
    with database.transaction() as connection:
        for index in range(history_days):
            started = first + timedelta(days=index)
            ended = started + timedelta(hours=1)
            connection.execute(
                """INSERT INTO recording_sessions
                (session_id,captured_start,captured_end,timezone,state,revision,status_code,
                 current_stage,progress,created_at,updated_at)
                VALUES (?,?,?,'Asia/Singapore','sealed',1,'available',NULL,1,?,?)""",
                (f"synthetic-{index:04d}", started.isoformat(), ended.isoformat(),
                 started.isoformat(), ended.isoformat()),
            )
    # Existing history predates this incremental run. The migration seeds only
    # recent dates; a single later write is the sole historical dirty source.
    with database.transaction() as connection:
        connection.execute("DELETE FROM daily_dirty_ranges")
        connection.execute(
            "UPDATE recording_sessions SET revision=2 WHERE session_id=?",
            (f"synthetic-{changed_index:04d}",),
        )
    changed_date = (first + timedelta(days=changed_index)).date().isoformat()
    inventory = DailyHistoryInventory(
        database, "synthetic-version",
        now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    start = perf_counter()
    affected = inventory.scan(only_dates=(changed_date,))
    elapsed_ms = (perf_counter() - start) * 1000
    assert [item["date"] for item in affected] == [changed_date]
    assert inventory.last_scan_stats["sessions"] == 1
    assert inventory.last_scan_stats["utterance_rows"] == 0
    assert inventory.last_scan_stats["audio_rows"] == 0
    assert elapsed_ms >= 0
    print(f"DAILY_PERF days={history_days} sessions_read=1 utterances_read=0 "
          f"audio_rows_read=0 elapsed_ms={elapsed_ms:.2f}")
    with database.read() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM daily_dirty_ranges
            WHERE first_date<=? AND last_date>=?""",
            (changed_date, changed_date),
        ).fetchone()[0] >= 1
    coordinator = DailyGenerationCoordinator(
        database, lambda _: None, "synthetic-version",
        now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    scan = coordinator.inventory.scan
    scanned_dates = []

    def observe_scope(**kwargs):
        scanned_dates.extend(kwargs.get("only_dates") or ())
        return scan(**kwargs)

    coordinator.inventory.scan = observe_scope
    coordinator.catch_up()
    assert changed_date in scanned_dates
    assert len(set(scanned_dates)) <= 3
    assert database.daily_dirty_ranges() == ()
