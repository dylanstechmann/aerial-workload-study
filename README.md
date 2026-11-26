# Civilian aerial workload study

An AI-assisted hobby prototype for reviewing the handoff from a fictional drone-assisted service to its recipient. It runs on Python 3.9 or newer using the standard library. The two examples cover construction progress imagery and last-mile parcel delivery. They use anonymous job IDs, relative minutes and an evidence flag. Flight and vehicle behavior are outside this model.

```bash
python3 audit.py handoffs.json
python3 audit.py handoffs.json --as-of 50
python3 audit.py handoffs-with-dependencies.json --as-of 55
python3 audit.py handoffs-with-delayed-evidence.json --as-of 41
python3 audit.py handoffs-with-delayed-evidence.json --as-of 42
python3 audit.py handoffs-with-delayed-evidence.json --compare-from 41 --as-of 42
```

A handoff at the deadline counts as on time. A record without evidence is unconfirmed even when its timestamp meets the deadline. A missing record has unknown timing and evidence. Duplicate and unknown job records are rejected. Use the per-job delay for scheduling analysis and the evidence flag for follow-up.

With `--as-of MINUTE`, the report shows only handoffs completed by that relative minute. Jobs without a visible handoff are `not_ready` before their ready minute, `pending` through the deadline, and `overdue` afterward. By default, recorded evidence becomes available at the completion minute. The review queue contains overdue and unconfirmed jobs, ordered by deadline and then anonymous job ID. At minute 50 the construction fixture has one confirmed late stockpile handoff. The delivery fixture has one unconfirmed parcel handoff and one overdue parcel without a visible handoff.

Jobs can list prerequisite job IDs in `depends_on`. Each direct prerequisite must have completed by the dependent handoff and have evidence available by the snapshot cutoff. `dependency_issues` flags missing or unconfirmed prerequisites and out-of-order completions. At a snapshot cutoff, later handoffs are invisible and the affected dependent job appears in the review queue. The handoff's own deadline and evidence status are reported separately. Unknown, repeated, self-referential and cyclic prerequisites are rejected. Omitting dependencies preserves the original report.

In the dependency example at minute 46, the restoration photo handoff already appears while its prerequisite stockpile photos are still missing from the snapshot. At minute 55, the stockpile record is visible and the audit flags the out-of-order restoration handoff. It also flags `parcel-03` because evidence for the preceding `parcel-02` handoff is missing.

## When proof arrives later

A handoff with `"evidence_recorded": true` can include an `evidence_minute` from its completion minute through minute 10080, inclusive. The value must be an integer. Earlier snapshots show the completed handoff as unconfirmed and omit the evidence timestamp. At the receipt minute the report shows the timestamp and uses the original completion minute to determine whether the handoff met its deadline.

In the delayed-evidence example, the depot transfer finishes at minute 28 and recipient delivery finishes at minute 35. At minute 41 both jobs are in the review queue because the transfer receipt is outstanding. At minute 42 the receipt arrives, confirms the transfer and clears the dependency issue. Both handoffs met their deadlines.

Add `--compare-from MINUTE` to an `--as-of` snapshot to see which jobs entered, cleared or stayed in each scenario's review queue between the two cutoffs. The comparison minute must be at or before the snapshot minute. Each list keeps queue order. In the example from minute 41 to 42, `depot-transfer` and `recipient-delivery` both appear under `cleared`. Use the rest of the report to see the handoff timing and evidence at minute 42.

Run `python3 -B -m unittest -v` in this directory to check snapshot boundaries, queue comparisons, evidence timing, dependency validation, malformed documents, minute limits, and the offline OCE adapter.

## Input schema and validation

`handoffs.schema.json` is the versioned JSON Schema (draft 2020-12) for `synthetic-aerial-handoffs/1`; its `$id` also contains `/synthetic-aerial-handoffs/1/`. It describes the exact object fields, identifier format, arrays, booleans and minute bounds used by the fixtures. The CLI always runs the equivalent dependency-free structural and relational validator before producing a report. Python callers can use `validate_document(data)` for validation alone or `audit_document(data, as_of=...)` for validation and reports. No `jsonschema` package or network access is required.

The schema cannot by itself express all relational rules, so the built-in validator additionally enforces ready/completion/evidence ordering, unique scenario and job IDs, known handoffs, unique known prerequisites, and acyclic dependencies. Unexpected or missing fields and malformed top-level values are errors rather than partial reports.

