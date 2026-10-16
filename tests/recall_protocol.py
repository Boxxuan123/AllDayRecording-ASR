"""Adapt frozen fake drivers by exact current-input identity, never guess references."""
from copy import deepcopy


def model_handles(value, payload):
    mapping = {"chat:"+r["record_id"]:r["id"] for r in payload["records"] if r.get("record_id")}
    mapping.update({o["native_ref"]:o["ref"] for o in payload.get("objects",[]) if o.get("native_ref")})
    mapping.update({o["key"]:o["ref"] for o in payload.get("objects",[]) if o.get("key") and o["type"]=="contact"})
    def visit(item):
        if isinstance(item,str): return mapping.get(item,item)
        if isinstance(item,list): return [visit(v) for v in item]
        if isinstance(item,dict): return {k:visit(v) for k,v in item.items()}
        return item
    return visit(deepcopy(value))
