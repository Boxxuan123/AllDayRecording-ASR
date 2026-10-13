"""JSON-lines bridge for the existing Phone repository tests; synthetic data only."""

import json
import sys
import hashlib
from pathlib import Path
from tests.test_history_recall import env, dump

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
service, cache, db, records, source = env.__wrapped__(root)
for line in sys.stdin:
    try:
        request = json.loads(line)
        if request.get("action") == "fingerprint":
            value = {
                "core": hashlib.sha256(dump(db.path).encode()).hexdigest(),
                "chat": hashlib.sha256(dump(cache.path).encode()).hexdigest(),
            }
        else:
            value = service.execute("phone", request)
        print(json.dumps({"value": value}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
