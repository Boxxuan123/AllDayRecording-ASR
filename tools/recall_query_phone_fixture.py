"""Fictional query/answer bridge through the real Phone use case; no live data."""

import json
import sys
from pathlib import Path
from pytest import MonkeyPatch
from tests.test_recall_query_loop import env, summary_model

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
patch = MonkeyPatch()
fixture = env.__wrapped__(root, patch)
for line in sys.stdin:
    try:
        request = json.loads(line)
        mode = request.pop("fixture_model", "success")
        if mode == "timeout":
            fixture.service.model = lambda p, c: (_ for _ in ()).throw(TimeoutError())
        elif mode == "invalid":
            fixture.service.model = lambda p, c: {
                "claims": [
                    {"kind": "summary", "text": "伪造答案", "citations": ["wrong"]}
                ],
                "unknown": [],
            }
        elif mode == "insufficient":
            fixture.service.model = lambda p, c: {"claims": [], "unknown": ["证据不足"]}
        else:
            fixture.service.model = summary_model
        if request.pop("fixture_offline", False):
            fixture.source.failure = "SOURCE_OFFLINE"
        else:
            fixture.source.failure = None
        print(
            json.dumps(
                {"value": fixture.service.execute("phone", request)}, ensure_ascii=False
            ),
            flush=True,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
patch.undo()
