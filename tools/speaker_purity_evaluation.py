"""Frozen retrospective diagnostics and four-way shadow abstention simulation."""

from collections import defaultdict

import numpy as np

from speaker_purity_pipeline import (
    AUDIT,
    OLD,
    V2,
    build,
    fit_clean_thresholds,
    query_features,
    read,
    source_key,
    sources,
    subwindows,
    unit,
    write,
    sha256,
)
from analyze_speaker_profile_purity import (
    frozen_queries,
    refs_current,
    score_rows,
    metrics,
)
from allday_asr.v3.application.query_purity import probe


def identity_metrics(rows, known, self_id):
    result = metrics(rows, known)
    for name in (
        "known",
        "correct",
        "false_reject",
        "wrong_person",
        "unknown",
        "unknown_false_accept",
        "unknown_reject",
    ):
        result.setdefault(name, 0)
    result["self_to_other"] = sum(
        r["truth"] == self_id and r["prediction"] not in (None, self_id) for r in rows
    )
    by_event = defaultdict(list)
    for row in rows:
        by_event[row["session"]].append(row)
    result["session_event_summary"] = {
        "events": len(by_event),
        "events_with_wrong_assignment": sum(
            any(
                r["prediction"] is not None and r["prediction"] != r["truth"]
                for r in values
            )
            for values in by_event.values()
        ),
        "note": "Correlated query views are not independent events; counts above are query-level diagnostics.",
    }
    return result


