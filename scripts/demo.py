"""Real HTTP demonstration; API and durable worker must already run separately."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sourcedesk.evaluation import ROOT, HTTPRun, citation_validity, persist_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--wait-seconds", type=float, default=120)
    parser.add_argument("--report", type=Path, default=ROOT / "reports/demo.json")
    parser.add_argument("--allow-real-provider", action="store_true")
    args = parser.parse_args()
    report: dict = {"run_type": "real_http_separate_worker", "started_at": datetime.now(UTC).isoformat(),
              "checks": {}, "completed": False}
    owner = outsider = None
    try:
        owner = HTTPRun(args.base_url, allow_real_provider=args.allow_real_provider)
        outsider = HTTPRun(args.base_url, allow_real_provider=args.allow_real_provider)
        report["provider"] = owner.health
        owner.register()
        outsider.register()
        collection_id = owner.collection("SourceDesk reproducible demonstration")
        document_id = owner.upload(ROOT / "eval/pdfs/harbor-handbook.pdf")
        owner.wait_ready([document_id], args.wait_seconds)
        report["checks"]["upload_processed_by_separate_worker"] = True
        answer, latency = owner.ask("At what time does the Harbor Research Room open on weekdays?")
        if answer["status"] != "answered" or not answer["citations"]:
            raise RuntimeError("Expected cited answer")
        if not citation_validity(owner, answer):
            raise RuntimeError("Citations did not match authorized PDF pages")
        report["answer"] = answer
        report["answer_latency_ms"] = latency
        report["checks"]["page_citations_valid"] = True
        abstention, _ = owner.ask("What is Neptune orbital period?")
        if abstention["status"] != "insufficient_evidence":
            raise RuntimeError("Expected explicit insufficient-evidence response")
        report["abstention"] = abstention
        report["checks"]["abstention"] = True
        denial_paths = [f"/api/v1/collections/{collection_id}",
                        f"/api/v1/collections/{collection_id}/documents",
                        f"/api/v1/documents/{document_id}",
                        f"/api/v1/documents/{document_id}/pages/1",
                        f"/api/v1/collections/{collection_id}/questions",
                        f"/api/v1/questions/{answer['id']}",
                        f"/api/v1/questions/{answer['id']}/sources"]
        for path in denial_paths:
            outsider.request("GET", path, expected=(404,))
        outsider.request("POST", f"/api/v1/collections/{collection_id}/questions",
                         json={"text": "What are the opening hours?"}, expected=(404,))
        report["checks"]["cross_user_denied"] = len(denial_paths) + 1
        owner.request("DELETE", f"/api/v1/documents/{document_id}")
        owner.request("GET", f"/api/v1/documents/{document_id}", expected=(404,))
        persisted = owner.request("GET", f"/api/v1/questions/{answer['id']}")
        sources_result = owner.request("GET", f"/api/v1/questions/{answer['id']}/sources")
        sources = sources_result.get("sources", []) if isinstance(sources_result, dict) else sources_result
        if persisted["answer"] != answer["answer"]:
            raise RuntimeError("Expected historical answer text to survive source deletion")
        for source in sources + persisted.get("sources", []) + persisted.get("citations", []):
            if source.get("document_id") == document_id:
                if source.get("available") is not False or source.get("excerpt") or source.get("quote"):
                    raise RuntimeError("Deleted source excerpt/quote was not redacted")
        if not sources:
            raise RuntimeError("Expected tombstoned source references")
        report["after_document_deletion"] = {"answer": persisted, "sources": sources}
        report["checks"]["deleted_source_redaction"] = True
        owner.cleanup()
        report["checks"]["collection_deletion_verified"] = True
        report["completed"] = True
    except Exception as exc:
        report["error_category"] = type(exc).__name__
        raise
    finally:
        for run in (owner, outsider):
            if run is not None:
                run.client.close()
        report["finished_at"] = datetime.now(UTC).isoformat()
        persist_report(report, args.report)
    print(json.dumps({"report": str(args.report), "provider": report["provider"],
                      "checks": report["checks"]}, indent=2))


if __name__ == "__main__":
    main()
