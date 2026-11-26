"""Validate synthetic aerial evidence references against an offline OCE result.

This adapter reads caller-supplied JSON files only. Evidence receipt never accepts
a quality gate, survey, construction work, or specialist review.
"""

import argparse
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
from pathlib import Path
import re


MANIFEST_SCHEMA = "synthetic-aerial-oce-evidence/1"
REPORT_SCHEMA = "synthetic-aerial-oce-validation/1"
OCE_SCHEMA_VERSION = "1.0.0"
MAX_JSON_BYTES = 1_000_000
MAX_ITEMS = 512
MAX_DEPENDENCIES = 64
MAX_MINUTE = 10_080

_PACKAGE_ID = re.compile(r"BW-\d{3}")
_GATE_ID = re.compile(r"BG-\d{3}")
_EVIDENCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_EVIDENCE_STATUS = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _object(value, field, required, optional=(), *, strict=True):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    required = set(required)
    allowed = required | set(optional)
    if strict:
        unknown = sorted(str(key) for key in value.keys() - allowed)
        if unknown:
            raise ValueError(f"{field} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"{field} is missing required fields: {', '.join(missing)}")
    return value


def _array(value, field, *, minimum=0, maximum=MAX_ITEMS):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} must contain {minimum} to {maximum} items")
    return value


def _text(value, field, pattern=None, *, maximum=256):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field} must be a nonempty string of at most {maximum} characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"{field} has an invalid format")
    return value


def _identifiers(value, field, pattern, *, maximum=MAX_DEPENDENCIES):
    values = _array(value, field, maximum=maximum)
    result = [_text(item, f"{field} item", pattern) for item in values]
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains a duplicate ID")
    return result


def _minute(value, field):
    if type(value) is not int or not 0 <= value <= MAX_MINUTE:
        raise ValueError(f"{field} must be an integer minute from 0 to {MAX_MINUTE}")
    return value


def _constant(data, key, expected, field):
    if data[key] != expected or type(data[key]) is not type(expected):
        raise ValueError(f"{field}.{key} must be {expected!r}")


