# Local verification report

Executed on 2026-10-15 in the project checkout. This report separates application plumbing from model quality. All executed model providers were explicitly **fake**; no paid requests were made.

## Environment and checks

- Native Windows CPython 3.12.13, uv 0.12.3, locked dependencies.
- PostgreSQL 18.6 / pgvector 0.8.1 in the dedicated Ubuntu WSL `18/sourcedesk` cluster, port 55432. Application and test use distinct databases and non-elevated logins with cross-database CONNECT denied.
- Alembic `0001_initial` and `0002_file_cleanup` applied to both databases; actual `vector(1536)` storage and distance probes verified from native Windows Python.
- `uv run --locked pytest -q --junitxml=reports/pytest.xml`: **25 passed, 0 failures/errors/skips**, across **12 test functions**. Upload, extraction, malformed metadata and lock-contention parameters are reported as separate executed cases rather than hidden beneath the function count. One Starlette/httpx TestClient deprecation warning remains, not hidden.
- `uv run --locked ruff check .`: passed.
- `uv run --locked mypy sourcedesk`: no issues in 18 source files.
- `uv build`: source distribution and wheel built successfully.
- A clean export excluding private environments, uploads, runtime data and the original venv installed the locked dependencies into a fresh venv; all 25 cases, Ruff and mypy passed again. It reused the already-provisioned isolated test database; this is not a claim that database installation was retested on a blank OS.
- All required OpenAPI route paths were present and Swagger returned HTTP 200. `/health` and `/ready` returned HTTP 200.

## Separate-process HTTP demonstration

Command: `uv run --locked python -m scripts.demo --base-url http://127.0.0.1:8000`.

The API and durable worker were separate running processes. The command uploaded a self-authored PDF, waited for real job processing, obtained a backend-validated page citation, exercised an explicit insufficient-evidence answer, verified eight cross-user access denials, deleted the original document, checked unavailable-source excerpt/quote redaction while answer text remained, then deleted the collection and read back 404.

Actual response evidence is in `reports/demo.json`. Its `completed` field is true. Temporary demonstration accounts remain with unrecorded random passwords; the collection was deleted. No default login is created for users.

## Draft dev evaluation — not semantic accuracy

Command: `uv run --locked python -m sourcedesk.evaluation --base-url http://127.0.0.1:8000 --split dev --cleanup`.

Eight draft dev questions ran over three original fictional PDFs. Five dev questions are labelled answerable and three unanswerable. The entire corpus contains twelve questions; no human review is claimed. Heldout evaluation was not executed.

| Measured programmatic result | This fake-provider run |
| --- | --- |
| Macro labelled-page recall@6 on supplied passages | 1.0 |
| Structural citation validity per response | 1.0 |
| Provider-error rate | 0.0 |
| Correct abstentions on three labelled-unanswerable questions | **0 / 3** |
| Incorrect abstentions on five labelled-answerable questions | 0 / 5 |
| Median HTTP question latency | 469.7381000005407 ms |
| p95 HTTP question latency, nearest-rank method | 570.584000000963 ms |
| Human correctness / citation support / unsupported-claim rate | unavailable |
| Real-provider token usage and cost | unavailable |

These numbers describe a tiny synthetic **fake** run, not production recall, language-model accuracy, calibrated confidence or real-provider latency. The lexical fake selected cited text for the unanswerable dev questions rather than abstaining: this is a visible limitation, not an error to hide or tune against heldout. The separate Neptune demonstration question did take the explicit abstention path. Structural validity can be perfect while semantic correctness is poor. No claim-level accuracy score is inferred.

The evaluation collection was deleted and that deletion read back. Full rows/configuration/hash are in `reports/evaluation.json`; summarized actual counts are in `reports/verification.json`; clean-export evidence is in `reports/clean-export.json`.

## Security-review regression checks

The review findings led to bounded PDF stream preflight before eager assembly,
collection-first publication locks, a transactional file-cleanup outbox,
validated provider model metadata, restrictive secret-file permissions at
creation, and default API correlation logging. The independent application
review approved its 36-file scope after those changes; this is a static review,
not a production security certification.

The native installer was hardened separately: administrator SQL pins the local
Unix socket/user/port after clearing inherited libpq settings; package-time
default-cluster/service guards restore original policies/configuration; verified
source builds use a root-owned private cache. Parent-executed checks passed all
13 isolated package-guard scenarios, hostile-environment read-only SQL probes,
and cache validation. Native `--start` preserved both existing clusters' status,
postmaster start times and configuration hashes. Package install/build behavior
was mocked for these new guard checks; no full apt reinstall or remote CI run is
implied. See `docs/local-database.md` for interruption and administration limits.
The separate independent installer review approved all 173 lines with no concrete
blocking findings in its intended trusted-administrator scope. Both static-review
records are included in the packaged reports.

## Frontend and Render extension

The same-origin browser workspace and native Render configuration were checked
locally after the backend baseline above. The current Python suite reports
**27 passing cases**, and the frontend has **2 passing JavaScript checks**.
JavaScript syntax, Ruff and mypy (19 source files including the Render supervisor)
pass; the existing upstream TestClient warning remains visible.

An actual headless Chrome workflow against the live API and separate durable
worker completed registration/login, collection creation, real PDF processing,
an OCR-required failure for a no-text PDF, cited questions, full extracted-page
inspection, byte-verified authenticated PDF download, explicit abstention,
390 px mobile navigation/source inspection, document deletion/redacted history,
collection deletion and logout/session clearing. No page JavaScript errors were
recorded. Actual screenshots and the report are under `reports/frontend/`.
The subsequent targeted browser regression probe passed 16/16 assertions for
failed/stale deletion refetches, response ordering and mobile keyboard focus;
the main browser workflow and all unit/static checks passed again afterward.

`render.yaml` passed the fetched official Render JSON Schema. The exact native
build script installed 44 production packages into an isolated environment and
served the UI, assets, docs, liveness and readiness routes successfully. No npm
build or Docker image is required. These are local readiness checks, not a Render
deployment: no resources were created, repository published or charges approved.
See `reports/render-readiness.json`, `frontend.md` and `render.md` for scope.

## Remaining verification

No real-provider requests, human semantic review, realistic load test, OCR, hostile-PDF sandbox assurance, full ten-PDF/fifty-reviewed-question dataset or remote GitHub Actions run has been completed. The optional paid provider smoke is disabled without its explicit configuration and opt-in flags. The app remains a localhost learning backend, not a production security certification.
