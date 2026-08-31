from allday_asr.v3.adapters.sqlite.migration_runner import V3Migration
from allday_asr.v3.adapters.sqlite.migrations.v001_core import SQL as V001_SQL
from allday_asr.v3.adapters.sqlite.migrations.v002_device_sync import (
    SQL as V002_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v003_processing import SQL as V003_SQL


MIGRATIONS = (
    V3Migration(version=1, name="v3_core", sql=V001_SQL),
    V3Migration(version=2, name="v3_device_sync", sql=V002_SQL),
    V3Migration(version=3, name="v3_durable_processing", sql=V003_SQL),
)

__all__ = ["MIGRATIONS"]
