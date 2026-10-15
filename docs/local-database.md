# Native local database and startup

SourceDesk uses a real PostgreSQL 18 cluster with pgvector 0.8.1. On Windows,
Python 3.12.13 runs natively and connects by TCP to PostgreSQL in the **Ubuntu**
WSL2 distribution. Linux development and CI use native Ubuntu services. No
container runtime, service container, or Testcontainers dependency is used.

## Scope and isolation

The setup scripts manage only the dedicated `18/sourcedesk` cluster on port
`55432`. An unrelated `18/main` cluster on port `5432` is not stopped, reconfigured,
or used by this project. Existing SourceDesk data is preserved; setup never drops
or truncates a database. Alembic applies forward migrations; take a backup before
applying migrations to valuable development data.

| Target | Database | Login / owner |
| --- | --- | --- |
| Application | `sourcedesk` | `sourcedesk_app` |
| Integration tests | `sourcedesk_test` | `sourcedesk_test_app` |

The two logins are not superusers and cannot create databases or roles, replicate,
bypass RLS, or inherit other roles. Each owns its own database so it can apply the
project's migrations. PUBLIC has no CONNECT privilege on either project database;
the application login cannot connect to the test database and vice versa. The
administrator, not the application, installs `CREATE EXTENSION vector` in each.
These are local development/migration roles, not a production deployment design.

The test fixture requires an explicitly named PostgreSQL test database distinct
from the application URL. Tests truncate only the isolated test tables. Never
point a test URL at application data.

## Windows prerequisites and first installation

Install WSL2 with a distribution named `Ubuntu` (Ubuntu 24.04 or 26.04), plus
native Windows `uv`. Run commands from the repository root in PowerShell:

```powershell
uv sync --locked --python 3.12.13
# This terminal is a keeper; leave it open until API/worker startup owns one.
wsl.exe -d Ubuntu -- sleep infinity
```

In a second PowerShell window:

```powershell
uv run --locked python scripts/setup_db.py --install --migrate both
.\scripts\start-api.ps1
```

