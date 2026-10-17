# Implementation progress

## 2026-10-17 — explicit watched-deletion scope

- The browser submits the exact watched video IDs included in its confirmation, capped at the 100 loaded records.
- The server rejects missing, duplicate, oversized, stale, unwatched, or foreign selections before contacting Notion.
- Local deletion uses the same user-scoped IDs that were archived, rather than re-querying mutable watched state. Other videos marked watched while Notion is running survive.
- Regression coverage includes >100 watched records, another user's matching ID, concurrent watched changes, invalid selection and remote archive failure. CI runs the regression suite.
- Partial remote archive failures can still leave some Notion pages trashed while local records remain. Durable remote-operation tracking is still planned. No deployment performed.

The hashes in AUDIT.md describe the recovered initial commit, not subsequently edited files. VALIDATION.md records initialization checks; current changes are validated by the regression suite and this log.
