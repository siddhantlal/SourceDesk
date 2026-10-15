# Security boundaries

## Authentication

Registration accepts a normalized, unique email and a password of 12–128 characters. It creates an ordinary active user, not an administrator. `pwdlib` hashes passwords with its recommended Argon2 configuration. Passwords and bearer tokens are not logged. Login returns an HS256 access JWT with `sub`, `iat`, and `exp`; signature verification pins HS256 and requires the subject and expiry. The default lifetime is 30 minutes. Every authenticated request checks that the account still exists and is active.

Use a unique random signing secret of at least 32 characters. Keep `.env` out of Git, protect the upload directory and database backups, and use HTTPS outside localhost. This version has no refresh tokens or token denylist. Deleting a token in a client does **not** revoke it; it remains usable until expiry unless the account is disabled or the signing key is rotated. Key rotation invalidates all tokens signed with the old key.

## Isolation

Collection, document, page, original-download, question history and source routes require an authenticated owner. Unauthorized IDs receive the same 404 shape as absent IDs. SQL vector retrieval joins chunks, documents and collections and includes ownership, collection, ready-state and embedding-model predicates **before ranking**. Optional document IDs only narrow this scope. UUIDs reduce accidental collisions, but unguessable IDs are not an authorization mechanism.

The database uses ordinary foreign keys and application-enforced ownership, not row-level security. Anyone with database credentials can bypass the HTTP checks. The local evaluation/maintenance tools are trusted administrative programs, not public API routes.

## Browser decisions

Authorization is an explicit `Authorization: Bearer ...` header. The API does not set an authentication cookie, so browsers do not automatically attach credentials in cross-site form submissions. Consequently there is no cookie-oriented CSRF token in this version. Switching to cookie authentication requires a fresh CSRF/SameSite design; CORS alone is not a CSRF defense. CORS origins, methods and headers are explicit and configurable, with credentialed cookies disabled. Do not put bearer tokens in URLs or render untrusted PDF content as HTML. The same-origin browser workspace renders untrusted content through text nodes. Its access token is stored in memory and tab-scoped `sessionStorage`, never in `localStorage` or URLs; this permits refresh continuity but is readable by same-origin JavaScript and is not HttpOnly protection. XSS could expose it, so the frontend uses no third-party scripts or model-generated HTML. Sign-out clears client state but does not revoke the JWT. See `frontend.md` for blob-download cleanup and browser verification.

## File and model boundaries

Uploads are bounded, signature/parser checked, encrypted-file rejected, and page limited. Display filenames are sanitized; original files are addressed only by generated opaque keys. Extensions and MIME types are not trusted. Scanned/image-only PDFs fail with an OCR-required explanation instead of being marked ready. PDFs are parsed as data, never executed. Decoded content streams now share a preflight byte budget, with bounded common filters and Form XObject depth/invocations, before pypdf eagerly assembles page contents. Unsupported filters or excessive expansion fail closed. These limits are not a parser sandbox: adversarial object graphs, fonts or other parser paths can still consume CPU/memory. Keep this learning server on localhost; hardened deployment needs process isolation, execution/memory limits, concurrency controls and malware policy appropriate to its users.

Retrieved text is untrusted evidence. The generation prompt rejects embedded instructions, uses no tools and permits no document-triggered code execution or network actions. Only the configured backend provider client can make model requests. Prompt instructions cannot eliminate prompt injection; backend source-ID and exact-quote validation restrict citation forgery, **not semantic manipulation**. Every cited claim still needs semantic evaluation. Do not send confidential content to an external provider without permission and a suitable retention agreement.

## Durable processing and deletion

Jobs live in PostgreSQL rather than an in-memory task queue. Claim locks, lease tokens, bounded attempts, final document/lease checks, and atomic index replacement protect against duplicate publication and deleted-document resurrection. Multiple workers may duplicate external computation after lease expiry; fencing protects final state rather than promising exactly-once API billing.

Document deletion immediately removes pages, chunks and the job and queues original-file cleanup transactionally, while historical answer text remains. Returned references to removed/reindexed chunks lose excerpts and exact quotes and are marked unavailable. Internally stored answer snapshots and generated answer text can still contain source information; this is not a full erasure implementation. Collection deletion removes its answer history as well. Backups, generated reports and provider retention need separate policies. File deletion and database commit are not a distributed transaction. A durable outbox preserves committed deletion work for worker retries after unlink failure or interruption; original files can remain on disk until cleanup succeeds. A crash during upload before document commit can still leave an untracked original requiring reconciliation.

## Safe operations and remaining controls

Public errors have stable codes and server-generated correlation IDs, without stack traces, raw validation inputs, credentials or provider bodies. Logs record stage durations, retry categories and job transitions, not document text or prompts by default. The HTTP server's ordinary access logs should also be reviewed before accepting sensitive URL/query data.

No application rate limiter is implemented: there is no misleading process-local limiter advertised as distributed protection. Public deployment needs a trusted reverse proxy with TLS, request/time/concurrency limits and abuse controls, plus registration/login throttling. The API buffers a bounded request body and the worker is sequential per process; running more workers increases concurrency rather than enforcing a global tenant quota. Email verification, password reset, account deletion, centralized audit retention, disk quotas and production hardening are outside v1.
