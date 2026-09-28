"""Join device monotonic events and receiver durations; never subtract device/PC clocks."""

import argparse
import json
import re
import math
import statistics
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("root", type=Path)
p.add_argument("--phone", required=True)
p.add_argument("--samples", required=True)
p.add_argument("--receiver", required=True)
a = p.parse_args()
root = a.root
lines = (root / a.phone).read_text(encoding="utf-8-sig", errors="replace").splitlines()
samples = json.loads((root / a.samples).read_text())
evidence = json.loads((root / a.receiver / "evidence.json").read_text())
server_lines = (root / a.receiver / "timings.log").read_text().splitlines()


def wall(line):
    m = re.match(r"\d\d-\d\d (\d\d):(\d\d):(\d\d)\.(\d{3})", line)
    return (
        sum(int(v) * k for v, k in zip(m.groups(), [3600000, 60000, 1000, 1], strict=True))
        if m
        else None
    )


phases = {}
offsets = []
requests = {}
auth = []
for line in lines:
    m = re.search(r"phase=(T\d[^ ]*) tick_ms=(\d+) count=\d+ ops=(\S+)", line)
    if m:
        offsets.append(wall(line) - int(m[2]))
        for op in m[3].split(","):
            phases.setdefault(op, {})[m[1]] = int(m[2])
    m = re.search(
        r"\[sync\] id=(\S+) connection=\S+ op=(\S+) phase=request start method=\S+ path=(\S+)",
        line,
    )
    if m:
        requests.setdefault(m[1], {}).update(op=m[2], path=m[3], wall=wall(line))
    m = re.search(r"\[sync\] id=(\S+) phase=response status=\d+ ms=(\d+)", line)
    if m:
        requests.setdefault(m[1], {})["http_ms"] = int(m[2])
    m = re.search(r"\[sync\] id=(\S+) reused=(\S+).*native_ms=([\d.-]+)", line)
    if m:
        requests.setdefault(m[1], {}).update(reused=m[2], native_ms=float(m[3]))
    m = re.search(r"\[sync\] op=(\S+) path=(\S+) phase=authentication ms=(\d+)", line)
    if m:
        auth.append(dict(op=m[1], path=m[2], ms=int(m[3]), wall=wall(line)))
offset = statistics.median(offsets)
server = {x["client_id"]: x for x in evidence["requests"]}
batches = []
for line in server_lines:
    m = re.search(
        r"T4-commit ops=(\S+) count=(\d+) service_ms=([\d.]+) wait_ms=([\d.]+) transaction_ms=([\d.]+)",
        line,
    )
    if m:
        batches.append(
            dict(
                ops=m[1].split(","),
                count=int(m[2]),
                service=float(m[3]),
                wait=float(m[4]),
                transaction=float(m[5]),
            )
        )
for sample in samples:
    ids = {
        op
        for op, v in phases.items()
        if sample["start"] <= v.get("T0-local-commit", -1) <= sample["visible"]
    }
    sample["observed_operations"] = len(ids)
    bs = [b for b in batches if ids.intersection(b["ops"])]
    sample["write_wait_ms"] = sum(b["wait"] for b in bs)
    sample["business_ms"] = sum(b["service"] - b["wait"] for b in bs)
    sample["transaction_ms"] = sum(b["transaction"] for b in bs)
    commits = {
        (v.get("T4-response-decoded"), v.get("T5-receipt-commit"))
        for op, v in phases.items()
        if op in ids
    }
    sample["phone_commit_ms"] = sum(
        end - start for start, end in commits if start is not None and end is not None
    )
    ui = {
        (v.get("T5-receipt-commit"), v.get("T6-ui-confirmed"))
        for op, v in phases.items()
        if op in ids
    }
    sample["ui_refresh_ms"] = sum(
        end - start for start, end in ui if start is not None and end is not None
    )
    last = max(
        (v.get("T6-ui-confirmed", 0) for op, v in phases.items() if op in ids),
        default=0,
    )
    sample["layout_after_ui_ms"] = sample["visible"] - last if last else None
    rs = {
        key: v
        for key, v in requests.items()
        if v.get("path") in ["/device/v3/sync", "/api/v1/status"]
        and sample["start"] <= v.get("wall", -1) - offset <= sample["visible"]
    }
    sample["authentication_ms"] = sum(
        x["ms"] for x in auth if x["op"] in {r["op"] for r in rs.values()}
    ) + sum(server.get(k, {}).get("auth_ms", 0) for k in rs)
    sample["transport_estimate_ms"] = sum(
        max(0, v["native_ms"] - server[k]["ms"])
        for k, v in rs.items()
        if k in server and v.get("native_ms", -1) >= 0
    )
    sample["phone_callback_queue_ms"] = sum(
        max(0, v["http_ms"] - v["native_ms"])
        for v in rs.values()
        if "http_ms" in v and v.get("native_ms", -1) >= 0
    )
    sample["request_ids"] = list(rs)
fields = [
    "elapsed",
    "authentication_ms",
    "write_wait_ms",
    "business_ms",
    "transport_estimate_ms",
    "phone_callback_queue_ms",
    "phone_commit_ms",
    "ui_refresh_ms",
    "layout_after_ui_ms",
]


def stats(values):
    values = sorted(v for v in values if v is not None)
    return (
        {
            "n": len(values),
            "p50": round(statistics.median(values), 2),
            "p95": round(values[math.ceil(len(values) * 0.95) - 1], 2),
            "max": round(max(values), 2),
        }
        if values
        else None
    )


summary = {}
for scenario, count in sorted({(s["scenario"], s["count"]) for s in samples}):
    group = [s for s in samples if s["scenario"] == scenario and s["count"] == count]
    summary[f"{scenario}_{count}"] = {
        field: stats([s[field] for s in group]) for field in fields
    }
result = {
    "summary": summary,
    "samples": samples,
    "clock_offset_ms": offset,
    "native_reuse": sum(v.get("reused") == "true" for v in requests.values()),
    "requests_with_native_metric": sum("native_ms" in v for v in requests.values()),
    "workers": len(evidence["workers"]),
    "model_calls": len(evidence.get("model_calls", [])),
}
(root / "acceptance-analysis.json").write_text(json.dumps(result, indent=2))
print(json.dumps(summary, indent=2))
