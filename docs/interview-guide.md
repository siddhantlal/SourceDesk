# Interview guide

Use the code and real verification reports as evidence. Do not present fake-provider output as LLM quality, a draft fixture as human-reviewed ground truth, or an unexecuted GitHub workflow as a successful remote run.

## Fifteen implementation-grounded questions

1. **Why RAG instead of fine-tuning?** SourceDesk needs to answer from an individual user's changing PDFs and cite exact pages. Retrieval supplies current, access-controlled evidence per question without retraining. Fine-tuning could adapt style or task behavior, but does not itself provide reliable page provenance or enforce document deletion/ownership.

2. **What do embeddings represent here?** Real embeddings map text into fixed-length vectors whose geometry is useful for similarity retrieval. v1 stores 1536-dimensional vectors in pgvector and records model identity. The default fake instead uses deterministic normalized signed word hashing; it is useful for reproducible plumbing tests but is not a semantic model.

3. **Why 400-token chunks and 60-token overlap?** They are configurable starting values. Smaller chunks can improve local specificity but lose context; larger chunks can dilute retrieval and consume the context budget. Overlap preserves boundary context but introduces duplication and cost. This implementation stays page-bounded, prefers sentence boundaries when practical, and derives page numbers directly from PDF extraction rather than chunk offsets.

4. **Why are retrieval and generation evaluated separately?** A model cannot reliably answer a fact that retrieval omitted. Conversely, correct retrieval does not guarantee faithful generation. The evaluator measures labelled-page recall on actual supplied evidence separately from structural citation validity; correctness and semantic support remain explicit human-review fields.

5. **Why exact vector search rather than HNSW?** The demonstration corpus is small and exact cosine SQL is easier to inspect and gives a clear baseline. Approximate indexes trade recall and operational tuning for speed. They should be added only after measuring query scale, latency, and recall with the same tenant filters.

6. **Why is a cosine score not confidence?** Cosine distance describes geometric proximity for a particular embedding model and corpus. It is not trained or calibrated to mean the probability that a generated claim is true. SourceDesk does not invent a calibrated similarity threshold or turn `1-distance` into a confidence percentage.

7. **How can a citation be valid but not support the answer?** A valid source ID and an exact quote establish that the passage exists and was supplied. The answer can still misinterpret it, omit qualifications, choose one conflicting source, or add unsupported facts. Backend structural validation is necessary but not an entailment proof; claim-level support needs semantic review.

8. **How is tenant isolation enforced?** Every API resource lookup checks ownership. Retrieval SQL joins collections/documents/chunks and constrains owner, selected collection, ready documents and model before ranking. Optional document IDs can narrow but never broaden scope. UUID secrecy is not the defense, and direct DB credentials are trusted because row-level security is not implemented.

9. **How does ingestion survive a worker crash?** Upload commits a persistent document and job. A worker claims available work with row locks and a lease token; expired work can be recovered with a bounded attempt budget. Slow provider work is outside database transactions. Final publication checks the current document and lease so an interrupted/stale worker cannot overwrite a newer attempt.

10. **What makes processing idempotent?** Final publication replaces the document's pages/chunks and commits readiness atomically. Unique page and chunk-position constraints guard duplicates. Same-collection SHA-256 deduplication rejects repeated uploads. Idempotent publication is not exactly-once external billing: a retry can repeat embeddings.

11. **What if a document is deleted during processing or after an answer?** Cascades remove its page/chunk/job rows and the same transaction queues original-file deletion. The API attempts a small batch immediately; the worker retries failed cleanup from a durable outbox. A late worker must still find the document and valid lease before publishing. Existing answer text remains for history, but public references to removed/reindexed chunks are marked unavailable and lose excerpts/quotes. Collection deletion removes its history too; this is not a legal all-copies erasure policy.

12. **What is the prompt-injection defense?** The model is told to treat PDF passages as untrusted evidence and ignore embedded commands. It receives temporary backend source IDs and has no executable/network tools. The server validates structure and exact quotes, but prompt instructions cannot prove resistance to semantic manipulation; adversarial evaluation and deployment limits remain necessary.

13. **How do you avoid evaluation leakage?** The fixture separates dev and heldout IDs, records hashes/configuration and marks every question draft rather than human-reviewed. Tune on dev, freeze settings before heldout, and disclose any use of heldout for debugging. A new untouched holdout is needed before generalization claims if that boundary is crossed.

14. **How do latency and cost trade off?** Embedding batches, exact scan size, evidence length, generation limits and bounded retries affect different stages. Worker logs separate extraction/chunking/embedding; question records separate request stages. More context and retries may improve robustness while increasing time and spend. Provider usage is stored only when reported; cost stays unavailable without verified dated pricing.

15. **What would you improve next, and what is not production-ready?** Expand/human-review evaluation first, opt into real-provider verification, measure retrieval errors, then consider reranking or approximate indexing. Before public exposure add parser isolation, login/upload abuse protection, quotas, TLS deployment, retention/orphan cleanup and operational monitoring. OCR, refresh-token revocation and object storage are not implemented. The new browser workspace is functional and the native Render Blueprint is prepared, but no actual Render deployment or high-availability claim follows from local verification.

## Three résumé bullets

Use these only after the corresponding local verification has passed; the final verification report is the authority. They intentionally contain no unmeasured accuracy, throughput or cost claims.

- Built a container-free FastAPI document-Q&A backend with PostgreSQL/pgvector, JWT authentication, owner-scoped APIs and page-aware PDF ingestion.
- Implemented durable database-backed ingestion with bounded retries and lease-fenced publication, plus retrieval-grounded answers with backend-validated page citations and explicit abstention/error states.
- Added a focused pytest suite, reproducible synthetic PDF fixtures, a cited-answer demonstration and programmatic evaluation tooling that separates structural checks from semantic quality judgments.

In conversation, state plainly that local runs used deterministic fake providers unless the opt-in real-provider report exists. A résumé should not silently imply that fake sentence extraction demonstrated production LLM quality.
