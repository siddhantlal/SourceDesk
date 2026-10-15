# Dependency verification

Verified in the build environment on 2026-10-15. The project uses CPython 3.12.13 in a local `.venv`; the host's unrelated Python installation was not replaced. `uv.lock` records exact versions, hashes and transitive dependencies. Use `uv sync --locked`, not an unpinned `pip install` command.

## Installed versions

The following versions were resolved and installed successfully with `uv 0.12.3`. Their installed `Requires-Python` metadata permits Python 3.12. Runtime behaviour is checked separately by the project tests; metadata compatibility alone is not proof of application correctness.

| Dependency | Installed version | Declared Python requirement |
| --- | --- | --- |
| FastAPI | 0.143.2 | >=3.10 |
| Pydantic | 2.14.1 | >=3.10 |
| SQLAlchemy | 2.0.54 | >=3.7 |
| Alembic | 1.20.0 | >=3.10 |
| psycopg | 3.3.6 | >=3.10 |
| pgvector Python client | 0.5.1 | >=3.10 |
| pypdf | 6.20.1 | >=3.9 |
| pytest | 9.1.1 | >=3.10 |
| Uvicorn | 0.54.0 | >=3.10 |
| tiktoken | 0.14.0 | >=3.9 |
| pwdlib | 0.3.1 | >=3.10 |
| PyJWT | 2.15.1 | >=3.9 |

The Python `pgvector` package and PostgreSQL `vector` extension are separate dependencies. Installing the Python client does not install the database extension. See [local-database.md](local-database.md) for the database setup and its actual verification results.

## Official references consulted

- FastAPI password/JWT tutorial: https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/ . Used to check the supported `pwdlib[argon2]`, PyJWT and bearer dependency approach.
- pgvector implementation and installation guide: https://github.com/pgvector/pgvector . Used to check the extension installation, vector dimension declaration and cosine-distance operator.
- pypdf text extraction: https://pypdf.readthedocs.io/en/stable/user/extract-text.html . Used to check extraction limitations; an image-only PDF needs OCR, which is outside this application's scope.
- uv locking/synchronisation: https://docs.astral.sh/uv/concepts/projects/sync/ . Used to check the reproducible `uv lock`/`uv sync --locked` workflow.
- SQLAlchemy session basics: https://docs.sqlalchemy.org/en/20/orm/session_basics.html . Reference for transaction boundaries and explicit session lifetimes.
- PostgreSQL SELECT reference: https://www.postgresql.org/docs/current/sql-select.html . Reference for row locking and `SKIP LOCKED` job claiming.
- OpenAI embeddings: https://developers.openai.com/api/docs/guides/embeddings . Reference for the configurable embedding request and model/dimension identity.
- OpenAI structured outputs: https://developers.openai.com/api/docs/guides/structured-outputs . Reference for schema-constrained generation requests and backend validation.

## Recheck on dependency upgrades

1. Read the relevant release notes and official compatibility documentation.
2. Run `uv lock --upgrade-package <package>` deliberately, review the lockfile changes, then `uv sync --locked`.
3. Run linting, type checks, the focused test suite against a real PostgreSQL/pgvector database, and the local demonstration.
4. Reindex documents if the embedding model, vector dimension or chunking configuration changes. Do not mix indexes or treat fake-provider verification as real-model quality measurement.
