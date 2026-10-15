"""PC-private relevance decisions; excluded evidence creates no personal event."""

SQL = """
CREATE TABLE chat_followup_admissions (
 source_key TEXT NOT NULL, dataset TEXT NOT NULL, conversation_key TEXT NOT NULL,
 evidence_digest TEXT NOT NULL, decision TEXT NOT NULL, origin TEXT NOT NULL,
 review_ref TEXT NOT NULL, value_json TEXT NOT NULL,
 PRIMARY KEY(source_key,dataset)
);
"""
