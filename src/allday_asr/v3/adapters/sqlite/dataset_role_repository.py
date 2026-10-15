"""SQLite facts for pure dataset isolation policy."""
from allday_asr.v3.domain import dataset_roles


def session_role(connection, session_id):
    row = connection.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (session_id,)).fetchone()
    return row[0] if row else None


def require_learning(connection, session_id):
    dataset_roles.require_learning(session_role(connection, session_id))


def is_learning(connection, session_id):
    return dataset_roles.is_learning(session_role(connection, session_id))
