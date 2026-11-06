# Workspace D-25 / WS-CF-524 implementation (2026-11-07)

D-25 uses the approved `workspace.v1` contract. Root workspaces retain their existing layout; multi workspaces are containers for independently named repositories. The Worker validates every repository selector, path, access level, quota and remote credential. Git operations select the persisted repository directory and remote, while archives and copies continue to include the entire workspace tree. The migration `213 workspace_multirepo` adds layout, repository records and the repository component of push audit records. Successful push evidence is retained per repository so detaching a repository does not mislabel a known pushed commit as unpushed. A failed attach preserves existing repositories. Dirty detach requires `discard_changes`.

WS-CF-524 adds `StartWorkspaceOperation` and `GetWorkspaceOperation`. A caller supplies a stable `request_id` and one existing operation request. The server returns an operation ID after durable acceptance, with no wait for cloning, setup, archive or restore. Poll with the same authenticated caller and workspace. Responses are RUNNING, COMPLETED with the original public response in `result`, or FAILED with the public error. Existing synchronous RPCs remain supported.

Migration `212 workspace_async_operations` stores an index of accepted IDs and their public results. The caller, tenant and request ID define identity; reuse with different serialized input is a conflict. Admission is serialized in the business database. A committed admission is returned unchanged even when Temporal has expired its history. Credentials are sealed before workflow start and are never stored in this index. The Worker persists the filtered public result before acknowledging the activity, and a completed activity retry reads it back; polling also retains terminal results. Temporal remains the execution state authority. No workflow command sequence changed.

Task-service applies 212 at startup; Worker applies 213 with existing workspace migrations. The sandbox archive format is unchanged. Multi git reads use the workspace Worker, since the Sandbox Service's root-only read RPC has no repository selector. ListFiles and ReadFile still address paths relative to the complete workspace root.

Validation: Buf lint/breaking pass; Go workspaceapi and task-service command tests pass. The real Sandbox Service suite (multi mount/archive lifecycle, isolated git operations, read-only behavior, invalid/conflicting paths, quotas, partial failure, dirty detach, copy/archive/delete, old root operations and Temporal execution) passed against both agents: 17 tests with the Go agent, then 19 tests including async result retention with the deployed Rust agent extracted from `sandbox-agent:809cb41`. Async tests include concurrent retries, parameter conflicts, tenant/caller/workspace isolation, public-field filtering, restart and expired-history deduplication. Coding-web UI and deployed acceptance are tracked by their integration owner.

Follow-up review: detach now checks every local ref plus detached HEAD rather
than HEAD alone, so switching away from an unpublished branch cannot hide its
commits. A corrupt ref/object inventory fails closed instead of being interpreted
as zero unpublished commits. `--all` also handles an empty repository without
requiring a HEAD commit. Added mount/archive regressions retain the repository
when inventory fails and require explicit discard for a non-current local branch.
The Git behavior was checked with real empty, detached and switched-branch
repositories. End-to-end Sandbox Service execution of these additional cases
remains pending: the current execution sandbox rejects socket creation with
EPERM. No assertions were removed or tests skipped. Independent async-result and
NET-OPEN Python checks pass (14 tests); Go workspace/task API and egress packages
compile, while their listening-server suites need the permitted host environment.

The approved D-25 baseline also calls for a legacy WorkspaceOperationWorkflow
golden. Existing Task/Stage histories remain intact, but no workspace golden is
currently present. Final integration must capture it using the pre-D-25 baseline
`67b4f5bc` and a real Temporal server, then replay it with the final implementation;
a synthetic fixture or the existing in-session workflow test is not that evidence.

Repository scoping follow-up: each selected repository must retain its own real
`.git` directory (a symlink or gitdir pointer to another repository is rejected).
Local scoped Git commands explicitly set their git-dir and work-tree, so a
sandbox-authored `core.worktree` cannot redirect discard/commit into a sibling.
The separately created safe bare repository used for credential-bearing remote
commands remains unchanged. `test_workspace_git_scope.py` exercises production
lease validation and command construction with real on-disk Git repositories:
three tests pass, including sibling file preservation, symlink/gitfile rejection
and continued safe-bare remote reads. Full mount/archive regressions are also
added, with the listening-server restriction above still applying.

READ remains the approved Git-operation policy, not a filesystem mount guarantee:
`RepoAccess_READ` rejects Commit/Push and permits Sync/Discard. The approved
`environment-config.md` decision 4 and D-25 section explicitly allow shell
commands to enter every repository and describe OAuth read-only access as a
policy restriction. No new chmod/mount isolation promise is introduced.
