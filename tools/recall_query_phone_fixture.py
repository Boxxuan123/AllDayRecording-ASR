"""Fictional query/answer bridge through the real Phone use case; no live data."""

import json
import sys
from pathlib import Path
from pytest import MonkeyPatch
from tests.test_recall_query_loop import env, summary_model
from tests.test_recall_contacts import native

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
patch = MonkeyPatch()
fixture = env.__wrapped__(root / "unified", patch)
native_fixture = native.__wrapped__(root / "native", patch)
native_model = native_fixture.service.model
for line in sys.stdin:
    try:
        request = json.loads(line)
        active = native_fixture if request.pop("fixture_native", False) else fixture
        mode = request.pop("fixture_model", "success")
        if mode == "timeout":
            active.service.model = lambda p, c: (_ for _ in ()).throw(TimeoutError())
        elif mode == "invalid":
            active.service.model = lambda p, c: {
                "claims": [
                    {"kind": "summary", "text": "伪造答案", "citations": ["wrong"]}
                ],
                "unknown": [],
            }
        elif mode == "insufficient":
            active.service.model = lambda p, c: {"claims": [], "unknown": ["证据不足"]}
        else:
            active.service.model = (
                native_model if active is native_fixture else summary_model
            )
        if request.pop("fixture_offline", False):
            active.source.failure = "SOURCE_OFFLINE"
        else:
            active.source.failure = None
        print(
            json.dumps(
                {"value": active.service.execute("phone", request)}, ensure_ascii=False
            ),
            flush=True,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
patch.undo()
