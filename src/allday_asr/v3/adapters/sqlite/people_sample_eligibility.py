"""Keep immutable voice samples out of matching when their source is excluded."""
from .sound_eligibility import usable_sample

def usable_voice_sample(alias: str, *, require_current: bool = True) -> str:
    # Call sites supply fixed SQL aliases, never external input.
    return f"""NOT EXISTS (SELECT 1 FROM annotation_sample_sets sample_set
      WHERE sample_set.prototype_id IN ({alias}.prototype_id,{alias}.source_prototype_id)
      AND (({1 if require_current else 0}=1 AND sample_set.current=0 AND {alias}.status='candidate') OR EXISTS (
        SELECT 1 FROM json_each(sample_set.facts_json) authorized
        JOIN annotation_facts fact ON fact.fact_id=authorized.value WHERE fact.state!='active')))
      AND NOT EXISTS (SELECT 1 FROM annotation_sample_revocations revoked
      WHERE revoked.prototype_id IN ({alias}.prototype_id, {alias}.source_prototype_id)) AND NOT EXISTS (
      SELECT 1 FROM json_each({alias}.representative_clips_json) clip
      LEFT JOIN utterances evidence ON evidence.utterance_id = json_extract(clip.value, '$.utterance_id')
      WHERE EXISTS (
        SELECT 1 FROM annotation_fact_audio anchor JOIN annotation_facts fact ON fact.fact_id=anchor.fact_id
        WHERE fact.state IN ('active','conflict','revoked') AND anchor.media_id=json_extract(clip.value,'$.media_id')
          AND anchor.start_ms < json_extract(clip.value,'$.end_ms')
          AND anchor.end_ms > json_extract(clip.value,'$.start_ms')
          AND ((fact.dimension='person' AND (fact.state='conflict' OR
            COALESCE(json_extract(fact.value_json,'$'),'') != COALESCE({alias}.person_id,
              (SELECT person_id FROM person_cluster_links WHERE cluster_id={alias}.cluster_id AND status='active' LIMIT 1), '')))
            OR (fact.dimension='sound' AND (fact.state='conflict' OR
              json_extract(fact.value_json,'$') NOT IN ('speech','live_speech'))))
      ) OR (evidence.utterance_id IS NOT NULL AND (
        evidence.speaker_track_id IS NOT {alias}.speaker_track_id
        OR evidence.status != 'active'
        OR (NOT {usable_sample('evidence')} AND NOT EXISTS (
          SELECT 1 FROM json_each(evidence.evidence_json,'$.annotation_fact_ids.sound') projected
          JOIN annotation_facts fact ON fact.fact_id=projected.value WHERE fact.state='superseded'))
      )) OR EXISTS (
        SELECT 1 FROM capture_segments seg
        JOIN audio_assets asset ON asset.asset_id = seg.asset_id
        JOIN utterances excluded ON excluded.session_id = seg.session_id
        WHERE asset.media_id = json_extract(clip.value, '$.media_id')
          AND seg.session_id = (SELECT session_id FROM speaker_tracks WHERE speaker_track_id = {alias}.speaker_track_id)
          AND excluded.status = 'active'
          AND NOT {usable_sample('excluded')}
          AND NOT EXISTS (SELECT 1 FROM json_tree(excluded.evidence_json,'$.annotation_fact_ids') projected
            JOIN annotation_facts fact ON fact.fact_id=projected.value WHERE fact.state='superseded')
          AND json_extract(clip.value, '$.end_ms') > seg.source_start_ms
          AND json_extract(clip.value, '$.start_ms') < seg.source_start_ms + seg.session_end_ms - seg.session_start_ms
          AND excluded.start_ms < MIN(seg.session_end_ms, seg.session_start_ms + json_extract(clip.value, '$.end_ms') - seg.source_start_ms)
          AND excluded.end_ms > MAX(seg.session_start_ms, seg.session_start_ms + json_extract(clip.value, '$.start_ms') - seg.source_start_ms)
      )
    )"""
