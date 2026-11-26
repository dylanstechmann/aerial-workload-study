# Civilian aerial workload study

An AI-assisted hobby prototype for reviewing the handoff from a fictional drone-assisted service to its recipient. It runs on Python 3.9 or newer using the standard library. The two examples cover construction progress imagery and last-mile parcel delivery. They use anonymous job IDs, relative minutes and an evidence flag. Flight and vehicle behavior are outside this model.

```bash
python3 audit.py handoffs.json
python3 audit.py handoffs.json --as-of 50
python3 audit.py handoffs-with-dependencies.json --as-of 55
python3 audit.py handoffs-with-delayed-evidence.json --as-of 41
python3 audit.py handoffs-with-delayed-evidence.json --as-of 42
```

A handoff at the deadline counts as on time. A record without evidence is unconfirmed even when its timestamp meets the deadline. A missing record has unknown timing and evidence. Duplicate and unknown job records are rejected. Use the per-job delay for scheduling analysis and the evidence flag for follow-up.

With `--as-of MINUTE`, the report shows only handoffs completed by that relative minute. Jobs without a visible handoff are `not_ready` before their ready minute, `pending` through the deadline, and `overdue` afterward. By default, recorded evidence becomes available at the completion minute. The review queue contains overdue and unconfirmed jobs, ordered by deadline and then anonymous job ID. At minute 50 the construction fixture has one confirmed late stockpile handoff. The delivery fixture has one unconfirmed parcel handoff and one overdue parcel without a visible handoff.

Jobs can list prerequisite job IDs in `depends_on`. Each direct prerequisite must have completed by the dependent handoff and have evidence available by the snapshot cutoff. `dependency_issues` flags missing or unconfirmed prerequisites and out-of-order completions. At a snapshot cutoff, later handoffs are invisible and the affected dependent job appears in the review queue. The handoff's own deadline and evidence status are reported separately. Unknown, repeated, self-referential and cyclic prerequisites are rejected. Omitting dependencies preserves the original report.

In the dependency example at minute 46, the restoration photo handoff already appears while its prerequisite stockpile photos are still missing from the snapshot. At minute 55, the stockpile record is visible and the audit flags the out-of-order restoration handoff. It also flags `parcel-03` because evidence for the preceding `parcel-02` handoff is missing.

## When proof arrives later

A handoff with `"evidence_recorded": true` can include an `evidence_minute` from its completion minute through minute 10080, inclusive. The value must be an integer. Earlier snapshots show the completed handoff as unconfirmed and omit the evidence timestamp. At the receipt minute the report shows the timestamp and uses the original completion minute to determine whether the handoff met its deadline.

In the delayed-evidence example, the depot transfer finishes at minute 28 and recipient delivery finishes at minute 35. At minute 41 both jobs are in the review queue because the transfer receipt is outstanding. At minute 42 the receipt arrives, confirms the transfer and clears the dependency issue. Both handoffs met their deadlines.

Run `python3 -B -m unittest -v` in this directory to check snapshot boundaries, queue ordering, evidence timing and dependency validation.

The construction job IDs refer to the topographic survey, spoil-handling and surface-restoration packages in the Open Construction Equipment BUNKER-01 work breakdown. The dependencies describe fictional photo and parcel handoffs. The fictional photos would supply context to a human reviewer. Survey control, inspection and construction acceptance remain with the qualified specialists named in the work breakdown. All timestamps, confirmations and parcel records were generated for this example. [Source work breakdown](https://github.com/dylanstechmann/open-construction-equipment/blob/42783cadb5fcb72f2dbd9dd56986a3050d8e8595/projects/bunker01/construction/work-breakdown.csv)
