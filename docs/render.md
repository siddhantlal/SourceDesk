# Deploy SourceDesk on Render (native Python, same origin)

This repository is **deployment-ready configuration, not evidence of a cloud deployment**. No Render resources, accounts, Git pushes, or paid model calls were created by this work. Deployment is an explicit operator action.

## Architecture and cost warning

The checked-in `render.yaml` defines **one paid Python web service** (`0.5c-512mb`, formerly Starter), **one paid PostgreSQL 18 database** (`0.1c-256mb`, formerly Basic-256mb), and a **1 GB persistent upload disk**. Budget for web compute, database compute/storage, the upload disk, and any applicable bandwidth/workspace costs; review the dashboard's actual estimate before confirming. These are not free-tier resources. Legacy compute-plan names remain valid, but this Blueprint uses current IDs.[1][5]

A persistent disk belongs to **one service instance**. Another Render worker service or one-off job cannot read it, and the disk is unavailable during build/pre-deploy. Disk-backed services cannot scale horizontally or perform zero-downtime deploys: Render stops the old instance before starting the replacement.[2]

Therefore `scripts/render_start.py` runs two **separate OS processes on the same instance**:

- One Uvicorn API process, one API worker, bound to `0.0.0.0:$PORT`.
- `python -m sourcedesk.worker`, using durable PostgreSQL claims/leases and `/var/data/uploads`.

There is no ingestion thread/background task inside FastAPI. One instance shares limited CPU/RAM between API and ingestion; load-test representative PDFs and increase the paid plan if necessary. This is not an HA configuration.

## Blueprint deployment

1. Review `render.yaml`, costs, retention policy, and provider mode. Have an operator commit/push the completed repository, including `frontend/`, `uv.lock`, scripts and migrations, to their chosen Git repository. This guide does not authorize that action.
2. In Render, choose **New > Blueprint**, select that repository/branch, and use the root `render.yaml`. Review the proposed resources and select **Deploy Blueprint** only after approving the charges.[9]
3. In the Blueprint's Settings, set **Auto Sync = No** if changes must require manual approval. The web service's `autoDeployTrigger: "off"` prevents code-triggered auto-deploys, but Blueprint auto-sync is a separate control.[1][9]
4. Keep both resources in **Oregon**. Do not add a separate worker service, extra web instances, a pre-deploy migration command, or a second disk-owning service.
5. The database connection string is injected with `fromDatabase`; the JWT secret is generated with `generateValue: true`. The database's empty IP allowlist disables public/external database connections while allowing private-network access. Keep the direct connection, not transaction-pooled PgBouncer, because provisioning holds a session advisory lock.[1]
6. Wait for the web deploy's `/ready` check to pass, then perform the checks below. Existing successful local checks do not establish that Render provisioning succeeded.

## Equivalent manual setup

If not using a Blueprint, create **New > PostgreSQL** first, then **New > Web Service** from the same repository. The web service must bind `0.0.0.0` and use Render's `PORT`; Render terminates public HTTPS at its load balancer.[8]

Use these exact values from the checked-in Blueprint:

| Setting | Value |
| --- | --- |
| Database name / database / user | `sourcedesk-db` / `sourcedesk` / `sourcedesk` |
| Database region / version / compute | Oregon / PostgreSQL 18 / `0.1c-256mb` |
| Database storage / autoscaling / pooling | 1 GB / disabled / none |
| Database external IP rules | Empty list; remove any public allow-all rule |
| Web name / language / region | `sourcedesk` / Python / Oregon |
| Web compute / instances | `0.5c-512mb` / 1; no autoscaling |
| Build | `bash scripts/render-build.sh` |
| Start | `uv run --locked --no-dev python scripts/render_start.py` |
| Health check / shutdown delay | `/ready` / 40 seconds |
| Persistent disk | `sourcedesk-data`, mount `/var/data`, 1 GB |
| Automatic deploys | Off |

Under web-service environment variables, set:

```text
PYTHON_VERSION=3.12.13
UV_VERSION=0.12.3
PYTHONUNBUFFERED=1
SOURCEDESK_DATABASE_URL=<the database's INTERNAL direct URL, entered privately>
SOURCEDESK_JWT_SECRET=<a privately generated random secret of at least 32 characters>
SOURCEDESK_PROVIDER=fake
SOURCEDESK_UPLOAD_DIR=/var/data/uploads
TIKTOKEN_CACHE_DIR=/var/data/tiktoken
SOURCEDESK_CORS_ORIGINS=[]
```

