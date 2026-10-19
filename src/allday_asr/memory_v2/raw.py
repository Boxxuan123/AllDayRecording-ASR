"""Original-text semantic + Chinese lexical branch, within the same retrieve port."""

import json
import math
import time
from collections import Counter
import jieba
import numpy as np


def tokenize(text):
    # Search-mode Jieba splits Chinese subwords and preserves mixed Latin terms.
    return [
        t.lower()
        for t in jieba.cut_for_search(text)
        if t.strip() and any(c.isalnum() for c in t)
    ]


def text(ep):
    return "\n".join(
        f"{r['speaker']} ({r['conversation']}): {r['text']}" for r in ep["records"]
    )


class RawIndex:
    def __init__(self, store, backend):
        self.store = store
        self.backend = backend
        with store.connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY,fingerprint TEXT,body TEXT)"
            )

    async def sync(self, episodes):
        with self.store.connect() as c:
            existing = {
                r[0]: r[1] for r in c.execute("SELECT id,fingerprint FROM vectors")
            }
        changed = [e for e in episodes if existing.get(e["id"]) != e["fingerprint"]]
        start = time.monotonic()
        for offset in range(0, len(changed), 4):
            batch = changed[offset : offset + 4]
            vectors = await self.backend.embeddings.encode_documents(
                [text(e) for e in batch]
            )
            with self.store.connect() as c:
                for ep, vec in zip(batch, vectors, strict=True):
                    c.execute(
                        "INSERT OR REPLACE INTO vectors VALUES(?,?,?)",
                        (ep["id"], ep["fingerprint"], json.dumps(list(vec))),
                    )
        self.backend.record(
            "raw_embeddings", start, {"changed_documents": len(changed)}
        )

    async def search(self, query, grant, limit=20):
        episodes = [
            e
            for e in self.store.build_episodes()
            if all(grant.allows(self.store.get(r["key"])) for r in e["records"])
        ]
        if not episodes:
            return []
        with self.store.connect() as c:
            vecs = {
                r[0]: (r[1], json.loads(r[2]))
                for r in c.execute("SELECT id,fingerprint,body FROM vectors")
            }
        episodes = [
            e
            for e in episodes
            if e["id"] in vecs and vecs[e["id"]][0] == e["fingerprint"]
        ]
        if not episodes:
            return []
        q = np.array((await self.backend.embeddings.encode_query([query]))[0])
        matrix = np.array([vecs[e["id"]][1] for e in episodes])
        semantic = (matrix @ q) / (
            np.linalg.norm(matrix, axis=1) * np.linalg.norm(q) + 1e-12
        )
        tokens = [Counter(tokenize(text(e))) for e in episodes]
        lengths = [sum(t.values()) for t in tokens]
        avg = sum(lengths) / len(lengths) or 1
        lexical = []
        for tok, length in zip(tokens, lengths, strict=True):
            score = 0.0
            for term in set(tokenize(query)):
                df = sum(term in doc for doc in tokens)
                tf = tok[term]
                score += (
                    math.log(1 + (len(tokens) - df + 0.5) / (df + 0.5))
                    * tf
                    * 2.2
                    / (tf + 1.2 * (0.25 + 0.75 * length / avg))
                )
            lexical.append(score)
        fused = {}
        for values in (semantic, lexical):
            for rank, i in enumerate(
                sorted(
                    range(len(episodes)), key=lambda i: (-values[i], episodes[i]["id"])
                )
            ):
                fused[i] = fused.get(i, 0) + 1 / (60 + rank)
        return [
            {
                **episodes[i],
                "raw_score": fused[i],
                "semantic_score": float(semantic[i]),
                "lexical_score": lexical[i],
            }
            for i in sorted(fused, key=lambda i: (-fused[i], episodes[i]["id"]))[:limit]
        ]
