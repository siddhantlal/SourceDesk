# SourceDesk

A study-sized document research application with a browser workspace and FastAPI
backend: upload text PDFs, ask questions, and inspect page-linked evidence.
It uses PostgreSQL/pgvector retrieval and durable ingestion. Open the workspace
at `http://localhost:8000/`; Swagger remains at `http://localhost:8000/docs`.
No containers, hosted vector database, orchestration framework or paid calls are
required. The default providers are deterministic **fakes, not real model quality**.

## Quick start

Use Python **3.12** and `uv`. The project currently pins the Python minor version
in `pyproject.toml`; do not substitute Python 3.14 merely because it is installed.
From the repository root:

```bash
uv sync --locked
```

For the automated native database setup, follow **docs/local-database.md** first.
The setup command creates missing `.env` and `.env.test` files with random secrets
and distinct least-privilege database logins. **Do not copy `.env.example` over
these generated files.** Windows startup helpers also keep Ubuntu WSL alive:

```powershell
# After the one-time installation in docs/local-database.md:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-api.ps1
# Separate terminal:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-worker.ps1
```

If managing PostgreSQL yourself instead, copy `.env.example` to `.env` and replace
every placeholder with your own database URL and random signing secret. That
manual template is not input for the narrowly scoped automated database setup.
Never overwrite an existing private environment or commit it. No SQLite fallback
is supported.

The verified development topology is documented there: native Windows Python
connects by TCP to the current private WSL address; startup refreshes that address
and narrowly scoped database access rules when necessary.
An all-WSL setup keeps application and database together and avoids forwarding
issues; create a separate Linux venv rather than sharing Windows `.venv`.
See **docs/dependency-verification.md** for the installed compatibility audit.

Once PostgreSQL is running and `.env` is configured:

```bash
# Once per schema upgrade, not on every request:
uv run --locked alembic upgrade head
uv run --locked python -m scripts.make_fixtures

# Terminal 1: leave running; Ctrl+C stops cleanly.
uv run --locked uvicorn sourcedesk.main:app --host 127.0.0.1 --port 8000

# Terminal 2: same repo/environment/database/upload directory; leave running.
uv run --locked python -m sourcedesk.worker
```

The API does **not** start the worker. `/health` reports process/provider identity;
`/ready` checks database connectivity, the pgvector extension and schema revision.
Uploaded originals live under `SOURCEDESK_UPLOAD_DIR` (default `var/uploads`),
not inside the database. PostgreSQL's service data directory is separate; see the
local database guide. Both need a backup/retention policy before production use.

## Browser workspace

The frontend is served by the same FastAPI service, so there is no separate dev
server, npm installation, frontend environment secret, or cross-origin API URL
to configure. Its HTML, CSS and JavaScript live in `frontend/`.

Register or log in, create a collection, upload a permitted text PDF, wait for
processing, then ask a question. Use citations to inspect the extracted page and
supporting excerpt. Document downloads use authenticated requests. History is
stored by the backend, not invented by the browser. A visible fake-provider label
distinguishes demonstration plumbing from real-model results.

## Deployable on Render

`render.yaml` defines a native Python deployment with managed PostgreSQL and one
disk-backed web service. Its startup supervisor runs API and durable ingestion
worker as separate child processes that share the same persistent upload path.
There is no Docker build and no separate worker service expecting access to an
unshareable local disk. The frontend ships with the API and needs no Node build.

See **docs/render.md** for Blueprint/manual setup, secrets, storage, migration,
backup and model configuration. The persistent deployment uses paid resources;
creating it is an explicit user action. Preparing these files does not mean a
Render account was accessed, a repository published, or a cloud deployment run.

## Run the real HTTP demonstration

With the API and worker running separately:

```bash
uv run --locked python -m scripts.demo --base-url http://localhost:8000
```

This registers two random users, uploads a self-authored PDF, waits for the
worker, checks an answer and page citations, checks an unsupported question,
tries cross-user reads/question creation, deletes the document, verifies source
redaction in history, then deletes its collection. It writes `reports/demo.json`.
The random accounts remain; their passwords and tokens are never printed or
persisted. Collection data is removed on success. Failed runs may leave fixture
collections for inspection. Reports contain only the synthetic fixture evidence.

## Tests and evaluation

Set an explicit **different** dedicated PostgreSQL test database URL whose database
name contains `test`, enable pgvector there, and migrate it as described in the
local database guide. The test fixture truncates its tables; never use real data.