The version environment variables pin Render's Python and uv; `uv.lock` is recognized by the native Python runtime.[4] Never paste a real DSN/JWT/provider key into source control, shell history, tickets, or screenshots. Do not use `scripts/setup_db.py`: it configures a local WSL cluster, not Render.

The build performs `uv sync --locked --no-dev` and verifies `frontend/index.html` exists. Frontend files are already plain HTML/CSS/ESM: no npm/Node step is required. Do not populate `/var/data` in the build.

## Startup, shutdown, and migrations

After the disk is mounted, startup creates the upload/token-cache directories, retries a direct database connection up to 12 times (5-second connection timeout, 2-second retry delay), acquires a bounded session advisory lock, enables `vector`, and runs `alembic upgrade head` with a 180-second deadline. Render supports pgvector on PostgreSQL 13+ through `CREATE EXTENSION vector`; use the database-owner connection supplied by Render.[3]

Only after provisioning succeeds are API and worker started. Alembic uses the existing migrations and dynamic readiness heads, not a hardcoded revision. All SourceDesk Render entrypoints use the same advisory lock; unrelated manual migration commands do not automatically participate. A failed migration leaves the service unavailable instead of serving against an incompatible schema. Startup errors and Alembic subprocess output are redacted to avoid DSN leakage; investigate with an authorized private database session rather than logging connection strings.

SIGTERM/SIGINT asks both children to stop; on Linux the supervisor translates this to group SIGINT, which Uvicorn and the worker handle. After a shared 25-second grace period it kills remaining process groups and reaps them. The Blueprint gives Render a 40-second shutdown allowance; Render can force-kill a process that exceeds its configured allowance.[6] Windows uses process-group Ctrl-Break with a bounded `taskkill /T /F` fallback; Windows graceful behavior is best-effort for children without Ctrl-Break handlers. An unexpected child exit, even exit 0, tears down its peer and makes the supervisor exit nonzero. Startup migrations are also stopped on shutdown.

A restart can interrupt a claimed job. Claims/leases remain in PostgreSQL, so the existing worker can recover expired work on later polls; committed publication and durable file-cleanup behavior are unchanged. This does not promise that in-flight requests finish, that retries are unlimited, or that a killed migration can be ignored. Disk-backed deploy downtime includes migration time; plan maintenance windows and backward-compatible schema changes.

## Same-origin routes and security

