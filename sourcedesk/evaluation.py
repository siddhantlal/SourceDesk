"""Offline metric definitions and opt-in HTTP evaluation against a live SourceDesk."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def summarize(rows: list[dict]) -> dict:
    """Programmatic checks only. Never infer semantic accuracy from valid citations."""
    def mean(values):
        values = [v for v in values if v is not None]
        return statistics.mean(values) if values else None

    latencies = sorted(float(r["latency_ms"]) for r in rows)
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    return {
        "questions": len(rows),
        "latency_ms": {
            "median": statistics.median(latencies) if latencies else None,
            "p95": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
            "p95_method": "nearest_rank",
        },
        "page_recall_at_k": mean(r.get("page_recall_at_k") for r in rows),
        "citation_validity_rate": mean(r.get("citation_validity") for r in rows),
        "provider_error_rate": mean(r["status"] == "provider_unavailable" for r in rows),
        "correct_abstention_rate": mean(
            r["status"] == "insufficient_evidence" for r in unanswerable),
        "incorrect_abstention_rate": mean(
            r["status"] == "insufficient_evidence" for r in answerable),
        "human_judgments": {"correctness": None, "citation_support": None,
                            "unsupported_claim_rate": None},
    }


def page_recall_at_k(gold: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Unique labelled pages found in first k passages / unique labelled pages.

    Passage duplicates consume ranks. Unanswerable rows (no gold pages) are excluded.
    """
    if k < 1:
        raise ValueError("k must be positive")
    expected = {(p["document"], int(p["page"])) for p in gold}
    if not expected:
        return None
    found = {(p["document"], int(p["page"])) for p in retrieved[:k]}
    return len(expected & found) / len(expected)


