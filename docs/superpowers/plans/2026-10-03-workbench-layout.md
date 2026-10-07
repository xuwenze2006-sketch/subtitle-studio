# Workbench and review layout implementation plan

**Goal:** Deliver the user-approved A light workbench and B dark, side-by-side subtitle review layout.

**Architecture:** Retain React/Vite, current API contracts and state ownership. Reuse Preview with a compact workbench variant; isolate dark review styling. No backend job, billing, review or export semantics change.

**Spec:** User approved the three concept images on 2026-10-03, selecting A for the main workspace and B for review. Main workspace uses a narrow project sidebar, center media/subtitles and right progress; review uses a compact header, video left and searchable bilingual cues right.

**Constraints:** Preserve all existing dirty work, account encryption, saved paths, provider limitations and manual-review gates. Use actual project data only. No paid API requests for tests, no service interruption. Small displays must stack without clipped controls.

## Tasks

- [x] App shell: compact sidebar with new task and recent projects; compact page headings; dark horizontal navigation in review; retain existing accessible navigation names and all connection/error states.
- [x] Workspace: compact source row, embedded Preview only for the matching persisted project; collapsible recognition settings; visible stage actions and side progress/stop control; collapsible logs. Keep all provider actions and review forms.
- [x] Preview: add `variant="workbench"` and `toReview` props; workbench stacks preview/cues with dedicated review entry; default review has independent video/list panes, dark palette, all existing playback/search/download actions; no fake edit or waveform controls.
- [x] Tests: retain full current UI coverage; add regression tests for workspace preview identity, review navigation and new-task reset. Review variant tests preserve search/seek/download and no auto-start/approval behavior.
- [x] Build and inspect at desktop and narrow widths using synthetic fixtures; read-only local service check. Review final diff and build production assets.

## Review focus

1. New unsaved source must never show a previous project's video/subtitles.
2. Busy task switching and new task creation remain disabled; navigation does not start jobs.
3. Review values survive page switching but reset when project identity changes.
4. Dark review must retain readable disabled controls, notices, empty states and account-sensitive content boundaries.
5. Long filenames/cues and small windows must not overflow page width; subtitle list scrolls locally.

## Verification

`npm test -- --reporter=dot`, `npm run build` in frontend; browser screenshots/interaction with local fixture server (no provider calls); `git diff --check` for touched files. Report browser verification separately from unit checks.

2026-10-03 outcome: 49 frontend tests pass; Vite production build and diff check pass. Browser fixture at 1536x1000 and 390x844 has no horizontal overflow; cue search filters results and SRT downloads succeed. Actual workbench-to-review transition retains 3s playback position, 1.25x speed and search query, remains paused. Automatic cue follow now scrolls only the cue list (document scroll stays zero). Review found and fixed navigation session loss; independent scoped rereview found no remaining actionable issues. No cloud transcription/translation calls made.