def _validate_source(result):
    """Validate the current OCE result/evidenceHandoff contract without importing OCE."""
    source = _object(
        result,
        "OCE result",
        (
            "schemaVersion", "projectId", "constructionAuthorized",
            "physicalValidationPerformed", "readinessStatus", "syntheticPlanningOnly",
            "unresolvedGateIds", "evidenceHandoff", "schedule",
        ),
        ("sourceCommit", "sourceVersion"),
        strict=False,
    )
    _constant(source, "schemaVersion", OCE_SCHEMA_VERSION, "OCE result")
    _constant(source, "projectId", "BUNKER-01", "OCE result")
    _constant(source, "constructionAuthorized", False, "OCE result")
    _constant(source, "physicalValidationPerformed", False, "OCE result")
    _constant(source, "readinessStatus", "BLOCKED_UNRESOLVED_HUMAN_GATES", "OCE result")
    _constant(source, "syntheticPlanningOnly", True, "OCE result")
    unresolved = _identifiers(source["unresolvedGateIds"], "OCE unresolvedGateIds", _GATE_ID)

    handoff = _object(
        source["evidenceHandoff"],
        "OCE evidenceHandoff",
        (
            "schemaVersion", "exportType", "status", "constructionAuthorized",
            "physicalValidationPerformed", "humanAcceptancePerformed", "canonicalRegisters",
            "workPackages", "qualityGates", "notice",
        ),
    )
    for key, expected in (
        ("schemaVersion", OCE_SCHEMA_VERSION),
        ("exportType", "SYNTHETIC_EVIDENCE_HANDOFF"),
        ("status", "SYNTHETIC_UNREVIEWED"),
        ("constructionAuthorized", False),
        ("physicalValidationPerformed", False),
        ("humanAcceptancePerformed", False),
    ):
        _constant(handoff, key, expected, "OCE evidenceHandoff")
    registers = _object(
        handoff["canonicalRegisters"], "OCE canonicalRegisters", ("workBreakdown", "qualityGates")
    )
    _constant(
        registers, "workBreakdown", "projects/bunker01/construction/work-breakdown.csv",
        "OCE canonicalRegisters",
    )
    _constant(
        registers, "qualityGates", "projects/bunker01/construction/quality-gates.csv",
        "OCE canonicalRegisters",
    )
    _text(handoff["notice"], "OCE evidenceHandoff.notice")

    gates = {}
    gate_rows = _array(handoff["qualityGates"], "OCE qualityGates", minimum=1)
    for index, row in enumerate(gate_rows):
        _object(row, f"OCE quality gate at index {index}", ("gateId", "status"))
        gate_id = _text(row["gateId"], "OCE gate ID", _GATE_ID)
        if gate_id in gates:
            raise ValueError(f"duplicate OCE quality gate: {gate_id}")
        _constant(row, "status", "UNRESOLVED", f"OCE quality gate {gate_id}")
        gates[gate_id] = row["status"]
    if set(unresolved) != set(gates):
        raise ValueError("OCE unresolvedGateIds must exactly match the unresolved quality gates")

    packages = {}
    package_rows = _array(handoff["workPackages"], "OCE workPackages", minimum=1)
    for index, row in enumerate(package_rows):
        _object(
            row, f"OCE work package at index {index}",
            ("packageId", "requiredGateIds", "evidenceStatus"),
        )
        package_id = _text(row["packageId"], "OCE package ID", _PACKAGE_ID)
        if package_id in packages:
            raise ValueError(f"duplicate OCE work package: {package_id}")
        required_gates = _identifiers(
            row["requiredGateIds"], f"{package_id}.requiredGateIds", _GATE_ID
        )
        unknown_gates = sorted(set(required_gates) - set(gates))
        if unknown_gates:
            raise ValueError(f"{package_id} references unknown OCE gates: {', '.join(unknown_gates)}")
        packages[package_id] = {
            "requiredGateIds": required_gates,
            "evidenceStatus": _text(row["evidenceStatus"], f"{package_id}.evidenceStatus", _EVIDENCE_STATUS),
        }

    schedule = _object(source["schedule"], "OCE schedule", ("packages",), strict=False)
    schedule_rows = _array(schedule["packages"], "OCE schedule.packages", minimum=1)
    scheduled = {}
    for index, row in enumerate(schedule_rows):
        _object(
            row, f"OCE scheduled package at index {index}",
            ("packageId", "dependencies", "requiredGateIds"), strict=False,
        )
        package_id = _text(row["packageId"], "OCE scheduled package ID", _PACKAGE_ID)
        if package_id in scheduled:
            raise ValueError(f"duplicate OCE scheduled package: {package_id}")
        dependencies = _identifiers(row["dependencies"], f"{package_id}.dependencies", _PACKAGE_ID)
        if package_id in dependencies:
            raise ValueError(f"{package_id} cannot depend on itself")
        required_gates = _identifiers(
            row["requiredGateIds"], f"{package_id}.schedule.requiredGateIds", _GATE_ID
        )
        scheduled[package_id] = {"dependencies": dependencies, "requiredGateIds": required_gates}
    if set(scheduled) != set(packages):
        raise ValueError("OCE schedule and evidenceHandoff must contain the same work package IDs")
    for package_id, package in packages.items():
        dependencies = scheduled[package_id]["dependencies"]
        unknown = sorted(set(dependencies) - set(packages))
        if unknown:
            raise ValueError(f"{package_id} references unknown OCE dependencies: {', '.join(unknown)}")
        if set(scheduled[package_id]["requiredGateIds"]) != set(package["requiredGateIds"]):
            raise ValueError(f"{package_id} required gates differ between schedule and evidenceHandoff")
        package["dependencies"] = dependencies
    try:
        TopologicalSorter({key: value["dependencies"] for key, value in packages.items()}).prepare()
    except CycleError as exc:
        raise ValueError("cyclic OCE work package dependencies") from exc

    metadata = {
        "resultSchemaVersion": source["schemaVersion"],
        "evidenceHandoffSchemaVersion": handoff["schemaVersion"],
    }
    if "sourceCommit" in source:
        metadata["sourceCommit"] = _text(source["sourceCommit"], "OCE sourceCommit", _COMMIT)
    if "sourceVersion" in source:
        metadata["sourceVersion"] = _text(source["sourceVersion"], "OCE sourceVersion", _VERSION)
    return packages, gates, metadata


