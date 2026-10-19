"""Bounded shared-kernel demonstration; synthetic changes are labeled separately."""

import json
from dataclasses import replace
from .contracts import SourceRecord, Grant
from .store import SourceStore
from .raw import RawIndex
from .contracts import digest


async def demonstrations(core, snapshot, grant, root):
    out = root / "demonstrations"
    out.mkdir(exist_ok=True)
    report_path = out / "report.json"
    report = (
        json.loads(report_path.read_text("utf-8"))
        if report_path.exists()
        else {"bank": core.backend.bank, "synthetic_changes": True, "steps": {}}
    )
    if report.get("completed"):
        return report

    def save():
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
        )

    # Real evidence: prior paired answers, then an evening view, then a natural follow-up.
    if "idempotent" not in report["steps"]:
        report["steps"]["idempotent"] = await core.ingest(
            [SourceRecord.native(r) for r in snapshot["records"]]
        )
    save()
    if "overview" not in report["steps"]:
        report["steps"]["overview"] = await core.overview(
            1791972000, 1791993600, snapshot["reference"], grant
        )
    save()
    examples = json.loads(
        (root / "evaluation-private/demonstrations.json").read_text("utf-8")
    )
    for name, question in examples["questions"]:
        if name in report["steps"]:
            continue
        retrieved = await core.retrieve(
            question, snapshot["reference"], grant, plan=False
        )
        report["steps"][name] = await core.answer(
            question, snapshot["reference"], grant, retrieved=retrieved
        )
        save()
    # Synthetic source has a separate namespace. It is not part of the real benchmark.
    stamp = snapshot["reference"] + 600
    r = SourceRecord(
        platform="synthetic",
        source_account_id="incremental-demo",
        conversation_id="demo",
        sender_account_id="planner",
        record_id="demo-1",
        revision=1,
        sent_at=stamp,
        timezone="Asia/Singapore",
        text="【合成演示】小林说：项目讨论暂定明天下午三点，还没有确认。",
        raw_text="【合成演示】小林说：项目讨论暂定明天下午三点，还没有确认。",
        is_from_self=False,
        speaker="小林",
        conversation_name="合成增量演示",
    )
    sg = Grant(frozenset({("synthetic", "incremental-demo")}), stamp, stamp + 1)
    if "add" not in report["steps"]:
        report["steps"]["add"] = await core.ingest([r])
        save()
    before = await core.backend.retrieve("合成演示 小林 项目讨论", stamp)
    (out / "synthetic-before.json").write_text(
        json.dumps(before, ensure_ascii=False, default=str), "utf-8"
    )
    revised = replace(
        r,
        revision=2,
        text="【合成演示】小林更正：讨论改到后天上午十点，仍未确认举行。",
        raw_text="【合成演示】小林更正：讨论改到后天上午十点，仍未确认举行。",
    )
    if "correct" not in report["steps"]:
        report["steps"]["correct"] = await core.ingest([revised])
        save()
    after = await core.backend.retrieve("合成演示 小林 讨论改期", stamp)
    (out / "synthetic-after.json").write_text(
        json.dumps(after, ensure_ascii=False, default=str), "utf-8"
    )
    report["steps"]["correct"]["current_revision"] = core.store.get(r.key).revision
    report["steps"]["correct"]["old_projection_absent"] = all(
        "明天下午三点" not in m["text"]
        for e in core.store.build_episodes()
        for m in e["records"]
        if m["key"] == r.key
    )
    report["steps"]["withdraw"] = await core.ingest(
        [replace(revised, revision=3, deleted=True)]
    )
    final = await core.backend.retrieve("合成演示 小林 讨论改期", stamp)
    (out / "synthetic-withdrawn.json").write_text(
        json.dumps(final, ensure_ascii=False, default=str), "utf-8"
    )
    report["steps"]["withdraw"]["authorized_records"] = len(core.store.records(sg))
    report["steps"]["withdraw"]["raw_hits"] = len(
        await core.raw.search("小林 讨论", sg)
    )
    report["completed"] = True
    save()
    return report


