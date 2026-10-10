from .v031_chat_followups import SQL as V031_SQL
from .v030_sample_queue_age import SQL as V030_SQL
from .v016_sample_input_revision import SQL as V016_SQL
from .v017_coalesced_sample_queue import SQL as V017_SQL
from .v018_manifest_revisions import SQL as V018_SQL
from .v019_page_read_indexes import SQL as V019_SQL
from .v020_timeline_paging import SQL as V020_SQL
from .v021_speaker_profile_purity import SQL as V021_SQL
from .v022_enrollment_purity import SQL as V022_SQL
from .v023_blind_reservation import SQL as V023_SQL
from .v024_blind_turn_truth import SQL as V024_SQL
from .v025_speaker_research_reservation import SQL as V025_SQL
from .v026_historical_self_backfill import SQL as V026_SQL
from .v027_daily_projection import SQL as V027_SQL
from .v028_daily_generation_queue import SQL as V028_SQL
from .v029_daily_dirty_dates import SQL as V029_SQL
from .v015_annotation_receipts import SQL as V015_SQL
from .v013_annotation_facts import SQL as V013_SQL
from .v014_annotation_samples import SQL as V014_SQL
from allday_asr.v3.adapters.sqlite.migration_runner import V3Migration
from allday_asr.v3.adapters.sqlite.migrations.v012_session_sync_identity import SQL as V012_SQL
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
    V3Migration(version=12, name="phone_session_sync_identity", sql=V012_SQL),
    V3Migration(version=13, name="versioned_audio_annotations", sql=V013_SQL),
    V3Migration(version=14, name="durable_annotation_samples", sql=V014_SQL),
    V3Migration(version=16, name="sample_input_revision", sql=V016_SQL),
    V3Migration(version=15, name="annotation_receipt_recovery", sql=V015_SQL),
    V3Migration(version=17, name="coalesced_sample_queue", sql=V017_SQL),
    V3Migration(version=18, name="session_manifest_revisions", sql=V018_SQL),
    V3Migration(version=19, name="page_read_indexes", sql=V019_SQL),
    V3Migration(version=20, name="timeline_paging", sql=V020_SQL),
    V3Migration(version=21, name="speaker_profile_purity_evidence", sql=V021_SQL),
    V3Migration(version=22, name="enrollment_purity_sources", sql=V022_SQL),
    V3Migration(version=23, name="blind_session_reservation", sql=V023_SQL),
    V3Migration(version=24, name="blind_turn_truth_dimensions", sql=V024_SQL),
    V3Migration(version=25, name="prospective_speaker_research_reservation", sql=V025_SQL,
                contract_visible=False),
    V3Migration(version=26, name="historical_self_identity_review", sql=V026_SQL,
                contract_visible=False),
    V3Migration(version=27, name="daily_projection_cursor", sql=V027_SQL),
    V3Migration(version=28, name="durable_daily_generation", sql=V028_SQL,
                contract_visible=False),
    V3Migration(version=29, name="incremental_daily_inventory", sql=V029_SQL,
                contract_visible=False),
    V3Migration(version=30, name="sample_queue_age", sql=V030_SQL, contract_visible=False),
    V3Migration(31, "chat_followup_provenance", V031_SQL, contract_visible=False),
)

__all__ = ["MIGRATIONS"]
