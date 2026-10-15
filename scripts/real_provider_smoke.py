"""Opt-in real embedding + generation smoke test; may incur provider charges."""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sourcedesk.evaluation import ROOT, persist_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid", action="store_true",
                        help="Explicitly permit external provider requests and possible charges")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/real-provider-smoke.json")
    args = parser.parse_args()
    if not args.allow_paid or os.getenv("SOURCEDESK_ALLOW_REAL_SMOKE") != "1":
        parser.exit(2, "Disabled: requires --allow-paid AND SOURCEDESK_ALLOW_REAL_SMOKE=1.\n")
    from sourcedesk.config import get_settings
    from sourcedesk.generation import generate_answer
    from sourcedesk.providers import get_embedding_provider, get_generation_provider

    settings = get_settings()
    if settings.provider != "openai" or not settings.openai_api_key:
        parser.exit(2, "Set SOURCEDESK_PROVIDER=openai and SOURCEDESK_OPENAI_API_KEY securely.\n")
    report = {"run_type": "real_provider_smoke_not_quality_evaluation",
              "started_at": datetime.now(UTC).isoformat(), "completed": False,
              "cost_estimate": None, "cost_note": "No verified pricing configured"}
    try:
        embedding = get_embedding_provider(settings).embed(["The test archive opens at 09:00."])
        if len(embedding.vectors) != 1 or len(embedding.vectors[0]) != 1536:
            raise RuntimeError("Unexpected embedding dimensions")
        source = {"source_id": "S1", "document_id": "smoke-document", "chunk_id": "smoke-chunk",
                  "filename": "self-authored-smoke", "page_number": 1,
                  "excerpt": "The test archive opens at 09:00."}
        answer = generate_answer("When does the test archive open?", [source],
                                 get_generation_provider(settings), max_output_tokens=200)
        report.update({"embedding_model": embedding.model,
                       "embedding_token_usage": embedding.token_usage,
                       "generation_model": answer.model, "status": answer.status,
                       "token_usage": answer.token_usage,
                       "structurally_valid_citations": len(answer.citations),
                       "warnings": answer.warnings})
        if answer.status != "answered" or not answer.citations:
            raise RuntimeError("Provider smoke did not produce a validated cited answer")
        report["completed"] = True
    except Exception as exc:
        report["error_category"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        persist_report(report, args.report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
