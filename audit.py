"""Audit fictional construction imagery and delivery handoffs from event logs."""

import argparse
from collections import Counter
from graphlib import CycleError, TopologicalSorter
import json
from pathlib import Path
import re


SCHEMA_ID = "synthetic-aerial-handoffs/1"
MAX_MINUTE = 10_080


def object_fields(value, field, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    required = set(required)
    allowed = required | set(optional)
    unknown = sorted(str(key) for key in value.keys() - allowed)
    if unknown:
        raise ValueError(f"{field} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"{field} is missing required fields: {', '.join(missing)}")
    return value


def array(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value):
        raise ValueError(f"{field} must be a short anonymous identifier")
    return value


def minute(value, field):
    if type(value) is not int or not 0 <= value <= MAX_MINUTE:
        raise ValueError(f"{field} must be a relative integer minute from 0 to 10080")
    return value


def check_dependencies(dependencies, windows):
    for job_id, prerequisites in dependencies.items():
        for prerequisite in prerequisites:
            if prerequisite not in windows:
                raise ValueError(f"{job_id}: unknown prerequisite {prerequisite}")
            if prerequisite == job_id:
                raise ValueError(f"{job_id}: a job cannot depend on itself")
    try:
        TopologicalSorter(dependencies).prepare()
    except CycleError as exc:
        raise ValueError("cyclic job dependencies") from exc


def audit_dependencies(dependencies, visible_records):
    issues = []
    for job_id, prerequisites in sorted(dependencies.items()):
        if job_id not in visible_records:
            continue
        completed, _ = visible_records[job_id]
        for prerequisite in sorted(prerequisites):
            if prerequisite not in visible_records:
                reason = "missing_prerequisite_handoff"
            else:
                prior_completion, prior_evidence = visible_records[prerequisite]
                if prior_completion > completed:
                    reason = "handoff_before_prerequisite"
                elif not prior_evidence:
                    reason = "unconfirmed_prerequisite"
                else:
                    continue
            issues.append({"job_id": job_id, "prerequisite_id": prerequisite, "reason": reason})
    return issues


def audit(scenario, *, as_of=None):
    """Validate one scenario and return its deterministic synthetic audit report."""
    object_fields(scenario, "scenario", ("id", "jobs", "handoffs"))
    scenario_id = identifier(scenario["id"], "scenario ID")
    if as_of is not None:
        as_of = minute(as_of, "as_of")
    jobs = array(scenario["jobs"], f"{scenario_id}: jobs")
    handoffs = array(scenario["handoffs"], f"{scenario_id}: handoffs")
    windows = {}
    dependencies = {}
    for index, job in enumerate(jobs):
        object_fields(
            job, f"job at index {index}", ("id", "ready_minute", "due_minute"), ("depends_on",)
        )
        job_id = identifier(job["id"], "job ID")
        if job_id in windows:
            raise ValueError(f"duplicate job: {job_id}")
        ready = minute(job["ready_minute"], "ready_minute")
        due = minute(job["due_minute"], "due_minute")
        if ready > due:
            raise ValueError(f"{job_id}: ready minute follows the deadline")
        windows[job_id] = (ready, due)
        required = job.get("depends_on", [])
        if not isinstance(required, list):
            raise ValueError(f"{job_id}: depends_on must list prerequisite job IDs")
        if required:
            prerequisites = [identifier(value, "prerequisite ID") for value in required]
            if len(set(prerequisites)) != len(prerequisites):
                raise ValueError(f"{job_id}: duplicate prerequisite")
            dependencies[job_id] = prerequisites
    if not windows:
        raise ValueError(f"{scenario_id}: no jobs")
    if dependencies:
        check_dependencies(dependencies, windows)

    records = {}
    visible_evidence_minutes = {}
    for index, record in enumerate(handoffs):
        object_fields(
            record,
            f"handoff at index {index}",
            ("job_id", "completed_minute", "evidence_recorded"),
            ("evidence_minute",),
        )
        job_id = identifier(record["job_id"], "handoff job ID")
        if job_id not in windows or job_id in records:
            raise ValueError(f"handoff must name one known job exactly once: {job_id}")
        timestamp = minute(record["completed_minute"], "completed_minute")
        if timestamp < windows[job_id][0]:
            raise ValueError(f"{job_id}: handoff precedes the ready minute")
        evidence = record["evidence_recorded"]
        if type(evidence) is not bool:
            raise ValueError(f"{job_id}: evidence_recorded must be a boolean")
        if "evidence_minute" in record:
            if not evidence:
                raise ValueError(f"{job_id}: evidence_minute requires evidence_recorded to be true")
            received = minute(record["evidence_minute"], "evidence_minute")
            if received < timestamp:
                raise ValueError(f"{job_id}: evidence_minute precedes completion")
            evidence = as_of is None or received <= as_of
            if evidence:
                visible_evidence_minutes[job_id] = received
        records[job_id] = (timestamp, evidence)

    visible_records = records if as_of is None else {
        job_id: record for job_id, record in records.items() if record[0] <= as_of
    }
    results = []
    missing_statuses = ("no_record",) if as_of is None else ("not_ready", "pending", "overdue")
    counts = Counter(dict.fromkeys(("confirmed_on_time", "confirmed_late", "unconfirmed", *missing_statuses), 0))
    for job_id, (ready, due) in windows.items():
        timestamp, evidence = visible_records.get(job_id, (None, None))
        delay = None
        if timestamp is None:
            if as_of is None:
                status = "no_record"
            elif as_of < ready:
                status = "not_ready"
            elif as_of <= due:
                status = "pending"
            else:
                status = "overdue"
        else:
            delay = max(timestamp - due, 0)
            if evidence:
                status = "confirmed_late" if delay else "confirmed_on_time"
            else:
                status = "unconfirmed"
        counts[status] += 1
        job_result = {
            "job_id": job_id, "ready_minute": ready, "due_minute": due,
            "completed_minute": timestamp, "delay_minutes": delay,
            "evidence_recorded": evidence, "status": status,
        }
        if job_id in visible_evidence_minutes:
            job_result["evidence_minute"] = visible_evidence_minutes[job_id]
        results.append(job_result)
    report = {"scenario": scenario_id, "job_count": len(results), "summary": dict(counts), "jobs": results}
    issues = audit_dependencies(dependencies, visible_records)
    if dependencies:
        report["dependency_issues"] = issues
    if as_of is not None:
        flagged = {issue["job_id"] for issue in issues}
        queue = [
            job for job in results if job["status"] in ("unconfirmed", "overdue") or job["job_id"] in flagged
        ]
        queue.sort(key=lambda job: (job["due_minute"], job["job_id"]))
        report["review_queue"] = [job["job_id"] for job in queue]
    return report


def review_queue_changes(earlier, current):
    earlier_ids = set(earlier)
    current_ids = set(current)
    return {
        "entered": [job_id for job_id in current if job_id not in earlier_ids],
        "cleared": [job_id for job_id in earlier if job_id not in current_ids],
        "still_queued": [job_id for job_id in current if job_id in earlier_ids],
    }


def audit_document(data, *, as_of=None):
    """Validate a complete input document and audit each of its scenarios."""
    object_fields(data, "document", ("schema", "scenarios"))
    if data["schema"] != SCHEMA_ID:
        raise ValueError(f"expected {SCHEMA_ID}")
    scenarios = array(data["scenarios"], "scenarios")
    if not scenarios:
        raise ValueError("expected at least one scenario")
    audits = [audit(scenario, as_of=as_of) for scenario in scenarios]
    if len({report["scenario"] for report in audits}) != len(audits):
        raise ValueError("expected unique scenario IDs")
    return audits


def validate_document(data):
    """Dependency-free structural and relational validation for an input document."""
    audit_document(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--as-of", type=int, metavar="MINUTE", help="Report handoffs visible at this relative minute")
    parser.add_argument("--compare-from", type=int, metavar="MINUTE",
                        help="Compare the review queue at this minute with the --as-of snapshot")
    args = parser.parse_args()
    try:
        if args.compare_from is not None:
            if args.as_of is None:
                raise ValueError("--compare-from requires --as-of")
            minute(args.compare_from, "compare_from")
            minute(args.as_of, "as_of")
            if args.compare_from > args.as_of:
                raise ValueError("--compare-from must be at or before --as-of")
        data = json.loads(args.log.read_text(encoding="utf-8"))
        audits = audit_document(data, as_of=args.as_of)
        if args.compare_from is not None:
            for scenario, current in zip(data["scenarios"], audits):
                earlier_queue = audit(scenario, as_of=args.compare_from)["review_queue"]
                current["review_queue_changes"] = review_queue_changes(earlier_queue, current["review_queue"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    report = {"data_kind": "synthetic", "audits": audits}
    if args.as_of is not None:
        report["as_of_minute"] = args.as_of
    if args.compare_from is not None:
        report["compare_from_minute"] = args.compare_from
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