async def metamorphic(core, snapshot, grant, root):
    """No generation: real corpus raw-branch invariants, not an end-to-end Hindsight score."""
    out = root / "metamorphic-full-corpus"
    out.mkdir(exist_ok=True)
    rows = [SourceRecord.native(r) for r in snapshot["records"]]
    # Select full native ACM conversation windows, including the natural interference.
    settings = json.loads(
        (root / "evaluation-private/demonstrations.json").read_text("utf-8")
    )["metamorphic"]
    selected = list(rows)
    baseline = None
    report = {
        "scope": "entire fixed corpus including cross-conversation evidence; raw semantic/Chinese lexical branch only; Hindsight graph not re-extracted",
        "checks": [],
    }
    for variant in [
        "original",
        "directory_order",
        "rename_and_namespace",
        "density",
        "withdrawal",
    ]:
        transformed = list(selected)
        question = settings["question"]
        if variant == "directory_order":
            transformed = transformed[::-1]
        if variant == "rename_and_namespace":
            names = sorted(
                {r.speaker for r in selected if r.speaker}, key=len, reverse=True
            )
            mapping = {n: "代称" + str(i) for i, n in enumerate(names)}

            def rename(t, mapping=mapping):
                for old, new in mapping.items():
                    t = t.replace(old, new)
                return t.replace(settings["label"], settings["replacement"])

            transformed = [
                replace(
                    r,
                    source_account_id="mapped-owner",
                    conversation_id="mapped-" + digest(r.conversation_id)[:10],
                    sender_account_id="mapped-" + digest(r.sender_account_id)[:10],
                    record_id="mapped-" + r.record_id,
                    speaker=rename(r.speaker),
                    conversation_name=rename(r.conversation_name),
                    text=rename(r.text),
                    raw_text=rename(r.raw_text),
                )
                for r in selected
            ]
            question = rename(question)
        if variant == "density":
            anchor = selected[0]
            transformed += [
                replace(
                    anchor,
                    record_id="distractor-" + str(i),
                    conversation_id="noise-" + str(i // 20),
                    text="无关的电脑电源散热讨论。",
                    raw_text="无关的电脑电源散热讨论。",
                )
                for i in range(100)
            ]
        if variant == "withdrawal":
            transformed = [
                replace(r, revision=r.revision + 1, deleted=True)
                if "报名" in r.text
                else r
                for r in selected
            ]
        s = SourceStore(out / variant)
        s.update(transformed)
        index = RawIndex(s, core.backend)
        eps = s.build_episodes()
        await index.sync(eps)
        g = Grant(
            frozenset((r.platform, r.source_account_id) for r in transformed),
            grant.start,
            grant.end,
        )
        hits = await index.search(question, g)
        keys = {m["key"] for e in hits[:6] for m in e["records"]}
        target = {
            r.key
            for r in transformed
            if not r.deleted and ("报名" in r.text or "代码" in r.text)
        }
        row = {
            "variant": variant,
            "records": len(transformed),
            "fresh_embeddings": len(eps),
            "target_records": len(target),
            "top6_target_records": len(keys & target),
            "target_coverage": len(keys & target) / len(target) if target else None,
            "fingerprints": [e["fingerprint"] for e in eps],
        }
        if variant == "original":
            baseline = eps
        if variant == "directory_order":
            row["episode_invariance"] = eps == baseline
        if variant == "rename_and_namespace":
            row["no_old_vectors"] = not (
                {e["fingerprint"] for e in baseline} & {e["fingerprint"] for e in eps}
            )
        if variant == "withdrawal":
            row["withdrawn_not_returned"] = not any(
                "报名" in m["text"] for e in hits for m in e["records"]
            )
        report["checks"].append(row)
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
        )
    report["remaining_questions_retrieval_only"] = []
    for case in json.loads(
        (root / "evaluation-private/cases-multiwindow.json").read_text("utf-8")
    ):
        if case["id"] in {"D1", "D2"}:
            found = await core.retrieve(
                case["question"], case["reference"], grant, plan=False
            )
            report["remaining_questions_retrieval_only"].append(
                {
                    "id": case["id"],
                    "records": len(found["records"]),
                    "source_keys": [r.key for r in found["records"]],
                    "plan": found["plan"],
                    "traces": found["traces"],
                    "answer": "NOT_RUN_BUDGET_EXHAUSTED",
                }
            )
    # A wrong upstream relation cannot introduce a source or establish identity.
    bad = core.store.episode("nonexistent-fabricated-related-document")
    report["wrong_relation_source_resolution"] = bad is None
    report["limitation"] = (
        "Wrong relation source-handle rejection only; semantic attribution under misleading relations still needs end-to-end evaluation."
    )
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    return report
