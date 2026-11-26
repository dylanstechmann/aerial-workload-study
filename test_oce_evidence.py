import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from oce_evidence import (
    MANIFEST_SCHEMA,
    MAX_JSON_BYTES,
    REPORT_SCHEMA,
    validate_files,
    validate_manifest,
)


ROOT = Path(__file__).parent
MANIFEST_PATH = ROOT / "aerial-oce-evidence.json"
OCE_RESULT_PATH = ROOT / "oce-result-fixture.json"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class OCEEvidenceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_json(MANIFEST_PATH)
        self.result = load_json(OCE_RESULT_PATH)
        self.digest = hashlib.sha256(OCE_RESULT_PATH.read_bytes()).hexdigest()

    def validate(self, manifest=None, result=None):
        return validate_manifest(
            self.manifest if manifest is None else manifest,
            self.result if result is None else result,
            source_sha256=self.digest,
        )

    def test_valid_report_hashes_source_and_never_implies_acceptance(self):
        original_manifest = copy.deepcopy(self.manifest)
        original_result = copy.deepcopy(self.result)
        report = self.validate()
        self.assertEqual(report["schema"], REPORT_SCHEMA)
        self.assertEqual(report["source"], {
            "artifactSha256": self.digest,
            "resultSchemaVersion": "1.0.0",
            "evidenceHandoffSchemaVersion": "1.0.0",
            "sourceCommit": "0123456789abcdef0123456789abcdef01234567",
            "sourceVersion": "synthetic-fixture-1",
        })
        self.assertEqual(self.manifest, original_manifest)
        self.assertEqual(self.result, original_result)
        self.assertEqual(report, self.validate())
        for flag in (
            "constructionAuthorized", "physicalValidationPerformed", "humanAcceptancePerformed",
            "surveyAcceptancePerformed", "constructionAcceptancePerformed",
            "specialistAcceptancePerformed",
        ):
            self.assertIs(report[flag], False)
        required_gates = [
            gate for row in report["evidence"] for gate in row["required_gates"]
        ]
        self.assertEqual([gate["gate_id"] for gate in required_gates], ["BG-001", "BG-002"])
        for gate in required_gates:
            self.assertEqual(gate["status"], "UNRESOLVED")
            self.assertIs(gate["acceptance_performed"], False)

    def test_fixture_uses_current_versioned_evidence_handoff_shape(self):
        handoff = self.result["evidenceHandoff"]
        self.assertEqual(set(handoff), {
            "schemaVersion", "exportType", "status", "constructionAuthorized",
            "physicalValidationPerformed", "humanAcceptancePerformed", "canonicalRegisters",
            "workPackages", "qualityGates", "notice",
        })
        self.assertEqual(handoff["schemaVersion"], "1.0.0")
        self.assertEqual(handoff["exportType"], "SYNTHETIC_EVIDENCE_HANDOFF")
        for row in handoff["workPackages"]:
            self.assertEqual(set(row), {"packageId", "requiredGateIds", "evidenceStatus"})
        for row in handoff["qualityGates"]:
            self.assertEqual(set(row), {"gateId", "status"})

    def test_source_commit_and_version_are_optional_but_validated(self):
        result = copy.deepcopy(self.result)
        del result["sourceCommit"]
        del result["sourceVersion"]
        source = self.validate(result=result)["source"]
        self.assertNotIn("sourceCommit", source)
        self.assertNotIn("sourceVersion", source)
        for key, value in (("sourceCommit", "not-a-commit"), ("sourceVersion", "bad version")):
            changed = copy.deepcopy(self.result)
            changed[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.validate(result=changed)

    def test_unknown_package_and_dependency_ids_are_rejected(self):
        unknown_package = copy.deepcopy(self.manifest)
        unknown_package["evidence"][0]["package_id"] = "BW-999"
        with self.assertRaisesRegex(ValueError, "unknown OCE work package: BW-999"):
            self.validate(unknown_package)
        unknown_dependency = copy.deepcopy(self.manifest)
        unknown_dependency["evidence"][2]["depends_on"] = ["BW-999"]
        with self.assertRaisesRegex(ValueError, "unknown OCE dependencies: BW-999"):
            self.validate(unknown_dependency)

    def test_manifest_dependencies_must_exactly_match_oce_schedule(self):
        for dependencies in ([], ["BW-001"], ["BW-002", "BW-001"]):
            changed = copy.deepcopy(self.manifest)
            changed["evidence"][2]["depends_on"] = dependencies
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(ValueError, "do not match"):
                self.validate(changed)

    def test_duplicate_evidence_ids_and_packages_are_rejected(self):
        duplicate_id = copy.deepcopy(self.manifest)
        duplicate_id["evidence"].append(copy.deepcopy(duplicate_id["evidence"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate evidence ID"):
            self.validate(duplicate_id)
        duplicate_package = copy.deepcopy(self.manifest)
        duplicate = copy.deepcopy(duplicate_package["evidence"][0])
        duplicate["evidence_id"] = "another-receipt"
        duplicate_package["evidence"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "duplicate evidence for OCE package"):
            self.validate(duplicate_package)

    def test_inconsistent_and_malformed_timestamps_are_rejected(self):
        early_receipt = copy.deepcopy(self.manifest)
        early_receipt["evidence"][0]["evidence_minute"] = 9
        with self.assertRaisesRegex(ValueError, "precedes completion"):
            self.validate(early_receipt)
        early_dependent = copy.deepcopy(self.manifest)
        early_dependent["evidence"][2]["completed_minute"] = 19
        early_dependent["evidence"][2]["evidence_minute"] = 19
        with self.assertRaisesRegex(ValueError, "before prerequisite BW-002"):
            self.validate(early_dependent)
        for value in (True, 1.0, -1, 10081, None, "12"):
            changed = copy.deepcopy(self.manifest)
            changed["evidence"][0]["evidence_minute"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "evidence_minute"):
                self.validate(changed)

    def test_delayed_prerequisite_receipt_does_not_rewrite_completion_order(self):
        changed = copy.deepcopy(self.manifest)
        changed["evidence"][0]["evidence_minute"] = 40
        report = self.validate(changed)
        self.assertEqual(report["evidence"][2]["completed_minute"], 30)
        self.assertEqual(report["evidence"][2]["required_gates"][0]["status"], "UNRESOLVED")

    def test_malformed_manifest_shapes_and_bounds_are_rejected(self):
        malformed = [
            None, [], {}, {**self.manifest, "schema": "wrong"},
            {**self.manifest, "extra": True}, {**self.manifest, "evidence": []},
            {**self.manifest, "evidence": {}},
        ]
        duplicate_dependencies = copy.deepcopy(self.manifest)
        duplicate_dependencies["evidence"][2]["depends_on"] = ["BW-002", "BW-002"]
        malformed.append(duplicate_dependencies)
        extra_field = copy.deepcopy(self.manifest)
        extra_field["evidence"][0]["accepted"] = True
        malformed.append(extra_field)
        too_many = copy.deepcopy(self.manifest)
        too_many["evidence"] = [copy.deepcopy(self.manifest["evidence"][0]) for _ in range(513)]
        malformed.append(too_many)
        for value in malformed:
            with self.subTest(value=repr(value)[:120]), self.assertRaises(ValueError):
                validate_manifest(value, self.result, source_sha256=self.digest)

    def test_source_authorization_and_gate_status_fail_closed(self):
        for location, key in (
            ("result", "constructionAuthorized"),
            ("result", "physicalValidationPerformed"),
            ("handoff", "constructionAuthorized"),
            ("handoff", "physicalValidationPerformed"),
            ("handoff", "humanAcceptancePerformed"),
        ):
            changed = copy.deepcopy(self.result)
            container = changed if location == "result" else changed["evidenceHandoff"]
            container[key] = True
            with self.subTest(location=location, key=key), self.assertRaisesRegex(ValueError, "must be False"):
                self.validate(result=changed)
        accepted = copy.deepcopy(self.result)
        accepted["evidenceHandoff"]["qualityGates"][0]["status"] = "ACCEPTED"
        with self.assertRaisesRegex(ValueError, "must be 'UNRESOLVED'"):
            self.validate(result=accepted)

    def test_malformed_source_graphs_and_handoffs_are_rejected(self):
        duplicate = copy.deepcopy(self.result)
        duplicate["evidenceHandoff"]["workPackages"].append(
            copy.deepcopy(duplicate["evidenceHandoff"]["workPackages"][0])
        )
        with self.assertRaisesRegex(ValueError, "duplicate OCE work package"):
            self.validate(result=duplicate)

        missing_dependency = copy.deepcopy(self.result)
        missing_dependency["schedule"]["packages"][1]["dependencies"] = ["BW-999"]
        with self.assertRaisesRegex(ValueError, "unknown OCE dependencies"):
            self.validate(result=missing_dependency)

        mismatched_ids = copy.deepcopy(self.result)
        mismatched_ids["schedule"]["packages"].pop()
        with self.assertRaisesRegex(ValueError, "same work package IDs"):
            self.validate(result=mismatched_ids)

        unknown_gate = copy.deepcopy(self.result)
        unknown_gate["evidenceHandoff"]["workPackages"][1]["requiredGateIds"] = ["BG-999"]
        with self.assertRaisesRegex(ValueError, "unknown OCE gates"):
            self.validate(result=unknown_gate)

        cyclic = copy.deepcopy(self.result)
        cyclic["schedule"]["packages"][0]["dependencies"] = ["BW-003"]
        with self.assertRaisesRegex(ValueError, "cyclic OCE"):
            self.validate(result=cyclic)

    def test_file_reader_enforces_byte_bound_and_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (MAX_JSON_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "byte input limit"):
                validate_files(MANIFEST_PATH, oversized)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema":"' + MANIFEST_SCHEMA + '","schema":"duplicate","scenario":"x","evidence":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key: schema"):
                validate_files(duplicate, OCE_RESULT_PATH)

    def test_cli_uses_explicit_paths_and_emits_source_hash(self):
        completed = subprocess.run(
            [
                sys.executable, "-B", "oce_evidence.py", "--manifest", str(MANIFEST_PATH),
                "--oce-result", str(OCE_RESULT_PATH),
            ],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["source"]["artifactSha256"], self.digest)
        self.assertFalse(report["humanAcceptancePerformed"])

        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.json"
            changed = copy.deepcopy(self.manifest)
            changed["evidence"][0]["package_id"] = "BW-999"
            invalid.write_text(json.dumps(changed), encoding="utf-8")
            rejected = subprocess.run(
                [
                    sys.executable, "-B", "oce_evidence.py", "--manifest", str(invalid),
                    "--oce-result", str(OCE_RESULT_PATH),
                ],
                cwd=ROOT, check=False, capture_output=True, text=True,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("unknown OCE work package", rejected.stderr)
            self.assertEqual(rejected.stdout, "")


if __name__ == "__main__":
    unittest.main()
