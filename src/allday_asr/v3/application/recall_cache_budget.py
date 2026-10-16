"""Attach decoded-body accounting to the normal recall query, including early returns."""
from allday_asr.v3.domain.recall_read_budget import reading


def query(service,owner,payload,selected=None,prepared=None):
    carried=(prepared or {}).get("_carried_metrics",{})
    with reading(carried.get("cache_scan_records",0),carried.get("cache_scan_bytes",0)) as budget:
        result=service._query(owner,payload,selected,prepared)
        counts={"cache_scan_records":budget.records,"cache_scan_bytes":budget.bytes,
                "cache_scan_limit":400,"cache_scan_byte_limit":524288}
        if "metrics" in result: result["metrics"].update(counts)
        with service.lock:
            saved=service.results.get((owner,payload.get("query_id")))
            if saved: saved[1]["metrics"].update(counts)
        return result
