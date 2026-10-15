"""Pure evaluation arithmetic; no database or model credentials required."""
import importlib.util

import pytest


def test_page_recall_counts_unique_pages_in_first_k_passages():
    assert importlib.util.find_spec("sourcedesk.evaluation") is not None, "Evaluator is missing"
    from sourcedesk.evaluation import page_recall_at_k

    gold = [{"document": "a.pdf", "page": 1}, {"document": "b.pdf", "page": 2}]
    retrieved = [
        {"document": "a.pdf", "page": 1},
        {"document": "a.pdf", "page": 1},
        {"document": "b.pdf", "page": 2},
    ]
    assert page_recall_at_k(gold, retrieved, 2) == 0.5
    assert page_recall_at_k(gold, retrieved, 3) == 1.0
    assert page_recall_at_k([], retrieved, 3) is None
    with pytest.raises(ValueError):
        page_recall_at_k(gold, retrieved, 0)


def test_summary_separates_measured_rates_nullable_judgments_and_latency():
    from sourcedesk import evaluation

    assert callable(getattr(evaluation, "summarize", None)), "Report summarizer is missing"
    rows = [
        {"answerable": True, "status": "answered", "latency_ms": 100,
         "page_recall_at_k": 0.5, "citation_validity": True},
        {"answerable": False, "status": "insufficient_evidence", "latency_ms": 200,
         "page_recall_at_k": None, "citation_validity": None},
        {"answerable": True, "status": "provider_unavailable", "latency_ms": 300,
         "page_recall_at_k": 1.0, "citation_validity": None},
        {"answerable": True, "status": "insufficient_evidence", "latency_ms": 400,
         "page_recall_at_k": 0.0, "citation_validity": None},
    ]
    report = evaluation.summarize(rows)
    assert report["questions"] == 4
    assert report["latency_ms"] == {"median": 250.0, "p95": 400.0,
                                      "p95_method": "nearest_rank"}
    assert report["page_recall_at_k"] == 0.5
    assert report["provider_error_rate"] == 0.25
    assert report["correct_abstention_rate"] == 1.0
    assert report["incorrect_abstention_rate"] == pytest.approx(1 / 3)
    assert report["human_judgments"] == {"correctness": None, "citation_support": None,
                                        "unsupported_claim_rate": None}
    assert evaluation.summarize([])["latency_ms"]["p95"] is None
