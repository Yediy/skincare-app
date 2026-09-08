# Dokploy Deployment

**Commit this document describes:** see the commit this pass ends on.

This describes how to deploy `backend/Dockerfile`'s image under Dokploy. Nothing in `backend/app/` contains a Dokploy-specific assumption — this document, not application code, is where Dokploy-specific configuration lives, per `PRODUCTION_ARCHITECTURE.md` principle 6.

## Required environment variables

| Variable | Required | Notes |
|---|---|---|
| `ENVIRONMENT` | Yes | Set to `production`. Triggers `app/config.py`'s startup validation — the app refuses to start if any variable below is left at a dev/CI default. |
| `JWT_SECRET` | Yes | 32+ random characters. Never the value in `.env.example` or any value in this repository's test fixtures/CI config — `Settings` explicitly rejects the known test/CI secrets. |
| `DATABASE_URL` | Yes | `postgresql://skincare_app:<real-password>@<host>:5432/<db>`. Must not contain `localhost`, `127.0.0.1`, `postgres:postgres@`, or the dev role's own dev-only password — `Settings` rejects all four in production. |
| `REDIS_URL` | Yes | `redis://<host>:6379/<db>`. Must not be `localhost`/`127.0.0.1`. |
| `ENABLE_DOCS` | Yes | `false` in production — `Settings` rejects `true`. |
| `ALLOWED_ORIGINS` | Yes | A JSON array of explicit origins, e.g. `["https://app.example.com"]`. `Settings` rejects the wildcard default. |
| `APP_BASE_URL` | Recommended | The public URL this deployment is reachable at. |
| `LOG_LEVEL` | Optional | Default `INFO`. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | Optional | Defaults 15 / 30. |
| `REDIS_CONNECT_TIMEOUT_SECONDS` / `REDIS_SOCKET_TIMEOUT_SECONDS` | Optional | Default 5.0 / 5.0. |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | Optional | Default 2 / 10. Multiply by replica count when sizing Postgres `max_connections` — see `SCALING_TRIGGERS.md`'s PgBouncer trigger. |
| `DB_POOL_CONNECT_TIMEOUT_SECONDS` / `DB_POOL_COMMAND_TIMEOUT_SECONDS` | Optional | Default 10.0 / 30.0. |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET` / `R2_ENDPOINT` | Optional | Only if a feature using `ObjectStorage` is deployed. Unset is fine — nothing in the request path depends on these yet. |

Set every one of these as Dokploy environment variables / secrets, never baked into the image — `backend/.dockerignore` already excludes `.env*` from the build context so this isn't a configuration option that can silently go wrong.

## Container port

`8000` (the `EXPOSE 8000` / `uvicorn --port 8000` in `backend/Dockerfile`). Map Dokploy's ingress to this port.

## Health / readiness endpoints

- **Liveness**: `GET /health/live` — used by the Dockerfile's own `HEALTHCHECK` already; point Dokploy's liveness probe (if configured separately from Docker's native healthcheck) at the same path. Never gate a restart on `/health/ready` — a temporary Postgres/Redis blip would restart-loop an otherwise-healthy process (`PRODUCTION_ARCHITECTURE.md`, Phase 3's own rationale).
- **Readiness**: `GET /health/ready` — use this to gate traffic (load-balancer target health / "ready to receive requests"), not to restart the container.

## Postgres / Redis connection

Neither is deployed by Dokploy alongside the API in this document's recommended topology (see `PRODUCTION_ARCHITECTURE.md`'s target topology) — both are separate services the API connects to over the network via `DATABASE_URL`/`REDIS_URL`. If Dokploy is also used to run Postgres/Redis as separate services, ensure they are **not** on the same host as the API for anything beyond initial launch-scale convenience (`PRODUCTION_ARCHITECTURE.md` Phase 32's explicit warning against collapsing control plane / API / Postgres / Redis / CV worker onto one machine).

## Migration procedure

Migrations are not run automatically by the container's `CMD` — `uvicorn app.main:app` does not call `alembic upgrade head` itself. Run migrations as an explicit, separate step before rolling out a new image version that depends on schema changes:

```bash
docker run --rm \
  -e DATABASE_URL="<production DSN, superuser/owner role — migrations run as the table owner, not skincare_app>" \
  skincare-app-backend:<tag> \
  python -m alembic upgrade head
```

Run this **before** rolling the new API image out, and ensure the migration is backward-compatible with the currently-running (old) image for the duration of the rollout — a rolling deploy briefly runs old and new code against the same schema.

## Rollback procedure

1. Redeploy the previous image tag.
2. If the failed deploy included a migration that isn't backward-compatible with the previous image, run that migration's `downgrade()` first — every migration in `backend/migrations/versions/` implements a real `downgrade()`, not a stub (verified by inspection of each file in this repository).
3. Confirm `/health/ready` returns `200` on the rolled-back version before considering the rollback complete.

## Worker deployment (later)

Not applicable yet — no separate CV worker or queue exists (Phase 14/15, `OPEN_ENGINEERING_ITEMS.md`). When it does, it deploys as its own Dokploy service/image, consuming the same `JobQueue` abstraction the API's `enqueue` call writes to, scaled independently of the API replica count per `SCALING_TRIGGERS.md`.

## Image size, stated honestly

The built image (`skincare-app-backend:test`, built and run locally as part of verifying this document) is approximately 2.1GB, driven almost entirely by `mediapipe`/`opencv-contrib-python`/`scipy`/`jax`/`jaxlib` — mediapipe's own dependency tree, not anything this application added on top of it. This is a real operational cost (image pull time on deploy, registry storage) worth knowing going in, not a defect to silently fix in this pass — trimming it would mean auditing whether `jax`/`jaxlib`/`matplotlib` are genuinely required by the mediapipe features this application actually uses, which is a real follow-up, not a one-line change.
