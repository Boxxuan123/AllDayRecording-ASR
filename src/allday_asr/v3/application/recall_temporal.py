"""Use the existing retrieval path for one bounded adjacent natural day."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from allday_asr.v3.domain.recall_temporal import nearby_scope, time_notice
from .recall_retrieval import expand


def nearby_dialogue(service, result, plan, cancel, deadline):
    if result["evidence"]:
        return
    scope = result["scope"]
    adjacent = nearby_scope(scope, result["_request"])
    if adjacent is None:
        return
    requested = deepcopy(scope)
    scope.update(adjacent)
    rows, more = service.sources.chat(
        scope, plan["terms"], 100, cancel, min(deadline, time.monotonic() + 3)
    )
    result["evidence"].extend(rows)
    if result["_request"].get("expand", True):
        expand(service, result, scope, plan, cancel, deadline)
    if result["evidence"]:
        result["requested_scope"] = requested
        result["time_notice"] = time_notice(requested, scope)
        result["coverage"].append(result["time_notice"])
        for bound in ("sent_from", "sent_to"):
            scope[bound + "_local"] = datetime.fromtimestamp(
                scope[bound], ZoneInfo(scope["timezone"])
            ).isoformat()
    else:
        scope.clear()
        scope.update(requested)
