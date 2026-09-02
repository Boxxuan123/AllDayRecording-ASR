from __future__ import annotations

import sqlite3

from .people_repository_analysis import PeopleAnalysisRepositoryMixin
from .people_repository_catalog import PeopleCatalogRepositoryMixin
from .people_repository_clusters import PeopleClusterRepositoryMixin
from .people_repository_identity import PeopleIdentityRepositoryMixin
from .people_repository_prototypes import PeoplePrototypeRepositoryMixin
from .people_repository_support import PeopleRepositorySupportMixin


class SqlitePeopleRepository(
    PeopleAnalysisRepositoryMixin,
    PeopleCatalogRepositoryMixin,
    PeopleClusterRepositoryMixin,
    PeopleIdentityRepositoryMixin,
    PeoplePrototypeRepositoryMixin,
    PeopleRepositorySupportMixin,
):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
