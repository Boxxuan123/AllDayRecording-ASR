from allday_asr.v3.adapters.sqlite.migration_runner import V3Migration
from allday_asr.v3.adapters.sqlite.migrations.v001_core import SQL as V001_SQL


MIGRATIONS = (
    V3Migration(version=1, name="v3_core", sql=V001_SQL),
)

__all__ = ["MIGRATIONS"]
