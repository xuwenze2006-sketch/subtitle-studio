# Studio Control I/O Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and superpowers:test-driven-development. Parent owns review, full verification and release.

**Goal:** Keep slow stop-marker writes and project selection reads from blocking unrelated controller requests, while preserving task and selection ownership.

**Architecture:** Stop captures the exact job, event and unique marker before detached I/O; a per-run I/O lock serializes duplicate stop requests outside the live lock. Selection prepares a detached candidate, including restoration evidence and pre-resolved identities, then atomically checks the captured selection epoch and job before committing.

**Tech Stack:** Python, threading, unittest, synthetic temporary projects.

**Spec:** Existing stop/selection semantics and batch7 audit artifacts under `output/验证记录/2026-10-20/continuous-optimization-batch7/`.

## Global Constraints

- Preserve marker-write failure semantics: a failed cloud stop must not set the cancellation event or mislabel natural completion.
- An old request must never mutate or cancel a replacement task, even after that task has already completed.
- Keep loaded-project validation, source binding, fresh-directory protection, bounded run history, redaction and durable selection records.
- No paid providers, real user media, account changes, dependency updates or destructive cleanup.
- Stop and selection implementers edit disjoint methods in the existing shared worktree; no whole-file restore or commit.

## Review Focus

- A finishing worker may clear a marker before a delayed stop writer creates it; stale writers must clean only their own marker.
- Duplicate stop writers must not recreate a marker after stale cleanup or block a newer run's stop.
- A start can begin and finish during selection preparation; idle status alone cannot authorize the stale selection.
- Selection A to B to A and overlapping selections require epoch checks, not path comparisons alone.
- Completed run restoration requires actual saved evidence; failed preparation must not mutate live state or history.

### Task 1: Detach cloud stop marker writes (round 18)

**Files:** `subtitle_pipeline/studio.py` init/start/stop only; `tests/test_studio_stop_concurrency.py`.

- [x] Save deterministic baseline failure for progress/touch while a marker write is blocked.
- [x] Cover finish/new-run, duplicate stops, failed write and cleanup, and an independent stop on the replacement run.
- [x] Capture exact per-run ownership, serialize marker I/O outside the live lock, and commit cancellation only while ownership remains current.
- [x] Run focused and existing stop/cancellation/persistence tests; independently review race schedules.

### Task 2: Detach project selection preparation (round 19)

**Files:** `subtitle_pipeline/studio.py` selection and narrowly required history helpers; `tests/test_studio_selection_concurrency.py`.

- [x] Save deterministic baseline failure with real state/manifest reads gated in temporary projects.
- [x] Cover both start/selection orders, completed competing run, overlapping selection/ABA, closing, restoration evidence, fresh naming and invalid loaded projects.
- [x] Prepare a candidate outside the live lock; use pre-resolved history identities at the final atomic commit and reject stale candidates.
- [x] Run focused and existing selection/start/persistence/shutdown tests; independently review history and failure semantics.

### Task 3: Verify and publish

- [x] Independent review of the combined diff, with fixes reverified before freezing source hashes.
- [x] Run the complete offline backend suite and loopback HTTP smoke test against frozen source/test hashes.
- [x] Record measured benefits, costs and evidence boundaries; count only the two demonstrated product fixes.

**Post-commit release gate:** Scan the exact public files, commit and push normally; verify exact-commit Windows CI, then fast-forward the clean primary checkout and hash-check copied flat evidence. Record the actual outcome in batch7 `release-receipt.json` after those operations, without a second documentation-only release.

## Verified outcome before commit

Baseline new-stop tests: 8 methods, 7 expected failures including both mkdir/write subcases; final 8/8 green. Baseline selection tests: 8 methods, 12 expected subcase failures; final 8/8 green. Related stop regressions 86/86, selection regressions 54/54. Independent reviewer accepted combined implementation after 16 tests plus 6 supplemental probes. Frozen full backend: 1,361 run, 1,358 pass, 3 Windows symlink privilege skips; loopback HTTP 26/26, provider requests zero; 143 source/test hashes unchanged.

The next five-round reflection remains due at round 20; do not invent a third change to reach it.
