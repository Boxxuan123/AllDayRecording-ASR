"""Canonical serialization and scope identity for the V1 read projection."""

import hashlib
import json


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def scope_key(scope):
    return hashlib.sha256(packed(scope).encode()).hexdigest()
