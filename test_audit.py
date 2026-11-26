import json
from pathlib import Path
import unittest

from audit import audit


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name("handoffs.json")
        self.construction, self.delivery = json.loads(path.read_text(encoding="utf-8"))["scenarios"]

    def test_cutoff_is_inclusive_for_ready_due_and_completed_times(self):
        before_ready = audit(self.delivery, as_of=9)
        self.assertEqual(before_ready["summary"]["not_ready"], 2)
        self.assertEqual(audit(self.delivery, as_of=10)["summary"]["pending"], 3)
        at_deadline = audit(self.delivery, as_of=45)
        self.assertEqual(at_deadline["summary"]["confirmed_on_time"], 1)
        self.assertEqual(at_deadline["summary"]["pending"], 2)
        self.assertIsNone(at_deadline["jobs"][1]["completed_minute"])
        self.assertIsNone(at_deadline["jobs"][1]["evidence_recorded"])

    def test_evidence_and_review_queue(self):
        construction = audit(self.construction, as_of=50)
        delivery = audit(self.delivery, as_of=50)
        self.assertEqual(construction["summary"]["confirmed_late"], 1)
        self.assertEqual(construction["review_queue"], [])
        self.assertEqual(delivery["summary"]["unconfirmed"], 1)
        self.assertEqual(delivery["summary"]["overdue"], 1)
        self.assertEqual(delivery["review_queue"], ["parcel-02", "parcel-03"])
        reordered = {**self.delivery, "jobs": list(reversed(self.delivery["jobs"]))}
        self.assertEqual(audit(reordered, as_of=50)["review_queue"], delivery["review_queue"])

    def test_invalid_future_records_are_rejected_and_default_report_still_works(self):
        full = audit(self.delivery)
        self.assertEqual(full["summary"]["no_record"], 1)
        self.assertNotIn("review_queue", full)
        self.assertNotIn("dependency_issues", full)
        duplicate = {**self.delivery, "handoffs": self.delivery["handoffs"] * 2}
        with self.assertRaisesRegex(ValueError, "exactly once"):
            audit(duplicate, as_of=0)
        with self.assertRaisesRegex(ValueError, "as_of"):
            audit(self.delivery, as_of=10081)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name("handoffs-with-dependencies.json")
        self.construction, self.delivery = json.loads(path.read_text(encoding="utf-8"))["scenarios"]

    def test_snapshot_flags_only_visible_handoffs_and_prior_evidence(self):
        self.assertEqual(audit(self.construction, as_of=45)["dependency_issues"], [])
        issue = {"job_id": "bw-016-restoration-photos", "prerequisite_id": "bw-009-stockpile-photos"}
        visible_early = audit(self.construction, as_of=46)
        self.assertEqual(visible_early["dependency_issues"], [{**issue, "reason": "missing_prerequisite_handoff"}])
        self.assertEqual(visible_early["jobs"][2]["status"], "confirmed_on_time")
        self.assertIn(issue["job_id"], visible_early["review_queue"])
        self.assertEqual(audit(self.construction, as_of=49)["dependency_issues"], [
            {**issue, "reason": "handoff_before_prerequisite"},
        ])
        self.assertEqual(audit(self.construction)["dependency_issues"], audit(self.construction, as_of=49)["dependency_issues"])

    def test_unconfirmed_prerequisite_and_simultaneous_completions(self):
        report = audit(self.delivery, as_of=55)
        self.assertEqual(report["dependency_issues"], [{
            "job_id": "parcel-03", "prerequisite_id": "parcel-02", "reason": "unconfirmed_prerequisite",
        }])
        self.assertEqual(report["review_queue"], ["parcel-02", "parcel-03"])

        handoffs = self.delivery["handoffs"].copy()
        handoffs[1] = {**handoffs[1], "completed_minute": 55, "evidence_recorded": True}
        changed = {**self.delivery, "handoffs": handoffs, "jobs": list(reversed(self.delivery["jobs"]))}
        self.assertEqual(audit(changed, as_of=55)["dependency_issues"], [])

    def test_invalid_graphs_fail_even_when_no_handoff_is_visible(self):
        for prerequisites, message in (
            ("parcel-03", "must list"),
            (["unknown-parcel"], "unknown prerequisite"),
            (["parcel-03", "parcel-03"], "duplicate prerequisite"),
            (["parcel-01"], "cannot depend on itself"),
            (["parcel-03"], "cyclic job dependencies"),
        ):
            with self.subTest(prerequisites=prerequisites):
                jobs = self.delivery["jobs"].copy()
                jobs[0] = {**jobs[0], "depends_on": prerequisites}
                with self.assertRaisesRegex(ValueError, message):
                    audit({**self.delivery, "jobs": jobs}, as_of=0)


class DelayedEvidenceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name("handoffs-with-delayed-evidence.json")
        self.scenario, = json.loads(path.read_text(encoding="utf-8"))["scenarios"]

    def test_receipt_visibility_and_dependency_review_at_boundaries(self):
        before = audit(self.scenario, as_of=27)["jobs"][0]
        self.assertIsNone(before["completed_minute"])
        self.assertIsNone(before["evidence_recorded"])
        self.assertNotIn("evidence_minute", before)

        at_completion = audit(self.scenario, as_of=28)["jobs"][0]
        self.assertEqual(at_completion["status"], "unconfirmed")
        self.assertFalse(at_completion["evidence_recorded"])
        self.assertNotIn("evidence_minute", at_completion)

        before_receipt = audit(self.scenario, as_of=41)
        self.assertEqual(before_receipt["review_queue"], ["depot-transfer", "recipient-delivery"])
        self.assertEqual(before_receipt["dependency_issues"], [{
            "job_id": "recipient-delivery", "prerequisite_id": "depot-transfer",
            "reason": "unconfirmed_prerequisite",
        }])
        self.assertNotIn("evidence_minute", before_receipt["jobs"][0])
        confirmed = audit(self.scenario, as_of=42)
        self.assertEqual(confirmed["review_queue"], [])
        self.assertEqual(confirmed["dependency_issues"], [])
        self.assertEqual(confirmed["summary"]["confirmed_on_time"], 2)
        self.assertEqual(confirmed["jobs"][0]["evidence_minute"], 42)
        self.assertNotIn("evidence_minute", confirmed["jobs"][1])
        self.assertEqual(confirmed["jobs"], audit(self.scenario)["jobs"])

    def test_receipt_cannot_fix_out_of_order_completion(self):
        transfer, delivery = self.scenario["handoffs"]
        scenario = {**self.scenario, "handoffs": [{**transfer, "completed_minute": 40}, delivery]}
        for cutoff in (41, 42, None):
            with self.subTest(cutoff=cutoff):
                result = audit(scenario, as_of=cutoff)
                self.assertEqual(result["dependency_issues"][0]["reason"], "handoff_before_prerequisite")

    def test_evidence_minute_validation_includes_future_records(self):
        transfer, delivery = self.scenario["handoffs"]
        for stamp, flag in ((27, True), (10081, True), (True, True), (42.0, True),
                            (None, True), (42, False)):
            with self.subTest(stamp=stamp, flag=flag):
                changed = {**transfer, "evidence_minute": stamp, "evidence_recorded": flag}
                with self.assertRaisesRegex(ValueError, "evidence_minute"):
                    audit({**self.scenario, "handoffs": [changed, delivery]}, as_of=0)

        on_completion = {**transfer, "evidence_minute": 28}
        result = audit({**self.scenario, "handoffs": [on_completion, delivery]}, as_of=28)
        self.assertTrue(result["jobs"][0]["evidence_recorded"])
        self.assertEqual(result["jobs"][0]["evidence_minute"], 28)


if __name__ == "__main__":
    unittest.main()
