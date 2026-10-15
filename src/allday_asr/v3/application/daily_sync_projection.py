"""One wire projection for content generation and local invalidity updates."""


def daily_summary_resource(value):
    return {k: value[k] for k in ('summary_id', 'revision', 'summary_date', 'timezone',
                                 'status', 'derivation_status', 'created_at', 'provenance')} | {
        'objective': {k: v for k, v in value['objective'].items() if k != 'events'}}


def publish_daily_summary(uow, value, *, actor=None):
    resource = daily_summary_resource(value)
    latest = uow.changes.latest('daily_summary', value['summary_id'])
    if latest is not None and latest.operation.value == 'upsert' and latest.payload == resource:
        return False
    sequence = uow.changes.append('daily_summary', value['summary_id'], value['revision'], 'upsert', resource)
    if actor is not None:
        uow.audit.append('daily.sync_projection.repaired', actor, 'daily_summary', value['summary_id'],
                         {'revision': value['revision'], 'derivation_status': value['derivation_status'],
                          'sequence': sequence, 'previous_sequence': latest.sequence if latest else None})
    return True
