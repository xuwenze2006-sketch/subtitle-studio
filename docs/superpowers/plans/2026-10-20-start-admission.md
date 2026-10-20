# Studio Start Admission Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and superpowers:test-driven-development for this bounded task. The parent owns final review and integration; do not commit.

**Goal:** Keep control requests responsive during startup validation while admitting at most one current, validated run.

**Architecture:** Capture a detached selection snapshot under the controller lock, perform account/project/action/payload validation outside it, and admit only after a final atomic check. Selection epochs detect A→B→A; live job identity detects another run that starts and finishes during validation. Successful Studio account/settings writes increment a small configuration revision so stale configuration cannot be admitted.

**Tech Stack:** Python, threading, unittest, tempfile, existing Studio detached read views.

**Spec:** Parent task and ignored audit `output/验证记录/2026-10-20/continuous-optimization-batch6/startup-audit.txt`.

## Global Constraints

- Own only `studio.py`, new startup admission tests, the idle shutdown test, and this plan.
- No source edits to runner/file observation; no providers, network, real credentials/user media, full suite, or commit.
- Keep select and stop behavior unchanged, including idle stop returning `stopped=False` during unadmitted preflight.
- Preserve action/config errors, thread launch recovery, redaction and persistence.
- Final admission becomes the race boundary. Shutdown, project selection, or another run can win while validation is detached.

## Review Focus

- A competing run can complete before preflight returns; `busy` alone is insufficient.
- A selection can switch away and back; paths/project tokens alone are insufficient.
- Studio account/settings writes during preflight must invalidate captured pricing/readiness/redactions.
- Failed validation must not replace a previous job, stop event, marker, redactions, or run history.
- Worker launch failure must clear busy and leave a recoverable scrubbed summary.

### Task 1: Detached startup with atomic late admission

**Files:** Modify `subtitle_pipeline/studio.py`, create `tests/test_studio_start_admission.py`, update `tests/test_studio_idle_shutdown.py`.

**Interfaces:** `StudioController.start(data)` keeps its existing success response and worker invocation. Extract a detached snapshot helper reused by `_read_view`; extract preflight payload construction returning the action, payload, and redactions. Only final admission mutates live lifecycle state.

- [x] Add event-gated real state read tests for responsive progress/touch/idle stop, competing starts including complete-before-return, selection ABA, shutdown wins, admitted worker blocks shutdown, failed validation, account/settings changes, and thread launch failure.
- [x] Run focused tests against unchanged production code and save red output in batch6. Expected failures prove lock coupling and stale preflight invalidation gaps.
- [x] Extract snapshot creation without changing ordinary read behavior. Build all startup reads and payloads on that view. Under the final lock recheck closing, selection epoch/token, busy, job identity, and configuration revision; retain existing launch/failure/persistence code.
- [x] Update idle shutdown test to gate final admitted work rather than claiming first lock acquisition is admission.
- [x] Run focused tests and related Studio suites; save green output in batch6. No full suite under this delegated task.
- [x] Inspect the scoped diff and report limitations to the parent for review. No commit.

## Execution Notes

- Ruling: use live job object identity only for comparison at final admission. It remains strongly referenced through preflight, so object ID reuse cannot hide another run.
- Ruling: invalidate preflight after successful Studio credential/pricing writes; external configuration changes remain revalidated by workers as before.
- Ruling: no reservation exists before final admission. A stop against an idle controller during preflight does not cancel a future admitted run; this is explicit and tested.
- Verification: original red run reports 12 failures in eight tests; the additional late path identity case independently failed before moving final identity resolution after preflight. Final related Studio run passed 76 tests, and main controller run passed 68 tests. Existing related suites include localhost HTTP fixtures; no outbound/provider/account access occurred. Logs live in batch6.
- Limitation: no general startup speedup is claimed. Existing persistence snapshot/worker launch critical sections, selection reads, and stop-marker writes remain scoped as before. Root owns independent review, full verification, and integration.
