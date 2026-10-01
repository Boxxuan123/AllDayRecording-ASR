"""Few development-only hypotheses; all predictions stay in offline JSON."""

from pathlib import Path

import numpy as np

from short_self_dataset import BINS, digest, read, write
from short_self_evidence import distribution, validate_manifest

RULES = (
    "current_two_window",
    "single_normal",
    "high_score",
    "score_margin",
    "crop_stability",
    "min_crop",
    "enrollment_consensus",
)


def fit(output):
    output = Path(output)
    path = output / "candidate-rules.json"
    if path.exists() or (output / "holdout-evidence.json").exists():
        raise ValueError("rules already frozen or holdout already inspected")
    manifest = validate_manifest(output)
    dev = read(output / "development-evidence.json")
    t = dev["matcher"]["self_threshold"]
    short = [
        e
        for e in dev["events"]
        if 2000 <= e["duration_ms"] < 4000 and e["safe_ownership"]
    ]
    positives = [e for e in short if e["truth"] == "self" and "score" in e["full"]]
    negatives = [e for e in short if e["truth"] == "non-self" and "score" in e["full"]]
    if not positives or len(negatives) < 5:
        raise ValueError(
            "insufficient development positives/negatives even for retrospective hypotheses"
        )
    high = max(t, max(e["full"]["score"] for e in negatives) + 0.02)
    gaps = [
        e["full"]["self_minus_best_other"]
        for e in negatives
        if e["full"]["self_minus_best_other"] is not None
    ]
    minimums = [
        e["crop_score_statistics"]["min"]
        for e in negatives
        if e["crop_score_statistics"].get("count", 0) >= 2
    ]
    supports = [
        e["full"]["reference_consensus"]["support_fraction_at_frozen_threshold"]
        for e in negatives
    ]
    ref_count = dev["matcher"]["reference_count"]
    params = {
        "T": t,
        "T_short": high,
        "M_short": max(0, max(gaps) + 0.02) if gaps else None,
        "crop_min_passes": 2,
        "crop_std_max": float(
            np.median([e["crop_score_statistics"]["std"] for e in positives])
        ),
        "min_crop_threshold": max(t, max(minimums) + 0.02) if minimums else None,
        "reference_support_min": max(supports) + 1 / ref_count,
        "minimum_reference_score": t,
    }
    # Explicitly document the small hypothesis budget before any holdout scores.
    rules = {
        "rule_version": "short-self-shadow-v1",
        "production_enabled": False,
        "manifest_sha256": digest(output / "split-manifest.json"),
        "development_evidence_sha256": digest(output / "development-evidence.json"),
        "parameters": params,
        "rules": list(RULES),
        "fitting": "ONE proposal per evidence family; max dev negative + fixed .02 safety offset; no grid search, no holdout or anchor input",
        "development_short_counts": {
            "positive": len(positives),
            "negative": len(negatives),
        },
        "development_short_positive_sessions": len(
            {e["session_id"] for e in positives}
        ),
        "strict_independent_positive_sessions": len(
            {
                e["session_id"]
                for e in manifest["events"]
                if e["strict_independent"] and e["truth"] == "self"
            }
        ),
        "promotion": "NO PROMOTION: retrospective session exposure, independent evidence required",
    }
    write(path, rules)
    return rules


def would_accept(e, rule, params, *, require_ownership=True):
    full = e["full"]
    if require_ownership and not e["safe_ownership"]:
        return False
    if (
        e["duration_ms"] < 2000
        or full.get("reason") == "embedding_unavailable"
        or "score" not in full
    ):
        return False
    if rule == "single_normal":
        return full.get("decision") == "self"
    if rule == "current_two_window" or e["duration_ms"] >= 4000:
        return e["current_two_window"] == "self"
    crops = e["crop_score_statistics"]
    gap = full.get("self_minus_best_other")
    if rule == "high_score":
        return full["score"] >= params["T_short"]
    if rule == "score_margin":
        return (
            full["score"] >= params["T_short"]
            and gap is not None
            and params["M_short"] is not None
            and gap >= params["M_short"]
        )
    if rule == "crop_stability":
        return (
            full["score"] >= params["T"]
            and sum(c.get("score", -1) >= params["T"] for c in e["crops"])
            >= params["crop_min_passes"]
            and crops.get("count", 0) >= 2
            and crops["std"] <= params["crop_std_max"]
        )
    if rule == "min_crop":
        return (
            full["score"] >= params["T"]
            and crops.get("count", 0) >= 2
            and params["min_crop_threshold"] is not None
            and crops["min"] >= params["min_crop_threshold"]
        )
    if rule == "enrollment_consensus":
        return (
            full["score"] >= params["T_short"]
            and full.get("reference_consensus", {}).get(
                "support_fraction_at_frozen_threshold", -1
            )
            >= params["reference_support_min"]
        )
    raise ValueError("unknown shadow rule")


def metrics(events, rule, params):
    positive = [e for e in events if e["truth"] == "self"]
    negative = [e for e in events if e["truth"] == "non-self"]
    accepted = sum(would_accept(e, rule, params) for e in positive)
    false = sum(would_accept(e, rule, params) for e in negative)
    return {
        "positive_events": len(positive),
        "negative_events": len(negative),
        "self_accepted": accepted,
        "self_unknown": len(positive) - accepted,
        "negative_self": false,
        "negative_rejected": len(negative) - false,
        "self_coverage": accepted / len(positive) if positive else None,
        "false_self_count": false,
    }


