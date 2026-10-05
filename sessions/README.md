# Long-Session Session Composer

Builds long-session multi-turn PinchBench tasks by composing existing
single-turn tasks. Design authority (ARIES repo):
`docs/customization/benchmarks/pinchbench/multi-turn-personas-design.md`.

This lives in the benchmark checkout because that is where tasks are defined:
ARIES pins this repository (`catalog/workloads/pinchbench.yaml`), clones it to
`/benchmark/pinchbench-skill`, and `pkg/benchmark/pinchbench` loads
`tasks/*.md` from it. The composer and its manifests therefore ship alongside
`tasks/`, not in ARIES.

A **session manifest** (`sessions/<name>.yaml`) declares problem *threads*
(ordered lists of existing collectible task IDs), an explicit interleaving
`order`, and a few hand-written `synthesis` turns. The composer emits one task
markdown under `tasks/` that merges the referenced tasks' fixtures, lists their
prompts verbatim in order, and defines a single aggregated
`grade(transcript, workspace_path)`.

Only deterministic grading is consumed: `grading_type: automated` in full and
`hybrid` by its automated `grade()` half. `llm_judge`-only tasks (no
`## Automated Checks` python block) are rejected by construction.

## Usage

```sh
# Generate the composed tasks into this checkout (defaults to the repo root):
python3 scripts/compose_sessions.py

# Drift check: regenerate and compare against the committed tasks.
python3 scripts/compose_sessions.py --check

# Unit tests (hermetic; no checkout needed):
python3 -m pytest tests/test_compose_sessions.py
```

`--root` and `--sessions-dir` default to this repository's root and `sessions/`.
`--only <name>` composes a single manifest. Commit the regenerated
`tasks/task_<persona>-day.md`, then update ARIES's `catalog/workloads/pinchbench.yaml`
pin to the new commit.

## Validation (fail-closed)

The composer rejects a manifest when any of these holds:

1. a fixture name is declared with different content by two tasks;
2. two tasks write the same output file (static scan of the prompt's backticked
   filenames and the grade code's fixed `workspace / "..."` paths);
3. a task's grade code globs `*.ext` and another task produces a top-level
   `*.ext` file (e.g. `task_workflow`'s `*.py` vs coding outputs);
4. a referenced task requires network access or fws/gh prerequisites;
5. a referenced task has no deterministic `grade()` (llm_judge-only);
6. `order` does not cover every thread step and synthesis turn exactly once.

The emitted task is re-parsed (frontmatter, sessions, workspace_files) and its
aggregated `grade()` is compiled before the file is written.

## Manifests

Four personas ship today: `engineer-day`, `analyst-day`, `sre-day`,
`assistant-day`. See the ARIES design doc §7 for the thread/order rationale.
