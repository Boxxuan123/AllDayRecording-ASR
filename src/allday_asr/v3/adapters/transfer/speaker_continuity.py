"""Speaker inference is optional after durable ASR, never an ingest dependency."""

import logging


def infer_safely(core, session_id):
    try:
        return core.people.analyze(session_id)
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Speaker analysis unavailable; preserving transcript and continuing workflow"
        )
        # Native utterances start Unknown. Never erase trusted/manual identities
        # or retry profile learning as a condition of downstream availability.
        return {
            "session_id": session_id,
            "status": "degraded",
            "fallback_identity": "unknown",
            "error_kind": type(exc).__name__,
            "product_inference_executed": False,
            "transcript_preserved": True,
            "automatic_person_attribution": False,
        }