def evaluate(output, partition):
    output = Path(output)
    path = output / f"{partition}-shadow.json"
    if path.exists():
        raise ValueError(
            "partition predictions already frozen; holdout may only execute once"
        )
    validate_manifest(output)
    rules = read(output / "candidate-rules.json")
    report = read(output / f"{partition}-evidence.json")
    if report["manifest_sha256"] != rules["manifest_sha256"]:
        raise ValueError("rule/evidence split mismatch")
    predictions = []
    for e in report["events"]:
        result = {
            rule: {
                "would_accept": would_accept(e, rule, rules["parameters"]),
                "would_reject": not would_accept(e, rule, rules["parameters"]),
                "score": e["full"].get("score"),
                "reason": "offline_rule_evidence_only",
                "rule_version": rules["rule_version"],
            }
            for rule in RULES
        }
        predictions.append(
            {
                "event_id": e["event_id"],
                "truth": e["truth"],
                "predictions": result,
                "ungated_diagnostic_only": {
                    r: would_accept(e, r, rules["parameters"], require_ownership=False)
                    for r in RULES
                },
            }
        )
    summary = {
        rule: {
            "all": metrics(report["events"], rule, rules["parameters"]),
            "2-4": metrics(
                [e for e in report["events"] if 2000 <= e["duration_ms"] < 4000],
                rule,
                rules["parameters"],
            ),
            "bins": {
                b: metrics(
                    [e for e in report["events"] if e["bin"] == b],
                    rule,
                    rules["parameters"],
                )
                for b in BINS
            },
        }
        for rule in RULES
    }
    result = {
        "partition": partition,
        "rule_snapshot_sha256": digest(output / "candidate-rules.json"),
        "production_writes": False,
        "metrics": summary,
        "predictions": predictions,
    }
    write(path, result)
    return {r: v["2-4"] for r, v in summary.items()}


def evidence_status(events, comparison, regression, params):
    independent_counts = {
        s: {
            truth: sum(
                e.get("strict_independent", False)
                and e.get("split") == s
                and e["truth"] == truth
                and 2000 <= e["duration_ms"] < 4000
                for e in events
            )
            for truth in ("self", "non-self")
        }
        for s in ("development", "holdout")
    }
    if any(n == 0 for counts in independent_counts.values() for n in counts.values()):
        return "INSUFFICIENT EVIDENCE", "INSUFFICIENT INDEPENDENT SHORT-SPEECH DATA"
    if (
        not regression
        or not {"paired", "production_ownership"}.issubset(regression["metrics"])
        or any(m["negative_self"] for m in regression["metrics"].values())
    ):
        return "INSUFFICIENT EVIDENCE", "FIXED REGRESSION GATE NOT SATISFIED"
    independent_holdout = [
        e for e in events if e.get("strict_independent") and e.get("split") == "holdout"
    ]
    independent_short = [
        e for e in independent_holdout if 2000 <= e["duration_ms"] < 4000
    ]
    baseline = metrics(independent_short, "current_two_window", params)
    candidates = [r for r in RULES if r not in ("current_two_window", "single_normal")]
    promising = any(
        comparison["holdout"][r]["all"]["negative_self"] == 0
        and metrics(independent_holdout, r, params)["negative_self"] == 0
        and metrics(independent_short, r, params)["self_accepted"]
        > baseline["self_accepted"]
        for r in candidates
    )
    return (
        "PROMISING FOR SHADOW" if promising else "NO SAFE SHORT-SPEECH RULE FOUND",
        "INDEPENDENT EVENTS AVAILABLE; FURTHER FROZEN VALIDATION REQUIRED",
    )


def summarize(output):
    output = Path(output)
    reports = {
        s: read(output / f"{s}-evidence.json")
        for s in ("development", "holdout", "reference")
    }
    events = [e for r in reports.values() for e in r["events"]]
    distributions = {
        s: {
            b: {
                truth: distribution(
                    [
                        e["full"]["score"]
                        for e in report["events"]
                        if e["bin"] == b
                        and e["truth"] == truth
                        and "score" in e["full"]
                    ]
                )
                for truth in ("self", "non-self")
            }
            for b in BINS
        }
        for s, report in reports.items()
    }
    false = [e for e in events if e.get("historical_false_accept")]
    anchors = [e for e in events if e.get("anchor")]
    rules = read(output / "candidate-rules.json")
    comparison = {s: read(output / f"{s}-shadow.json")["metrics"] for s in reports}
    regression_path = output / "fixed-regression.json"
    conclusion, independence = evidence_status(
        events,
        comparison,
        read(regression_path) if regression_path.exists() else None,
        rules["parameters"],
    )
    result = {
        "dataset": read(output / "dataset-summary.json"),
        "distributions": distributions,
        "historical_false_accepts": false,
        "anchors": anchors,
        "rule_snapshot": rules,
        "comparison": comparison,
        "conclusion": conclusion,
        "independent_data_status": independence,
        "production_promotion": "NO PROMOTION",
        "production_enabled": False,
        "unit_of_analysis": "event, not crop; session/date held apart; prior session exposure excluded from strict independence",
    }
    write(output / "summary.json", result)
    return {
        "conclusion": result["conclusion"],
        "independent_data_status": result["independent_data_status"],
    }
