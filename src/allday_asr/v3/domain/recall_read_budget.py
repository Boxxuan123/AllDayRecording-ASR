"""Per-query decoded cache-body budget; database pages remain timeout bounded."""
from contextvars import ContextVar
from contextlib import contextmanager
import json

CURRENT = ContextVar("recall_body_read_budget",default=None)


class ReadBudgetExceeded(RuntimeError): pass


class ReadBudget:
    def __init__(self): self.records=0; self.bytes=0
    def limit(self, requested=400):
        left=400-self.records
        if left<=0: raise ReadBudgetExceeded("CACHE_RECORD_BUDGET_REACHED")
        return min(requested,left)
    def read(self,value):
        size=len((value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)).encode())
        if self.records>=400 or self.bytes+size>524288:
            raise ReadBudgetExceeded("CACHE_BODY_BUDGET_REACHED")
        self.records+=1; self.bytes+=size
        return value


def scan_limit(requested=400):
    value=CURRENT.get()
    return value.limit(requested) if value else requested


def charge(value):
    budget=CURRENT.get()
    return budget.read(value) if budget else value


@contextmanager
def reading(records=0, bytes_read=0):
    budget=ReadBudget(); budget.records=records; budget.bytes=bytes_read
    token=CURRENT.set(budget)
    try: yield budget
    finally: CURRENT.reset(token)
