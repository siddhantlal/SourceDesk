# Evaluation: definitions before claims

## Fixture, provenance and gaps

`eval/source/corpus.json` is original fictional text and draft labels. The fixture
builder uses ReportLab invariant output and explicit one-page sections. It checks
all extracted page texts using pypdf and records SHA-256 hashes in
`eval/provenance.json`. Regenerating twice with the locked environment was checked
to produce identical PDF bytes. `eval/LICENSE.txt` dedicates the authored fixture
content to the public domain under CC0-1.0; third-party software is not relicensed.

The corpus contains **three PDFs / nine pages / twelve draft questions**: eight
answerable, four unanswerable; dev has eight questions and heldout four. It covers
direct facts, multiple passages/documents, conflicting fees, an ambiguous visitor
limit and absent facts. None has been human-reviewed. A reference answer is a
draft target, not established ground truth.

The original target remains **10 permitted PDFs / 50 manually reviewed questions**,
roughly 40 answerable and 10 unanswerable. At least seven PDFs, 38 additional
questions, review of the existing twelve, and full human review of additions are
still required. The small synthetic corpus is not representative of arbitrary
academic, scanned, tabular or multilingual PDFs.

## Commands

Use the same `.env`, PostgreSQL database and configured index as the running API.
Start API and worker as separate processes (README). The evaluator does not call
`run_once` internally or pretend background work occurred.

```bash
uv run --locked python -m scripts.make_fixtures
uv run --locked python -m sourcedesk.evaluation --base-url http://localhost:8000 --split dev --cleanup
# Freeze changes before intentionally examining heldout:
uv run --locked python -m sourcedesk.evaluation --split heldout --report reports/heldout.json --cleanup
# All twelve is a plumbing check, not an untouched-holdout claim:
uv run --locked python -m sourcedesk.evaluation --split all --report reports/all-fixtures.json --cleanup
```

The CLI creates a random ordinary user and a fixture collection, validates PDF
hashes, uploads all three, waits for the running worker, POSTs questions, reads
history/sources/pages, and saves measured results in `reports/`. `--cleanup`
deletes its collection and verifies 404; the random account remains with an
unrecorded random password. Without cleanup the collection remains for citation
inspection. It never logs passwords/tokens. Reports intentionally contain only
synthetic text; do not repurpose this harness on private documents casually.

Public endpoints expose evidence excerpts but **not internal prompts or raw
retrieval debug payloads**. For recall only, the trusted CLI reads persisted
`Answer.retrieved_chunks` through a local session joining the exact question ID,
user ID and collection ID. This is the actual evidence used, not a second search
or a model-selected citation proxy. Configure the CLI against the same API DB or
it fails rather than guessing. No extra embedding call is made for diagnostics.
Do not publish this internal database credential or expose this helper as a
public diagnostic endpoint.

Fake run reports contain the provider identity and fake warnings. A health check
blocks accidental real-provider use unless `--allow-real-provider` is passed.
Ordinary CI uses fakes; missing keys are not worked around with fabricated data.

## Programmatic metrics

- **Page recall@k per answerable row:** unique labelled `(filename, one-based
  page)` pairs found in the **first k supplied passages**, divided by unique
  labelled page pairs. Repeated passages consume ranks but do not double-count
  a page. Supplied passages are post-dedup/context-budget evidence, not the raw
  vector candidate pool. Corpus filenames are unique. Rows with no gold pages
  are null and excluded from macro mean. CLI `--k` cannot manufacture more
  evidence than the server's configured top-k.
- **Citation validity:** answer citations agree with authorized source IDs,
  document/chunk/page metadata and exact normalized page excerpts. Optional
  quotes must occur in the excerpt. An answered response without citations is
  false; no citations on a non-answer is null. The reported validity rate is a
  per-response mean over non-null checks, not a per-citation or per-claim score.
- **Correct abstention:** `insufficient_evidence` / labelled unanswerable rows.
  **Incorrect abstention:** `insufficient_evidence` / labelled answerable rows.
  Provider failures stay in the denominator but are not abstentions. The
  ambiguous row can validly ask for clarification or provide both qualified
  alternatives; the binary status metric misses that nuance (manual rubric).
- **Provider error rate:** `provider_unavailable` / all completed question rows.
  Generation validation errors are separate statuses, not missing evidence.
- **Latency:** measured wall-clock `perf_counter` around the actual POST question,
  including HTTP and server work but excluding later verification GETs and
  ingestion. Median is the ordinary sample median. p95 uses nearest rank:
  sorted sample at one-based rank `ceil(0.95*n)`. Empty samples are null; tiny
  samples often have p95 equal to the maximum and are not load-test evidence.
- **Tokens/cost:** preserve provider-reported usage and cost fields per response;
  fake usage and unavailable pricing are null, not estimated zero. Usage may
  omit question embedding or ingestion costs; do not label generation usage
  total system spend. No pricing table or cost estimation is invented here.

Any run exception records a failed/incomplete report once HTTP initialization has
succeeded. Partial rows are not a completed dataset score. If the API itself is
unreachable, initialization may fail before a report exists. Interpret these as
blocked runs, not a result with zero latency or zero errors.

## Semantic judgments stay separate

`human_judgments.correctness`, `citation_support` and `unsupported_claim_rate`
are null. No reference-string match is called semantic accuracy. Structural
validation cannot establish entailment, completeness or honest conflict handling.
See `eval/rubric.md` for claim-level checks and correctness scoring. Record actual
reviewer identity/time and rubric version when review happens, then preserve both
raw human decisions and programmatic checks. No model judge is enabled.

Use dev for prompt/chunk/provider experiments; do not tune repeatedly on heldout.
If heldout is used for debugging, disclose that and create a new holdout before
making generalization claims. Freeze corpus hashes, config, model, prompt and
processing version; server retrieval config, provider/model IDs and dataset hash
are saved for reproducibility. Real provider drift and nondeterminism remain.

## Verified locally versus remaining

Metric tests were run through RED then GREEN: two pure tests pass, covering
rank/dedup/null behavior, abstention/provider distinction and percentile method.
The builder verified all nine PDF page texts and byte reproducibility. Live HTTP
reports, when produced, are the authority for request outcomes; do not infer live
success merely from these offline checks. Real provider quality, human semantic
scores, realistic-scale latency and the full dataset remain unverified until
those checks are actually run and reviewed.