class HTTPRun:
    """Real HTTP only. Never starts an API or calls the worker inline."""

    def __init__(self, base_url: str, *, allow_real_provider: bool = False):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=180)
        self.health = self.request("GET", "/health")
        if not self.health.get("fake_provider", self.health.get("provider") == "fake"):
            if not allow_real_provider:
                self.client.close()
                raise RuntimeError("Real provider disabled: pass --allow-real-provider explicitly")
        self.request("GET", "/ready")
        self.user_id: str | None = None
        self.collection_id: str | None = None

    def request(self, method: str, path: str, *, expected: tuple[int, ...] = (200, 201, 202, 204),
                **kwargs):
        response = self.client.request(method, path, **kwargs)
        if response.status_code not in expected:
            # Do not log request bodies, auth headers, passwords or arbitrary server text.
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}; "
                               f"correlation_id={response.headers.get('X-Correlation-ID', 'unknown')}")
        return response.json() if response.content else None

    def register(self) -> None:
        email = f"sourcedesk-{uuid4().hex}@example.com"
        password = secrets.token_urlsafe(30)
        self.request("POST", "/api/v1/auth/register", json={"email": email, "password": password})
        login = self.request("POST", "/api/v1/auth/login",
                             data={"username": email, "password": password})
        self.client.headers["Authorization"] = f"Bearer {login['access_token']}"
        self.user_id = self.request("GET", "/api/v1/users/me")["id"]

    def collection(self, name: str) -> str:
        created = self.request("POST", "/api/v1/collections",
                               json={"name": name, "description": "Ephemeral fixture-only HTTP run"})
        self.collection_id = str(created["id"])
        return self.collection_id

    def upload(self, path: Path) -> str:
        with path.open("rb") as handle:
            data = self.request("POST", f"/api/v1/collections/{self.collection_id}/documents",
                                files={"file": (path.name, handle, "application/pdf")})
        return data["id"]

    def wait_ready(self, document_ids: list[str], timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        pending = set(document_ids)
        while pending and time.monotonic() < deadline:
            for document_id in list(pending):
                document = self.request("GET", f"/api/v1/documents/{document_id}")
                if document["status"] == "ready":
                    pending.remove(document_id)
                elif document["status"] == "failed":
                    raise RuntimeError(f"Ingestion failed: {document.get('error_code', 'unknown')}")
            if pending:
                time.sleep(0.25)
        if pending:
            raise RuntimeError("Processing timed out: start the separate worker and inspect safe job errors")

    def ask(self, text: str) -> tuple[dict, float]:
        started = time.perf_counter()
        response = self.request("POST", f"/api/v1/collections/{self.collection_id}/questions",
                                json={"text": text})
        return response, (time.perf_counter() - started) * 1000

    def cleanup(self) -> None:
        if self.collection_id:
            path = f"/api/v1/collections/{self.collection_id}"
            self.request("DELETE", path)
            self.request("GET", path, expected=(404,))
            self.collection_id = None


def authorized_diagnostics(question_id: str, user_id: str, collection_id: str) -> tuple[list[dict], dict]:
    """Read only this run's persisted retrieval, not public API debug/prompt data.

    Requires local DB config pointing at the same API database. No extra model call.
    Authorization is in SQL, not a post-retrieval global filter.
    """
    from sqlalchemy import select

    from sourcedesk.db import session_factory
    from sourcedesk.models import Answer, Question

    with session_factory()() as session:
        row = session.execute(select(Answer, Question).join(Question, Answer.question_id == Question.id)
                              .where(Question.id == UUID(question_id),
                                     Question.user_id == UUID(user_id),
                                     Question.collection_id == UUID(collection_id))).first()
        if row is None:
            raise RuntimeError("Authorized evaluation diagnostics unavailable; check API/CLI database agreement")
        answer, question = row
        passages = [{"document": p["filename"], "page": p["page_number"]}
                    for p in answer.retrieved_chunks]
        return passages, question.retrieval_config


def citation_validity(run: HTTPRun, answer: dict) -> bool | None:
    """Page/ID/excerpt consistency, explicitly not semantic claim support."""
    citations = answer.get("citations", [])
    if not citations:
        return False if answer["status"] == "answered" else None
    sources_result = run.request("GET", f"/api/v1/questions/{answer['id']}/sources")
    sources = sources_result.get("sources", []) if isinstance(sources_result, dict) else sources_result
    by_id = {s["source_id"]: s for s in sources}
    for citation in citations:
        source = by_id.get(citation["source_id"])
        if source is None or source.get("available") is False:
            return False
        for key in ("document_id", "chunk_id", "page_number", "filename"):
            if citation[key] != source[key]:
                return False
        page = run.request("GET", f"/api/v1/documents/{citation['document_id']}/pages/"
                                  f"{citation['page_number']}")
        excerpt = " ".join(citation["excerpt"].split())
        if not excerpt or excerpt not in " ".join(page["text"].split()):
            return False
        if citation.get("quote") and citation["quote"] not in citation["excerpt"]:
            return False
    return True


def persist_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def evaluate(args) -> dict:
    provenance = json.loads((ROOT / "eval/provenance.json").read_text(encoding="utf-8"))
    dataset_path = ROOT / "eval/questions.jsonl"
    rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line]
    rows = [row for row in rows if args.split == "all" or row["split"] == args.split]
    run = HTTPRun(args.base_url, allow_real_provider=args.allow_real_provider)
    results: list[dict] = []
    report = {"run_type": "real_http", "provider": run.health,
              "dataset_id": provenance["dataset_id"], "review_status": provenance["review_status"],
              "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
              "split": args.split, "started_at": datetime.now(UTC).isoformat(),
              "k": args.k, "recall_definition": "unique gold pages in first k supplied passages / unique gold pages",
              "judgment_type": "programmatic_only_not_semantic_accuracy", "rows": results,
              "cleanup_requested": args.cleanup}
    try:
        run.register()
        run.collection("SourceDesk fixture evaluation")
        report["user_id"] = run.user_id
        report["collection_id"] = run.collection_id
        ids = []
        for doc in provenance["documents"]:
            path = ROOT / "eval/pdfs" / doc["filename"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
                raise RuntimeError("Fixture hash changed: regenerate and review provenance")
            ids.append(run.upload(path))
        run.wait_ready(ids, args.wait_seconds)
        for item in rows:
            answer, latency = run.ask(item["question"])
            history = run.request("GET", f"/api/v1/questions/{answer['id']}")
            if history["status"] != answer["status"]:
                raise RuntimeError("Persisted answer status disagrees with POST response")
            retrieved, config = authorized_diagnostics(
                answer["id"], cast(str, run.user_id), cast(str, run.collection_id))
            result = {"id": item["id"], "answerable": item["answerable"],
                      "status": answer["status"], "latency_ms": latency,
                      "page_recall_at_k": page_recall_at_k(item["relevant_pages"], retrieved, args.k),
                      "retrieved_pages": retrieved, "retrieval_config": config,
                      "citation_validity": citation_validity(run, answer),
                      "response": answer, "reference_answer": item["reference_answer"],
                      "human_judgments": item["human_judgments"]}
            results.append(result)
        report["summary"] = summarize(results)
        report["completed"] = True
    except Exception as exc:
        report["completed"] = False
        report["error_category"] = type(exc).__name__
        raise
    finally:
        try:
            if args.cleanup:
                run.cleanup()
                report["collection_deleted_verified"] = True
        finally:
            run.client.close()
            report["finished_at"] = datetime.now(UTC).isoformat()
            persist_report(report, args.report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="dev")
    parser.add_argument("--k", type=int, default=6)
    parser.add_argument("--wait-seconds", type=float, default=120)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/evaluation.json")
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be positive")
    try:
        report = evaluate(args)
    except (RuntimeError, httpx.HTTPError) as exc:
        parser.exit(1, f"Evaluation blocked ({type(exc).__name__}); inspect API/worker and report.\n")
    print(json.dumps({"report": str(args.report), "provider": report["provider"],
                      "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()

