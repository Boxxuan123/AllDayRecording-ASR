from allday_asr.v3.adapters.sqlite.migration_runner import V3Migration
from allday_asr.v3.adapters.sqlite.migrations.v001_core import SQL as V001_SQL
from allday_asr.v3.adapters.sqlite.migrations.v002_device_sync import (
    SQL as V002_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v003_processing import SQL as V003_SQL
from allday_asr.v3.adapters.sqlite.migrations.v004_unified_timeline import (
    SQL as V004_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v005_self_identity import SQL as V005_SQL
from allday_asr.v3.adapters.sqlite.migrations.v006_three_layer_knowledge import (
    SQL as V006_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v007_intelligent_reminders import (
    SQL as V007_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v008_open_speaker_identity import (
    SQL as V008_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v009_cross_day_person_memory import (
    SQL as V009_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v010_daily_insights import (
    SQL as V010_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v011_layered_speaker_identity import (
    SQL as V011_SQL,
)


MIGRATIONS = (
    V3Migration(version=1, name="v3_core", sql=V001_SQL),
    V3Migration(version=2, name="v3_device_sync", sql=V002_SQL),
    V3Migration(version=3, name="v3_durable_processing", sql=V003_SQL),
    V3Migration(version=4, name="v31_unified_timeline", sql=V004_SQL),
    V3Migration(version=5, name="v31_self_identity", sql=V005_SQL),
    V3Migration(version=6, name="v32_three_layer_knowledge", sql=V006_SQL),
    V3Migration(version=7, name="v33_intelligent_reminders", sql=V007_SQL),
    V3Migration(version=8, name="v34_open_speaker_identity", sql=V008_SQL),
    V3Migration(version=9, name="v35_cross_day_person_memory", sql=V009_SQL),
    V3Migration(version=10, name="v36_daily_insights", sql=V010_SQL),
    V3Migration(version=11, name="v37_layered_speaker_identity", sql=V011_SQL),
)

__all__ = ["MIGRATIONS"]
