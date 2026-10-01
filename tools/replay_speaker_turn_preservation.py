"""Read-only private structural replay. Never creates prospective Blind evidence."""
import argparse
import json
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from audit_speaker_turn_boundaries import Audit, attributed_tokens, open_readonly, sha  # noqa: E402
from allday_asr.v3.adapters.models.native_projection import _group_utterances, _group_utterances_legacy  # noqa: E402
from allday_asr.v3.domain.blind_windows import plan_source_windows  # noqa: E402
from allday_asr.v3.domain.speaker_turns import foreign_turns  # noqa: E402
from allday_asr.v3.adapters.sqlite.blind_queries import automatic_queries  # noqa: E402
from allday_asr.v3.domain.speaker_turns import QUERY_BUILDER_VERSION  # noqa: E402


def write(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--database", required=True, type=Path)
    p.add_argument("--audit", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    c = open_readonly(args.database)
    audit = Audit(c, args.database)
    patterns = json.loads((args.audit / "backchannel-patterns.json").read_text(encoding="utf-8"))["patterns"]
    selected = [r for r in patterns if r["outcome"] == "downstream_remerged"]
    projected, projection_checks = {}, []
    for run in sorted({r["run_id"] for r in selected}):
        data = audit.run(run)
        turns = data["diarization"]["exclusive_turns"]
        tokens = attributed_tokens(data["asr"]["primary_tokens"], turns, data["threshold"])
        before = _group_utterances_legacy(tokens)
        after = _group_utterances(tokens, turns)
        projected[run] = (before, after)
        ordered = sorted(tokens, key=lambda t:(t["start_ms"],t["end_ms"]))
        signature = [(t["start_ms"],t["end_ms"],t.get("speaker"),t["source_refs"]) for t in ordered if str(t["text"])]
        provenance = [(t["start_ms"],t["end_ms"],t["attributed_speaker"],t["source_refs"])
                      for g in after for t in g["token_attributions"]]
        projection_checks.append(dict(run_id=run, input_tokens=len(signature),
            output_tokens=sum(g["token_count"] for g in after), provenance_exact=signature == provenance,
            text_preserved= "".join(g["text"].replace(" ","") for g in before)
                            == "".join(g["text"].replace(" ","") for g in after),
            foreign_labelled_hulls=sum(bool(foreign_turns(g["start_ms"],g["end_ms"],g["speaker"],turns)) for g in after)))
    aba = []
    for r in selected:
        before, after = projected[r["run_id"]]
        b, label = r["b"], r["a"]["speaker_label"]
        def bridges(groups, b=b, label=label):
            return [g for g in groups if g["speaker"] == label and
                    g["start_ms"] < b["start_ms"] and g["end_ms"] > b["end_ms"]]
        aba.append(dict(candidate=r, before=bridges(before), after=bridges(after),
            after_neighbor_groups=[g for g in after if g["start_ms"] < r["a_resumes"]["end_ms"]
                                    and g["end_ms"] > r["a"]["start_ms"]]))
    old_boundary = json.loads((args.audit / "boundary-cases.json").read_text(encoding="utf-8"))
    boundaries = []
    for row in old_boundary:
        u = row["source_utterance"]
        run = c.execute("SELECT run_id FROM utterances WHERE utterance_id=?", (u["utterance_id"],)).fetchone()[0]
        data = audit.run(run)
        track = audit.tracks[u["original_speaker_track_id"]]
        vad = []
        for h in data["asr"].get("hypotheses", []):
            if h.get("role") == "primary":
                vad.extend((a+h["analysis_start_ms"],b+h["analysis_start_ms"])
                           for a,b in h.get("raw_response",{}).get("speech_ranges_ms",[]))
        result = plan_source_windows(u,track["label"],track["speaker_track_id"],
            data["diarization"]["exclusive_turns"],data["asr"]["primary_tokens"],vad,
            audit.captures(track["session_id"]), "legacy-unversioned-diagnostic")
        reasons = [w["provenance"]["end_boundary_reason"] for w in result["windows"]]
        first = result["windows"][0] if result["windows"] else None
        boundaries.append(dict(before=row, after=result,
            natural_boundary=bool(first and first["provenance"]["end_boundary_reason"] in {"speaker_turn","utterance","token","vad"}),
            multiple_safe_windows=len(result["windows"])>1,
            cross_capture_continuation=len({w["media_id"] for w in result["windows"]})>1,
            still_hard_cap="hard_cap" in reasons,
            foreign_window_count=sum(bool(foreign_turns(w["session_start_ms"],w["session_end_ms"],
                track["label"],data["diarization"]["exclusive_turns"])) for w in result["windows"])))
    # Full query composition replay on historical projection is diagnostic only.
    queries = []
    for run in projected:
        sid = c.execute("SELECT session_id FROM processing_runs WHERE run_id=?",(run,)).fetchone()[0]
        old = automatic_queries(c,sid,"historical-replay",run)
        new = automatic_queries(c,sid,"historical-replay",run,builder_version=QUERY_BUILDER_VERSION,
                                artifact_root=args.database.parent/"artifacts")
        assert new == automatic_queries(c,sid,"historical-replay",run,builder_version=QUERY_BUILDER_VERSION,
                                       artifact_root=args.database.parent/"artifacts")
        queries.append(dict(run_id=run,before=old,after=new))
    summary = dict(aba_before=sum(bool(r["before"]) for r in aba),aba_after=sum(bool(r["after"]) for r in aba),
        boundary_cases=len(boundaries),natural_boundary=sum(r["natural_boundary"] for r in boundaries),
        multiple_safe_windows=sum(r["multiple_safe_windows"] for r in boundaries),
        cross_capture_continuation=sum(r["cross_capture_continuation"] for r in boundaries),
        still_hard_cap=sum(r["still_hard_cap"] for r in boundaries),
        foreign_windows=sum(r["foreign_window_count"] for r in boundaries),
        end_reasons=dict(Counter(w["provenance"]["end_boundary_reason"] for r in boundaries for w in r["after"]["windows"])),
        projection_checks=projection_checks, classifications_overlap=True,
        replay_read_only=True, historical_only=True, acoustic_truth="unverified_model_candidates",
        latest_mixed_first_bad_layer="UNKNOWN")
    summary["all_replayed_query_foreign_windows"] = sum(bool(foreign_turns(
        w["session_start_ms"],w["session_end_ms"],
        audit.tracks[q["speaker_track_id"]]["label"],
        audit.run(q["run_id"])["diarization"]["exclusive_turns"]))
        for r in queries for q in r["after"] for w in q["windows"])
    assert len(selected)==18 and summary["aba_before"]==18 and summary["aba_after"]==0
    assert len(boundaries)==13 and summary["foreign_windows"]==0
    assert summary["all_replayed_query_foreign_windows"] == 0 and c.total_changes == 0
    assert all(r["provenance_exact"] and r["text_preserved"] and r["foreign_labelled_hulls"]==0 for r in projection_checks)
    c.rollback()
    c.close()
    assert all(sha(path)==expected for path,expected in audit.input_hashes.items())
    write(args.output,"projection-before-after.json",dict(checks=projection_checks,cases=aba))
    write(args.output,"aba-regression.json",dict(summary=summary,cases=aba))
    write(args.output,"boundary-regression.json",dict(summary=summary,cases=boundaries))
    write(args.output,"window-before-after.json",dict(boundaries=boundaries,full_query_replay=queries))
    write(args.output,"structural-regression.json",summary)
    print(json.dumps(summary | {"projection_checks":"saved privately"},indent=2))


if __name__ == "__main__":
    main()