def validate_manifest(manifest, oce_result, *, source_sha256):
    """Return a deterministic validation report; all human acceptance remains false."""
    if not isinstance(source_sha256, str) or not _SHA256.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    packages, gates, metadata = _validate_source(oce_result)
    document = _object(manifest, "aerial OCE manifest", ("schema", "scenario", "evidence"))
    _constant(document, "schema", MANIFEST_SCHEMA, "aerial OCE manifest")
    scenario = _text(document["scenario"], "scenario", _SCENARIO_ID)
    rows = _array(document["evidence"], "evidence", minimum=1)

    evidence = []
    evidence_ids = set()
    package_ids = set()
    for index, row in enumerate(rows):
        _object(
            row, f"evidence at index {index}",
            ("evidence_id", "package_id", "depends_on", "completed_minute", "evidence_minute"),
        )
        evidence_id = _text(row["evidence_id"], "evidence_id", _EVIDENCE_ID)
        package_id = _text(row["package_id"], "package_id", _PACKAGE_ID)
        if evidence_id in evidence_ids:
            raise ValueError(f"duplicate evidence ID: {evidence_id}")
        if package_id in package_ids:
            raise ValueError(f"duplicate evidence for OCE package: {package_id}")
        evidence_ids.add(evidence_id)
        package_ids.add(package_id)
        if package_id not in packages:
            raise ValueError(f"unknown OCE work package: {package_id}")
        dependencies = _identifiers(row["depends_on"], f"{evidence_id}.depends_on", _PACKAGE_ID)
        unknown = sorted(set(dependencies) - set(packages))
        if unknown:
            raise ValueError(f"{evidence_id} names unknown OCE dependencies: {', '.join(unknown)}")
        if set(dependencies) != set(packages[package_id]["dependencies"]):
            raise ValueError(f"{package_id} dependencies do not match the supplied OCE result")
        completed = _minute(row["completed_minute"], f"{evidence_id}.completed_minute")
        receipt = _minute(row["evidence_minute"], f"{evidence_id}.evidence_minute")
        if receipt < completed:
            raise ValueError(f"{evidence_id}.evidence_minute precedes completion")
        evidence.append({
            "evidence_id": evidence_id,
            "package_id": package_id,
            "depends_on": dependencies,
            "completed_minute": completed,
            "evidence_minute": receipt,
        })

    by_package = {row["package_id"]: row for row in evidence}
    for row in evidence:
        for dependency in row["depends_on"]:
            prior = by_package.get(dependency)
            if prior is not None and row["completed_minute"] < prior["completed_minute"]:
                raise ValueError(f"{row['evidence_id']} completes before prerequisite {dependency}")

    output_evidence = []
    for row in evidence:
        package = packages[row["package_id"]]
        output_evidence.append({
            **row,
            "oce_evidence_status": package["evidenceStatus"],
            "required_gates": [
                {"gate_id": gate_id, "status": gates[gate_id], "acceptance_performed": False}
                for gate_id in package["requiredGateIds"]
            ],
        })
    source = {"artifactSha256": source_sha256, **metadata}
    return {
        "schema": REPORT_SCHEMA,
        "scenario": scenario,
        "source": source,
        "evidence": output_evidence,
        "constructionAuthorized": False,
        "physicalValidationPerformed": False,
        "humanAcceptancePerformed": False,
        "surveyAcceptancePerformed": False,
        "constructionAcceptancePerformed": False,
        "specialistAcceptancePerformed": False,
        "notice": (
            "Offline synthetic reference validation only. Evidence presence or receipt does not resolve "
            "any BG human gate and does not imply survey, construction, or specialist acceptance."
        ),
    }


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonstandard JSON number: {value}")


def _read_json(path, label):
    with Path(path).open("rb") as handle:
        payload = handle.read(MAX_JSON_BYTES + 1)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds the {MAX_JSON_BYTES}-byte input limit")
    try:
        text = payload.decode("utf-8")
        data = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"malformed {label} JSON: {exc}") from exc
    return data, payload


def validate_files(manifest_path, oce_result_path):
    """Validate two explicit offline paths and hash the exact OCE source bytes."""
    result, result_bytes = _read_json(oce_result_path, "OCE result")
    manifest, _ = _read_json(manifest_path, "aerial OCE manifest")
    return validate_manifest(
        manifest, result, source_sha256=hashlib.sha256(result_bytes).hexdigest()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Aerial OCE evidence manifest JSON")
    parser.add_argument("--oce-result", type=Path, required=True, help="Offline OCE result/export JSON")
    args = parser.parse_args()
    try:
        report = validate_files(args.manifest, args.oce_result)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