- `/` serves `frontend/index.html`; `/assets/...` serves bounded static paths from that directory.
- API endpoints remain under `/api/v1`; `/docs`, `/openapi.json`, `/health`, and `/ready` remain operational. No SPA catchall converts unknown API paths to HTML; unknown paths/traversals stay 404.
- `SOURCEDESK_FRONTEND_DIR` optionally overrides the repository-based default. An absent frontend does not prevent importing/serving the API: `/` and `/ready` return 503 until the index exists. `/health` reports process liveness and provider mode; `/ready` also checks database connectivity, pgvector and current Alembic heads. Readiness is not an independent worker-progress heartbeat.
- Same-origin requests require no CORS allowlist; use the literal JSON string `[]`. Add explicit trusted origins only if intentionally hosting a different frontend origin.
- Responses include `nosniff`, `Referrer-Policy: no-referrer`, correlation IDs and `Cache-Control: no-store`, including auth/errors/assets. No global CSP is imposed that would break Swagger. Do not configure an edge cache to override private/auth response protections.
- Forwarded headers are trusted only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`). This intentionally does **not** trust arbitrary clients. If trusted Render proxy peers do not match, configure their verified IP/CIDR allowlist. Do not blindly use `*`; only consider it after confirming ingress cannot bypass the trusted edge. The current frontend uses relative same-origin URLs and does not depend on application-generated HTTPS URLs. Test absolute redirects/URLs after changing proxy settings.

## Opt into a real model only deliberately

Default **FAKE** mode requires no provider key and makes no paid model calls. Its deterministic outputs are plumbing/demo evidence, not LLM quality evidence. The optional `SOURCEDESK_OPENAI_API_KEY` is deliberately omitted from the Blueprint: a `sync: false` entry would prompt for a secret even when fake mode needs none.[1]

For an empty database, add the key privately in the Render UI, set `SOURCEDESK_PROVIDER=openai`, verify the base URL/models/budget, and redeploy. For documents already indexed with fake or different embeddings:

1. Back up PostgreSQL and original uploads. Preview the existing maintenance command in the live service's private Shell: `uv run --locked --no-dev python -m scripts.reindex` (dry-run only).
2. Choose a **new** `SOURCEDESK_PROCESSING_VERSION` and schedule downtime. Prepare the target provider/model/version settings and temporary start command together in **one manual Blueprint change/sync**, or save dashboard changes without deploying until all are ready. A key may be added privately while still in fake mode. Never deploy the target provider alone against old vectors.
3. For this already-migrated deployment only, use this temporary start command in that same deploy:
   ```sh
   uv run --locked --no-dev python -m scripts.reindex --confirm-all && uv run --locked --no-dev python scripts/render_start.py
   ```
   On a disk-backed redeploy the old API/worker stop before this command runs.[2] The maintenance command operates on **all tenants**, checks original PDFs, fences claims and verifies queued rows before the supervisor launches new processes. It must not run concurrently with another worker or from a separate Render one-off job.
4. Verify the maintenance result, then restore the normal start command immediately. The same-version guard deliberately refuses to repeat a reindex; if a restart occurs before removing the override, remove it and restart normally so queued work can resume. Preserve any temporary override in the source-of-truth configuration until removal; a Blueprint sync can otherwise overwrite dashboard changes.[9]
5. Wait for ingestion to complete, test retrieval/citations, and separately run an explicitly approved paid-provider quality check. Do not change the fixed 1536 dimensions without a schema migration and reindex.

## Backups, retention, and verification

Keep a documented retention policy for original PDFs, extracted/indexed text, answers, account records, logs and backup copies. User deletion and durable filesystem cleanup do not delete historical backup snapshots. Protect backups as sensitive tenant data and expire them according to policy.

Paid PostgreSQL has point-in-time recovery (currently 3 days for Hobby workspaces, 7 days for Pro or higher); logical exports are retained for 7 days. Download encrypted logical backups for longer retention and test restoration into a separate database.[7] Disk snapshots are daily and retained for at least 7 days; a full restore discards later filesystem changes.[2] Database recovery and upload recovery must be coordinated: a database row without its original PDF cannot be reindexed. Monitor the 1 GB upload disk and database storage; this Blueprint disables automatic database storage growth, so capacity needs operator attention.

After the operator's actual deploy, verify:

1. `/health` reports `fake_provider: true` initially; `/ready` returns 200; `/` loads and `/assets/app.js` is JavaScript, not HTML.
2. Register/login, create a collection, upload a small PDF, wait for ready, ask a cited question, open/download its source, and delete it. Confirm correlation IDs in private logs without secrets or document text.
3. `/api/does-not-exist` is JSON 404; a missing asset/path traversal is not index HTML. Swagger still loads.
4. In a maintenance window, redeploy and confirm a previously uploaded PDF persists. Exercise interrupted ingestion and observe durable recovery; local mocks do not establish Render process/disk behavior.

Repository checks performed without touching the running local database:

```sh
uv run --locked pytest tests/test_deployment.py -q
bash -n scripts/render-build.sh
uv sync --locked --no-dev --dry-run
```

The two focused tests cover URL normalization/rejection, same-origin routing/headers/API-only startup, mocked extension/migration ordering and bounded connection retries, plus real short-lived child failure/shutdown/forced teardown. The full `scripts/render-build.sh` also ran successfully with `UV_PROJECT_ENVIRONMENT` pointing to an isolated scratch environment (44 production packages installed); that environment served the actual `/`, JavaScript, stylesheet, docs and health routes with 200 responses, with unknown API paths returning 404. The Blueprint passed validation against Render's fetched official JSON Schema. The dry-run and isolated build deliberately avoid removing the shared local environment's test tools. Real Render provisioning, Linux signal delivery, model quality, resource sizing, restoration and migration execution against Render remain operator verification tasks.

## Sources

[1] https://render.com/docs/blueprint-spec
[2] https://render.com/docs/disks
[3] https://render.com/docs/postgresql-extensions
[4] https://render.com/docs/python-version
[5] https://render.com/docs/compute-plans
[6] https://render.com/docs/deploys
[7] https://render.com/docs/postgresql-backups
[8] https://render.com/docs/web-services
[9] https://render.com/docs/infrastructure-as-code
