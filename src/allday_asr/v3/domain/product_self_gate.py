"""The shared production window gate; review never changes this decision."""
from .identity import SelfIdentity


def independent_source_windows(windows):
    """Two captures cannot turn the same physical audio frames into two votes."""
    return not any(a['sha256'] == b['sha256'] and a['start_ms'] < b['end_ms']
                   and b['start_ms'] < a['end_ms']
                   for i, a in enumerate(windows) for b in windows[i+1:])


def product_self_gate(decisions, reason=None):
    eligible = [d for d in decisions if d['duration_ms'] >= 2000]
    identity = SelfIdentity.UNKNOWN
    if reason is None:
        if len(eligible) < 2:
            reason = 'insufficient_clean_windows'
        elif all(d['decision'] == 'self' for d in decisions):
            identity, reason = SelfIdentity.SELF, 'all_clean_windows_above_self_threshold'
        elif all(d['decision'] == 'not_self' for d in decisions):
            identity, reason = SelfIdentity.NOT_SELF, 'all_clean_windows_below_not_self_threshold'
        else:
            reason = 'window_disagreement_or_unknown_band'
    return identity, reason, len(eligible)
