"""Conservative input revision covering all SQL writers and new shared-media facts."""

TABLES = (
    "annotation_facts",
    "annotation_fact_audio",
    "utterances",
    "capture_segments",
    "audio_assets",
    "audio_replicas",
    "speaker_cluster_memberships",
    "person_cluster_links",
    "speaker_tracks",
    "speaker_clusters",
    "persons",
)
SQL = "CREATE TABLE annotation_input_revision(singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);\nINSERT INTO annotation_input_revision VALUES(1,0);\n"
for table in TABLES:
    for action in ("INSERT", "UPDATE", "DELETE"):
        SQL += f"CREATE TRIGGER sample_revision_{table}_{action.lower()} AFTER {action} ON {table} BEGIN UPDATE annotation_input_revision SET revision=revision+1 WHERE singleton=1; END;\n"
