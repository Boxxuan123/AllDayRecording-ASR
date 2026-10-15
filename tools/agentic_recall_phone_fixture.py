"""Fictional JSON-lines phone bridge. No production data or real model calls."""

import json
import sys
from pathlib import Path
from pytest import MonkeyPatch
from tests.test_agentic_recall import lab, investigator

patch = MonkeyPatch()
env = lab.__wrapped__(Path(sys.argv[1]), patch)
env.service.investigator = investigator(env)
for line in sys.stdin:
    try:
        request = json.loads(line)
        value = env.service.execute("phone", request)
        print(json.dumps({"value": value}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), flush=True)
patch.undo()
