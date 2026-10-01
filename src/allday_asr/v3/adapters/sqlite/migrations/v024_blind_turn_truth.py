"""Append dimensions; do not reinterpret or rewrite any legacy human verdict."""

SQL = """
ALTER TABLE blind_ground_truth ADD COLUMN review_schema_version INTEGER NOT NULL DEFAULT 1
 CHECK(review_schema_version IN (1,2));
ALTER TABLE blind_ground_truth ADD COLUMN speaker_composition TEXT
 CHECK(speaker_composition IN ('clean_single','simultaneous_overlap','sequential_multi_speaker','backchannel','uncertain'));
ALTER TABLE blind_ground_truth ADD COLUMN boundary_quality TEXT
 CHECK(boundary_quality IN ('clean','cut','uncertain'));
CREATE TRIGGER validate_blind_truth_dimensions BEFORE INSERT ON blind_ground_truth
WHEN NEW.review_schema_version != COALESCE((
 SELECT json_extract(q.query_json,'$.review_schema_version') FROM blind_review_tasks t
 JOIN blind_query_views q USING(query_id) WHERE t.task_id=NEW.task_id),1)
 OR (NEW.review_schema_version=1 AND (NEW.speaker_composition IS NOT NULL OR NEW.boundary_quality IS NOT NULL))
 OR (NEW.review_schema_version=2 AND (NEW.purity IS NOT NULL OR
      (NEW.action='submit' AND (NEW.speaker_composition IS NULL OR NEW.boundary_quality IS NULL))))
BEGIN SELECT RAISE(ABORT,'blind review schema mismatch'); END;
"""
