# Operations and recovery

## Inspect the existing deployment

```bash
flyctl status -a elmontaqa
flyctl config show -a elmontaqa
flyctl checks list -a elmontaqa
flyctl volumes list -a elmontaqa
flyctl logs -a elmontaqa --no-tail
```

The app belongs to `muaaz-amr`. Do not create another app with `fly launch` as part of routine updates. One machine is now kept running in the primary region to avoid idle cold starts.

## Before the first deployment from this repository

1. Review the recovered-source hashes and all local changes. The Dockerfile is reconstructed and still runs the baseline server as root.
2. Create a consistent SQLite backup using the SQLite backup API, not a plain copy of the active main file in WAL mode. Store encrypted backups outside the attached volume. Verify restore to a separate test app and run integrity and foreign-key checks.
3. Retain the current APP_SECRET securely alongside recovery procedures. Changing it without a data migration makes encrypted integration credentials unreadable and invalidates signed sessions. Never print or commit it.
4. Build and test the image locally, then in a separate staging Fly app with synthetic data and a separate Notion integration. The unpinned yt-dlp requirement prevents exact dependency reproduction.
5. Verify environment variable names from `.env.example`; inspect secret names only if needed. The source does not load dotenv files. Never copy production credentials into this repository.
6. Add and verify readiness checks, migration behavior, and a rollback procedure before enabling automatic production releases.
7. Confirm the target app is `elmontaqa` in `muaaz-amr`; deploy deliberately with `flyctl deploy -a elmontaqa` after staging validation. This initialization did not deploy.

## Rollback

Record the previous image and release before a deployment. A rollback of code does not undo SQLite schema/data changes or remote Notion mutations. Restore a verified backup into a separate volume/app first, reconcile any writes since the backup, and use backward-compatible migrations. Do not overwrite the live volume as a routine rollback step.

## Proposed operating targets

- Initially: encrypted offsite backups at least daily, proposed recovery point <=24 hours and recovery time <=2 hours, proven by restore rehearsal.
- Track DB/WAL size, memory, response latency, sync queue age, failed external requests, failed logins, and backup age.
- If background sync is introduced, ensure an independent scheduler/worker remains runnable while the web machine auto-stops; do not assume an in-process timer runs on a stopped machine.
- Additional SQLite machines require a replication/single-writer design. Scaling count alone creates independent volumes, not replicas.


## 2026-10-18 performance deployment recovery

Previous image: `registry.fly.io/elmontaqa:deployment-01M540KES32P5S6Z44M47FQF59`.
A consistent encrypted backup was created and restore-checked on the attached volume at `/data/pre-performance-20261018-090140.sqlite3.aesgcm`. It is a local recovery copy, not an offsite backup. Preserve the current APP_SECRET.

Backup format: first 12 bytes are the AES-GCM nonce; remaining bytes are ciphertext with tag. The key is SHA-256 of APP_SECRET bytes followed by `elmontaqa-backup-v1`; associated data is `elmontaqa-backup-v1`. Decrypt into a separate temporary database file, then open with SQLite and verify integrity. Never overwrite the active database during verification.

This performance change adds an index and keeps existing tables compatible with the previous image. A code rollback can use `flyctl deploy -a elmontaqa --image registry.fly.io/elmontaqa:deployment-01M540KES32P5S6Z44M47FQF59 --ha=false`; it does not require restoring the database. Restoring the old cold-start behavior also requires setting `min_machines_running` to 0 before deployment.
