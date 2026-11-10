# Explicit historical version selection

Manual, temporary Go probes ran on 2026-11-10 with the race detector. The replay
sources are preserved as `.go.txt` artifacts; they are not permanent package test
files. No real user files or production state were used.

The Runtime HTTP probe uses real SQLite catalog, Ingress and disk ObjectStore.
It creates an original version and a newer head, then confirms the new version
metadata route returns the requested original bytes reference while retaining
the current logical head. Wrong file/version pairs, other users, and revoked
read grants are denied; an explicit current read grant can retrieve it.

The Coding HTTP probe uses the actual authenticated session and task handlers,
with a controlled Runtime transport fixture. A `source_versions` selection
resolves the requested immutable URI, forwards it in Task attachments, and is
persisted in task history. A mismatched response version and an extra unselected
file ID are rejected. This is an HTTP wiring/authorization check, not a claim of
model or document-engine execution.

Existing source/upload clients can still omit `source_versions`. Only explicit
version selections use the new metadata endpoint. The Composer preserves and
shows the selected version, and historical message source links reopen it.

A final probe also submits ordinary `source_ids` without an explicit version
map. The returned Task record now stores every resolved source's version, not
only explicit historical choices. New task-history links therefore retain the
version actually sent even if the logical head changes later. Older task records
that predate this metadata cannot be reconstructed from current head metadata.

The final Runtime probe also verifies candidate visibility on a shared file.
The owner sees its own candidate. A different actor with a write grant can still
see both authorized immutable versions but does not see the owner's private job
in its candidate list; the detail projection now matches the job route's actor
boundary, avoiding a visible review button that can only return 404.
