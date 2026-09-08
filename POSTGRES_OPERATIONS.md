# Postgres Operations

**Commit this document describes:** see the commit this pass ends on.

This is operational guidance for the self-hosted Postgres instance backing this application at launch. Nothing described here is automated or provisioned by this repository — no backup job, no monitoring dashboard, no replica exists as code or infrastructure yet. This document exists so the launch operator has a concrete procedure to follow, and so "we have backups" is never asserted without a tested restore behind it.

## Current state (verified against this repository, not assumed)

- Single Postgres 15 instance (`docker-compose.yml`'s `db` service in dev; a self-hosted equivalent at launch).
- 8 migrations, managed by Alembic (`backend/migrations/`), applied via `alembic upgrade head`.
- Application connects as the restricted `skincare_app` role (migration `7b38b717546e`); migrations themselves run as the superuser/owner role.
- Row-level security on all four application tables (`users`, `refresh_tokens`, `user_profiles`, `consent_events`) — see `SECURITY_AND_SAFETY_NOTES.md`.
- No replica. No PITR. No automated backup job. This document's own existence is the first step toward closing that gap, not a claim it's closed.

## Backup schedule (recommended, not yet automated)

- **Base backups**: daily, via `pg_basebackup` (or the hosting provider's managed snapshot equivalent, if the launch Postgres is a managed instance rather than self-hosted on the VPS).
- **WAL archiving**: continuous, `archive_mode = on` with `archive_command` shipping WAL segments to the backup destination as they close — this is what makes point-in-time recovery possible between daily base backups, not the base backups alone.
- **Destination**: Cloudflare R2, via the `ObjectStorage`/`CloudflareR2ObjectStorage` abstraction introduced this pass (`backend/app/storage/`) or a dedicated backup tool (`wal-g`/`pgbackrest`) configured with R2 as its S3-compatible target — either way, off the VPS the database itself runs on, so a lost VPS doesn't also lose the backups.
- **Encryption**: backups must be encrypted at rest. `wal-g`/`pgbackrest` both support this natively (age/GPG); if using a bucket without a tool, encrypt before upload — never rely solely on R2's own at-rest encryption as the only layer, since that protects against a different threat (Cloudflare's storage being compromised) than a leaked access key protects against.
- **Retention**: recommend 7 daily base backups + their WAL, 4 weekly, 3 monthly — tune once actual data volume and the operator's actual RPO/RTO requirements are known; these are starting numbers, not a measured requirement.

## Restore procedure (must be drilled, not just documented)

1. Provision a fresh Postgres instance (same major version).
2. Restore the most recent base backup preceding the target recovery point.
3. Configure `restore_command` to fetch WAL segments from the backup destination.
4. Set `recovery_target_time` (or `recovery_target_lsn`) for the desired point-in-time, or omit it to replay to the latest available WAL.
5. Start Postgres in recovery mode; verify it reaches the target and promotes cleanly.
6. **Run the application's own test suite's `tests/database/` smoke checks against the restored instance** (`test_all_expected_tables_exist`, `test_migrations_reach_head`) as a concrete, automatable acceptance check that the restore is actually usable, not just "Postgres started without erroring."

A restore drill that has never been executed is not a backup strategy — it's a hope. This procedure should be run against a real base backup at least once before launch, and on a recurring schedule (quarterly is a reasonable starting cadence) after that, with the actual wall-clock time recorded as the measured RTO.

## Upgrade strategy

- Minor version upgrades (e.g. 15.3 -> 15.4): in-place, during a maintenance window, after a fresh base backup.
- Major version upgrades (e.g. 15 -> 16): `pg_upgrade` or logical replication to a new major-version instance, then cutover — never in-place without a tested fallback, and always preceded by a full backup plus a restore-tested fallback plan.

## Replica strategy (not built; documented for when it's needed)

A streaming read replica becomes worth the operational cost when either is true (see `SCALING_TRIGGERS.md`):

- Read load materially contends with write latency on the primary.
- The recovery-time requirement for a primary failure drops below what a restore-from-backup can deliver (a warm streaming replica promotes in seconds; a restore from R2 takes as long as the last drill measured).

Until then, a single primary with WAL-archived backups is the honest, right-sized posture — not because replicas are hard, but because an unused replica is pure operational surface area (another thing to patch, monitor, and pay for) with no user-facing benefit yet.

## Monitoring (not built; minimum viable list for when observability lands)

- Connection count vs. `max_connections` and vs. the application's own configured pool `max_size` (`app/config.py`'s `db_pool_max_size`).
- Replication lag (once a replica exists).
- WAL archiving failures — an `archive_command` that starts silently failing is the single most common way self-hosted PITR setups discover, only during an actual outage, that their backups stopped working weeks earlier.
- Disk usage and growth rate.
- Long-running/blocked queries (`pg_stat_activity`).

See `PLATFORM_FOUNDATION` observability phase (`OPEN_ENGINEERING_ITEMS.md`) for when this becomes real metrics rather than a list.