def evaluate(connection, raw, out, frozen):
    if not (out / "shadow-profile.json").exists():
        build(connection, raw, out)
    shadow = read(out / "shadow-profile.json")
    # A profile artifact is only valid while its review references remain current.
    source_rows = sources(connection, raw)
    by_key = {r["source_key"]: r for r in source_rows}
    eligible = {r["source_key"]: r for r in shadow["eligible_pool"]}
    contamination = []
    for key, item in eligible.items():
        from allday_asr.v3.domain.speaker_purity import source_ineligibility

        row = by_key.get(key)
        reason = (
            "missing_source"
            if row is None
            else source_ineligibility(
                row["evidence"],
                item["target_person_id"],
                audio_available=row["audio_available"],
            )
        )
        if reason or (row and row["evidence"].evidence_id != item["evidence_id"]):
            contamination.append(
                {"source_key": key, "reason": reason or "changed_review"}
            )
    if contamination:
        raise RuntimeError(
            "shadow profile has stale/ineligible evidence; rebuild required"
        )
    run_ids = {r["audit_run_id"] for r in source_rows}
    if len(run_ids) != 1:
        raise RuntimeError(
            "multiple audit snapshots require explicit reconciliation before evaluation"
        )
    # Use the actual audit's DB snapshot, not arbitrary current eligible profiles.
    import json

    graph = json.loads(
        connection.execute(
            "SELECT source_snapshot_json FROM speaker_profile_purity_runs WHERE audit_run_id=?",
            (next(iter(run_ids)),),
        ).fetchone()[0]
    )
    known = set(frozen["known_ids"])
    legacy = refs_current(connection, graph, known)
    clean = defaultdict(list)
    for center in shadow["centers"]:
        if center["person_id"] in known:
            clean[center["person_id"]].append(unit(center["vector"]))
    if len(clean) < 2:
        raise RuntimeError(
            "insufficient clean comparator; never fall back to mixed enrollment"
        )
    manifest = read(V2 / "manifest.json")
    ws = {r["id"]: r for r in manifest["windows"]}
    cached = dict(np.load(OLD / "vectors.npz"))
    controlled = frozen_queries(manifest, ws, cached, "test")
    for q in controlled:
        q["source_windows"] = [
            {
                "media_id": ws[key]["media_id"],
                "start_ms": ws[key]["start_ms"],
                "end_ms": ws[key]["end_ms"],
            }
            for key in q["windows"]
        ]
    tracks = []
    for track in read(V2 / "track-level-results.json")["tracks"]:
        if track["status"] != "fully_labelled_single_person":
            continue
        row = connection.execute(
            "SELECT vector_json,speaker_track_id,cluster_id FROM voice_prototypes WHERE prototype_id=?",
            (track["prototype_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("frozen query prototype missing")
        tracks.append(
            {
                "id": track["prototype_id"],
                "truth": track["truth_id"],
                "session": track["session_id"],
                "date": track["date"],
                "windows": track["clips"],
                "source_windows": [
                    w
                    | {
                        "speaker_track_id": row["speaker_track_id"],
                        "cluster_id": row["cluster_id"],
                    }
                    for w in track["clips"]
                ],
                "duration_s": track["duration_s"],
                "quality": 1.0,
                "vector": unit(json.loads(row["vector_json"])),
            }
        )
    sets = {"controlled": controlled, "track": tracks}
    all_queries = controlled + tracks
    raw.prepare(
        [w for q in all_queries for r in q["source_windows"] for w in subwindows(r)]
    )
    feature_gold = read(out / "query-purity-features.json")
    # Exclude the whole component when the query shares session OR any media.
    group_sessions, group_media = defaultdict(set), defaultdict(set)
    gold_by_key = {r["source_key"]: r for r in feature_gold}
    for row in source_rows:
        group = gold_by_key[row["source_key"]]["group"]
        group_sessions[group].add(row["source_session_id"])
        group_media[group].add(row["source_media_id"])
    mapping = []
    for query in all_queries:
        windows = [w for r in query["source_windows"] for w in subwindows(r)]
        for w in windows:
            w["embedding"] = raw.vector(w).tolist()
        features = query_features(windows)
        media = {w["media_id"] for w in windows}
        excluded = {
            g
            for g in group_sessions
            if query["session"] in group_sessions[g] or media & group_media[g]
        }
        training = [r for r in feature_gold if r["group"] not in excluded]
        thresholds = fit_clean_thresholds(training)
        query["purity"] = probe(
            features, thresholds, minimum_duration_s=frozen["minimum_speech_seconds"]
        )
        matched = [
            by_key.get(source_key(w["media_id"], w["start_ms"], w["end_ms"]))
            for w in query["source_windows"]
        ]
        verdicts = [r["evidence"].verdict for r in matched if r]
        if "mixed_overlap" in verdicts:
            truth = "mixed_overlap"
        elif "boundary_cross" in verdicts:
            truth = "boundary_cross"
        elif all(matched) and all(
            r["evidence"].verdict == "clean_single"
            and r["evidence"].primary_person_id == query["truth"]
            for r in matched
        ):
            truth = "clean_single"
        else:
            truth = None
        mapping.append(
            {
                "query_id": query["id"],
                "exact_reviewed_windows": sum(r is not None for r in matched),
                "total_source_windows": len(matched),
                "query_level_gold": truth,
                "mapping": "direct positive contamination evidence"
                if truth in ("mixed_overlap", "boundary_cross")
                else "all exact windows individually verified clean"
                if truth
                else "window-only or no query-level gold",
                "excluded_training_groups": sorted(excluded),
                "training_sources": len(training),
                "thresholds": thresholds,
                "features": features,
                "probe": query["purity"],
                "windows": windows,
            }
        )
    write(out, "query-benchmark-mapping.json", mapping)
    verification = read(AUDIT / "verification.json")
    self_id, focus_id = verification["self_person_id"], verification["focus_person_id"]
    ablation = {
        "purpose": "retrospective diagnostic only; suspicious query abstention is shadow simulation",
        "filter": "abstain only SUSPICIOUS; INSUFFICIENT retains legacy decision",
        "sets": {},
    }
    failure_cases = []
    legacy_vs_clean = {}
    for name, queries in sets.items():
        outcomes = {}
        for label, refs, filtered in (
            ("A", legacy, False),
            ("B", clean, False),
            ("C", legacy, True),
            ("D", clean, True),
        ):
            rules = {}
            for rule in ("G", "P"):
                # Avoid embedding arrays in score_rows' copied metadata.
                prepared = [
                    {k: v for k, v in q.items() if k != "source_windows"}
                    for q in queries
                ]
                scored = score_rows(prepared, refs, frozen, per_person=rule == "P")
                for r in scored:
                    r["legacy_identity_prediction"] = r["prediction"]
                    if filtered and r["purity"]["status"] == "SUSPICIOUS":
                        r["prediction"] = None
                        r["shadow_abstained"] = True
                rules[rule] = {
                    "metrics": identity_metrics(scored, known, self_id),
                    "rows": scored,
                }
                failure_cases.extend(
                    {"set": name, "arm": label, "rule": rule, **r}
                    for r in scored
                    if r["prediction"] is not None and r["prediction"] != r["truth"]
                )
            outcomes[label] = rules
        ablation["sets"][name] = outcomes
        legacy_vs_clean[name] = {
            label: outcomes[arm]
            for label, arm in (("legacy", "A"), ("purity_shadow", "B"))
        }
    write(out, "four-way-ablation.json", ablation)
    write(
        out,
        "legacy-vs-clean-results.json",
        {"label": "retrospective diagnostic only", "sets": legacy_vs_clean},
    )
    write(out, "failure-cases.json", failure_cases)
    cv = read(out / "query-purity-evaluation.json")
    cv["shadow_identity_abstention_effect"] = {
        name: {
            rule: {arm: outcomes[arm][rule]["metrics"] for arm in ("A", "B", "C", "D")}
            for rule in ("G", "P")
        }
        for name, outcomes in ablation["sets"].items()
    }
    cv["deployment_query_metrics_note"] = (
        "Identity queries have partial purity gold; source-clip CV metrics must not be treated as complete query-level accuracy."
    )
    write(out, "query-purity-evaluation.json", cv)
    event_info = read(AUDIT / "failure-nearest-neighbors.json")["failure_events"]
    event_ids = {e["failure_id"] for e in event_info}
    event_queries = [q for q in tracks if q["id"] in event_ids]
    original = {
        arm: {
            rule: [
                r
                for r in ablation["sets"]["track"][arm][rule]["rows"]
                if r["id"] in event_ids
            ]
            for rule in ("G", "P")
        }
        for arm in ("A", "B", "C", "D")
    }
    oracle = []
    for query in event_queries:
        ranges = [
            r
            for r in query["source_windows"]
            if (s := by_key.get(source_key(r["media_id"], r["start_ms"], r["end_ms"])))
            and s["evidence"].verdict == "clean_single"
            and s["evidence"].primary_person_id == self_id
        ]
        raw.prepare(ranges)
        windows = [w for r in ranges for w in subwindows(r)]
        for w in windows:
            w["embedding"] = raw.vector(w).tolist()
        feature = query_features(windows)
        training = [
            r
            for r in feature_gold
            if query["session"] not in group_sessions[r["group"]]
            and not ({w["media_id"] for w in windows} & group_media[r["group"]])
        ]
        if not ranges:
            continue
        oracle.append(
            {
                k: v
                for k, v in query.items()
                if k not in ("source_windows", "vector", "purity")
            }
            | {
                "windows": ranges,
                "duration_s": sum(w["end_ms"] - w["start_ms"] for w in ranges) / 1000,
                "vector": unit(np.mean([raw.vector(w) for w in ranges], axis=0)),
                "purity": probe(feature, fit_clean_thresholds(training)),
                "features": feature,
            }
        )
    oracle_scores = {
        label: {
            rule: score_rows(oracle, refs, frozen, per_person=rule == "P")
            for rule in ("G", "P")
        }
        for label, refs in (("legacy", legacy), ("purity_shadow", clean))
    }
    prior = read(AUDIT / "failure-comparison.json")
    # Prior artifacts are the permanent local fixture; only aggregate assertions published.
    previous = prior["comparison"]
    write(
        out,
        "historical-regression-fixture.json",
        {
            "fixture_version": 1,
            "event_count": 1,
            "correlated_query_ids": sorted(event_ids),
            "frozen_matcher": frozen,
            "legacy_expected_focus_accepts_G": 2,
            "clean_expected_focus_accepts_G": 0,
            "clean_self_expected_accepts_G": 0,
            "previous_diagnostic": previous,
            "original_diagnostic_sha256": sha256(AUDIT / "failure-comparison.json"),
            "profile_snapshot_hash": verification["profile_snapshot_hash"],
            "event_sources": [
                {"query_id": e["failure_id"], "source_keys": e["query_sources"]}
                for e in event_info
            ],
        },
    )
    assertions = {
        "legacy_G_retains_prior_behavior": sum(
            r["prediction"] == focus_id for r in original["A"]["G"]
        )
        == 2,
        "shadow_G_removes_contamination_effect": all(
            r["prediction"] != focus_id for r in original["B"]["G"]
        ),
        "frozen_P_rejects_original_views": all(
            r["prediction"] is None for a in ("A", "B") for r in original[a]["P"]
        ),
        "clean_self_queries_rejected": all(
            r["prediction"] is None for a in oracle_scores.values() for r in a["G"]
        ),
        "no_ineligible_shadow_source": not contamination,
        "all_event_views_present": len(event_queries) == len(event_ids) == 3
        and len(oracle) == 3,
    }
    historical = {
        "independent_event_count": 1,
        "correlated_query_views": len(event_queries),
        "original_queries": original,
        "human_verified_clean_self_sensitivity": oracle_scores,
        "assertions": assertions,
        "regression_passed": all(assertions.values()),
        "note": "Oracle clean-self recrops are separate sensitivity analysis; C/D are automatic probe abstention, not oracle filtering.",
    }
    write(out, "historical-regression.json", historical)
    coverage = read(out / "clean-profile-coverage.json")["people"]
    primary_metrics = cv["metrics"][cv["primary_rule"]]
    # Cross-session signal is reported, not turned into a production switch.
    signal = cv["groups"] >= 2 and (
        primary_metrics["mixed_overlap"]["detection_rate"] or 0
    ) > (primary_metrics["false_suspicious_rate"] or 0)
    gates = {
        "enrollment_contamination_count": len(contamination),
        "clean_profile": "NEEDS_MORE_CLEAN_DATA",
        "frozen_known_subset_coverage_ready": all(
            r["profile_ready"] for r in coverage if r["person_id"] in known
        ),
        "query_purity": "PURITY_PROBE_NOT_READY",
        "cross_session_signal": signal,
        "canary_overall": "PURITY_PROBE_NOT_READY",
        "reason": "Some core profiles have only one session or no enrollment. Pure-self false accepts increase on controlled diagnostics. "
        "Clip gold and historical CV do not establish safe query-level generalization; no production switch",
        "production_mode": "legacy",
    }
    write(out, "rollout-gates.json", gates)
    private_report(out, coverage, cv, ablation, historical, gates)
    print(
        f"Regression: {historical['regression_passed']}; query gate: {gates['query_purity']}",
        flush=True,
    )


def private_report(out, coverage, cv, ablation, historical, gates):
    lines = [
        "# Enrollment Purity Gate — Private Report",
        "",
        "Retrospective diagnostic only. Production remains legacy.",
        "",
        "| Person | Legacy | Clean | Excluded | Sessions | Dates | Duration(s) | Ready |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in coverage:
        lines.append(
            f"| {r['person']} | {r['legacy_sources']} | {r['clean_sources']} | {r['excluded_contamination']} | "
            f"{r['clean_sessions']} | {r['clean_dates']} | {r['clean_duration_s']:.3f} | {r['profile_ready']} |"
        )
    lines += [
        "",
        "Readiness uses the existing quality gate plus a conservative two-session diversity recommendation. "
        "A one-session seed is not claimed to be a robust profile. Self query reviews are not reused as enrollment.",
        "",
        "## Query probe",
        "",
        f"Validation: {cv['method']}; {cv['groups']} session/media components. "
        "Thresholds fit only training clean clips using a predeclared 95% clean retention target. "
        "No future blind evidence is read and no historical event is specially tuned.",
        "",
        "| Rule | Clean retention | Mixed detection | Boundary detection | False suspicious |",
        "|---|---:|---:|---:|---:|",
    ]

    def rate(value):
        return "N/A" if value is None else f"{value:.1%}"

    for rule, m in cv["metrics"].items():
        lines.append(
            f"| {rule} | {rate(m['clean_retention'])} | {rate(m['mixed_overlap']['detection_rate'])} | "
            f"{rate(m['boundary_cross']['detection_rate'])} | {rate(m['false_suspicious_rate'])} |"
        )
    lines += [
        "",
        "Wrong-primary can be internally consistent; this identity-agnostic probe cannot establish correct identity. "
        "Uncertain sources remain excluded even if internally consistent. PASS is a consistency signal, not a human purity grant. "
        "Clip-level gold is not complete query-level gold; see query-benchmark-mapping.json.",
        "",
        "## Four-way ablation",
        "",
        "C/D abstain suspicious queries; insufficient queries keep the legacy identity decision. "
        "The matcher, query vectors, split, G/P and margin are frozen.",
        "",
        "| Set | Arm | Rule | Known correct/total | Wrong known | Unknown FA/total | Self→other |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    m = cv["metrics"][cv["primary_rule"]]
    # Include both denominators; insufficient is neither suspicious nor pass.
    lines.insert(
        lines.index("## Four-way ablation"),
        f"Primary rule: clean PASS {m['clean_single']['status_counts'].get('PASS', 0)}/{m['clean_single']['total']}; "
        f"clean insufficient {m['clean_single']['status_counts'].get('INSUFFICIENT', 0)}/{m['clean_single']['total']}; "
        f"among assessable clips retention {rate(m['clean_single']['conditional_pass_rate'])}. "
        f"Mixed detection among assessable clips {rate(m['mixed_overlap']['conditional_detection_rate'])}.",
    )
    lines.insert(lines.index("## Four-way ablation"), "")
    for name, arms in ablation["sets"].items():
        for arm, rules in arms.items():
            for rule, result in rules.items():
                m = result["metrics"]
                lines.append(
                    f"| {name} | {arm} | {rule} | {m['correct']}/{m['known']} | {m['wrong_person']} | "
                    f"{m['unknown_false_accept']}/{m['unknown']} | {m['self_to_other']} |"
                )
    lines += [
        "",
        "A = legacy+legacy; B = clean+legacy; C = legacy+probe abstention; D = clean+probe abstention. "
        "Full current profiles and retrospective test audio can share provenance and dates. "
        "These are diagnostics, not unbiased production accuracy estimates.",
        "",
        "## Historical event",
        "",
        f"One independent event, {historical['correlated_query_views']} correlated views. "
        f"Permanent private regression passed: {historical['regression_passed']}.",
        "",
    ]
    for arm, rules in historical["original_queries"].items():
        lines.append(
            f"{arm}: "
            + "; ".join(
                f"{rule} predictions {[r['prediction'] for r in rows]} / purity "
                f"{[r['purity']['status'] for r in rows]}"
                for rule, rows in rules.items()
            )
        )
    lines += [
        "",
        "Clean-self-only query sensitivity is reported separately and uses individually heard windows. "
        "It changes query composition and must not be counted as independent new events.",
        "",
        "## Source coverage concentration",
        "",
    ]
    oracle = historical["human_verified_clean_self_sensitivity"]["legacy"]["G"]
    lines += [
        "Pure-self query probe statuses: "
        + str([r["purity"]["status"] for r in oracle])
        + ". "
        "Their mean pairwise cosine and centroid distance improve relative to the mixed views, "
        "but the current rule still falsely flags them; the probe is not ready.",
        "",
    ]
    for r in coverage:
        if r["legacy_sources"]:
            lines.append(
                f"{r['person']}: per-session clean counts {r['sources_per_session']}; dates {r['dates']}; excluded {r['excluded']}."
            )
    lines += [
        "",
        "## Rollout",
        "",
        f"Clean profile: {gates['clean_profile']}. Query purity: {gates['query_purity']}. "
        f"Cross-session signal: {gates['cross_session_signal']}. Overall: {gates['canary_overall']}.",
        "",
        "Next stage: acquire independent, fully reviewed query-level validation before a separately authorized canary. "
        "Keep legacy profiles and frozen matcher available for rollback. No canary is activated here.",
    ]
    (out / "private-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
