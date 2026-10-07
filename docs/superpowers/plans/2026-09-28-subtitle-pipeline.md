# Subtitle Pipeline Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for integration, with focused independent delegation. User authorized execution of the in-chat design.

**Goal:** Install and validate a resumable local subtitle pipeline with early per-chunk delivery.
**Architecture:** Python standard library, pure subtitle functions, limited network translator, scheduler with atomic state, small Tkinter launcher. Reuse installed whisper.cpp small and FFmpeg.
**Tech Stack:** Python 3.14, unittest, tkinter, FFmpeg, whisper.cpp.
**Spec:** ../specs/2026-09-28-subtitle-pipeline.md

## Global Constraints
- Preserve ongoing Subtitle Edit work; no broad process termination.
- Default 300 second cores with 2 second context; ASR workers 1, translation workers 1.
- Keep absolute timestamps; no overwrite of original input or external subtitles.
- No paid API; free Google translation is a draft, semantic review remains manual.

## Review Focus
- Unicode and spaces in paths; use subprocess argument arrays.
- Resume with changed media/config must fail safely.
- Translation outage must preserve ASR and report incomplete results.
- Overlap context must not shift global time or discard genuine repetitions.
- Cancel/restart must clear only owned lock/processes and rerun unfinished work.

## Task 1: Subtitle math
- [ ] Write and run failing tests in tests/test_subtitles.py.
- [ ] Implement subtitle_pipeline/subtitles.py: Cue, Chunk, parse_srt, render_srt, plan_chunks, merge_chunk_cues, check_cues.
- [ ] Independently review pure transformations and boundary tests.

## Task 2: Translation
- [ ] Implement subtitle_pipeline/translate.py with tests first: translate_texts(texts, source, target, cache_path, stop_event=None), one output per input, persistent cache and bounded retries.
- [ ] Test cache reuse, malformed response, timeout, cancelled job; one neutral live smoke test.

## Task 3: Scheduler and launcher
- [ ] Write state/resume/pipeline tests using injected fake ASR and translator.
- [ ] Implement runner.py: PipelineConfig, run_pipeline, atomic state, progress files, exclusive run lock, per-chunk durable outputs and final merge.
- [ ] Implement GUI and CLI plus desktop shortcut and Chinese guide.
- [ ] Verify real synthetic multi-chunk ASR/translation, resume and GUI start/stop.
- [ ] Fresh review, fix important findings, record evidence and hand off usable entry.

## Execution record
Proceed in current dedicated project on codex/subtitle-pipeline. Existing untracked setup guide/samples are preserved. No initial git commit exists, so a separate worktree is unnecessary; new code is additive and isolated by package/tests paths.
