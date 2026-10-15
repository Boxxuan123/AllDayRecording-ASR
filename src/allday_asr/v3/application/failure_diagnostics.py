"""Correlated diagnostics without exception messages or source payloads."""
import traceback


class ProviderOutputInvalid(ValueError):
    """A provider response failed the bounded protocol/grounding validator."""


def causes(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def failure_category(error):
    for cause in causes(error):
        name = type(cause).__name__
        if name == 'GenerationSourceConflict':
            return 'source_conflict', False
        if name == 'ProviderOutputInvalid':
            return 'provider_invalid_output', True
        if name in {'ModelExecutionTerminal', 'DailyExecutionTerminal', 'RemoteStartLimitReached'}:
            return 'budget_terminal', False
        if 'Cancel' in name:
            return 'cancelled', False
        if isinstance(cause, (TypeError, ValueError, AssertionError, KeyError)):
            return 'invalid_input' if isinstance(cause, ValueError) else 'program_error', False
        if isinstance(cause, TimeoutError):
            return 'provider_retryable', True
        if isinstance(cause, OSError):
            return 'storage_failure', True
    return 'provider_failure', True


def log_failure(logger, error, **correlation):
    stacks = '\n'.join(
        type(cause).__name__ + '\n' + '\n'.join(f'File {frame.filename}, line {frame.lineno}, in {frame.name}' for frame in traceback.extract_tb(cause.__traceback__))
        for cause in causes(error)
    )
    logger.error('failure category=%s context=%s stack=%s', failure_category(error)[0], correlation, stacks)
