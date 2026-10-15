# Browser workspace

SourceDesk serves a working responsive frontend from the FastAPI origin. Open `/` for the workspace and `/docs` for Swagger. No separate frontend server, Node production runtime, CDN fonts or JavaScript framework is required.

## Files and request flow

- `frontend/index.html`: semantic page shell, sign-in/registration, collection dialogs, document library, question composer and source inspector.
- `frontend/styles.css`: an original paper/olive editorial workspace, responsive sidebar and source panes, keyboard focus states and reduced-motion support.
- `frontend/api.js`: same-origin authenticated JSON/form requests, multipart upload progress, timeouts, safe errors/correlation IDs and cancellation.
- `frontend/ui.js`: safe text-node construction, result labels, document-failure explanations and bounded document-scope requests.
- `frontend/app.js`: collection/session state, polling queued documents, persisted history, source inspection, authenticated original-PDF downloads and destructive-action confirmation.

Authentication uses the actual backend registration JSON and OAuth2-compatible login form. There are no seeded credentials or fake preloaded conversations. The selected collection and optional ready-document IDs determine scope; the backend remains the authority for ownership. When a question succeeds the browser reads it back from history. Deletion refreshes owner-scoped lists and saved sources rather than retaining stale excerpts.

## Sessions and untrusted content

The access JWT is held in memory and in tab-scoped `sessionStorage` so a refresh can restore the session. It is not placed in `localStorage`, a URL, a source file or a frontend build-time environment variable. `sessionStorage` is readable by same-origin JavaScript: this is not HttpOnly-cookie isolation and an XSS vulnerability could expose it. Do not embed third-party scripts. Sign-out/401 clears the token, in-flight client requests, private UI state and temporary blob URLs; it does not revoke the signed JWT on the server. The backend expiry/active-user check still applies.

Document text, filenames, questions and answers are rendered with text nodes, never interpreted as model-provided HTML. Original PDFs are obtained through authenticated fetches and temporary object URLs, not public upload paths. Opening an original uses a separate `noopener noreferrer` tab. Deleted source references remain visible as unavailable, without their excerpt or quote.

The visible test-provider banner and answer warnings are deliberate. Default fake output demonstrates dataflow, not real model quality. Provider errors, invalid generation, insufficient evidence and answered states are labelled separately. PDFs with insufficient extractable text explain that OCR is outside this version; bounded extraction errors do not pretend indexing succeeded.

## Verified local workflow

A real Chrome browser exercised the same-origin app against the live API and a separate PostgreSQL-backed worker:

1. Register and log in with a synthetic account.
2. Create a collection and upload a self-authored PDF.
3. Wait for durable indexing and ask a question.
4. Inspect the cited excerpt and complete extracted page.
5. Download the authenticated original and verify byte equality.
6. Exercise an explicit abstention.
7. Open collection/source panes at a 390 px mobile viewport without horizontal overflow.
8. Delete the source, inspect redacted/unavailable history, delete the collection, and sign out.

The run completed without page JavaScript errors.
Targeted browser fault injection also passed 16 checks: a committed deletion
immediately clears excerpts/blob URLs even if refetch fails; tombstones sanitize
late saved-answer/history responses; latest-wins generations reject older document
snapshots; and the mobile sidebar contains Tab/Shift+Tab, restores Escape focus,
defers to native dialogs and never competes with the source pane. These were
real-browser regression checks with selected HTTP responses delayed/failed,
not additional pytest cases or a claim of exhaustive fault coverage. Reports and actual desktop/mobile screenshots are under `reports/frontend/`. These are browser checks, not a comprehensive accessibility certification or cross-browser/device test. The sample text is fictional and self-authored; no private user PDFs were sent to a model.

## Local checks

```bash
# Optional developer checks; Node is not needed by the deployed frontend.
npm --prefix frontend test
# Serve the application and process uploaded documents in separate terminals:
uv run --locked uvicorn sourcedesk.main:app --host 127.0.0.1 --port 8000
uv run --locked python -m sourcedesk.worker
```

See `docs/render.md` for native Render deployment, persistent files and the process supervisor. Preparing and verifying the Blueprint does not deploy or authorize paid cloud resources.
