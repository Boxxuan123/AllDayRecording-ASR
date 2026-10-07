from __future__ import annotations

import sqlite3
import json

from .people_repository_analysis import PeopleAnalysisRepositoryMixin
from .people_repository_catalog import PeopleCatalogRepositoryMixin
from .people_repository_clusters import PeopleClusterRepositoryMixin
from .people_repository_identity import PeopleIdentityRepositoryMixin
from .people_repository_prototypes import PeoplePrototypeRepositoryMixin
from .people_repository_support import PeopleRepositorySupportMixin
from .annotation_sample_repository import AnnotationSampleRepositoryMixin


class SqlitePeopleRepository(
    AnnotationSampleRepositoryMixin,
    PeopleAnalysisRepositoryMixin,
    PeopleCatalogRepositoryMixin,
    PeopleClusterRepositoryMixin,
    PeopleIdentityRepositoryMixin,
    PeoplePrototypeRepositoryMixin,
    PeopleRepositorySupportMixin,
):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        # Repository instances live for one UOW. Only immutable annotation choices
        # are cached; missing people are queried again after an in-batch creation.
        self._person_basics = {}
        self.cache_annotation_people = False

    def session_dataset_role(self, session_id: str) -> str | None:
        row = self.connection.execute(
            'SELECT dataset_role FROM session_dataset_roles WHERE session_id=?',
            (session_id,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def record_product_self_run(self, run_id: str, session_id: str,
                                model: str, model_version: str,
                                policy: dict, track_count: int,
                                created_at: str) -> None:
        self.connection.execute(
            """INSERT INTO speaker_cluster_runs
            (cluster_run_id,session_id,producer,model,model_version,policy_json,
             status,track_count,created_at,completed_at)
            VALUES (?,?,'product-self-inference',?,?,?,'succeeded',?,?,?)""",
            (run_id, session_id, model, model_version,
             json.dumps(policy), track_count, created_at, created_at),
        )

    def annotation_sample_snapshot(self, session_id: str):
        from .annotation_sample_snapshot import load_snapshot
        return load_snapshot(self.connection, session_id)

    def annotation_input_revision(self) -> int:
        row = self.connection.execute(
            "SELECT revision FROM annotation_input_revision WHERE singleton=1"
        ).fetchone()
        return int(row[0])

    def register_sample_candidates(self, plan, now: str) -> None:
        from .speaker_purity_repository import register_candidates
        register_candidates(self.connection, plan, now)

    def begin_identity_prediction(self, session_id: str) -> dict | None:
        from .speaker_research_reservations import record_prediction

        return record_prediction(self.connection, session_id)

    def speaker_event_provenance(self, utterance_id: str) -> dict:
        from .speaker_research_reservations import provenance

        return provenance(self.connection, utterance_id)
