"""Finite, product-only historical recovery. Dry-run is strictly read-only."""
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.product_self_gate import product_self_gate
from .durable_processing import CorrectUtteranceCommand, apply_utterance_correction

VERSION = 'historical-self-v1:product-self-v1:cam++-single-waveform-v2'


def query_snapshot(query):
    return json.loads(json.dumps({k: v for k, v in query.items() if k != 'tracks'} | {
        'query_inputs': [asdict(t) for t in query['tracks']]}))


def reason_code(query):
    reason = query.get('reason')
    if reason == 'overlapping_or_foreign_regular_turn':
        return 'FOREIGN_SPEAKER_OR_OVERLAP'
    if reason == 'not_contained_in_single_exclusive_turn':
        return 'OWNERSHIP_GATE'
    if reason == 'incomplete_or_mixed_query':
        codes = {e['reason'] for e in query.get('exclusions', [])}
        return 'INSUFFICIENT_SAFE_DURATION' if codes == {'below_minimum_useful_duration'} else 'MISSING_CAPTURE_MAPPING'
    return str(reason or 'LOW_SELF_SCORE').upper()


class HistoricalSelfBackfillService:
    def __init__(self, people, audio_store):
        self.people = people
        self.audio_store = audio_store
        self.factory = people._uow_factory

    def _inputs(self, uow, session):
        skip = uow.self_backfill.session_skip(session['session_id'])
        if skip:
            return [], Counter({skip: 1})
        queries = uow.people.product_self_queries(session['session_id'], self.people._artifact_root)
        selected, skips = [], Counter()
        for query in queries:
            skip = uow.self_backfill.utterance_skip(query['utterance_id'])
            if skip:
                skips[skip] += 1
            else:
                row = uow.evidence.get_utterance(query['utterance_id'])
                selected.append((query, row))
        return selected, skips

    def _audio(self, query):
        signatures = {}
        for track in query['tracks']:
            for clip in track.clips:
                path = self.audio_store.path_for(clip.storage_key)
                if not path.is_file():
                    return None
                stat = path.stat()
                signatures[clip.storage_key] = [stat.st_size, stat.st_mtime_ns]
        return signatures

    def dry_run(self, *, since=None, until=None, limit=100, session_id=None):
        if not 1 <= limit <= 100000:
            raise ValueError('session limit must be 1..100000')
        status = self.people._self_identity_matcher.status()
        items, skips, selected = [], Counter(), []
        with self.factory().reading() as uow:
            possible = uow.self_backfill.sessions(since, until, 100000, session_id)
            sessions = []
            eligible_sessions = 0
            available = bool(uow.people.self_person_id() and status.get('auto_identity_enabled'))
            for session in possible:
                sessions.append(session)
                inputs, excluded = self._inputs(uow, session)
                skips.update(excluded)
                selected.extend((session, q, row) for q, row in inputs)
                eligible_sessions += bool(inputs)
                if eligible_sessions >= limit:
                    break
        for session, query, row in selected:
            decisions = []
            audio = self._audio(query)
            reason = query.get('reason')
            if reason is None and audio is None:
                reason = 'MISSING_AUDIO'
            if reason is None and not available:
                reason = 'MATCHER_UNAVAILABLE'
            if reason is None:
                try:
                    embeddings = {e.speaker_track_id: e for e in self.people._provider.embed(query['tracks'])}
                    for track in query['tracks']:
                        duration = sum(c.source_end_ms-c.source_start_ms for c in track.clips)
                        e = embeddings.get(track.speaker_track_id)
                        evidence = self.people._self_identity_matcher.match(e).evidence if e else {'decision': 'unknown', 'reason': 'embedding_unavailable'}
                        decisions.append(dict(evidence, duration_ms=duration))
                except Exception as error:
                    # Partial scoring is never sufficient for automatic or review recovery.
                    decisions = []
                    reason = 'INFERENCE_FAILED:' + type(error).__name__
            identity, gate, count = product_self_gate(decisions, reason)
            decision, code = 'KEEP_UNKNOWN', reason_code(dict(query, reason=reason))
            if identity is SelfIdentity.SELF:
                decision, code = 'AUTO_SELF', 'ALL_PRODUCTION_WINDOWS_PASS'
            elif reason is None and count < 2 and any(d['decision'] == 'self' and d['duration_ms'] >= 2000 for d in decisions) and not any(d['decision'] == 'not_self' for d in decisions):
                decision, code = 'REVIEW_SELF_CANDIDATE', 'ONLY_ONE_VALID_WINDOW'
            elif reason is None and count < 2:
                code = 'INSUFFICIENT_SAFE_DURATION' if not count else 'LOW_SELF_SCORE'
            elif reason is None and any(d.get('reason') == 'insufficient_track_quality' for d in decisions):
                code = 'WINDOW_QUALITY_BELOW_PRODUCTION_MINIMUM'
            item = {
                'session_id': session['session_id'], 'utterance_id': row.utterance_id,
                'revision': row.revision, 'previous_identity': row.identity.value,
                'new_identity': 'self' if decision == 'AUTO_SELF' else row.identity.value,
                'duration_ms': row.end_ms-row.start_ms, 'day': session['captured_start'][:10],
                'decision': decision, 'reason_code': code, 'production_reason': gate,
                'window_decisions': decisions, 'eligible_window_count': count,
                'inputs': query_snapshot(query), 'audio_signatures': audio,
                'algorithm_version': VERSION,
            }
            item['item_id'] = canonical_json_sha256(item)
            items.append(item)
        plan = {'algorithm_version': VERSION, 'matcher': status,
                'cohort': {'since': since, 'until': until, 'limit': limit, 'session_id': session_id},
                'sessions': sessions, 'items': items, 'skips': dict(skips)}
        plan['run_id'] = canonical_json_sha256(plan)
        return plan

    def apply(self, plan):
        body = {k: v for k, v in plan.items() if k != 'run_id'}
        if plan['run_id'] != canonical_json_sha256(body) or plan['algorithm_version'] != VERSION:
            raise ValueError('backfill plan digest/version mismatch')
        with self.factory() as uow:
            prior = uow.self_backfill.run(plan['run_id'])
            if prior is not None:
                return prior
            if self.people._self_identity_matcher.status() != plan['matcher']:
                raise ValueError('self policy changed since dry-run')
            current = {}
            for session in plan['sessions']:
                queries, _ = self._inputs(uow, session)
                current.update({q['utterance_id']: q for q, _ in queries})
            # Validate the entire cohort before writing anything. Manual facts win.
            for item in plan['items']:
                query = current.get(item['utterance_id'])
                if query is None or query_snapshot(query) != item['inputs'] or self._audio(query) != item['audio_signatures']:
                    raise ValueError('backfill source/manual facts changed since dry-run')
            now = self.people._now()
            result = {'run_id': plan['run_id'], **metrics(plan),
                      'applied_self': sum(i['decision'] == 'AUTO_SELF' for i in plan['items']),
                      'created_reviews': sum(i['decision'] == 'REVIEW_SELF_CANDIDATE' for i in plan['items'])}
            uow.self_backfill.record_run(plan, result, now.isoformat())
            for item in plan['items']:
                if item['decision'] == 'AUTO_SELF':
                    row = uow.evidence.get_utterance(item['utterance_id'])
                    apply_utterance_correction(uow, CorrectUtteranceCommand(
                        row.utterance_id, row.revision, row.text, 'system:historical-self-backfill',
                        identity=SelfIdentity.SELF, change_identity=True), now)
                uow.self_backfill.record_item(plan['run_id'], item, now.isoformat())
            # Counts are derivable from the immutable plan; no mutable run updates.
            return result


def metrics(plan):
    items = plan['items']
    counts = Counter({'AUTO_SELF': 0, 'REVIEW_SELF_CANDIDATE': 0, 'KEEP_UNKNOWN': 0})
    counts.update(i['decision'] for i in items)
    distributions = {}
    for name, key in [('day', lambda i: i['day']), ('session', lambda i: hashlib.sha256(i['session_id'].encode()).hexdigest()[:12]),
                      ('duration_bucket', lambda i: '<2s' if i['duration_ms'] < 2000 else '2-4s' if i['duration_ms'] < 4000 else '4-6s' if i['duration_ms'] < 6000 else '6s+')]:
        rows = defaultdict(Counter)
        for item in items:
            rows[key(item)][item['decision']] += 1
        distributions[name] = {k: dict(v) for k, v in rows.items()}
    return {'eligible_sessions': len({i['session_id'] for i in items}), 'eligible_unknown': len(items),
            'audio_duration_ms': sum(i['duration_ms'] for i in items),
            'decisions': dict(counts), 'reasons': dict(Counter(i['reason_code'] for i in items)),
            'skips': plan['skips'], 'distributions': distributions}