Installation runs the narrowly scoped shell script as root in Ubuntu. It does not
install or select another WSL distribution. Without `--install`, no packages are
installed or upgraded. If script execution policy blocks the entry point, use the
process-local invocation (do not change machine-wide execution policy):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-api.ps1
```

`start-api.ps1` starts its own hidden `wsl.exe ... sleep infinity`, ensures the
cluster is running, refreshes the private WSL address, verifies both databases,
upgrades **only the application database**, and runs Uvicorn on
`127.0.0.1:8000`. Use `-Port 8011` or `-Reload` when needed. Its keeper is stopped
when the foreground API command exits. In another terminal:

```powershell
.\scripts\start-worker.ps1
# Process at most one eligible job, then exit:
.\scripts\start-worker.ps1 -Once
```

The worker owns its own keeper and checks connectivity but never applies
migrations. Start the API first (or explicitly provision/migrate beforehand).
Repeated API startup reloads access rules without restarting a healthy cluster;
a changed WSL listener address requires a restart of this project cluster only.

### Why the keeper matters

A PostgreSQL systemd service alone does not prevent WSL from stopping when no
Windows-launched Linux command remains. A user stop that kills `sleep infinity`
can therefore turn otherwise-correct database URLs into connection timeouts.
Do not use `wsl --shutdown` or `wsl --terminate Ubuntu` while SourceDesk runs.
For direct `pytest` or setup commands without an API/worker, keep the dedicated
`sleep infinity` terminal open. Closing all keeper processes permits WSL to stop.

If recovering from a stop, open a keeper, then run:

```powershell
uv run --locked python scripts/setup_db.py --refresh-wsl-host --migrate both
uv run --locked python scripts/setup_db.py --check
```

The address refresh is automatic for Windows setup; the explicit flag remains
available for clarity. WSL IP changes update only the two validated project URLs
in `.env` and `.env.test`; passwords, JWT secrets, comments, and unrelated settings
are preserved. Quoted URLs are supported. A non-project URL or an unexpected
existing database owner is rejected rather than modified.

## Private environment files

First setup exclusively creates missing `.env` and `.env.test` with random
credentials, a random signing secret, `SOURCEDESK_PROVIDER=fake`, and local upload
paths. Reruns do not rotate existing secrets. These files and `.local/` are ignored
by Git; do not paste their contents into issue reports or terminal logs.

Both files retain `SOURCEDESK_DATABASE_URL` as the application URL and
`SOURCEDESK_TEST_DATABASE_URL` as the test URL so the fixture can enforce separation.
The fixture switches its own process to the explicit test URL. `--check` accepts
environment URL overrides and makes no persistent environment or database writes;
its vector probe uses a temporary table that is dropped on commit.

On Windows, the cluster binds loopback plus the current private WSL IPv4 address,
never a wildcard address. Additional host rules admit only the Windows-side WSL
gateway `/32`, only the matching database/login pair, and SCRAM authentication.
Linux development/CI need only loopback. No Windows port proxy or broad firewall
exception is installed.

## Ubuntu 24.04 / 26.04 native setup

Use a checkout with `uv` installed and noninteractive sudo available:

```bash
uv sync --locked --python 3.12.13
sudo -n bash scripts/setup-postgres-wsl.sh --install
uv run --locked python scripts/setup_db.py --migrate both
uv run --locked python scripts/setup_db.py --check --test
```

The shell installer pins these package versions rather than silently following a
new database or extension release:

- Ubuntu 26.04: `postgresql-18=18.6-0ubuntu0.26.04.1` and
  `postgresql-18-pgvector=0.8.1-2` from the Ubuntu repositories.
- Ubuntu 24.04: `postgresql-18=18.6-1.pgdg24.04+2` and
  `postgresql-server-dev-18=18.6-1.pgdg24.04+2` from the signed official PGDG
  repository. Builds pgvector tag `v0.8.1` with PostgreSQL 18's `pg_config` and
  portable `OPTFLAGS=''`. The release tarball must match SHA-256
  `a9094dfb85ccdde3cbb295f1086d4c71a20db1d26bf1d6c39f07a7d164033eb4`.

Build files and guard recovery copies use a validated root-owned private
`/root/.cache/sourcedesk-build` directory with mode `0700`, not the user-writable
repository. Per-install workspaces are removed after successful cleanup. If pinned packages disappear from a repository,
installation intentionally fails: review and update the pins explicitly rather
than substituting unverified binaries. Root access is required for native package
and cluster administration, not for the Python application.

Package installation temporarily suppresses default-main cluster creation and
package-triggered service starts, preserving existing policies/configuration and
restoring them on success, failure, and catchable termination. Run installation
as a deliberate administrator maintenance operation, not alongside unrelated
package/service administration. SIGKILL or power loss cannot run a shell trap;
inspect retained private recovery copies and restore the original policy/config
before retrying after such an interruption. No complete package installation was
re-executed for these guards: isolated mocked maintainer probes and native
read-only startup/target checks were used, not a fabricated blank-machine run.

The shell script also accepts `--start`, `--start-windows`, `--install-windows`,
and restricted `--psql DATABASE` for administration of `postgres`, `sourcedesk`, or
`sourcedesk_test` on port `55432`. Do not include secret SQL on command lines.

## Verification and migrations

```powershell
# Provision without package upgrades, verify, then upgrade both schemas:
uv run --locked python scripts/setup_db.py --migrate both
# Read-only persistent state check:
uv run --locked python scripts/setup_db.py --check
# Real isolated PostgreSQL suite; no external model calls:
uv run --locked python scripts/setup_db.py --check --test
```

`--migrate application`, `--migrate test`, and `--migrate both` run the existing
Alembic revisions with each database's own login. A database-scoped advisory lock
prevents concurrent setup-script migrations; another migration causes a clear
retry-later error. Success is read back from `alembic_version` and the actual
`public.chunks.embedding` type, which must be `vector(1536)`. No schema is generated
through `create_all`. `--check` rejects install/refresh/migrate options.

The non-secret `.local/database-verification.json` records both database names,
login names, PostgreSQL/pgvector versions, no elevated/inherited privileges,
cross-database denial, and these real SQL results:

- Euclidean distance between `[1,2,3]` and `[4,6,3]`: `5.0`.
- Successful insert into a temporary `vector(1536)` column: dimensions `1536`.
- Cosine distance between distinct 1536-dimensional basis vectors: `1.0`.

The checks use psycopg from the executing native Python process, not just a WSL
`psql` probe. Migration logs suppress potentially sensitive failure details; a
nonzero exit must be investigated, not reported as a successful upgrade.

For an API started by the script:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/ready` checks database connectivity, pgvector, and schema revision. `/health`
labels the deterministic fake provider explicitly; these checks do not establish
real-model quality.

## Native CI

`.github/workflows/ci.yml` uses `ubuntu-24.04`, locked Python 3.12.13 dependencies,
and the exact native installation/provision/migration commands above. It has no
service container. It creates fresh private credentials, masks URLs and signing
secrets before exporting them, verifies native vector operations, runs the real
PostgreSQL integration suite with the fake provider, and runs Ruff and mypy.
A local green suite does not mean GitHub Actions itself has been executed.

## Diagnosis without exposing secrets

```powershell
wsl.exe -d Ubuntu -- pg_lsclusters
wsl.exe -d Ubuntu -- systemctl is-active postgresql@18-sourcedesk.service
uv run --locked python scripts/setup_db.py --check
```

If SQL works inside WSL but native Python times out, check that a keeper remains
running, then rerun setup to refresh the address and narrowly scoped listener/HBA
rules. Do not weaken authentication or bind PostgreSQL to `0.0.0.0`. Never stop or
reconfigure the unrelated main cluster to resolve a SourceDesk port problem.