## Offline OCE evidence-handoff adapter

`oce_evidence.py` is a separate, opt-in validator for linking synthetic aerial receipts to canonical OCE work-package IDs. It does not change `audit.py`, the existing fixtures, or their reports.

```bash
python3 -B oce_evidence.py --manifest aerial-oce-evidence.json --oce-result oce-result-fixture.json
```

Both paths are explicit and local. The adapter uses only the Python standard library, performs no network access, does not import or execute OCE, and has no default sibling-checkout path. The caller must supply an OCE BUNKER-01 result/export whose top-level `schemaVersion` and nested `evidenceHandoff.schemaVersion` are both `1.0.0`. Package IDs and required BG gates come from `evidenceHandoff.workPackages`; direct dependencies come from `schedule.packages`, because the current evidence handoff intentionally does not duplicate them. The two package sets and their required-gate references must agree, every dependency must resolve, and the graph must be acyclic. The named canonical CSV registers remain authoritative; this offline check validates the supplied export's internal contract, not a live OCE checkout.

The aerial manifest uses version `synthetic-aerial-oce-evidence/1` and contains one `scenario` plus an `evidence` array. Every entry has exactly these fields:

- `evidence_id`: a unique anonymous lowercase identifier;
- `package_id`: one canonical uppercase `BW-NNN` ID present in the supplied export, with at most one evidence entry per package;
- `depends_on`: the package's exact direct dependency set from `schedule.packages` (array order is irrelevant, duplicates are rejected);
- `completed_minute` and `evidence_minute`: integers from 0 through 10080, with receipt no earlier than completion.

When both a package and its prerequisite have entries, dependent completion cannot precede prerequisite completion. A delayed prerequisite receipt does not rewrite either completion timestamp. An omitted prerequisite receipt remains absent; it is not synthesized or treated as accepted. Unknown IDs, missing or extra manifest fields, duplicate evidence IDs or packages, duplicate JSON keys, malformed JSON, nonstandard numbers, and invalid dependency sets are errors.

Inputs are bounded to 1,000,000 bytes each, at most 512 work packages, quality gates, or evidence entries, and at most 64 dependencies or required-gate references per entry. Strings and identifier formats are also bounded. The output report is versioned as `synthetic-aerial-oce-validation/1`. Its `source.artifactSha256` hashes the exact supplied OCE file bytes, and it records both OCE schema versions. Optional top-level `sourceCommit` (7–64 lowercase hexadecimal characters) and `sourceVersion` metadata are copied when an exporter supplies them; current OCE results are not required to contain those optional fields. The source path itself is not embedded in the report.

The adapter fails closed if construction authorization, physical validation, or human acceptance is asserted, or if any supplied BG gate is not `UNRESOLVED`. Every output required-gate entry remains unresolved with `acceptance_performed: false`; survey, construction, specialist, and overall human acceptance flags also remain false. **Evidence presence or receipt never resolves a BG human gate and never implies survey control, construction acceptance, specialist sign-off, physical validation, or authorization.**

`oce-result-fixture.json` and `aerial-oce-evidence.json` are original minimal synthetic fixtures. They use a small structurally valid subset of canonical BW/BG IDs and dependencies to exercise the current contract; they are not a replacement work breakdown, an OCE-generated project result, copied third-party content, or records of real evidence. Python callers can use `validate_files(manifest_path, oce_result_path)` or `validate_manifest(manifest, result, source_sha256=...)`.

## Synthetic and safety boundary

The construction job IDs refer to the topographic survey, spoil-handling and surface-restoration packages in the Open Construction Equipment BUNKER-01 work breakdown. The dependencies describe fictional photo and parcel handoffs. Fictional photos would supply context to a human reviewer. An evidence flag or timestamp is only a synthetic receipt-availability marker: it is **not** real proof of delivery, inspection acceptance, specialist sign-off, survey control, construction acceptance, or closure of any BUNKER gate. Survey control, inspection and construction acceptance remain with the qualified specialists named in the work breakdown. All timestamps, confirmations and parcel records were generated for these examples. [Source work breakdown](https://github.com/dylanstechmann/open-construction-equipment/blob/42783cadb5fcb72f2dbd9dd56986a3050d8e8595/projects/bunker01/construction/work-breakdown.csv)
