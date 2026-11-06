# Workspace compatibility histories

Recorded on 2026-11-06 using a `git archive 67b4f5bc` checkout and a real local
Temporal server. The baseline Workflow source is unchanged. A fixture activity
returns its actual queue or raises a controlled transport-outcome error for a
push, so these histories test scheduling and failure compatibility without
touching a real repository or publishing a remote side effect.

- `workspace-queue.json`: workspace queue schedules its activity on workspace.
- `legacy-task-queue.json`: old task queue schedules the activity on program.
- `legacy-push-failure.json`: push fails once, with no automatic retry.

Each history has 11 real server events. Recording evidence is
`.goal-tools/workspace-old-history-record.log`; temporary recording driver is
`/tmp/goal-record-workspace-history.py`. Fixtures contain no live credentials.
They remain separate from the existing 40 Task/Stage histories and must not be
regenerated merely to make a changed Workflow pass.
