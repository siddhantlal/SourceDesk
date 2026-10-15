# API walkthrough

Swagger: `http://127.0.0.1:8000/docs`. OpenAPI JSON: `/openapi.json`. These snippets use Bash/Git Bash; PowerShell users can use Swagger or `curl.exe` with shell-appropriate quoting. Values shown are illustrative placeholders, not claimed execution output. For a completely executed walkthrough use `uv run --locked python -m scripts.demo` with API and worker running.

## Register, log in and authorize

Choose your own local password; never reuse the example identity for production. Registration is JSON; OAuth2-compatible login is form-encoded.

```bash
export BASE=http://127.0.0.1:8000
# Read rather than placing a password in the shell command/history:
read -r -p 'Email: ' EMAIL
read -r -s -p 'Password (12–128 characters): ' PASSWORD; printf '\n'
# Use Swagger to submit the registration JSON securely, or build the body locally.
curl -sS "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  --data-binary '{"email":"student@example.com","password":"REPLACE-WITH-YOUR-OWN-PASSWORD"}'
curl -sS "$BASE/api/v1/auth/login" \
  --data-urlencode "username=$EMAIL" --data-urlencode "password=$PASSWORD"
# Copy access_token into a local variable; do not put it in a URL or commit it.
read -r -s -p 'Access token: ' TOKEN; printf '\n'
curl -sS "$BASE/api/v1/users/me" -H "Authorization: Bearer $TOKEN"
```

For interview demos, Swagger's **Authorize** button can submit the login form directly. Login returns `access_token`, `token_type: bearer` and `expires_in`; registration never returns a password hash. The demo/evaluation scripts create random credentials in memory and never print them.

## Collection, upload, status and pages

```bash
curl -sS "$BASE/api/v1/collections" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' --data '{"name":"Placement research","description":"Permitted sample PDFs"}'
# Copy the returned UUID, not this placeholder:
export COLLECTION_ID=REPLACE_WITH_COLLECTION_UUID
curl -sS "$BASE/api/v1/collections/$COLLECTION_ID/documents" \
  -H "Authorization: Bearer $TOKEN" -F 'file=@eval/pdfs/harbor-handbook.pdf;type=application/pdf'
export DOCUMENT_ID=REPLACE_WITH_DOCUMENT_UUID
curl -sS "$BASE/api/v1/documents/$DOCUMENT_ID" -H "Authorization: Bearer $TOKEN"
# Repeat status until ready; failed includes a safe error_code/error_message.
curl -sS "$BASE/api/v1/documents/$DOCUMENT_ID/pages/1" -H "Authorization: Bearer $TOKEN"
curl -sS "$BASE/api/v1/documents/$DOCUMENT_ID/download" \
  -H "Authorization: Bearer $TOKEN" --output downloaded-sample.pdf
```

Upload returns **202**, not a claim that indexing finished. The separate worker must be running. Document status progresses through queued/processing to ready or failed. Uploading identical bytes into the same collection returns **409 duplicate_document**. The original extension/MIME type alone cannot validate a PDF.

## Ask and inspect citations

```bash
curl -sS "$BASE/api/v1/collections/$COLLECTION_ID/questions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data '{"text":"At what time does the Harbor Research Room open on weekdays?"}'
export QUESTION_ID=REPLACE_WITH_QUESTION_UUID
curl -sS "$BASE/api/v1/questions/$QUESTION_ID" -H "Authorization: Bearer $TOKEN"
curl -sS "$BASE/api/v1/questions/$QUESTION_ID/sources" -H "Authorization: Bearer $TOKEN"
curl -sS "$BASE/api/v1/collections/$COLLECTION_ID/questions?limit=20&offset=0" \
  -H "Authorization: Bearer $TOKEN"
curl -sS "$BASE/api/v1/collections/$COLLECTION_ID/questions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data '{"text":"What is Neptune orbital period?"}'
```

An answer includes its ID, status, answer text, backend-mapped citations and source excerpts, timings, model, warnings, and nullable usage/cost metadata. A citation carries `source_id`, `document_id`, `chunk_id`, `filename`, `page_number` and `excerpt`; optional exact quotes are checked against supplied evidence. Review both the original PDF page and whether it actually supports the answer. Fake responses have explicit fake warnings.

Statuses distinguish **answered**, **insufficient_evidence**, **provider_unavailable**, **generation_error**, and **internal_error**. A successful HTTP response can persist one of these application outcomes; provider failure is not an assertion that the documents lack an answer. HTTP validation/authorization failures use the error envelope instead. Internal prompts and unrestricted retrieval diagnostics are not exposed publicly.

## Delete and check history

```bash
curl -i -X DELETE "$BASE/api/v1/documents/$DOCUMENT_ID" -H "Authorization: Bearer $TOKEN"
curl -i "$BASE/api/v1/documents/$DOCUMENT_ID" -H "Authorization: Bearer $TOKEN"
curl -sS "$BASE/api/v1/questions/$QUESTION_ID/sources" -H "Authorization: Bearer $TOKEN"
# Historical answer text remains; deleted source excerpts/quotes are unavailable.
curl -i -X DELETE "$BASE/api/v1/collections/$COLLECTION_ID" -H "Authorization: Bearer $TOKEN"
curl -i "$BASE/api/v1/collections/$COLLECTION_ID" -H "Authorization: Bearer $TOKEN"
unset PASSWORD TOKEN
```

Delete returns **204**. Readback should be **404**. Original-file removal is backed by a durable cleanup outbox, so a permissions/storage failure can delay physical deletion until a worker retry succeeds; successful HTTP deletion does not promise instantaneous disk erasure. Collection deletion also removes its question history. The demonstration additionally registers a second user and checks that guessing the first user's IDs cannot read or mutate their collection.

## Endpoint reference and errors

| Area | Endpoints |
| --- | --- |
| Authentication | `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `GET /api/v1/users/me` |
| Collections | `POST /api/v1/collections`, `GET /api/v1/collections`, `GET /api/v1/collections/{id}`, `DELETE /api/v1/collections/{id}` |
| Documents | `POST /api/v1/collections/{id}/documents`, `GET /api/v1/collections/{id}/documents`, `GET /api/v1/documents/{id}`, `GET /api/v1/documents/{id}/pages/{page_number}`, `GET /api/v1/documents/{id}/download`, `DELETE /api/v1/documents/{id}` |
| Questions | `POST /api/v1/collections/{id}/questions`, `GET /api/v1/collections/{id}/questions`, `GET /api/v1/questions/{id}`, `GET /api/v1/questions/{id}/sources` |
| Operations | `GET /health`, `GET /ready` |

Lists use bounded `limit`/`offset` pagination. Common status codes: **200** read/login, **201** registration/collection/question result, **202** queued upload, **204** delete, **400** invalid/encrypted PDF, **401** missing/expired/invalid bearer, **404** absent or unauthorized resource, **409** duplicate email/content, **413** body/upload limit, **422** invalid fields, **503** readiness failure, **500** safe internal error. Use the actual OpenAPI schema for field limits and response shapes.

Illustrative safe error (the correlation ID is generated per request):

```json
{"error":{"code":"not_found","message":"Collection was not found","correlation_id":"SERVER-GENERATED-UUID"}}
```

The matching `X-Correlation-ID` header helps correlate logs. Errors never expose passwords, tokens, provider bodies or stack traces. A missing or inactive user's token returns 401 even if its cryptographic signature is otherwise valid.