```bash
# Automated local setup already created/migrated the separate .env.test database.
uv run --locked python scripts/setup_db.py --check
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked mypy sourcedesk

# Pure metric tests need no database:
uv run --locked pytest tests/test_evaluation.py -q

# Evaluator talks to running API and reads authorized diagnostics in same DB:
uv run --locked python -m sourcedesk.evaluation --split dev --cleanup
# Freeze configuration before intentionally opening heldout examples:
uv run --locked python -m sourcedesk.evaluation --split heldout --report reports/heldout.json --cleanup
```

Database tests without a test URL are skipped, **not passed**. Evaluator reports
measure real HTTP latency and programmatic checks, not human semantic accuracy.
The corpus is **3 self-authored PDFs, 12 draft questions, zero human-reviewed
questions**. The original goal of 10 permitted PDFs / 50 reviewed questions
(about 40 answerable, 10 unanswerable) remains unfinished. See
**docs/evaluation.md** and `eval/rubric.md` before quoting any result.

## Provider configuration

All settings use `SOURCEDESK_`; `.env.example` shows placeholders.

| Setting suffix | Default / meaning |
|---|---|
| `PROVIDER` | `fake`; opt-in `openai` for external calls |
| `EMBEDDING_MODEL` | `text-embedding-3-small`; fake records `fake-hash-v1` |
| `EMBEDDING_DIMENSIONS` | fixed 1536 in schema v1 |
| `GENERATION_MODEL` | `gpt-4.1-mini`; fake records `fake-extractive-v1` |
| `OPENAI_API_KEY` | unset; required for real calls |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` |
| `CHUNK_TOKENS` / `CHUNK_OVERLAP` | 400 / 60, starting choices not optimum claims |
| `RETRIEVAL_TOP_K` / `CONTEXT_TOKENS` | 6 / 3000 |
| `PROCESSING_VERSION` | `v1`; increment before reindexing |
| `MAX_OUTPUT_TOKENS` | 1000 |
| `CORS_ORIGINS` | JSON list, default `[]`; same-origin frontend needs no cross-origin access |

Do not switch embedding models without reindexing. Follow
**docs/architecture.md** for the maintenance procedure. Real provider calls send
selected document content off-machine; obtain permission first. Ordinary tests,
CI, demo and evaluation do not require an external key. Demo/evaluation reject
real-provider execution unless `--allow-real-provider` is explicitly given.

An optional direct provider smoke command is doubly gated and may cost money:

```bash
# First configure PROVIDER=openai and OPENAI_API_KEY securely; no key in command history.
SOURCEDESK_ALLOW_REAL_SMOKE=1 uv run --locked python -m scripts.real_provider_smoke --allow-paid
```

No real provider quality or cost claim follows from fake tests or this small
smoke. Without verified pricing, cost remains null, not zero.

## Guide map

- **docs/architecture.md** — pipelines, transaction boundaries, reindexing.
- **docs/frontend.md** — browser workflow, session storage and local UI verification.
- **docs/render.md** — native Render Blueprint, paid-resource warning and deployment steps.
- **docs/data-model.md** — ERD, constraints and deletion semantics.
- **docs/api-examples.md** — authentication, endpoints, requests and errors.
- **docs/security.md** — trust boundaries and residual risks.
- **docs/evaluation.md** — fixture provenance, definitions and review workflow.
- **docs/limitations.md** — scope and remaining verification.
- **docs/verification.md** — actual local checks and honestly labelled fake results.
- **docs/interview-guide.md** — 15 implementation-grounded Q&A and résumé bullets.

This repository is a learning backend, not a production security or model-quality
certification. Inspect generated reports and actual test output; documentation
examples are not fabricated execution results.

## Local verification

`reports/pytest.xml`, `reports/demo.json`, and `reports/evaluation.json` contain
actual local runs. With frontend/deployment checks, the Python suite now has
**14 focused functions / 27 executed cases**, plus **2 small JavaScript unit
checks** (`npm --prefix frontend test`). It passes against PostgreSQL 18.6 / pgvector 0.8.1; one
upstream TestClient deprecation warning remains. The separate-worker HTTP demo
passed upload, cited answer, explicit abstention, eight cross-user denials, source
redaction and collection-deletion checks. The dev evaluator ran eight **draft**
questions using **fake** providers. It reported structural validity and page
recall, but failed to abstain on all three dev questions labelled unanswerable;
this exposes the fake's limits, not real-model performance. Human semantic scores,
real-provider verification and remote GitHub Actions execution remain unfinished.
